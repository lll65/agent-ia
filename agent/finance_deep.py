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
from typing import Generator

logger = logging.getLogger(__name__)

# ─── Univers ETF PEA éligibles ────────────────────────────────────────────────

PEA_ETF_UNIVERSE: dict[str, dict] = {
    "WPEA.PA":  {"name": "Amundi MSCI World PEA Acc",       "index": "MSCI World",    "ter": 0.38, "geo": "monde",    "aum_b": 2.5},
    "MWRD.PA":  {"name": "Amundi MSCI World II PEA",        "index": "MSCI World",    "ter": 0.20, "geo": "monde",    "aum_b": 0.8},
    "CW8.PA":   {"name": "Amundi MSCI World UCITS ETF",     "index": "MSCI World",    "ter": 0.38, "geo": "monde",    "aum_b": 3.1},
    "PSP5.PA":  {"name": "Amundi PEA S&P 500",              "index": "S&P 500",       "ter": 0.15, "geo": "usa",      "aum_b": 1.2},
    "PANX.PA":  {"name": "Amundi Nasdaq-100 PEA Acc",       "index": "Nasdaq-100",    "ter": 0.23, "geo": "tech",     "aum_b": 2.8},
    "PUST.PA":  {"name": "Lyxor Nasdaq-100 PEA",            "index": "Nasdaq-100",    "ter": 0.30, "geo": "tech",     "aum_b": 0.9},
    "MEH.PA":   {"name": "Amundi MSCI Europe PEA",          "index": "MSCI Europe",   "ter": 0.15, "geo": "europe",   "aum_b": 0.5},
    "PAEEM.PA": {"name": "Amundi MSCI Emerging Mkts PEA",   "index": "MSCI EM",       "ter": 0.20, "geo": "emerging", "aum_b": 0.7},
    "RS2K.PA":  {"name": "Lyxor Russell 2000 PEA",          "index": "Russell 2000",  "ter": 0.30, "geo": "smallcap", "aum_b": 0.3},
}

# Actions PEA éligibles (valeurs françaises + européennes cotées sur Euronext)
PEA_STOCKS: dict[str, dict] = {
    # Tech / Croissance
    "SOI.PA":    {"name": "Soitec",          "sector": "Semiconducteurs", "cap": "mid"},
    "CAP.PA":    {"name": "Capgemini",       "sector": "Tech/IT",         "cap": "large"},
    "DSY.PA":    {"name": "Dassault Systèmes","sector": "Logiciel",       "cap": "large"},
    "SAF.PA":    {"name": "Safran",          "sector": "Aéronautique",    "cap": "large"},
    "AIR.PA":    {"name": "Airbus",          "sector": "Aéronautique",    "cap": "large"},
    "STMPA.PA":  {"name": "STMicroelectronics","sector": "Semiconducteurs","cap": "large"},
    # Luxe / Consommation
    "MC.PA":     {"name": "LVMH",            "sector": "Luxe",            "cap": "large"},
    "RMS.PA":    {"name": "Hermès",          "sector": "Luxe",            "cap": "large"},
    "KER.PA":    {"name": "Kering",          "sector": "Luxe",            "cap": "large"},
    # Finance
    "BNP.PA":    {"name": "BNP Paribas",     "sector": "Banque",          "cap": "large"},
    "GLE.PA":    {"name": "Société Générale","sector": "Banque",          "cap": "large"},
    "ACA.PA":    {"name": "Crédit Agricole", "sector": "Banque",          "cap": "large"},
    # Énergie / Utilities
    "TTE.PA":    {"name": "TotalEnergies",   "sector": "Énergie",         "cap": "large"},
    "VIE.PA":    {"name": "Veolia",          "sector": "Utilities",       "cap": "large"},
    # Santé
    "SAN.PA":    {"name": "Sanofi",          "sector": "Pharma",          "cap": "large"},
    # Cycliques
    "RNO.PA":    {"name": "Renault",         "sector": "Auto",            "cap": "mid"},
    "ML.PA":     {"name": "Michelin",        "sector": "Auto",            "cap": "large"},
    # Biotech / Santé small-mid
    "VLA.PA":    {"name": "Valneva",           "sector": "Biotech/Vaccins",  "cap": "small"},
    "OSE.PA":    {"name": "OSE Immunotherapeutics","sector": "Biotech",       "cap": "micro"},
    "DBV.PA":    {"name": "DBV Technologies",  "sector": "Biotech",          "cap": "small"},
    "ERF.PA":    {"name": "Eurofins Scientific","sector": "Laboratoires",    "cap": "large"},
    "OPT.PA":    {"name": "Optics Balzers",    "sector": "Optique",          "cap": "micro"},
    # Industrie / Défense
    "HO.PA":     {"name": "Thales",            "sector": "Défense/Électro",  "cap": "large"},
    "AM.PA":     {"name": "Dassault Aviation", "sector": "Défense/Aéro",     "cap": "large"},
    "LDL.PA":    {"name": "Latecoere",         "sector": "Aéronautique",     "cap": "small"},
}

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


# ─── Calcul niveaux techniques (entry, TP, SL) ────────────────────────────────

def _compute_levels(closes: list[float]) -> dict:
    """
    Calcule les niveaux clés de trading depuis une série de prix.
    Retourne dict avec entry_low, entry_high, tp1, tp2, sl, rsi, sma20, sma50, bb_pos.
    """
    if len(closes) < 14:
        return {}
    try:
        price = closes[-1]

        # RSI(14)
        delta  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(d, 0.0) for d in delta]
        losses = [max(-d, 0.0) for d in delta]
        ag     = sum(gains[-14:]) / 14
        al     = sum(losses[-14:]) / 14
        rsi    = 100 - 100 / (1 + ag / al) if al > 0 else (100.0 if ag > 0 else 50.0)

        # SMA
        sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else price
        sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

        # Bollinger(20)
        std20  = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5 if len(closes) >= 20 else price * 0.02
        bb_up  = sma20 + 2 * std20
        bb_lo  = sma20 - 2 * std20
        bb_pct = (price - bb_lo) / (bb_up - bb_lo) * 100 if bb_up != bb_lo else 50

        # ATR(14) approximé
        atr = sum(abs(closes[i] - closes[i - 1]) for i in range(-14, 0)) / 14

        # Niveaux d'entrée (zone ±0.5% autour du prix ou support SMA)
        support = max([s for s in [bb_lo, sma50, sma20 * 0.97] if s and s < price], default=price - 2 * atr)
        entry_low  = round(max(support, price - 2 * atr), 4)
        entry_high = round(price * 1.005, 4)  # légèrement au-dessus pour ordre market

        # TP basés sur résistances dynamiques
        tp1 = round(price + 2 * atr, 4)
        tp2 = round(price + 4 * atr, 4)
        # Fallback: TP1 = +8%, TP2 = +18%
        if tp1 <= price * 1.01:
            tp1 = round(price * 1.08, 4)
            tp2 = round(price * 1.18, 4)

        # Stop-loss sous support le plus proche
        sl = round(min([s for s in [bb_lo, sma20 * 0.96, price - 2.5 * atr] if s and s < price],
                       default=price * 0.92), 4)

        # Ratio risque/rendement
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
        if p1m > 8:   score += 8;  reasons.append(f"Momentum 1m fort ({p1m:+.1f}%)")
        elif p1m > 3: score += 4;  reasons.append(f"Momentum 1m positif ({p1m:+.1f}%)")
        elif p1m < -10:score -= 12;reasons.append(f"Correction récente importante ({p1m:+.1f}%)")
        elif p1m < -5: score -= 6; reasons.append(f"Correction récente ({p1m:+.1f}%)")

    ter = data.get("ter", 0.3)
    if ter <= 0.15:   score += 6;  reasons.append(f"TER excellent ({ter*100:.2f}%/an)")
    elif ter <= 0.25: score += 3;  reasons.append(f"TER compétitif ({ter*100:.2f}%/an)")
    elif ter >= 0.40: score -= 4;  reasons.append(f"TER élevé ({ter*100:.2f}%/an)")

    aum = data.get("aum_b", 0)
    if aum >= 2:   score += 4; reasons.append(f"Fonds large ({aum:.1f}B€) → liquidité forte")
    elif aum < 0.2:score -= 3; reasons.append(f"Fonds petit ({aum:.1f}B€) → liquidité réduite")

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
    is_stock = intent.get("want_stocks", False)

    # Macro
    macro_lines = []
    for sym, d in market_data.items():
        label = MARKET_INDICES.get(sym, sym)
        icon  = "🟢" if d.get("chg", 0) >= 0 else "🔴"
        p1m   = f" | 1m:{d['perf1m']:+.1f}%" if d.get("perf1m") is not None else ""
        p3m   = f" | 3m:{d['perf3m']:+.1f}%" if d.get("perf3m") is not None else ""
        macro_lines.append(f"  {label:20s} {icon} J:{d.get('chg',0):+.2f}%{p1m}{p3m}")

    vix_regime = (
        "⚠️ COMPLACENCY (VIX<15) — euphorie, risque de correction élevé, entrées fractionnées recommandées"
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

    # ETF data block
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
                f"| AUM:{data.get('aum_b',0):.1f}B€ | Prix:{price_str} "
                f"| 1m:{p1m} | 3m:{p3m} | 1y:{p1y} | Score:{score}/100\n"
            )
            if levels.get("entry_low"):
                etf_block += (
                    f"    → Entrée:{levels['entry_low']:.2f}-{levels['entry_high']:.2f}€ "
                    f"| TP1:{levels['tp1']:.2f}€ | TP2:{levels['tp2']:.2f}€ "
                    f"| SL:{levels['sl']:.2f}€ | R/R:{levels['rr']:.1f}:1 "
                    f"| RSI:{levels['rsi']:.0f}\n"
                )
            if reasons:
                etf_block += f"    Signaux: {' | '.join(reasons[:3])}\n"

    # Stock data block
    stock_block = ""
    if stock_data:
        stock_block = "\nACTIONS PEA ANALYSÉES:\n"
        for ticker, d in stock_data.items():
            meta  = PEA_STOCKS.get(ticker, {})
            p1m   = f"{d.get('perf_1m'):+.1f}%" if d.get("perf_1m") is not None else "N/D"
            lvl   = d.get("levels", {})
            price_str = f"{lvl['price']:.2f}€" if lvl.get("price") else "N/D"
            stock_block += (
                f"  {ticker} | {meta.get('name',''):20s} | {meta.get('sector',''):20s} "
                f"| Prix:{price_str} | 1m:{p1m}\n"
            )
            if lvl.get("entry_low"):
                stock_block += (
                    f"    → Entrée:{lvl['entry_low']:.2f}-{lvl['entry_high']:.2f}€ "
                    f"| TP1:{lvl['tp1']:.2f}€ | SL:{lvl['sl']:.2f}€ "
                    f"| RSI:{lvl['rsi']:.0f} | BB:{lvl['bb_pct']:.0f}%\n"
                )

    # Allocation pré-calculée
    alloc_block = ""
    if budget and etf_ranked:
        top3 = [(t, lv) for t, _, d, _, lv in etf_ranked[:3] if lv.get("price")]
        if top3:
            if budget < 300:
                weights = [(top3[0][0], top3[0][1], 1.0)]
            elif budget < 800:
                weights = [(top3[0][0], top3[0][1], 0.65), (top3[1][0], top3[1][1], 0.35)] if len(top3) >= 2 else [(top3[0][0], top3[0][1], 1.0)]
            else:
                weights = [(top3[0][0], top3[0][1], 0.50), (top3[1][0], top3[1][1], 0.30), (top3[2][0], top3[2][1], 0.20)] if len(top3) >= 3 else [(top3[0][0], top3[0][1], 0.65), (top3[1][0], top3[1][1], 0.35)]

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
    from agent.system_prompt import FINANCE_SYSTEM_PROMPT

    t0 = time.time()

    # Buffer de sortie cumulatif
    output: list[str] = []

    def emit(text: str) -> str:
        output.append(text)
        return "\n".join(output)

    # ── Header ─────────────────────────────────────────────────────────────────
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
            '"specific_tickers": []}\n'
            "Réponds UNIQUEMENT JSON valide."
        )
        raw = llm_chat([{"role": "user", "content": ip}], temperature=0.0)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            intent = json.loads(m.group())
    except Exception:
        pass

    budget      = intent.get("budget")
    acct        = intent.get("account_type") or "PEA"
    risk        = intent.get("risk_profile") or "équilibré"
    horizon     = intent.get("time_horizon") or "long"
    specific_tickers_raw = [t.upper() for t in (intent.get("specific_tickers") or [])]
    # Normalize: add .PA suffix if missing and looks like French ticker
    specific_tickers: list[str] = []
    for t in specific_tickers_raw:
        if "." not in t and len(t) <= 6:
            specific_tickers.append(t + ".PA")
        else:
            specific_tickers.append(t)

    # Also check question text for known stock names
    _q_low = question.lower()
    _name_to_ticker = {v["name"].lower(): k for k, v in PEA_STOCKS.items()}
    for name_low, ticker in _name_to_ticker.items():
        if name_low in _q_low and ticker not in specific_tickers:
            specific_tickers.append(ticker)

    # If specific tickers detected, force want_stocks=True
    if specific_tickers:
        want_stocks = True
        want_etf = intent.get("want_etf", False)  # only ETF if explicitly asked

    want_stocks = intent.get("want_stocks", False) or bool(specific_tickers)
    want_etf    = (not want_stocks or intent.get("want_etf", False)) and not specific_tickers
    prefs       = [p.lower() for p in (intent.get("preferences") or [])]

    yield emit(
        f"\n  ✅ **Budget:** {budget}€ | **Compte:** {acct} | "
        f"**Profil:** {risk} | **Mode:** {'Actions' if want_stocks and not want_etf else 'ETFs' if want_etf and not want_stocks else 'ETFs + Actions'}"
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
                         "chg": d1m["chg"], "perf1m": d1m.get("perf"), "perf3m": d3m.get("perf") if d3m else None}
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

    # ── Phase 3a : Scan ETFs (si demandé) ─────────────────────────────────────
    etf_ranked: list[tuple] = []

    if want_etf:
        # Filtre par préférences
        if any(p in prefs for p in ("nasdaq", "tech")):
            tgt = {k: v for k, v in PEA_ETF_UNIVERSE.items() if v["geo"] in ("tech", "monde", "usa")}
        elif "europe" in prefs:
            tgt = {k: v for k, v in PEA_ETF_UNIVERSE.items() if v["geo"] in ("europe", "monde")}
        elif "emerging" in prefs:
            tgt = {k: v for k, v in PEA_ETF_UNIVERSE.items() if v["geo"] in ("emerging", "monde")}
        else:
            tgt = PEA_ETF_UNIVERSE

        yield emit(f"\n\n**[3/6]** Scan {len(tgt)} ETFs PEA en parallèle...")

        def _fetch_etf(tk: str):
            d1m = _fetch_ticker_http(tk, "1mo")
            d3m = _fetch_ticker_http(tk, "3mo")
            d1y = _fetch_ticker_http(tk, "1y")
            return tk, d1m, d3m, d1y

        etf_raw: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_etf, tk): tk for tk in tgt}
            for f in as_completed(futs):
                try:
                    tk, d1m, d3m, d1y = f.result()
                    if d3m:
                        meta = tgt[tk]
                        etf_raw[tk] = {
                            **meta,
                            "price":    d3m["price"],
                            "chg_24h":  d3m["chg"],
                            "perf_1m":  d1m.get("perf") if d1m else None,
                            "perf_3m":  d3m.get("perf"),
                            "perf_1y":  d1y.get("perf") if d1y else None,
                            "closes":   d3m.get("closes", []),
                        }
                except Exception:
                    pass

        for tk, data in etf_raw.items():
            score, reasons = _score_etf(data.get("closes", []), data)
            levels = _compute_levels(data.get("closes", []))
            if levels and data.get("price"):
                levels["price"] = data["price"]
            etf_ranked.append((tk, score, data, reasons, levels))
        etf_ranked.sort(key=lambda x: x[1], reverse=True)

        top_str = ", ".join(f"{t}({s:.0f})" for t, s, _, _, _ in etf_ranked[:5])
        yield emit(f"\n  ✅ {len(etf_raw)}/{len(tgt)} ETFs avec données | Top: {top_str}")

    # ── Phase 3b : Scan actions PEA (si demandé) ──────────────────────────────
    stock_data: dict[str, dict] = {}

    if want_stocks:
        # Sélectionner les actions pertinentes selon préférences
        candidates = {}
        if any(p in prefs for p in ("tech", "semiconducteur")):
            candidates = {k: v for k, v in PEA_STOCKS.items() if v["sector"] in ("Semiconducteurs", "Tech/IT", "Logiciel", "Aéronautique")}
        elif "luxe" in prefs or "consommation" in prefs:
            candidates = {k: v for k, v in PEA_STOCKS.items() if v["sector"] in ("Luxe", "Consommation")}
        elif any(p in prefs for p in ("dividende", "banque")):
            candidates = {k: v for k, v in PEA_STOCKS.items() if v["sector"] in ("Banque", "Énergie", "Utilities")}
        else:
            candidates = dict(list(PEA_STOCKS.items())[:10])  # top 10 par défaut

        # Always include explicitly requested tickers
        for tk in specific_tickers:
            if tk not in candidates:
                if tk in PEA_STOCKS:
                    candidates[tk] = PEA_STOCKS[tk]
                else:
                    # Dynamic lookup — try fetching even if not in our universe
                    candidates[tk] = {"name": tk, "sector": "?", "cap": "?"}

        yield emit(f"\n\n**[3b/6]** Scan {len(candidates)} actions PEA...")

        def _fetch_stock(tk: str):
            d3m = _fetch_ticker_http(tk, "3mo")
            d1y = _fetch_ticker_http(tk, "1y")
            return tk, d3m, d1y

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_stock, tk): tk for tk in candidates}
            for f in as_completed(futs):
                try:
                    tk, d3m, d1y = f.result()
                    if d3m:
                        stock_data[tk] = {
                            "price":    d3m["price"],
                            "perf_1m":  d3m.get("perf"),
                            "perf_1y":  d1y.get("perf") if d1y else None,
                            "closes":   d3m.get("closes", []),
                            "levels":   _compute_levels(d3m.get("closes", [])),
                        }
                except Exception:
                    pass

        yield emit(f"\n  ✅ {len(stock_data)}/{len(candidates)} actions avec données")
    else:
        yield emit("\n\n**[3b/6]** *(actions ignorées — ETFs demandés)*")

    # ── Phase 4 : Qualité des données ─────────────────────────────────────────
    total_assets = len(etf_raw) if want_etf else 0
    total_assets += len(stock_data)
    data_pct     = total_assets / max(len(PEA_ETF_UNIVERSE) + len(PEA_STOCKS), 1) * 100

    if total_assets == 0 and not macro_ok:
        data_quality = "⚠️ AUCUNE donnée temps réel — analyse basée sur connaissances du modèle [estimation]"
    elif total_assets == 0:
        data_quality = f"⚠️ Données macro partielles ({len(market_data)} indices) — données ETF/actions indisponibles [estimation partielle]"
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
        # Queries spécifiques aux tickers demandés (priorité maximale)
        for tk in specific_tickers[:3]:
            name = PEA_STOCKS.get(tk, {}).get("name", tk.replace(".PA", ""))
            queries.append(f"{name} {tk} analyse investissement achat 2026")
            queries.append(f"{name} actualité cours bourse {datetime.now().strftime('%B %Y')}")
        if not specific_tickers:
            if acct == "PEA":
                queries.append(f"meilleur ETF PEA investissement {datetime.now().year} analyse")
            if "nasdaq" in prefs or "tech" in prefs:
                queries.append("Nasdaq ETF perspective achat technique analyse")
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

    # Rapport final (remplace tout le contenu précédent)
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
