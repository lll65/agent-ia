"""
Deep Finance Research Agent — niveau gérant de portefeuille senior.
Analyse multi-sources en streaming : macro, scan ETF/actions, technicals, web, synthèse LLM.
Fonctionne même sans données temps réel (mode dégradé avec labelisation explicite).
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# ─── Indices macro ────────────────────────────────────────────────────────────

MARKET_INDICES: dict[str, str] = {
    "^GSPC":     "S&P 500",
    "^IXIC":     "NASDAQ",
    "^FCHI":     "CAC 40",
    "^GDAXI":    "DAX",
    "^STOXX50E": "Euro Stoxx 50",
    "^VIX":      "VIX (fear)",
    "EURUSD=X":  "EUR/USD",
    "GC=F":      "Or ($/oz)",
    "^TNX":      "US 10Y (%)",
}

# ─── Suffixes de places éligibles PEA / européennes courantes ─────────────────
_PEA_SUFFIXES = (".PA", ".AS", ".BR", ".LS", ".MI", ".DE", ".MC", ".HE", ".VI", ".IR")

# Pattern d'un ticker explicite : 1-6 lettres/chiffres + . + 2 lettres de place
_TICKER_RE = re.compile(r"\b([A-Z0-9]{1,6}\.[A-Z]{2})\b")

# Chemin vers le JSON d'univers ETF PEA
_ETF_UNIVERSE_PATH = Path("data/pea_etf_universe.json")


# ─── Fonctions dynamiques d'accès aux données ─────────────────────────────────

def lookup_any_ticker(ticker: str) -> dict:
    """
    Récupère n'importe quel ticker (PEA ou international) depuis Yahoo Finance.

    Essaie le ticker tel quel, puis avec suffixes de places européennes si
    aucun suffixe de marché n'est présent.

    Retourne dict enrichi :
      {found, ticker, name, price, currency, chg_24h,
       perf_1m, perf_3m, perf_1y, closes, levels}
    ou {found: False, ticker: <original>} si introuvable.
    """
    from plugins.builtin.finance import _fetch_ticker_http

    tk = (ticker or "").strip().upper()
    if not tk:
        return {"found": False, "ticker": ticker}

    candidates = [tk]
    if "." not in tk and "=" not in tk and not tk.startswith("^"):
        candidates += [tk + sfx for sfx in _PEA_SUFFIXES]

    for cand in candidates:
        d3m = _fetch_ticker_http(cand, "3mo")
        if not d3m or not d3m.get("price"):
            continue
        d1m = _fetch_ticker_http(cand, "1mo")
        d1y = _fetch_ticker_http(cand, "1y")
        closes = d3m.get("closes", [])
        levels = _compute_levels(closes)
        if levels and d3m.get("price"):
            levels["price"] = d3m["price"]
        return {
            "found":    True,
            "ticker":   cand,
            "name":     d3m.get("name", cand),
            "price":    d3m.get("price"),
            "currency": d3m.get("currency", ""),
            "chg_24h":  d3m.get("chg"),
            "perf_1m":  d1m.get("perf") if d1m else None,
            "perf_3m":  d3m.get("perf"),
            "perf_1y":  d1y.get("perf") if d1y else None,
            "closes":   closes,
            "levels":   levels,
        }

    return {"found": False, "ticker": tk}


def search_ticker_by_name(name: str) -> list[dict]:
    """
    Résout un nom d'entreprise / ETF en ticker(s) Yahoo Finance.
    Retourne une liste de jusqu'à 5 résultats
    [{ticker, name, exchange, type_}, ...].

    Utilise yfinance.Search si disponible, sinon renvoie liste vide.
    """
    if not name:
        return []
    try:
        import yfinance as yf
        results = yf.Search(name, max_results=5).quotes
        out = []
        for r in results:
            sym = r.get("symbol") or ""
            if not sym:
                continue
            out.append({
                "ticker":   sym,
                "name":     r.get("longname") or r.get("shortname") or sym,
                "exchange": r.get("exchDisp") or r.get("exchange") or "",
                "type_":    r.get("quoteType") or "",
            })
        return out
    except Exception as e:
        logger.debug(f"search_ticker_by_name({name!r}): {e}")
        return []


def screen_etf_pea(budget: float = 0.0, nb_etf: int = 5,
                   geo_filter: str = "") -> list[dict]:
    """
    Charge data/pea_etf_universe.json, récupère les prix réels pour chaque ETF,
    calcule un score et retourne les nb_etf meilleurs triés par score décroissant.

    geo_filter : "monde" | "usa" | "tech" | "europe" | "emerging" | ""
    budget     : 0 = pas de filtre de prix
    """
    from plugins.builtin.finance import _fetch_ticker_http

    # Charge l'univers
    universe: list[dict] = []
    if _ETF_UNIVERSE_PATH.exists():
        try:
            universe = json.loads(_ETF_UNIVERSE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"screen_etf_pea: cannot load universe: {e}")

    if not universe:
        logger.warning("screen_etf_pea: univers ETF vide — fichier manquant ?")
        return []

    # Filtre géographique
    if geo_filter:
        gf = geo_filter.lower()
        subset = [e for e in universe if gf in (e.get("geo") or "").lower()]
        if subset:
            universe = subset

    # Filtre sur les ETFs PEA uniquement (champ pea == true)
    pea_only = [e for e in universe if e.get("pea", True)]
    if pea_only:
        universe = pea_only

    # Récupère les données en parallèle (limité à 50 ETFs pour la vitesse)
    candidates = universe[:50]

    def _fetch(entry: dict):
        tk = entry["ticker"]
        d3m = _fetch_ticker_http(tk, "3mo")
        d1m = _fetch_ticker_http(tk, "1mo")
        d1y = _fetch_ticker_http(tk, "1y")
        return entry, d3m, d1m, d1y

    results = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch, e): e for e in candidates}
        for f in as_completed(futs):
            try:
                entry, d3m, d1m, d1y = f.result()
                if not d3m or not d3m.get("price"):
                    continue
                price = d3m["price"]
                closes = d3m.get("closes", [])
                data = {
                    **entry,
                    "price":    price,
                    "chg_24h":  d3m.get("chg"),
                    "perf_1m":  d1m.get("perf") if d1m else None,
                    "perf_3m":  d3m.get("perf"),
                    "perf_1y":  d1y.get("perf") if d1y else None,
                    "closes":   closes,
                }
                score, reasons = _score_etf(closes, data)
                # levels peut être {} si l'historique est trop court : on garantit
                # au moins le prix pour l'affichage et l'allocation.
                levels = _compute_levels(closes) or {}
                levels["price"] = price
                # Filtre budget : on écarte les ETFs dont une part dépasse le budget.
                if budget and budget > 0 and price > budget:
                    continue
                results.append((entry["ticker"], score, data, reasons, levels))
            except Exception:
                pass

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:nb_etf]


def extract_explicit_tickers(text: str) -> list[str]:
    """Détecte les tickers explicites dans un texte (ex: 'VLA.PA', 'ASML.AS')."""
    if not text:
        return []
    found = _TICKER_RE.findall(text.upper())
    blacklist = {"P.S", "P.M", "A.M", "E.G", "I.E", "U.S", "C.A", "N.D"}
    return [t for t in dict.fromkeys(found) if t not in blacklist]


# ─── Calcul niveaux techniques (entry, TP, SL) ────────────────────────────────

def _compute_levels(closes: list[float]) -> dict:
    """Calcule les niveaux clés de trading depuis une série de prix."""
    if len(closes) < 14:
        return {}
    try:
        price = closes[-1]

        delta  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(d, 0.0) for d in delta]
        losses = [max(-d, 0.0) for d in delta]
        ag     = sum(gains[-14:]) / 14
        al     = sum(losses[-14:]) / 14
        rsi    = 100 - 100 / (1 + ag / al) if al > 0 else (100.0 if ag > 0 else 50.0)

        sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else price
        sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

        std20  = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5 if len(closes) >= 20 else price * 0.02
        bb_up  = sma20 + 2 * std20
        bb_lo  = sma20 - 2 * std20
        bb_pct = (price - bb_lo) / (bb_up - bb_lo) * 100 if bb_up != bb_lo else 50

        atr = sum(abs(closes[i] - closes[i - 1]) for i in range(-14, 0)) / 14

        # MACD (EMA12 - EMA26, signal = EMA9 du MACD)
        macd = macd_signal = macd_hist = None
        if len(closes) >= 26:
            def _ema(series, span):
                k = 2 / (span + 1)
                e = series[0]
                out = [e]
                for v in series[1:]:
                    e = v * k + e * (1 - k)
                    out.append(e)
                return out
            ema12 = _ema(closes, 12)
            ema26 = _ema(closes, 26)
            macd_line = [a - b for a, b in zip(ema12, ema26)]
            sig_line = _ema(macd_line[-35:], 9) if len(macd_line) >= 9 else macd_line
            macd = round(macd_line[-1], 4)
            macd_signal = round(sig_line[-1], 4)
            macd_hist = round(macd - macd_signal, 4)

        support    = max([s for s in [bb_lo, sma50, sma20 * 0.97] if s and s < price], default=price - 2 * atr)
        entry_low  = round(max(support, price - 2 * atr), 4)
        entry_high = round(price * 1.005, 4)

        tp1 = round(price + 2 * atr, 4)
        tp2 = round(price + 4 * atr, 4)
        if tp1 <= price * 1.01:
            tp1 = round(price * 1.08, 4)
            tp2 = round(price * 1.18, 4)

        sl   = round(min([s for s in [bb_lo, sma20 * 0.96, price - 2.5 * atr] if s and s < price],
                         default=price * 0.92), 4)
        risk   = price - sl
        reward = tp1 - price
        rr     = round(reward / risk, 2) if risk > 0 else 0

        return {
            "price":     round(price, 4),
            "rsi":       round(rsi, 1),
            "sma20":     round(sma20, 4),
            "sma50":     round(sma50, 4) if sma50 else None,
            "bb_pct":    round(bb_pct, 1),
            "bb_up":     round(bb_up, 4),
            "bb_lo":     round(bb_lo, 4),
            "atr":       round(atr, 4),
            "macd":      macd,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "entry_low": entry_low,
            "entry_high":entry_high,
            "tp1":       tp1,
            "tp2":       tp2,
            "sl":        sl,
            "rr":        rr,
        }
    except Exception:
        return {}


# ─── Scoring ETF ──────────────────────────────────────────────────────────────

def _score_etf(closes: list[float], data: dict) -> tuple[float, list[str]]:
    score   = 50.0
    reasons = []

    if len(closes) >= 20:
        lvl = _compute_levels(closes)
        rsi = lvl.get("rsi", 50)

        if rsi < 30:
            score += 18; reasons.append(f"RSI survendu ({rsi:.0f}) → signal achat fort")
        elif rsi < 42:
            score += 8;  reasons.append(f"RSI faible ({rsi:.0f}) → accumulation")
        elif rsi > 72:
            score -= 18; reasons.append(f"RSI suracheté ({rsi:.0f}) → attendre correction")
        elif rsi > 60:
            score -= 8;  reasons.append(f"RSI élevé ({rsi:.0f}) → prudence")

        sma20 = lvl.get("sma20", closes[-1])
        price = closes[-1]
        if price > sma20 * 1.02:
            score += 10; reasons.append("Tendance CT haussière (> SMA20 +2%)")
        elif price > sma20:
            score += 5;  reasons.append("Au-dessus SMA20")
        elif price < sma20 * 0.98:
            score -= 10; reasons.append("Tendance CT baissière (< SMA20 -2%)")
        else:
            score -= 5;  reasons.append("Sous SMA20")

        bb_pct = lvl.get("bb_pct", 50)
        if bb_pct < 15:
            score += 8; reasons.append(f"Sous Bollinger inférieur ({bb_pct:.0f}%) → survente")
        elif bb_pct > 85:
            score -= 8; reasons.append(f"Au-dessus Bollinger supérieur ({bb_pct:.0f}%) → surachat")

    p1m = data.get("perf_1m")
    if p1m is not None:
        if p1m > 8:    score += 8;  reasons.append(f"Momentum 1m fort ({p1m:+.1f}%)")
        elif p1m > 3:  score += 4;  reasons.append(f"Momentum 1m positif ({p1m:+.1f}%)")
        elif p1m < -10:score -= 12; reasons.append(f"Correction récente importante ({p1m:+.1f}%)")
        elif p1m < -5: score -= 6;  reasons.append(f"Correction récente ({p1m:+.1f}%)")

    ter = data.get("ter", 0.3)
    if ter <= 0.15:   score += 6;  reasons.append(f"TER excellent ({ter*100:.2f}%/an)")
    elif ter <= 0.25: score += 3;  reasons.append(f"TER compétitif ({ter*100:.2f}%/an)")
    elif ter >= 0.40: score -= 4;  reasons.append(f"TER élevé ({ter*100:.2f}%/an)")

    aum = data.get("aum_b", 0)
    if aum >= 2:    score += 4; reasons.append(f"Fonds large ({aum:.1f}B€) → liquidité forte")
    elif aum < 0.2: score -= 3; reasons.append(f"Fonds petit ({aum:.1f}B€) → liquidité réduite")

    return round(max(0.0, min(100.0, score)), 1), reasons


# ─── Prompt LLM final ────────────────────────────────────────────────────────

def _build_synthesis_prompt(
    question: str, intent: dict,
    market_data: dict, etf_ranked: list,
    stock_data: dict, vix: float, eurusd: float,
    web_snippets: str, data_quality: str,
) -> tuple[str, str]:
    """Retourne (system_prompt, user_prompt) pour la synthèse finale."""

    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    budget   = intent.get("budget")
    acct     = intent.get("account_type", "PEA")
    risk     = intent.get("risk_profile", "équilibré")
    horizon  = intent.get("time_horizon", "long")

    macro_lines = []
    for sym, d in market_data.items():
        label = MARKET_INDICES.get(sym, sym)
        icon  = "🟢" if d.get("chg", 0) >= 0 else "🔴"
        p1m   = f" | 1m:{d['perf1m']:+.1f}%" if d.get("perf1m") is not None else ""
        p3m   = f" | 3m:{d['perf3m']:+.1f}%" if d.get("perf3m") is not None else ""
        macro_lines.append(f"  {label:20s} {icon} J:{d.get('chg',0):+.2f}%{p1m}{p3m}")

    vix_regime = (
        "⚠️ COMPLACENCY (VIX<15) — euphorie, risque de correction élevé"
        if vix < 15 else (
        "🟢 FEAR (VIX>30) — panique = opportunité historique, DCA agressif justifié"
        if vix > 30 else (
        "🟠 NERVOSITÉ (VIX 20-30) — prudence, préférer DCA sur 4-6 semaines"
        if vix > 20 else "✅ NORMAL (VIX 15-20) — conditions standard")))

    eur_ctx = (
        f"EUR/USD={eurusd:.4f} — "
        + ("EUR faible: ETFs US coûtent plus cher" if eurusd < 1.05
           else "EUR fort: favorable pour ETFs US libellés en €" if eurusd > 1.12
           else "EUR/USD stable")
    )

    etf_block = ""
    if etf_ranked:
        etf_block = "\nTOP ETFs PEA CLASSÉS (données réelles ou estimation):\n"
        for ticker, score, data, reasons, levels in etf_ranked[:8]:
            p1m = f"{data.get('perf_1m'):+.1f}%" if data.get("perf_1m") is not None else "N/D"
            p3m = f"{data.get('perf_3m'):+.1f}%" if data.get("perf_3m") is not None else "N/D"
            p1y = f"{data.get('perf_1y'):+.1f}%" if data.get("perf_1y") is not None else "N/D"
            price_str = f"{levels['price']:.2f}€" if levels.get("price") else "N/D"
            etf_block += (
                f"  {ticker} | {data.get('name','')[:30]:30s} | TER:{data.get('ter',0)*100:.2f}% "
                f"| Prix:{price_str} | 1m:{p1m} | 3m:{p3m} | Score:{score}/100\n"
            )
            if levels.get("entry_low"):
                etf_block += (
                    f"    → Entrée:{levels['entry_low']:.2f}-{levels['entry_high']:.2f}€ "
                    f"| TP1:{levels['tp1']:.2f}€ | SL:{levels['sl']:.2f}€ "
                    f"| R/R:{levels['rr']:.1f}:1 | RSI:{levels['rsi']:.0f}\n"
                )
            if reasons:
                etf_block += f"    Signaux: {' | '.join(reasons[:3])}\n"

    stock_block = ""
    if stock_data:
        stock_block = "\nACTIONS ANALYSÉES:\n"
        for ticker, d in stock_data.items():
            p1m   = f"{d.get('perf_1m'):+.1f}%" if d.get("perf_1m") is not None else "N/D"
            lvl   = d.get("levels", {})
            price_str = f"{lvl['price']:.2f}" if lvl.get("price") else "N/D"
            currency  = d.get("currency", "€")
            name      = d.get("name", ticker)
            stock_block += (
                f"  {ticker} | {name[:22]:22s} | Prix:{price_str}{currency} | 1m:{p1m}\n"
            )
            if lvl.get("entry_low"):
                stock_block += (
                    f"    → Entrée:{lvl['entry_low']:.2f}-{lvl['entry_high']:.2f} "
                    f"| TP1:{lvl['tp1']:.2f} | SL:{lvl['sl']:.2f} "
                    f"| RSI:{lvl['rsi']:.0f} | BB:{lvl['bb_pct']:.0f}%\n"
                )

    alloc_block = ""
    if budget and etf_ranked:
        top3 = [(t, lv) for t, _, d, _, lv in etf_ranked[:3] if lv.get("price")]
        if top3:
            if budget < 300:
                weights = [(top3[0][0], top3[0][1], 1.0)]
            elif budget < 800:
                weights = ([(top3[0][0], top3[0][1], 0.65), (top3[1][0], top3[1][1], 0.35)]
                           if len(top3) >= 2 else [(top3[0][0], top3[0][1], 1.0)])
            else:
                weights = ([(top3[0][0], top3[0][1], 0.50),
                            (top3[1][0], top3[1][1], 0.30),
                            (top3[2][0], top3[2][1], 0.20)]
                           if len(top3) >= 3
                           else [(top3[0][0], top3[0][1], 0.65), (top3[1][0], top3[1][1], 0.35)])

            alloc_block = f"\nALLOCATION PRÉ-CALCULÉE pour {budget}€:\n"
            for tk, lv, w in weights:
                amount    = round(budget * w)
                price_lv  = lv.get("price", 0)
                n_units   = int(amount / price_lv) if price_lv > 0 else 0
                real_cost = n_units * price_lv
                reste     = amount - real_cost
                alloc_block += (
                    f"  {tk}: {amount}€ ({w*100:.0f}%) → {n_units} parts × {price_lv:.2f}€ "
                    f"= {real_cost:.2f}€ réels (reste: {reste:.2f}€)\n"
                )

    system_prompt = (
        "Tu es un gérant de portefeuille senior (CFA, 20 ans d'expérience, ex-desk prop trading Goldman Sachs). "
        "Tu analyses des données réelles et fournis des recommandations PRÉCISES et ACTIONNABLES. "
        "RÈGLES ABSOLUES:\n"
        "1. Pour CHAQUE actif recommandé: zone d'entrée exacte + TP1 + TP2 + stop-loss + ratio R/R\n"
        "2. Verdict clair: ACHETER MAINTENANT / DCA PROGRESSIF / ATTENDRE REPLI / ÉVITER\n"
        "3. Si budget donné: montants EXACTS en euros + nombre de parts calculé\n"
        "4. JAMAIS inventer de chiffres fondamentaux non fournis — écrire N/D\n"
        "5. JAMAIS 'consulter un conseiller financier'\n"
        "6. Données labelisées [estimation] si temps réel non disponible\n"
        "7. Minimum 3 risques concrets avec probabilité estimée\n"
        "8. Plan d'action numéroté EN CONCLUSION avec étapes précises pour AUJOURD'HUI"
    )

    user_prompt = f"""
QUESTION: "{question}"
Budget: {budget}€ | Compte: {acct} | Profil: {risk} | Horizon: {horizon}
Qualité données: {data_quality}

═══ DONNÉES MARCHÉ RÉELLES ({date_str}) ═══
{chr(10).join(macro_lines) if macro_lines else "Non disponibles"}

VIX = {vix:.1f} → {vix_regime}
{eur_ctx}
{etf_block}
{stock_block}
{alloc_block}
═══ ACTUALITÉS/CONTEXTE WEB ═══
{web_snippets[:2500] if web_snippets else "Non disponible — base-toi sur tes connaissances récentes"}

═══ MISSION ═══
Rédige un RAPPORT D'INVESTISSEMENT PROFESSIONNEL en français.
Structure OBLIGATOIRE:

## 🌍 Contexte de Marché
[3 constats CHIFFRÉS sur la situation: indices, VIX, tendance dominante. 5 lignes max.]

## ⚡ VERDICT
**[ACHETER MAINTENANT / DCA EN X FOIS / ATTENDRE REPLI VERS X€ / ÉVITER]**
[Justification en 2 phrases avec les données ci-dessus]

## 🏆 Allocation Recommandée
[TABLEAU compact (max 6 colonnes): Actif | Montant€ | Zone Entrée | TP1/TP2 | SL | R/R]
[1 ligne par position. Logique de diversification en dessous du tableau.]

## 📊 Analyse Technique par Position
[Pour chaque position: RSI actuel, tendance SMA, signal MACD (si dispo), niveau Bollinger, momentum]

## ⏰ Stratégie d'Entrée Précise
[Lump sum ou DCA? Si DCA: montant × fréquence × durée]
[Ordre limite ou marché? Quel prix d'alerte pour renforcer?]

## ⚠️ Risques Concrets (3 minimum)
[Chaque risque: nature | probabilité estimée | impact | comment se protéger]

## ✅ Plan d'Action — CE QUE TU FAIS AUJOURD'HUI
1. [Action précise avec ticker, montant, type d'ordre]
2. [...]
3. [Ordre limite à placer pour renforcement]

---
CRITIQUE FINALE: Avant de répondre, vérifie que chaque position a ses niveaux TP/SL et que les montants totalisent exactement le budget.
"""
    return system_prompt, user_prompt


# ─── Agent principal (streaming generator) ────────────────────────────────────

def deep_finance_research(question: str) -> Generator[str, None, None]:
    """
    Analyse financière professionnelle multi-sources.
    Génère du markdown CUMULATIF (chaque yield = texte complet jusqu'ici).
    Fonctionne en mode dégradé si données temps réel indisponibles.
    """
    from plugins.builtin.finance import _fetch_ticker_http
    from llm.client import chat as llm_chat

    t0 = time.time()

    output: list[str] = []

    def emit(text: str) -> str:
        output.append(text)
        return "\n".join(output)

    yield emit(
        f"## 🔬 Rapport d'Investissement Professionnel\n"
        f"*Démarré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}*\n"
        f"\n---\n"
    )
    yield emit("\n### ⏳ Analyse en cours...\n")

    # ── Phase 1 : Intent ───────────────────────────────────────────────────────
    yield emit("\n**[1/6]** Interprétation de ta question...")

    intent: dict = {}
    try:
        ip = (
            f'Question: "{question}"\n\n'
            "Extrais en JSON strict (null si non mentionné):\n"
            '{"budget": <nombre|null>, "account_type": "PEA"|"CTO"|null,\n'
            '"risk_profile": "conservateur"|"équilibré"|"dynamique"|"agressif"|null,\n'
            '"time_horizon": "court"|"moyen"|"long"|null,\n'
            '"want_stocks": true|false,\n'
            '"want_etf": true|false,\n'
            '"preferences": ["nasdaq"|"monde"|"europe"|"usa"|"tech"|"dividende"|"smallcap"|"emerging"|"crypto"],\n'
            '"specific_tickers": [], "specific_names": []}\n'
            "Réponds UNIQUEMENT JSON valide."
        )
        raw = llm_chat([{"role": "user", "content": ip}], temperature=0.0)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            intent = json.loads(m.group())
    except Exception:
        pass

    budget  = intent.get("budget")
    acct    = intent.get("account_type") or "PEA"
    risk    = intent.get("risk_profile") or "équilibré"
    horizon = intent.get("time_horizon") or "long"
    prefs   = [p.lower() for p in (intent.get("preferences") or [])]

    # Tickers explicites — depuis l'intent LLM + regex sur le texte brut
    specific_tickers: list[str] = []
    for t in (intent.get("specific_tickers") or []):
        t = t.upper().strip()
        if "." not in t and len(t) <= 6:
            t += ".PA"
        if t not in specific_tickers:
            specific_tickers.append(t)
    for tk in extract_explicit_tickers(question):
        if tk not in specific_tickers:
            specific_tickers.append(tk)

    # Noms propres mentionnés (ex: "Valneva") → recherche dynamique
    specific_names: list[str] = intent.get("specific_names") or []

    want_stocks = intent.get("want_stocks", False) or bool(specific_tickers) or bool(specific_names)
    want_etf    = (not want_stocks or intent.get("want_etf", False)) and not specific_tickers

    mode_label = ('Actions' if want_stocks and not want_etf
                  else 'ETFs' if want_etf and not want_stocks
                  else 'ETFs + Actions')
    yield emit(
        f"\n  ✅ **Budget:** {budget}€ | **Compte:** {acct} | "
        f"**Profil:** {risk} | **Mode:** {mode_label}"
    )

    # ── Phase 2 : Macro (parallèle) ────────────────────────────────────────────
    yield emit("\n\n**[2/6]** Récupération données macro (indices, VIX, taux, change)...")

    market_data: dict[str, dict] = {}
    vix    = 20.0
    eurusd = 1.09

    def _fetch_index(sym: str):
        d1m = _fetch_ticker_http(sym, "1mo")
        d3m = _fetch_ticker_http(sym, "3mo")
        if d1m:
            return sym, {"label": MARKET_INDICES.get(sym, sym), "price": d1m["price"],
                         "chg": d1m["chg"], "perf1m": d1m.get("perf"),
                         "perf3m": d3m.get("perf") if d3m else None}
        return sym, {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_index, s): s for s in MARKET_INDICES}
        for f in as_completed(futs):
            try:
                sym, d = f.result()
                if d:
                    market_data[sym] = d
            except Exception:
                pass

    if "^VIX" in market_data:
        vix = market_data["^VIX"]["price"]
    if "EURUSD=X" in market_data:
        eurusd = market_data["EURUSD=X"]["price"]

    macro_ok = len(market_data) >= 3
    yield emit(f"\n  {'✅' if macro_ok else '⚠️'} {len(market_data)}/{len(MARKET_INDICES)} marchés récupérés")

    # ── Phase 3a : Scan ETFs via screen_etf_pea ───────────────────────────────
    etf_ranked: list[tuple] = []

    if want_etf:
        geo = ""
        if any(p in prefs for p in ("nasdaq", "tech")):
            geo = "tech"
        elif "europe" in prefs:
            geo = "europe"
        elif "emerging" in prefs:
            geo = "emerging"
        elif "usa" in prefs:
            geo = "usa"
        elif "monde" in prefs:
            geo = "monde"

        yield emit(f"\n\n**[3/6]** Scan univers ETF PEA (data/pea_etf_universe.json)...")
        etf_ranked = screen_etf_pea(budget=budget or 0.0, nb_etf=8, geo_filter=geo)
        top_str = ", ".join(f"{t}({s:.0f})" for t, s, _, _, _ in etf_ranked[:5])
        yield emit(f"\n  ✅ {len(etf_ranked)} ETFs classés | Top: {top_str or 'N/D'}")
    else:
        yield emit("\n\n**[3/6]** *(ETFs ignorés — actions/tickers spécifiques demandés)*")

    # ── Phase 3b : Scan actions (priorité aux tickers/noms demandés) ──────────
    stock_data: dict[str, dict] = {}

    if want_stocks:
        # Résolution des noms propres → tickers via search_ticker_by_name
        resolved_from_names: list[str] = []
        if specific_names:
            yield emit(f"\n\n**[3b/6]** Résolution de noms → tickers: {', '.join(specific_names)}")
            for name in specific_names[:5]:
                hits = search_ticker_by_name(name)
                if hits:
                    resolved_from_names.append(hits[0]["ticker"])
                    yield emit(f"\n  🔍 `{name}` → `{hits[0]['ticker']}` ({hits[0]['name']})")
                else:
                    # Fallback: essai direct avec .PA
                    fallback = name.upper().replace(" ", "") + ".PA"
                    resolved_from_names.append(fallback)
                    yield emit(f"\n  ⚠️ `{name}` non résolu — essai `{fallback}`")

        all_tickers = list(dict.fromkeys(specific_tickers + resolved_from_names))

        if not all_tickers:
            # Pas de tickers spécifiques → quelques grandes caps françaises via lookup
            default_tickers = ["MC.PA", "AIR.PA", "SAF.PA", "SAN.PA", "TTE.PA",
                               "BNP.PA", "CAP.PA", "RMS.PA", "DSY.PA", "KER.PA"]
            all_tickers = default_tickers[:8]

        yield emit(f"\n\n**[3b/6]** Analyse dynamique de {len(all_tickers)} valeurs...")
        if all_tickers:
            yield emit(f"\n  🎯 Tickers: {', '.join(all_tickers)}")

        def _fetch_stock(tk: str):
            return tk, lookup_any_ticker(tk)

        not_found: list[str] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_stock, tk): tk for tk in all_tickers}
            for f in as_completed(futs):
                try:
                    tk, look = f.result()
                    if look.get("found"):
                        resolved = look["ticker"]
                        stock_data[resolved] = {
                            "price":    look["price"],
                            "perf_1m":  look.get("perf_1m"),
                            "perf_1y":  look.get("perf_1y"),
                            "closes":   look.get("closes", []),
                            "levels":   look.get("levels", {}),
                            "name":     look.get("name", resolved),
                            "currency": look.get("currency", ""),
                        }
                    else:
                        not_found.append(look.get("ticker", "?"))
                except Exception:
                    pass

        if not_found:
            yield emit(f"\n  ⚠️ Introuvables: {', '.join(f'`{t}`' for t in not_found)}")
        yield emit(f"\n  ✅ {len(stock_data)} valeurs avec données")
    else:
        yield emit("\n\n**[3b/6]** *(actions ignorées — ETFs demandés)*")

    # ── Phase 4 : Qualité des données ─────────────────────────────────────────
    total_assets = len(etf_ranked) + len(stock_data)

    if total_assets == 0 and not macro_ok:
        data_quality = "⚠️ AUCUNE donnée temps réel — analyse basée sur connaissances du modèle [estimation]"
    elif total_assets == 0:
        data_quality = f"⚠️ Données macro partielles ({len(market_data)} indices) — données actifs indisponibles [estimation partielle]"
    else:
        data_quality = f"✅ Données temps réel ({len(market_data)} indices + {total_assets} actifs scannés)"

    yield emit(f"\n\n**[4/6]** Qualité données: {data_quality}")

    # ── Phase 5 : Recherche web ────────────────────────────────────────────────
    yield emit("\n\n**[5/6]** Recherche web — contexte et actualités...")

    web_snippets = ""
    web_count    = 0
    try:
        from duckduckgo_search import DDGS
        queries = []
        all_query_tickers = list(dict.fromkeys(specific_tickers + list(stock_data.keys())))
        for tk in all_query_tickers[:3]:
            name = stock_data.get(tk, {}).get("name", tk.replace(".PA", ""))
            queries.append(f"{name} {tk} analyse investissement achat {datetime.now().year}")
            queries.append(f"{name} actualité cours bourse {datetime.now().strftime('%B %Y')}")
        if not queries:
            if acct == "PEA":
                queries.append(f"meilleur ETF PEA investissement {datetime.now().year} analyse")
            if want_stocks:
                queries.append(f"actions françaises PEA opportunité {datetime.now().strftime('%B %Y')}")
            queries.append(f"marché boursier perspective {datetime.now().strftime('%B %Y')} analyse")

        snips: list[str] = []
        with DDGS() as ddgs:
            for q in queries[:4]:
                try:
                    for r in ddgs.text(q, max_results=3, region="fr-fr"):
                        t = str(r.get("title", ""))
                        b = str(r.get("body", ""))[:400]
                        if t:
                            snips.append(f"• {t}\n  {b}")
                    time.sleep(0.5)
                except Exception:
                    pass
        web_snippets = "\n\n".join(snips[:9])
        web_count    = len(snips)
    except Exception as e:
        logger.warning(f"[DeepFinance] Web search error: {e}")

    yield emit(f"\n  {'✅' if web_count > 0 else '⚠️'} {web_count} extraits web")

    # ── Phase 6 : Synthèse LLM ─────────────────────────────────────────────────
    yield emit("\n\n**[6/6]** Synthèse par le modèle LLM (rapport complet)...\n")

    sys_prompt, usr_prompt = _build_synthesis_prompt(
        question=question,
        intent=intent,
        market_data=market_data,
        etf_ranked=etf_ranked,
        stock_data=stock_data,
        vix=vix,
        eurusd=eurusd,
        web_snippets=web_snippets,
        data_quality=data_quality,
    )

    try:
        report = llm_chat(
            [{"role": "system", "content": sys_prompt},
             {"role": "user",   "content": usr_prompt}],
            temperature=0.2,
        )
    except Exception as e:
        report = f"❌ Erreur LLM: {e}"

    elapsed = round(time.time() - t0)
    mins    = elapsed // 60
    secs    = elapsed % 60

    final = [
        f"## 🔬 Rapport d'Investissement Professionnel",
        f"*{datetime.now().strftime('%d/%m/%Y %H:%M')} — {mins}m{secs:02d}s — "
        f"{len(market_data)} marchés — {total_assets} actifs — {web_count} sources web*",
        "",
        report,
        "",
        "---",
        f"*Données: Yahoo Finance temps réel • Scoring: RSI+SMA+momentum+TER+AUM • "
        f"Sources web: {web_count}*",
    ]
    yield "\n".join(final)
