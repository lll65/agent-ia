"""
Deep Finance Research Agent — niveau conseiller en patrimoine senior.
Analyse multi-sources en streaming : macro, ETF scan, technicals, web, synthèse LLM.
Temps typique : 2-5 minutes selon la connexion.
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
# Tous éligibles PEA via réplication synthétique (swap) ou physique qualifiée.
# TER = Total Expense Ratio annuel en %

PEA_ETF_UNIVERSE: dict[str, dict] = {
    # ── Monde diversifié (cœur de portefeuille recommandé)
    "WPEA.PA":  {"name": "Amundi MSCI World PEA Acc",         "index": "MSCI World",    "ter": 0.38, "geo": "monde",   "aum_b": 2.5},
    "EWLD.PA":  {"name": "Lyxor MSCI World PEA Acc",          "index": "MSCI World",    "ter": 0.45, "geo": "monde",   "aum_b": 1.8},
    "MWRD.PA":  {"name": "Amundi MSCI World II PEA",          "index": "MSCI World",    "ter": 0.20, "geo": "monde",   "aum_b": 0.8},
    "CW8.PA":   {"name": "Amundi MSCI World UCITS ETF",       "index": "MSCI World",    "ter": 0.38, "geo": "monde",   "aum_b": 3.1},
    # ── S&P 500
    "PSP5.PA":  {"name": "Amundi PEA S&P 500",                "index": "S&P 500",       "ter": 0.15, "geo": "usa",     "aum_b": 1.2},
    "500H.PA":  {"name": "Amundi PEA S&P 500 ESG",            "index": "S&P 500 ESG",   "ter": 0.15, "geo": "usa",     "aum_b": 0.3},
    # ── Nasdaq / Tech
    "PANX.PA":  {"name": "Amundi Nasdaq-100 PEA Acc",         "index": "Nasdaq-100",    "ter": 0.23, "geo": "tech",    "aum_b": 2.8},
    "PUST.PA":  {"name": "Lyxor Nasdaq-100 PEA",              "index": "Nasdaq-100",    "ter": 0.30, "geo": "tech",    "aum_b": 0.9},
    # ── Europe
    "MEH.PA":   {"name": "Amundi MSCI Europe PEA",            "index": "MSCI Europe",   "ter": 0.15, "geo": "europe",  "aum_b": 0.5},
    "ESE.PA":   {"name": "Amundi Euro Stoxx 50 PEA",          "index": "Euro Stoxx 50", "ter": 0.15, "geo": "europe",  "aum_b": 0.4},
    # ── Émergents
    "PAEEM.PA": {"name": "Amundi MSCI Emerging Mkts PEA",     "index": "MSCI EM",       "ter": 0.20, "geo": "emerging","aum_b": 0.7},
    # ── Small Caps
    "RS2K.PA":  {"name": "Lyxor Russell 2000 PEA",            "index": "Russell 2000",  "ter": 0.30, "geo": "smallcap","aum_b": 0.3},
    # ── Japon
    "JPNH.PA":  {"name": "Amundi MSCI Japan PEA",             "index": "MSCI Japan",    "ter": 0.20, "geo": "japan",   "aum_b": 0.2},
    # ── Obligations (PEA-PME mais souvent utilisé)
    "OBLI.PA":  {"name": "Lyxor Euro Government Bond PEA",    "index": "Euro Govt Bonds","ter": 0.17, "geo": "bonds",  "aum_b": 0.4},
}

MARKET_INDICES: dict[str, str] = {
    "^GSPC":     "S&P 500",
    "^IXIC":     "NASDAQ",
    "^FCHI":     "CAC 40",
    "^GDAXI":    "DAX",
    "^STOXX50E": "Euro Stoxx 50",
    "^VIX":      "VIX",
    "EURUSD=X":  "EUR/USD",
    "GC=F":      "Or",
    "^TNX":      "US 10Y (taux)",
    "CL=F":      "Pétrole WTI",
}


# ─── Scoring technique ETF ────────────────────────────────────────────────────

def _score_etf(closes: list[float], data: dict) -> tuple[float, list[str]]:
    """
    Score 0-100 basé sur : RSI, tendance SMA, momentum, TER, AUM.
    Retourne (score, raisons[]).
    """
    import math

    reasons: list[str] = []
    score = 50.0  # neutre

    if len(closes) >= 20:
        try:
            # RSI(14)
            delta = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gains = [max(d, 0) for d in delta]
            losses = [max(-d, 0) for d in delta]
            avg_g = sum(gains[-14:]) / 14
            avg_l = sum(losses[-14:]) / 14
            rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else (100 if avg_g > 0 else 50)

            if rsi < 30:
                score += 18; reasons.append(f"RSI survendu ({rsi:.0f}) — signal d'achat fort")
            elif rsi < 42:
                score += 8;  reasons.append(f"RSI faible ({rsi:.0f}) — zone d'accumulation")
            elif rsi > 72:
                score -= 18; reasons.append(f"RSI suracheté ({rsi:.0f}) — attendre correction")
            elif rsi > 60:
                score -= 8;  reasons.append(f"RSI élevé ({rsi:.0f}) — prudence")
            else:
                reasons.append(f"RSI neutre ({rsi:.0f})")
        except Exception:
            pass

        try:
            # SMA20 vs prix
            sma20 = sum(closes[-20:]) / 20
            price = closes[-1]
            if price > sma20 * 1.02:
                score += 10; reasons.append(f"Tendance haussière CT (prix > SMA20 +2%)")
            elif price > sma20:
                score += 5;  reasons.append(f"Au-dessus SMA20")
            elif price < sma20 * 0.98:
                score -= 10; reasons.append(f"Tendance baissière CT (prix < SMA20 -2%)")
            else:
                score -= 5;  reasons.append(f"Sous la SMA20")
        except Exception:
            pass

    # Momentum 1m
    p1m = data.get("perf_1m")
    if p1m is not None:
        if p1m > 8:
            score += 8;  reasons.append(f"Momentum 1m fort ({p1m:+.1f}%)")
        elif p1m > 3:
            score += 4;  reasons.append(f"Momentum 1m positif ({p1m:+.1f}%)")
        elif p1m < -10:
            score -= 12; reasons.append(f"Correction récente importante ({p1m:+.1f}%)")
        elif p1m < -5:
            score -= 6;  reasons.append(f"Correction récente ({p1m:+.1f}%)")

    # TER (coût annuel) — favorise les ETFs peu chers
    ter = data.get("ter", 0.3)
    if ter <= 0.15:
        score += 6;  reasons.append(f"TER excellent ({ter*100:.2f}%/an)")
    elif ter <= 0.25:
        score += 3;  reasons.append(f"TER compétitif ({ter*100:.2f}%/an)")
    elif ter >= 0.40:
        score -= 4;  reasons.append(f"TER élevé ({ter*100:.2f}%/an)")

    # AUM (liquidité) — favorise les gros fonds
    aum = data.get("aum_b", 0)
    if aum >= 2:
        score += 4;  reasons.append(f"Large fonds ({aum:.1f}B€) — forte liquidité")
    elif aum >= 0.5:
        score += 2
    elif aum < 0.2:
        score -= 3;  reasons.append(f"Fonds petit ({aum:.1f}B€) — liquidité réduite")

    return round(max(0.0, min(100.0, score)), 1), reasons


# ─── Génération rapport markdown final ───────────────────────────────────────

def _build_final_prompt(
    question: str,
    intent: dict,
    market_data: dict,
    etf_ranked: list[tuple[str, float, dict, list[str]]],
    vix: float,
    eurusd: float,
    web_snippets: str,
) -> str:
    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    budget   = intent.get("budget")
    acct     = intent.get("account_type", "PEA")
    risk     = intent.get("risk_profile", "équilibré")
    horizon  = intent.get("time_horizon", "long")

    # Macro context
    macro_lines = []
    for sym, d in market_data.items():
        label = MARKET_INDICES.get(sym, sym)
        icon  = "🟢" if d.get("chg", 0) >= 0 else "🔴"
        p1m   = f" | 1m: {d['perf1m']:+.1f}%" if d.get("perf1m") is not None else ""
        p3m   = f" | 3m: {d['perf3m']:+.1f}%" if d.get("perf3m") is not None else ""
        macro_lines.append(f"- {label}: {icon} J: {d.get('chg',0):+.2f}%{p1m}{p3m}")

    # ETF data context
    etf_ctx_lines = []
    for ticker, score, data, reasons in etf_ranked[:10]:
        p1m = f"{data.get('perf_1m'):+.1f}%" if data.get("perf_1m") is not None else "N/D"
        p3m = f"{data.get('perf_3m'):+.1f}%" if data.get("perf_3m") is not None else "N/D"
        p1y = f"{data.get('perf_1y'):+.1f}%" if data.get("perf_1y") is not None else "N/D"
        etf_ctx_lines.append(
            f"  {ticker} | {data.get('name','')[:35]} | TER:{data.get('ter',0)*100:.2f}% "
            f"| Prix:{data.get('price',0):.2f}€ | 1m:{p1m} | 3m:{p3m} | 1y:{p1y} "
            f"| AUM:{data.get('aum_b',0):.1f}B€ | SCORE:{score}/100"
        )
        for r in reasons[:3]:
            etf_ctx_lines.append(f"    → {r}")

    # Allocation calculation
    alloc_text = ""
    if budget:
        top3 = etf_ranked[:3]
        if budget < 300:
            weights = [(top3[0][0], 1.0)]
        elif budget < 800:
            weights = [(top3[0][0], 0.65), (top3[1][0], 0.35)] if len(top3) >= 2 else [(top3[0][0], 1.0)]
        else:
            weights = [(top3[0][0], 0.50), (top3[1][0], 0.30), (top3[2][0], 0.20)] if len(top3) >= 3 else [(top3[0][0], 0.65), (top3[1][0], 0.35)]

        alloc_lines = []
        for ticker, w in weights:
            d = next((d for t, _, d, _ in etf_ranked if t == ticker), {})
            amount = round(budget * w)
            price  = d.get("price", 0)
            units  = amount / price if price > 0 else 0
            full_units = int(units)
            cost   = full_units * price
            alloc_lines.append(
                f"  {ticker}: {amount}€ ({w*100:.0f}%) "
                f"→ {full_units} parts × {price:.2f}€ = {cost:.2f}€ réels"
            )
        alloc_text = f"\nALLOCATION CALCULÉE pour {budget}€:\n" + "\n".join(alloc_lines)

    vix_regime = (
        "⚠️ COMPLACENCY (VIX<15) — marché euphorique, risque correction élevé"
        if vix < 15 else (
            "🟢 FEAR (VIX>30) — panique = opportunité historique d'achat"
            if vix > 30 else (
                "🟠 ATTENTION (VIX 20-30) — nervosité, rester prudent"
                if vix > 20 else "✅ NORMAL (VIX 15-20) — marché sain"
            )
        )
    )

    eur_comment = (
        "EUR faible vs USD → ETFs US coûtent plus cher en €, attention au change"
        if eurusd < 1.05 else (
            "EUR fort vs USD → favorable pour acheter des ETFs US en €"
            if eurusd > 1.12 else "EUR/USD stable"
        )
    )

    return f"""
Tu es un gérant de portefeuille senior (20 ans d'expérience, CFA).
Date d'analyse: {date_str}

QUESTION CLIENT: "{question}"
Budget: {budget}€ | Compte: {acct} | Profil: {risk} | Horizon: {horizon}

═══════════════════════════════
DONNÉES MARCHÉ RÉELLES ({date_str}):
{chr(10).join(macro_lines)}

VIX = {vix:.1f} → {vix_regime}
EUR/USD = {eurusd:.4f} → {eur_comment}
═══════════════════════════════

TOP 10 ETFs PEA (classés par score technique):
{chr(10).join(etf_ctx_lines)}
{alloc_text}

ACTUALITÉS ET CONTEXTE WEB:
{web_snippets[:2000] if web_snippets else "Non disponible"}
═══════════════════════════════

MISSION: Rédige un RAPPORT D'INVESTISSEMENT PROFESSIONNEL en français.
Utilise UNIQUEMENT les données réelles fournies ci-dessus.

Structure EXACTE à suivre (sois précis, concret, chiffré):

## 🌍 Contexte de Marché
[2-3 constats chiffrés clés sur la situation actuelle: tendance indices, VIX, taux, EUR/USD]

## ⚡ VERDICT
**[ACHETER MAINTENANT / DCA PROGRESSIF / ATTENDRE REPLI / SOUS-PONDÉRER]**
[1-2 phrases justificatives basées sur les données]

## 🏆 Allocation Recommandée
[Tableau avec: ETF | Montant exact | Nb parts | Prix/part | Justification 1 phrase]
[Si plusieurs ETFs: explique la logique de diversification]

## 📊 Analyse par ETF
[Pour chaque ETF recommandé: pourquoi cet index, la perf historique réelle, le TER, le signal technique actuel]

## ⏰ Stratégie d'Entrée
[Lump sum ou DCA? Si DCA: combien par mois, pendant combien de temps?]
[Prix d'alerte: à quel niveau renforcer?]
[Prix stop-loss psychologique si applicable]

## ⚠️ Risques Concrets
[3 risques spécifiques avec leur probabilité et impact, basés sur les données actuelles]

## ✅ Plan d'Action (étapes numérotées pour aujourd'hui)
1. [Action concrète avec montant et ticker exact]
2. ...

---
*INTERDIT: inventer des chiffres non fournis | recommander un professionnel | être vague*
*Sois direct, précis, actionnable. Max 700 mots.*
"""


# ─── Agent principal (streaming generator) ────────────────────────────────────

def deep_finance_research(question: str) -> Generator[str, None, None]:
    """
    Analyse financière professionnelle multi-sources.
    Génère du markdown cumulatif (chaque yield = texte complet jusqu'ici).
    """
    from plugins.builtin.finance import _fetch_ticker_http
    from llm.client import chat as llm_chat

    t0      = time.time()
    lines   : list[str] = []
    progress: list[str] = []

    def flush(text: str | None = None) -> str:
        if text:
            lines.append(text)
        return "\n".join(lines)

    # ── Header ────────────────────────────────────────────────────────────────
    yield flush(
        f"## 🔬 Analyse Financière Professionnelle\n"
        f"*Démarré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}*\n"
    )
    yield flush("\n---\n### ⏳ Analyse en cours — ne pas fermer cette page\n")

    # ── Phase 1 : Intent ──────────────────────────────────────────────────────
    yield flush("\n**[1/6]** Interprétation de la question...")

    intent: dict = {}
    try:
        intent_prompt = (
            f'Question: "{question}"\n\n'
            "Extrais en JSON (seulement les champs connus, null sinon):\n"
            '{"budget": <nombre|null>, "account_type": "PEA"|"CTO"|null, '
            '"risk_profile": "conservateur"|"équilibré"|"dynamique"|"agressif"|null, '
            '"time_horizon": "court"|"moyen"|"long"|null, '
            '"preferences": ["nasdaq"|"monde"|"europe"|"usa"|"tech"|"dividende"|"smallcap"|"emerging"],'
            '"specific_tickers": [...]}\n'
            "Réponds UNIQUEMENT avec du JSON valide."
        )
        raw = llm_chat([{"role": "user", "content": intent_prompt}], temperature=0.0)
        m   = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            intent = json.loads(m.group())
    except Exception:
        pass

    budget   = intent.get("budget")
    acct     = intent.get("account_type") or "PEA"
    risk     = intent.get("risk_profile") or "équilibré"
    horizon  = intent.get("time_horizon") or "long terme"
    prefs    = [p.lower() for p in (intent.get("preferences") or [])]
    spec_tks = intent.get("specific_tickers") or []

    yield flush(
        f"\n  ✅ Budget: **{budget}€** | Compte: **{acct}** | "
        f"Profil: **{risk}** | Horizon: **{horizon}**"
    )

    # ── Phase 2 : Macro data (parallèle) ──────────────────────────────────────
    yield flush("\n\n**[2/6]** Données macro (indices, VIX, change, taux)...")

    market_data: dict[str, dict] = {}
    vix    = 20.0
    eurusd = 1.09

    def _fetch_index(sym: str):
        d1m = _fetch_ticker_http(sym, "1mo")
        d3m = _fetch_ticker_http(sym, "3mo")
        if d1m:
            return sym, {
                "label":  MARKET_INDICES.get(sym, sym),
                "price":  d1m["price"],
                "chg":    d1m["chg"],
                "perf1m": d1m.get("perf"),
                "perf3m": d3m.get("perf") if d3m else None,
            }
        return sym, {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_index, sym): sym for sym in MARKET_INDICES}
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

    yield flush(f"\n  ✅ {len(market_data)}/{len(MARKET_INDICES)} marchés récupérés")

    # ── Phase 3 : Scan ETF PEA (parallèle) ────────────────────────────────────
    # Filtre l'univers selon les préférences détectées
    if "nasdaq" in prefs or "tech" in prefs:
        priority_geos = {"tech", "monde", "usa"}
    elif "europe" in prefs:
        priority_geos = {"europe", "monde"}
    elif "emerging" in prefs:
        priority_geos = {"emerging", "monde"}
    else:
        priority_geos = set()  # scan tout

    target_etfs = (
        {k: v for k, v in PEA_ETF_UNIVERSE.items() if v["geo"] in priority_geos}
        if priority_geos else PEA_ETF_UNIVERSE
    )
    # Toujours inclure les spécifiques mentionnés
    for tk in spec_tks:
        if tk.upper() not in target_etfs:
            target_etfs[tk.upper()] = {"name": tk, "index": "?", "ter": 0.30, "geo": "autre", "aum_b": 0}

    yield flush(f"\n\n**[3/6]** Scan de {len(target_etfs)} ETFs PEA...")

    def _fetch_etf(ticker: str):
        d1m = _fetch_ticker_http(ticker, "1mo")
        d3m = _fetch_ticker_http(ticker, "3mo")
        d1y = _fetch_ticker_http(ticker, "1y")
        return ticker, d1m, d3m, d1y

    etf_raw: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_etf, tk): tk for tk in target_etfs}
        done = 0
        for f in as_completed(futs):
            try:
                tk, d1m, d3m, d1y = f.result()
                meta = target_etfs[tk]
                if d3m:
                    etf_raw[tk] = {
                        **meta,
                        "price":    d3m["price"],
                        "chg_24h":  d3m["chg"],
                        "perf_1m":  d1m.get("perf") if d1m else None,
                        "perf_3m":  d3m.get("perf"),
                        "perf_1y":  d1y.get("perf") if d1y else None,
                        "closes":   d3m.get("closes", []),
                    }
                    done += 1
            except Exception:
                pass

    yield flush(f"\n  ✅ {done}/{len(target_etfs)} ETFs avec données")

    # ── Phase 4 : Scoring technique ───────────────────────────────────────────
    yield flush("\n\n**[4/6]** Scoring technique + fondamental...")

    scored: list[tuple[str, float, dict, list[str]]] = []
    for ticker, data in etf_raw.items():
        score, reasons = _score_etf(data.get("closes", []), data)
        scored.append((ticker, score, data, reasons))
    scored.sort(key=lambda x: x[1], reverse=True)

    top_names = ", ".join(f"**{t}** ({s:.0f}/100)" for t, s, _, _ in scored[:5])
    yield flush(f"\n  ✅ Classement: {top_names}")

    # ── Phase 5 : Recherche web ────────────────────────────────────────────────
    yield flush("\n\n**[5/6]** Recherche web — contexte marché...")

    web_snippets = ""
    try:
        from duckduckgo_search import DDGS
        queries = []

        if acct == "PEA":
            queries.append(f"meilleur ETF PEA {datetime.now().year} performance analyse")
        if "nasdaq" in prefs or "tech" in prefs:
            queries.append(f"ETF Nasdaq PEA acheter {datetime.now().strftime('%B %Y')}")
        if spec_tks:
            queries.append(f"{' '.join(spec_tks[:2])} analyse technique achat")
        queries.append(f"marchés financiers perspective {datetime.now().strftime('%B %Y')}")

        snippets: list[str] = []
        with DDGS() as ddgs:
            for q in queries[:4]:
                try:
                    for r in ddgs.text(q, max_results=3, region="fr-fr"):
                        t = str(r.get("title", ""))
                        b = str(r.get("body", ""))[:350]
                        if t:
                            snippets.append(f"• {t}\n  {b}")
                    time.sleep(0.3)  # courtesy delay
                except Exception:
                    pass

        web_snippets = "\n\n".join(snippets[:8])
        yield flush(f"\n  ✅ {len(snippets)} extraits web récupérés")
    except Exception as e:
        yield flush(f"\n  ⚠️ Recherche web indisponible ({e})")

    # ── Phase 6 : Synthèse LLM ────────────────────────────────────────────────
    yield flush("\n\n**[6/6]** Synthèse par le modèle LLM...")

    prompt = _build_final_prompt(
        question=question,
        intent=intent,
        market_data=market_data,
        etf_ranked=scored,
        vix=vix,
        eurusd=eurusd,
        web_snippets=web_snippets,
    )

    try:
        report = llm_chat(
            [
                {"role": "system", "content": (
                    "Tu es un gérant de portefeuille senior (CFA, 20 ans d'expérience). "
                    "Tu analyses des données réelles et donnes des conseils précis, chiffrés, actionnables. "
                    "Tu n'inventes aucun chiffre. Tu ne recommandes jamais de consulter un conseiller financier. "
                    "Tu assumes tes recommandations avec des données factuelles."
                )},
                {"role": "user", "content": prompt},
            ],
            temperature=0.25,
        )
    except Exception as e:
        report = f"❌ Erreur LLM: {e}"

    elapsed = round(time.time() - t0)
    mins    = elapsed // 60
    secs    = elapsed % 60

    # Replace progress section with final report
    final_lines = [
        f"## 🔬 Rapport d'Investissement Professionnel",
        f"*{datetime.now().strftime('%d/%m/%Y %H:%M')} — analyse en {mins}m{secs:02d}s — "
        f"{len(etf_raw)} ETFs scannés — {len(market_data)} marchés analysés*\n",
        "---\n",
        report,
        "\n---",
        f"*Données temps réel Yahoo Finance • {len(web_snippets.splitlines())} sources web • "
        f"Score technique basé sur RSI, SMA20, momentum, TER, AUM*",
    ]
    yield "\n".join(final_lines)
