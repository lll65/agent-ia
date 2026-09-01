"""
Plugin d'analyse financière — données réelles via yfinance.
RSI, MACD, Bollinger, ATR — recommandation achat/vente/hold.
Dashboard marchés, portefeuille P&L, comparaison multi-actifs.
"""
from plugins.base import Plugin


# ─── HTTP fallback (fonctionne sans yfinance) ─────────────────────────────────

def _fetch_ticker_http(ticker: str, period: str = "1mo") -> dict:
    """Récupère prix + variation + série historique via l'API Yahoo Finance directement."""
    try:
        import requests
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&range={period}"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return {}
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return {}
        rr = result[0]
        meta       = rr.get("meta") or {}
        ts_raw     = rr.get("timestamp") or []
        closes_raw = (rr.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
        # Pair timestamps + closes, filter None values
        if ts_raw and len(ts_raw) == len(closes_raw):
            pairs = [(t, c) for t, c in zip(ts_raw, closes_raw) if c is not None]
        else:
            pairs = [(i, c) for i, c in enumerate(closes_raw) if c is not None]
        if not pairs:
            return {}
        timestamps = [p[0] for p in pairs]
        closes     = [p[1] for p in pairs]
        price = closes[-1]
        prev  = closes[-2] if len(closes) > 1 else price
        chg   = (price - prev) / prev * 100 if prev else 0
        # Sanity check : performance max 150% sur 1 mois, 300% sur 1 an
        _perf_lim = 150 if period in ("1mo", "3mo") else 400
        c1m = (price / closes[0] - 1) * 100 if closes[0] else 0
        if abs(c1m) > _perf_lim:
            c1m = None  # anomalie de données
        return {
            "price":      price,
            "chg":        chg,
            "currency":   meta.get("currency", ""),
            "name":       meta.get("longName") or meta.get("shortName") or ticker,
            "h":          max(closes),
            "l":          min(closes),
            "perf":       c1m,
            "n":          len(closes),
            "closes":     closes,
            "timestamps": timestamps,
        }
    except Exception:
        return {}



def _rsi_liste(closes, period=14):
    """RSI en Python pur, sur une simple liste de cloturees.

    ⚠️ Quand yfinance echoue — Yahoo repond 401 aux adresses de centres de donnees
    comme Render, c'est TRES courant — le repli HTTP rendait quand meme les
    cotations, mais on ecrivait « RSI 50, volatilite 0.00 %/j, Sharpe 0.00 » en dur.
    Des chiffres INVENTES, presentes exactement comme des vrais, sur les
    investissements de Lohan. Un titre qui perdait 2 EUR par jour s'affichait
    « ✅ Neutre ». On calcule donc a partir de ce qu'on a, et on dit « N/D » quand
    c'est trop court — jamais une valeur plausible sortie de nulle part.
    """
    if not closes or len(closes) < period + 1:
        return None
    gains, pertes = [], []
    for a, b in zip(closes[-(period + 1):], closes[-period:]):
        d = b - a
        gains.append(max(0.0, d))
        pertes.append(max(0.0, -d))
    g = sum(gains) / period
    pe = sum(pertes) / period
    if pe == 0:
        return 100.0 if g > 0 else None
    return round(100 - 100 / (1 + g / pe), 1)


def _volatilite_liste(closes):
    """Ecart-type des variations quotidiennes, en %. None si indeterminable."""
    if not closes or len(closes) < 3:
        return None
    var = [(b - a) / a for a, b in zip(closes, closes[1:]) if a]
    if len(var) < 2:
        return None
    moy = sum(var) / len(var)
    ecart = (sum((v - moy) ** 2 for v in var) / (len(var) - 1)) ** 0.5
    return round(ecart * 100, 2)


def _rsi(close, period=14):
    import pandas as pd
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    l, g = float(loss.iloc[-1]), float(gain.iloc[-1])
    if l == 0:
        return 100.0 if g > 0 else float("nan")
    return float(100 - 100 / (1 + g / l))


def _atr(hist, period=14):
    h, lo, c = hist["High"], hist["Low"], hist["Close"]
    tr = (h - lo).combine((h - c.shift()).abs(), max).combine((lo - c.shift()).abs(), max)
    return float(tr.rolling(period).mean().iloc[-1])


def _pct_bar(pct, length=22):
    """0-100 → bloc ASCII."""
    import math
    pct = max(0.0, min(100.0, float(pct) if not math.isnan(float(pct)) else 50.0))
    filled = int(pct / 100 * length)
    return "█" * filled + "░" * (length - filled)


# ─── Génération de graphiques ─────────────────────────────────────────────────

def generate_stock_chart(ticker: str, period: str = "6mo") -> str | None:
    """Génère un graphique prix + indicateurs. Retourne le chemin de l'image ou None."""
    try:
        import yfinance as yf
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        from pathlib import Path

        hist = yf.Ticker(ticker.upper()).history(period=period)
        if hist.empty:
            return None

        close = hist["Close"]
        vol   = hist["Volume"]
        dates = close.index

        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean() if len(close) >= 50 else None
        std20 = close.rolling(20).std()
        bb_up = sma20 + 2 * std20
        bb_lo = sma20 - 2 * std20

        BG, GRID, BORDER = "#0d1117", "#21262d", "#30363d"
        TCLR = "#8b949e"
        C_PRICE, C_SMA20, C_SMA50, C_BB = "#58a6ff", "#f0883e", "#bc8cff", "#388bfd"
        C_UP, C_DN = "#3fb950", "#f85149"

        fig = plt.figure(figsize=(13, 7), facecolor=BG)
        gs  = gridspec.GridSpec(2, 1, height_ratios=[3.5, 1], hspace=0.03)
        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax1)

        for ax in (ax1, ax2):
            ax.set_facecolor(BG)
            ax.tick_params(colors=TCLR, labelsize=8)
            for sp in ax.spines.values():
                sp.set_color(BORDER)
            ax.grid(color=GRID, linewidth=0.5, alpha=0.7)

        # Bollinger
        ax1.fill_between(dates, bb_lo, bb_up, alpha=0.07, color=C_BB)
        ax1.plot(dates, bb_up, color=C_BB, linewidth=0.6, alpha=0.5, linestyle="--", label="Bollinger")
        ax1.plot(dates, bb_lo, color=C_BB, linewidth=0.6, alpha=0.5, linestyle="--")

        # SMAs
        ax1.plot(dates, sma20, color=C_SMA20, linewidth=1.1, alpha=0.85, label="SMA20")
        if sma50 is not None:
            ax1.plot(dates, sma50, color=C_SMA50, linewidth=1.1, alpha=0.85, label="SMA50")

        # Prix
        ax1.plot(dates, close, color=C_PRICE, linewidth=1.5, label="Prix", zorder=4)

        # Annotation prix courant
        last_p = float(close.iloc[-1])
        ax1.axhline(y=last_p, color=C_UP, linewidth=0.8, linestyle=":", alpha=0.8)
        ax1.annotate(f" {last_p:.2f}", xy=(dates[-1], last_p),
                     color=C_UP, fontsize=8, va="center", ha="left")

        ax1.set_title(f"{ticker.upper()} — Analyse Technique ({period})",
                      color="#e6edf3", fontsize=12, pad=8, loc="left")
        ax1.legend(loc="upper left", facecolor="#161b22", edgecolor=BORDER,
                   labelcolor=TCLR, fontsize=8, framealpha=0.9)
        plt.setp(ax1.xaxis.get_ticklabels(), visible=False)

        # Volume
        vol_colors = [C_UP if float(close.iloc[i]) >= float(close.iloc[i - 1]) else C_DN
                      for i in range(len(close))]
        ax2.bar(dates, vol, color=vol_colors, alpha=0.7, width=0.6)
        ax2.plot(dates, vol.rolling(20).mean(), color=TCLR, linewidth=0.8,
                 linestyle="--", alpha=0.6, label="Vol moy.")
        ax2.set_ylabel("Volume", color=TCLR, fontsize=7)
        ax2.tick_params(axis="x", rotation=25, labelsize=7)

        plt.tight_layout(pad=1.5)
        Path("output/charts").mkdir(parents=True, exist_ok=True)
        path = f"output/charts/{ticker.lower()}_{period}.png"
        fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        return path
    except Exception:
        return None


def generate_compare_chart(tickers: list, period: str = "3mo") -> str | None:
    """Génère un graphique de performance normalisée (base 100)."""
    try:
        import yfinance as yf
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from pathlib import Path

        BG, GRID, BORDER = "#0d1117", "#21262d", "#30363d"
        TCLR = "#8b949e"
        PALETTE = ["#58a6ff", "#3fb950", "#bc8cff", "#f0883e", "#ff7b72", "#ffa657", "#79c0ff", "#d2a8ff"]

        fig, ax = plt.subplots(figsize=(12, 6), facecolor=BG)
        ax.set_facecolor(BG)
        ax.tick_params(colors=TCLR, labelsize=9)
        for sp in ax.spines.values():
            sp.set_color(BORDER)
        ax.grid(color=GRID, linewidth=0.5, alpha=0.7)

        for i, sym in enumerate(tickers[:8]):
            try:
                h = yf.Ticker(sym).history(period=period)["Close"]
                if h.empty:
                    continue
                norm = (h / float(h.iloc[0]) - 1) * 100
                ax.plot(h.index, norm, label=sym, color=PALETTE[i % len(PALETTE)], linewidth=1.5)
            except Exception:
                pass

        ax.axhline(y=0, color=TCLR, linewidth=0.8, linestyle="--", alpha=0.5)
        ax.set_title(f"Performance comparée — {period.upper()} (base 0%)",
                     color="#e6edf3", fontsize=12, pad=8, loc="left")
        ax.set_ylabel("Performance (%)", color=TCLR, fontsize=9)
        ax.legend(loc="upper left", facecolor="#161b22", edgecolor=BORDER,
                  labelcolor=TCLR, fontsize=9, framealpha=0.9)
        ax.tick_params(axis="x", rotation=20, labelsize=8)

        plt.tight_layout(pad=1.5)
        Path("output/charts").mkdir(parents=True, exist_ok=True)
        safe = "_".join(tickers)[:50]
        path = f"output/charts/compare_{safe}.png"
        fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        return path
    except Exception:
        return None


def _generate_portfolio_chart(
    rows: list, total_value: float, hist_data: dict | None = None
) -> str | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        from pathlib import Path
        from datetime import datetime

        BG, BORDER = "#0d1117", "#30363d"
        TEXT  = "#e6edf3"
        TCLR  = "#8b949e"
        GRID  = "#21262d"
        PALETTE = ["#58a6ff", "#3fb950", "#bc8cff", "#f0883e", "#ff7b72",
                   "#ffa657", "#79c0ff", "#56d364", "#d2a8ff", "#ffb3b3"]

        with_pnl    = [r for r in rows if r.get("pnl_pct") is not None]
        has_history = bool(
            hist_data and any(len(v.get("closes", [])) >= 5 for v in hist_data.values())
        )

        if has_history:
            fig = plt.figure(figsize=(14, 11), facecolor=BG)
            gs  = gridspec.GridSpec(2, 2, height_ratios=[1.4, 1], hspace=0.45, wspace=0.35)
            ax_pie  = fig.add_subplot(gs[0, 0])
            ax_bar  = fig.add_subplot(gs[0, 1]) if with_pnl else None
            ax_line = fig.add_subplot(gs[1, :])
        else:
            n = 2 if with_pnl else 1
            fig, axes = plt.subplots(1, n, figsize=(13 if n == 2 else 7, 6), facecolor=BG)
            if n == 1:
                axes = [axes]
            ax_pie  = axes[0]
            ax_bar  = axes[1] if n == 2 else None
            ax_line = None

        # ── Pie (allocation) ────────────────────────────────────────────────────
        ax_pie.set_facecolor(BG)
        labels = [r["ticker"] for r in rows]
        sizes  = [r["cur_value"] / total_value * 100 for r in rows]
        colors = PALETTE[: len(labels)]

        _, _, pcts = ax_pie.pie(
            sizes, labels=labels, colors=colors, autopct="%1.1f%%",
            startangle=90, textprops={"color": TEXT, "fontsize": 9},
            wedgeprops={"edgecolor": BG, "linewidth": 2}, pctdistance=0.75,
        )
        for pt in pcts:
            pt.set_color(TEXT); pt.set_fontsize(8)
        ax_pie.set_title("Allocation", color=TEXT, fontsize=12, pad=10)

        # ── Bar (P&L%) ──────────────────────────────────────────────────────────
        if with_pnl and ax_bar is not None:
            ax_bar.set_facecolor(BG)
            ax_bar.tick_params(colors=TCLR, labelsize=9)
            for sp in ax_bar.spines.values():
                sp.set_color(BORDER)
            ax_bar.grid(axis="y", color=GRID, linewidth=0.5, alpha=0.6)
            ax_bar.axhline(y=0, color=TCLR, linewidth=0.8)

            tks     = [r["ticker"] for r in with_pnl]
            vals    = [r["pnl_pct"] for r in with_pnl]
            bcolors = ["#3fb950" if v >= 0 else "#f85149" for v in vals]

            bars = ax_bar.bar(tks, vals, color=bcolors, edgecolor=BG, linewidth=1.5, width=0.6)
            ax_bar.set_title("Performance par Position (%)", color=TEXT, fontsize=12, pad=10)

            for bar, val in zip(bars, vals):
                off = max(abs(val) * 0.03, 0.3) * (1 if val >= 0 else -1)
                ax_bar.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + off,
                    f"{val:+.1f}%", ha="center",
                    va="bottom" if val >= 0 else "top",
                    color=TEXT, fontsize=8,
                )

        # ── Line (évolution 1 mois) ─────────────────────────────────────────────
        if has_history and ax_line is not None:
            ax_line.set_facecolor(BG)
            ax_line.tick_params(colors=TCLR, labelsize=8)
            for sp in ax_line.spines.values():
                sp.set_color(BORDER)
            ax_line.grid(color=GRID, linewidth=0.5, alpha=0.7)

            entries  = [(tk, v) for tk, v in hist_data.items() if len(v.get("closes", [])) >= 5]
            min_len  = min(len(v["closes"]) for _, v in entries)
            timeline = [
                sum(v["closes"][-(min_len - i)] * v["qty"] for _, v in entries)
                for i in range(min_len)
            ]

            # Try to build real dates from timestamps
            sample_ts = entries[0][1].get("timestamps", [])
            if sample_ts and len(sample_ts) >= min_len:
                x_axis = [datetime.fromtimestamp(ts) for ts in sample_ts[-min_len:]]
                ax_line.plot(x_axis, timeline, color="#58a6ff", linewidth=2, zorder=4)
                ax_line.fill_between(x_axis, timeline, alpha=0.12, color="#58a6ff")
                ax_line.tick_params(axis="x", rotation=20, labelsize=7)
            else:
                x_axis = list(range(min_len))
                ax_line.plot(x_axis, timeline, color="#58a6ff", linewidth=2, zorder=4)
                ax_line.fill_between(x_axis, timeline, alpha=0.12, color="#58a6ff")

            ref       = timeline[0]
            last_v    = timeline[-1]
            chg_t_pct = (last_v / ref - 1) * 100 if ref else 0
            icon      = "▲" if chg_t_pct >= 0 else "▼"
            col_t     = "#3fb950" if chg_t_pct >= 0 else "#f85149"
            ax_line.axhline(y=ref, color=TCLR, linewidth=0.8, linestyle="--", alpha=0.5)
            ax_line.set_title(
                f"Évolution du portefeuille — 1 mois    {icon} {chg_t_pct:+.2f}%",
                color=TEXT, fontsize=12, pad=8, loc="left",
            )
            # color title pct part
            ax_line.set_ylabel("Valeur (€)", color=TCLR, fontsize=9)

        plt.tight_layout(pad=2)
        Path("output/charts").mkdir(parents=True, exist_ok=True)
        path = "output/charts/portfolio.png"
        fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        return path
    except Exception:
        return None


# ─── Résolution noms communs → tickers ───────────────────────────────────────

_NAME_TO_TICKER: dict[str, str] = {
    # Actions françaises
    "valneva": "VLA.PA", "soitec": "SOI.PA", "lvmh": "MC.PA",
    "airbus": "AIR.PA", "bnp": "BNP.PA", "sanofi": "SAN.PA",
    "total": "TTE.PA", "totalenergies": "TTE.PA",
    "kering": "KER.PA", "hermes": "RMS.PA", "hermès": "RMS.PA",
    "loreal": "OR.PA", "l'oreal": "OR.PA", "l'oréal": "OR.PA",
    "renault": "RNO.PA", "orange": "ORA.PA", "carrefour": "CA.PA",
    "danone": "BN.PA", "schneider": "SU.PA", "thales": "HO.PA",
    "michelin": "ML.PA", "capgemini": "CAP.PA", "dassault": "DSY.PA",
    "safran": "SAF.PA", "alstom": "ALO.PA", "worldline": "WLN.PA",
    "teleperformance": "TEP.PA", "pernod": "RI.PA", "veolia": "VIE.PA",
    "stellantis": "STLAM.MI", "stmicro": "STMPA.PA", "st": "STMPA.PA",
    "sg": "GLE.PA", "societegenerale": "GLE.PA",
    "creditagricole": "ACA.PA",
    # Actions US
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA",
    "amazon": "AMZN", "microsoft": "MSFT", "google": "GOOGL",
    "alphabet": "GOOGL", "meta": "META", "netflix": "NFLX",
    "amd": "AMD", "intel": "INTC", "palantir": "PLTR", "arm": "ARM",
    "qualcomm": "QCOM", "broadcom": "AVGO",
    # ETFs PEA (Euronext Paris) — tickers directs
    "panx": "PANX.PA", "panx.pa": "PANX.PA",
    "wpea": "WPEA.PA", "wpea.pa": "WPEA.PA",
    "pust": "PUST.PA", "pust.pa": "PUST.PA",
    "psp5": "PSP5.PA", "psp5.pa": "PSP5.PA",
    "paeem": "PAEEM.PA",
    "ewld": "EWLD.PA",
    "mwrd": "MWRD.PA",
    "cndx": "CNDX.PA",
    "lcwd": "LCWD.PA",
    "wsri": "WSRI.PA",
    "ep500": "EP500.PA",
    # ETFs US communs
    "spy": "SPY", "qqq": "QQQ", "voo": "VOO", "vti": "VTI",
    "iwda": "IWDA.AS", "vwce": "VWCE.DE", "xwld": "XWLD.PA",
    # Crypto
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    "bnb": "BNB-USD", "xrp": "XRP-USD",
    "cardano": "ADA-USD", "ada": "ADA-USD",
    "avalanche": "AVAX-USD", "avax": "AVAX-USD",
    # Matières
    "or": "GC=F", "gold": "GC=F",
    "petrole": "CL=F", "pétrole": "CL=F", "oil": "CL=F",
    "argent": "SI=F", "silver": "SI=F",
}

# Patterns de mots-clés pour résolution ETFs multi-mots
_ETF_KEYWORDS: list[tuple[frozenset, str]] = [
    (frozenset({"amundi", "nasdaq"}), "PANX.PA"),
    (frozenset({"amundi", "world"}), "WPEA.PA"),
    (frozenset({"amundi", "msci", "world"}), "WPEA.PA"),
    (frozenset({"amundi", "sp500"}), "PSP5.PA"),
    (frozenset({"amundi", "s&p"}), "PSP5.PA"),
    (frozenset({"amundi", "emerging"}), "PAEEM.PA"),
    (frozenset({"amundi", "stoxx"}), "MEH.PA"),
    (frozenset({"lyxor", "nasdaq"}), "PUST.PA"),
    (frozenset({"lyxor", "world"}), "EWLD.PA"),
    (frozenset({"lyxor", "sp500"}), "PSP5.PA"),
    (frozenset({"lyxor", "s&p"}), "PSP5.PA"),
    (frozenset({"ishares", "nasdaq"}), "CNDX.PA"),
    (frozenset({"ishares", "core", "world"}), "IWDA.AS"),
    (frozenset({"ishares", "world"}), "IWDA.AS"),
    (frozenset({"xtrackers", "world"}), "XWLD.PA"),
    (frozenset({"vanguard", "world"}), "VWCE.DE"),
    (frozenset({"vanguard", "sp500"}), "VOO"),
    (frozenset({"blackrock", "world"}), "IWDA.AS"),
    (frozenset({"spdr", "sp500"}), "SPY"),
    (frozenset({"invesco", "nasdaq"}), "QQQ"),
]


def _resolve_ticker(raw: str) -> str:
    """Convertit un nom commun ou multi-mots en ticker Yahoo Finance."""
    # 1. Exact match (clé normalisée sans espaces/tirets)
    key = raw.lower().strip().replace("-", "").replace(" ", "").replace("'", "")
    result = _NAME_TO_TICKER.get(key) or _NAME_TO_TICKER.get(raw.lower().strip())
    if result:
        return result

    # 2. Keyword matching pour noms multi-mots (ETFs, fonds, etc.)
    if " " in raw.strip() or len(raw) > 12:
        words = set(raw.lower().replace("-", " ").replace("/", " ").replace("'", " ").split())
        for kw_set, ticker in _ETF_KEYWORDS:
            if kw_set.issubset(words):
                return ticker

    # 3. Mot unique ou non reconnu → majuscules (ticker direct)
    return raw.split()[0].upper() if " " in raw.strip() else raw.upper()


# ─── Portfolio public ─────────────────────────────────────────────────────────

def analyze_portfolio(positions_text: str) -> tuple:
    """
    Analyse un portefeuille depuis texte.
    Format: TICKER/NOM QUANTITE [PRIX_ACHAT]  — supporte noms multi-mots et # commentaires
    Retourne (markdown_str, chart_path_or_None).
    """
    try:
        import yfinance as yf
        import pandas as pd
        _yf_ok = True
    except ImportError:
        _yf_ok = False

    raw_lines = [l.strip() for l in positions_text.strip().split("\n") if l.strip()]
    positions: list[dict] = []
    errors: list[str]     = []

    for line in raw_lines:
        # Ignorer les lignes de commentaire pures
        if line.startswith("#"):
            continue
        # Striper les commentaires inline
        if "#" in line:
            line = line[:line.index("#")].strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 2:
            errors.append(f"Format invalide: `{line}`")
            continue

        ticker_raw: str | None = None
        qty:        float | None = None
        buy_price:  float | None = None

        # Stratégie 1: premier token = ticker/nom, second = quantité (nombre)
        try:
            qty_test   = float(parts[1].replace(",", "."))
            ticker_raw = parts[0]
            qty        = qty_test
            if len(parts) > 2:
                try:
                    buy_price = float(parts[2].replace(",", "."))
                except ValueError:
                    buy_price = None
        except ValueError:
            # Stratégie 2 (noms multi-mots ETFs): détecter les N derniers tokens numériques
            # Ex: "Amundi Nasdaq UCITS ETF PEA 19 5.29" → name="Amundi Nasdaq…", qty=19, price=5.29
            name_parts = list(parts)
            num_tail: list[float] = []
            while name_parts:
                try:
                    val = float(name_parts[-1].replace(",", "."))
                    num_tail.insert(0, val)
                    name_parts.pop()
                except ValueError:
                    break
            if len(num_tail) >= 1 and name_parts:
                ticker_raw = " ".join(name_parts)
                qty        = num_tail[0]
                buy_price  = num_tail[1] if len(num_tail) > 1 else None
            else:
                errors.append(f"Erreur de format: `{line}`")
                continue

        if ticker_raw is None or qty is None or qty <= 0:
            errors.append(f"Erreur de format: `{line}`")
            continue

        ticker = _resolve_ticker(ticker_raw)
        # Signaler si le nom a été auto-résolu vers un ticker différent
        resolved_note = (
            f"→ {ticker}" if ticker.upper() != ticker_raw.upper().split()[0] else None
        )
        positions.append({
            "ticker":        ticker,
            "ticker_orig":   ticker_raw,
            "qty":           qty,
            "buy_price":     buy_price,
            "resolved_note": resolved_note,
        })

    if not positions:
        return (
            "❌ Aucune position valide.\n\n"
            "**Format** (une ligne par position) :\n"
            "```\n"
            "AAPL 10 150.00\n"
            "valneva 25 4.10\n"
            "BTC-USD 0.5 42000\n"
            "MC.PA 3 650\n"
            "Amundi Nasdaq UCITS ETF PEA 100 5.29  # ETF multi-mots ok\n"
            "PANX.PA 50 25.00  # ticker direct ETF\n"
            "```\n\n"
            "Tu peux utiliser les noms (apple, tesla, valneva…), "
            "les tickers directs (AAPL, VLA.PA…) ou les noms complets d'ETF.",
            None,
        )

    rows: list[dict]              = []
    total_value: float            = 0.0
    total_cost:  float            = 0.0
    hist_data:   dict[str, dict]  = {}  # ticker → {closes, timestamps, qty}

    for p in positions:
        cur_price: float | None = None
        chg_24h:   float        = 0.0
        name:      str          = p["ticker_orig"][:20]
        currency:  str          = ""

        # Essai 1: yfinance (1mo pour avoir l'historique timeline)
        if _yf_ok:
            try:
                tk   = yf.Ticker(p["ticker"])
                info = tk.info or {}
                hist = tk.history(period="1mo")
                if not hist.empty:
                    close     = hist["Close"].squeeze().dropna()
                    cur_price = float(close.iloc[-1])
                    prev_p    = float(close.iloc[-2]) if len(close) > 1 else cur_price
                    chg_24h   = (cur_price / prev_p - 1) * 100
                    name      = (info.get("shortName") or p["ticker"])[:20]
                    currency  = info.get("currency", "")
                    hist_data[p["ticker"]] = {
                        "closes":     close.tolist(),
                        "timestamps": [],
                        "qty":        p["qty"],
                    }
            except Exception:
                pass

        # Essai 2: HTTP fallback si yfinance absent ou données vides
        if cur_price is None:
            d = _fetch_ticker_http(p["ticker"], "1mo")
            if not d:
                d = _fetch_ticker_http(p["ticker"], "5d")
            if d:
                cur_price = d["price"]
                chg_24h   = d["chg"]
                name      = d["name"][:20]
                currency  = d["currency"]
                if d.get("closes"):
                    hist_data[p["ticker"]] = {
                        "closes":     d["closes"],
                        "timestamps": d.get("timestamps", []),
                        "qty":        p["qty"],
                    }

        if cur_price is None:
            rows.append({**p, "name": name, "error": "Prix introuvable (ticker invalide?)"})
            continue

        cur_value = cur_price * p["qty"]
        cost      = p["buy_price"] * p["qty"] if p["buy_price"] else None
        pnl       = cur_value - cost if cost is not None else None
        pnl_pct   = (cur_price / p["buy_price"] - 1) * 100 if p["buy_price"] else None

        total_value += cur_value
        if cost is not None:
            total_cost += cost

        # Détecte si le prix actuel semble incohérent avec le prix d'achat (possible mauvais ticker)
        price_warning = None
        if p["buy_price"] and cur_price:
            ratio = cur_price / p["buy_price"]
            if ratio > 8 or ratio < 0.12:
                price_warning = (
                    f"⚠️ Prix actuel ({cur_price:.2f}) très éloigné du prix d'achat ({p['buy_price']:.2f}) "
                    f"— vérifie que le ticker **{p['ticker']}** est bien le bon !"
                )

        rows.append({
            "ticker":        p["ticker"],
            "ticker_orig":   p["ticker_orig"],
            "resolved_note": p.get("resolved_note"),
            "name":          name,
            "qty":           p["qty"],
            "buy_price":     p["buy_price"],
            "cur_price":     cur_price,
            "chg_24h":       chg_24h,
            "cur_value":     cur_value,
            "cost":          cost,
            "pnl":           pnl,
            "pnl_pct":       pnl_pct,
            "currency":      currency,
            "price_warning": price_warning,
        })

    valid    = [r for r in rows if "error" not in r]
    with_pnl = [r for r in valid if r["pnl_pct"] is not None]
    out      = ["## 💼 Mon Portefeuille\n"]

    # ── Résumé global ─────────────────────────────────────────────────────────
    if total_value > 0:
        out.append("### 📊 Résumé Global")
        out.append("| | |")
        out.append("|---|---|")
        out.append(f"| **Valeur totale** | **{total_value:,.2f}** |")

        if total_cost > 0:
            total_pnl     = total_value - total_cost
            total_pnl_pct = (total_value / total_cost - 1) * 100
            icon_pnl = "🟢" if total_pnl >= 0 else "🔴"
            out.append(f"| Coût total investi | {total_cost:,.2f} |")
            out.append(
                f"| **P&L Total** | {icon_pnl} "
                f"**{total_pnl:+,.2f}  ({total_pnl_pct:+.2f}%)** |"
            )

        # Gain / perte du jour
        daily_gain     = sum(r["chg_24h"] / 100 * r["cur_value"] for r in valid)
        daily_gain_pct = daily_gain / total_value * 100 if total_value > 0 else 0
        icon_day = "🟢" if daily_gain >= 0 else "🔴"
        out.append(
            f"| Variation du jour | {icon_day} "
            f"{daily_gain:+,.2f}  ({daily_gain_pct:+.2f}%) |"
        )
        out.append(f"| Nombre de lignes | {len(valid)} actif(s) |")
        out.append("")

    # ── Best / Worst performer ─────────────────────────────────────────────────
    if with_pnl:
        best  = max(with_pnl, key=lambda x: x["pnl_pct"])
        worst = min(with_pnl, key=lambda x: x["pnl_pct"])
        out.append("### 🏆 Top / Flop")
        out.append("| | Ticker | P&L% | P&L |")
        out.append("|---|---|---|---|")
        out.append(
            f"| 🏆 Meilleure | **{best['ticker']}** "
            f"| 🟢 {best['pnl_pct']:+.2f}% "
            f"| {best['pnl']:+,.2f} |"
        )
        out.append(
            f"| ⚠️ Moins bonne | **{worst['ticker']}** "
            f"| 🔴 {worst['pnl_pct']:+.2f}% "
            f"| {worst['pnl']:+,.2f} |"
        )
        out.append("")

    # ── Tableau positions ──────────────────────────────────────────────────────
    out.append("### Positions")
    out.append("| Ticker | Nom | Qté | Px achat | Px actuel | 24h | Valeur | P&L | P&L% |")
    out.append("|---|---|---|---|---|---|---|---|---|")

    for r in rows:
        if "error" in r:
            out.append(
                f"| **{r.get('ticker','')}** | ❌ {r['error'][:20]} "
                f"| {r.get('qty','')} | — | — | — | — | — | — |"
            )
            continue

        pnl_str     = f"{'🟢' if r['pnl'] >= 0 else '🔴'} {r['pnl']:+,.2f}" if r["pnl"] is not None else "—"
        pnl_pct_str = f"{r['pnl_pct']:+.2f}%" if r["pnl_pct"] is not None else "—"
        chg_str     = f"{'🟢' if r['chg_24h'] >= 0 else '🔴'} {r['chg_24h']:+.2f}%"
        buy_str     = f"{r['buy_price']:.4f}" if r["buy_price"] else "—"
        price_str   = f"{r['cur_price']:,.2f}" if r["cur_price"] >= 100 else f"{r['cur_price']:.4f}"
        out.append(
            f"| **{r['ticker']}** | {r['name']} | {r['qty']} | {buy_str} "
            f"| **{price_str}** | {chg_str} | {r['cur_value']:,.2f} | {pnl_str} | {pnl_pct_str} |"
        )

    # ── Allocation visuelle ────────────────────────────────────────────────────
    if valid and total_value > 0:
        out.append("\n### Allocation")
        out.append(
            "*La barre montre le poids de chaque position dans le portefeuille total.*\n"
        )
        for r in sorted(valid, key=lambda x: x["cur_value"], reverse=True):
            pct = r["cur_value"] / total_value * 100
            bar = _pct_bar(pct, 25)
            out.append(f"**{r['ticker']:<10}** `{bar}` {pct:.1f}%  ({r['cur_value']:,.2f})")

    # ── Résolutions automatiques (noms → tickers) ─────────────────────────────
    resolved_notes = [
        f"- `{r['ticker_orig']}` → **{r['ticker']}** *(auto-résolu)*"
        for r in rows
        if r.get("resolved_note") and "error" not in r
    ]
    if resolved_notes:
        out.append("\n### 🔀 Résolutions automatiques")
        out.append(
            "*Ces noms ont été convertis automatiquement en tickers Yahoo Finance. "
            "Si le prix semble faux, utilise le ticker direct de ton courtier.*"
        )
        for n in resolved_notes:
            out.append(n)

    # ── Avertissements prix incohérents ────────────────────────────────────────
    price_warns = [r["price_warning"] for r in rows if r.get("price_warning")]
    if price_warns:
        out.append("\n### ⚠️ Prix suspects — vérification recommandée")
        for w in price_warns:
            out.append(f"- {w}")
        out.append(
            "\n*Pour éviter les confusions, utilise le code ticker exact visible "
            "sur Boursorama / Trade Republic / ton courtier (ex: `PANX.PA`, `WPEA.PA`…)*"
        )

    if errors:
        out.append("\n### ⚠️ Erreurs de parsing")
        for e in errors:
            out.append(f"- {e}")

    chart_path = (
        _generate_portfolio_chart(valid, total_value, hist_data)
        if valid and total_value > 0
        else None
    )
    return "\n".join(out), chart_path


# ─── Plugins ──────────────────────────────────────────────────────────────────

class StockAnalysisPlugin(Plugin):
    name = "analyze_stock"
    description = "Analyse technique complète d'une action/crypto: RSI, MACD, Bollinger, ATR. Recommandation achat/vente."
    parameters = {
        "ticker": {"type": "string", "description": "Symbole boursier (AAPL, MSFT, BTC-USD, MC.PA, ETH-USD...)", "required": True},
        "period": {"type": "string", "description": "Période: 1mo 3mo 6mo 1y 2y", "required": False},
    }

    def run(self, ticker: str, period: str = "6mo") -> str:
        try:
            import yfinance as yf
            import pandas as pd
        except ImportError:
            return "❌ yfinance non installé. Installe: pip install yfinance pandas"

        try:
            stock = yf.Ticker(ticker.upper())
            info  = {}
            try:
                info = stock.info or {}
            except Exception:
                pass

            # Essayer plusieurs méthodes pour récupérer les données historiques
            hist = pd.DataFrame()
            for _kwargs in [
                {"period": period},
                {"period": period, "auto_adjust": True, "actions": False},
                {"period": "6mo"},
                {"period": "1y"},
            ]:
                try:
                    h = stock.history(**_kwargs)
                    if not h.empty:
                        hist = h
                        break
                except Exception:
                    pass

            # Fallback: yf.download() (parfois plus fiable pour les actions européennes)
            if hist.empty:
                try:
                    df = yf.download(
                        ticker.upper(), period=period,
                        progress=False, auto_adjust=True,
                    )
                    if not df.empty:
                        # yf.download renvoie un MultiIndex si plusieurs tickers
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.droplevel(1)
                        hist = df
                except Exception:
                    pass

            if hist.empty:
                return f"❌ Aucune donnée pour {ticker}. Vérifie le symbole (ex: AAPL, BTC-USD, MC.PA)."

            close = hist["Close"]
            vol   = hist.get("Volume", pd.Series(dtype=float))
            # yf.download peut renvoyer une Series ou un DataFrame colonne unique
            if hasattr(close, "squeeze"):
                close = close.squeeze()
            if hasattr(vol, "squeeze"):
                vol = vol.squeeze()
            close = close.dropna()
            if len(close) < 5:
                return f"❌ Données insuffisantes pour {ticker} ({len(close)} points seulement)."

            price = float(close.iloc[-1])
            prev  = float(close.iloc[-2]) if len(close) > 1 else price
            chg   = (price - prev) / prev * 100

            # ⚠️ On affichait « Plus haut / plus bas 52 semaines » alors que la periode
            # par defaut est SIX MOIS : tail(252) ne coupait rien, et les extremes des six
            # derniers mois sortaient sous une etiquette « 52s ». Un plus-bas vieux de neuf
            # mois disparaissait, et la « position 52s » affichait 100 % alors que le titre
            # etait en fait 39 % au-dessus de son vrai plancher. Sur de l'argent, c'est un
            # chiffre faux servi comme vrai. On dit donc la fenetre REELLEMENT couverte.
            fenetre = close.tail(252)
            h52, l52 = float(fenetre.max()), float(fenetre.min())
            pos52 = (price - l52) / (h52 - l52) * 100 if h52 != l52 else 50
            _mois = max(1, round(len(fenetre) / 21))
            label52 = "52s" if len(fenetre) >= 200 else f"{_mois} mois"
            perf_1w = (price / float(close.iloc[-5]) - 1) * 100 if len(close) >= 5 else 0
            perf_1m = (price / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0

            rsi_v = _rsi(close)

            ema12  = close.ewm(span=12, adjust=False).mean()
            ema26  = close.ewm(span=26, adjust=False).mean()
            macd_v = float((ema12 - ema26).iloc[-1])
            sig_v  = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])
            hist_v = macd_v - sig_v

            sma20  = float(close.rolling(20).mean().iloc[-1])
            sma50  = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
            sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

            std20 = float(close.rolling(20).std().iloc[-1])
            bb_up = sma20 + 2 * std20
            bb_lo = sma20 - 2 * std20
            bb_pct = (price - bb_lo) / (bb_up - bb_lo) * 100 if bb_up != bb_lo else 50

            try:
                atr_v   = _atr(hist)
                atr_pct = atr_v / price * 100
            except Exception:
                atr_v = atr_pct = 0

            try:
                avg_vol   = float(vol.rolling(20).mean().iloc[-1]) if len(vol) > 0 else 0.0
                cur_vol   = float(vol.iloc[-1]) if len(vol) > 0 else 0.0
            except Exception:
                avg_vol = cur_vol = 0.0
            vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

            score, bull, bear = 0.0, [], []

            if not pd.isna(rsi_v):
                if rsi_v < 30:
                    score += 2.5; bull.append(f"RSI survendu ({rsi_v:.1f}) — rebond probable")
                elif rsi_v < 45:
                    score += 1.0; bull.append(f"RSI faible ({rsi_v:.1f}) — zone d'accumulation")
                elif rsi_v > 70:
                    score -= 2.5; bear.append(f"RSI suracheté ({rsi_v:.1f}) — correction probable")
                elif rsi_v > 55:
                    score -= 1.0; bear.append(f"RSI élevé ({rsi_v:.1f})")

            if not pd.isna(hist_v):
                if hist_v > 0 and macd_v > 0:
                    score += 1.5; bull.append("MACD positif avec momentum haussier")
                elif hist_v > 0 and macd_v < 0:
                    score += 0.5; bull.append("MACD en croisement haussier")
                elif hist_v < 0 and macd_v < 0:
                    score -= 1.5; bear.append("MACD négatif avec momentum baissier")
                else:
                    score -= 0.5; bear.append("MACD en croisement baissier")

            if not pd.isna(sma20):
                if price > sma20:
                    score += 1.0; bull.append(f"Prix au-dessus de la SMA20 ({sma20:.2f})")
                else:
                    score -= 1.0; bear.append(f"Prix sous la SMA20 ({sma20:.2f})")

            if sma50:
                if price > sma50:
                    score += 1.0; bull.append(f"Tendance MT haussière (> SMA50 {sma50:.2f})")
                else:
                    score -= 1.0; bear.append(f"Tendance MT baissière (< SMA50 {sma50:.2f})")

            if sma200:
                if price > sma200:
                    score += 1.0; bull.append(f"Tendance LT haussière (> SMA200 {sma200:.2f})")
                else:
                    score -= 1.0; bear.append(f"Tendance LT baissière (< SMA200 {sma200:.2f})")

            if price < bb_lo:
                score += 2.0; bull.append("Prix sous Bollinger inférieur — survente technique")
            elif price > bb_up:
                score -= 2.0; bear.append("Prix au-dessus de Bollinger supérieur — surachat")

            if vol_ratio > 2:
                if chg > 0:
                    score += 0.5; bull.append(f"Volume x{vol_ratio:.1f} avec hausse — acheteurs forts")
                else:
                    score -= 0.5; bear.append(f"Volume x{vol_ratio:.1f} avec baisse — vendeurs forts")

            if score >= 4:
                rec, action = "🟢 ACHAT FORT", "ACHETER"
            elif score >= 1.5:
                rec, action = "🟡 ACHAT MODÉRÉ", "SURVEILLER / ACHETER"
            elif score >= -1.5:
                rec, action = "⚪ NEUTRE / TENIR", "TENIR"
            elif score >= -4:
                rec, action = "🟠 VENTE MODÉRÉE", "RÉDUIRE"
            else:
                rec, action = "🔴 VENTE FORTE", "VENDRE"

            currency = info.get("currency", "")
            name     = info.get("shortName") or info.get("longName") or ticker
            mc       = info.get("marketCap")
            sector   = info.get("sector", "")
            pe       = info.get("trailingPE")
            div      = info.get("dividendYield")

            cap_fmt = ""
            if mc:
                if mc >= 1e12:   cap_fmt = f"{mc/1e12:.2f}T {currency}"
                elif mc >= 1e9:  cap_fmt = f"{mc/1e9:.1f}B {currency}"
                else:            cap_fmt = f"{mc/1e6:.0f}M {currency}"

            chg_icon  = "📈" if chg >= 0 else "📉"
            price_str = f"{price:,.4f}" if price < 100 else f"{price:,.2f}"
            lines     = []

            lines.append(f"## {chg_icon} {ticker.upper()} — {name}")
            if sector:
                lines.append(f"*{sector}*")
            lines.append("")
            lines.append(f"### 💰 {price_str} {currency} &nbsp;&nbsp; `{chg:+.2f}%`")
            lines.append("")

            # Stats table 2 colonnes
            lines.append("| Métrique | Valeur | Métrique | Valeur |")
            lines.append("|---|---|---|---|")
            lines.append(f"| Plus haut {label52} | {h52:,.2f} | Plus bas {label52} | {l52:,.2f} |")
            lines.append(f"| Position {label52} | {pos52:.0f}% | Volatilité ATR | {atr_pct:.1f}%/j |")
            lines.append(f"| Perf 1 semaine | {perf_1w:+.2f}% | Perf 1 mois | {perf_1m:+.2f}% |")
            if cap_fmt:
                pe_str = f"{pe:.1f}" if pe else "—"
                div_str = f"{div*100:.2f}%" if div else "—"
                lines.append(f"| Capitalisation | {cap_fmt} | P/E | {pe_str} |")
                lines.append(f"| Dividende | {div_str} | Volume | {vol_ratio:.1f}x moy. |")
            lines.append("")

            # Indicateurs
            lines.append("### 📊 Indicateurs Techniques")
            lines.append("")

            if not pd.isna(rsi_v):
                rsi_bar = _pct_bar(rsi_v)
                rsi_sig = "🔥 **SURVENDU** (signal achat)" if rsi_v < 30 else (
                    "❄️ **SURACHETÉ** (signal vente)" if rsi_v > 70 else "✅ Zone neutre")
                lines.append(f"**RSI(14):** `{rsi_bar}` {rsi_v:.1f}  —  {rsi_sig}")

            bb_bar = _pct_bar(bb_pct)
            bb_sig = "🔥 Sous bande inf." if bb_pct < 10 else (
                "❄️ Au-dessus bande sup." if bb_pct > 90 else "✅ Dans les bandes")
            lines.append(f"**Bollinger %B:** `{bb_bar}` {bb_pct:.0f}%  —  {bb_sig}")
            lines.append("")

            lines.append("| Indicateur | Valeur | Signal |")
            lines.append("|---|---|---|")
            lines.append(f"| MACD | {macd_v:+.4f} | {'📈 Haussier' if hist_v > 0 else '📉 Baissier'} (histo: {hist_v:+.4f}) |")
            lines.append(f"| SMA 20 | {sma20:.4f} | {'✅ Prix dessus' if price > sma20 else '❌ Prix dessous'} |")
            if sma50:
                lines.append(f"| SMA 50 | {sma50:.4f} | {'✅ Tendance MT haussière' if price > sma50 else '❌ Tendance MT baissière'} |")
            if sma200:
                lines.append(f"| SMA 200 | {sma200:.4f} | {'✅ Tendance LT haussière' if price > sma200 else '❌ Tendance LT baissière'} |")
            vol_sig = "⚡ Fort" if vol_ratio > 1.5 else ("🔇 Faible" if vol_ratio < 0.6 else "Normal")
            lines.append(f"| Volume | {cur_vol:,.0f} | {vol_sig} ({vol_ratio:.1f}× moyenne) |")
            lines.append("")

            # Niveaux clés
            lines.append("### 📍 Niveaux Clés")
            lines.append("| Niveau | Prix | Distance |")
            lines.append("|---|---|---|")
            lines.append(f"| 🔴 Résistance Bollinger | {bb_up:.4f} | {(bb_up/price-1)*100:+.2f}% |")
            if sma200 and sma200 > price:
                lines.append(f"| 🟡 SMA200 (résistance) | {sma200:.4f} | {(sma200/price-1)*100:+.2f}% |")
            if sma50 and sma50 > price:
                lines.append(f"| 🟠 SMA50 (résistance) | {sma50:.4f} | {(sma50/price-1)*100:+.2f}% |")
            if sma50 and sma50 < price:
                lines.append(f"| 🟠 SMA50 (support) | {sma50:.4f} | {(sma50/price-1)*100:+.2f}% |")
            if sma200 and sma200 < price:
                lines.append(f"| 🟡 SMA200 (support) | {sma200:.4f} | {(sma200/price-1)*100:+.2f}% |")
            lines.append(f"| 🟢 Support Bollinger | {bb_lo:.4f} | {(bb_lo/price-1)*100:+.2f}% |")
            lines.append(f"| 🔵 Plus bas {label52} | {l52:.4f} | {(l52/price-1)*100:+.2f}% |")
            lines.append("")

            # Recommandation
            lines.append("---")
            lines.append(f"### 🎯 RECOMMANDATION : {rec}")
            lines.append(f"**Score : {score:+.1f} / 9** &nbsp;|&nbsp; Action : **{action}**")
            lines.append("")

            if bull:
                lines.append("#### ✅ Signaux Haussiers")
                for b in bull:
                    lines.append(f"- {b}")
                lines.append("")

            if bear:
                lines.append("#### ⚠️ Signaux Baissiers")
                for b in bear:
                    lines.append(f"- {b}")
                lines.append("")

            # ── Plan de trading concret (entrée / objectifs / stop) ──────────
            atr_ref = atr_v if atr_v and atr_v > 0 else price * 0.02
            lines.append("### 📋 Plan de Trading")
            if score >= 1.5:
                entry = price
                supports = [s for s in [bb_lo, sma50, sma200, l52] if s and s < price]
                sl = max(supports) if supports else entry - 2 * atr_ref
                sl = max(sl, entry - 2.5 * atr_ref)          # borne: pas trop loin
                resists = sorted([r for r in [bb_up, sma50, sma200, h52] if r and r > price])
                tp1 = resists[0] if resists else entry + 2 * atr_ref
                tp2 = resists[1] if len(resists) > 1 else entry + 4 * atr_ref
                risk, reward = entry - sl, tp1 - entry
                rr = reward / risk if risk > 0 else 0
                rr_sig = "✅ favorable" if rr >= 2 else ("🟡 correct" if rr >= 1 else "🔴 défavorable")
                lines.append("**Sens : 🟢 LONG (position acheteuse)**")
                lines.append("")
                lines.append("| Niveau | Prix | Distance |")
                lines.append("|---|---|---|")
                lines.append(f"| 🎯 Zone d'entrée | {entry:.4f} | — |")
                lines.append(f"| 🟢 Objectif 1 (TP1) | {tp1:.4f} | {(tp1/entry-1)*100:+.2f}% |")
                lines.append(f"| 🟢 Objectif 2 (TP2) | {tp2:.4f} | {(tp2/entry-1)*100:+.2f}% |")
                lines.append(f"| 🔴 Stop-loss | {sl:.4f} | {(sl/entry-1)*100:+.2f}% |")
                lines.append("")
                lines.append(f"**Ratio risque/rendement : {rr:.1f}:1** — {rr_sig}")
            elif score <= -1.5:
                entry = price
                resists = [r for r in [bb_up, sma50, sma200, h52] if r and r > price]
                sl = min(resists) if resists else entry + 2 * atr_ref
                sl = min(sl, entry + 2.5 * atr_ref)
                supports = sorted([s for s in [bb_lo, sma50, sma200, l52] if s and s < price], reverse=True)
                tp1 = supports[0] if supports else entry - 2 * atr_ref
                tp2 = supports[1] if len(supports) > 1 else entry - 4 * atr_ref
                risk, reward = sl - entry, entry - tp1
                rr = reward / risk if risk > 0 else 0
                rr_sig = "✅ favorable" if rr >= 2 else ("🟡 correct" if rr >= 1 else "🔴 défavorable")
                lines.append("**Sens : 🔴 Éviter / alléger (pression vendeuse)**")
                lines.append("")
                lines.append("| Niveau | Prix | Distance |")
                lines.append("|---|---|---|")
                lines.append(f"| ⛔ Sortie / pas d'achat | {entry:.4f} | — |")
                lines.append(f"| 🟢 Zone de rachat 1 | {tp1:.4f} | {(tp1/entry-1)*100:+.2f}% |")
                lines.append(f"| 🟢 Zone de rachat 2 | {tp2:.4f} | {(tp2/entry-1)*100:+.2f}% |")
                lines.append("")
                lines.append(f"Attends un repli vers **{tp1:.4f}** ({(tp1/entry-1)*100:+.2f}%) avant d'envisager une entrée.")
            else:
                sup = max([s for s in [bb_lo, sma50, sma200, l52] if s and s < price], default=price - 2 * atr_ref)
                res = min([r for r in [bb_up, sma50, sma200, h52] if r and r > price], default=price + 2 * atr_ref)
                lines.append("**Sens : ⚪ Neutre — attendre une cassure**")
                lines.append("")
                lines.append(f"- 🟢 **Acheter** si cassure au-dessus de **{res:.4f}** ({(res/price-1)*100:+.2f}%)")
                lines.append(f"- 🔴 **Vendre** si cassure sous **{sup:.4f}** ({(sup/price-1)*100:+.2f}%)")
            lines.append("")

            lines.append("---")
            lines.append("*Analyse technique automatisée — gère toujours ton risque (taille de position + stop-loss).*")
            return "\n".join(lines)

        except Exception as e:
            return f"Erreur analyse {ticker}: {e}"


class MarketDashboardPlugin(Plugin):
    name = "market_dashboard"
    description = "Tableau de bord des marchés: indices, crypto, forex, matières premières en temps réel."
    parameters = {}

    def run(self) -> str:
        from datetime import datetime
        groups = {
            "📈 INDICES":         [("^GSPC","S&P 500"),("^IXIC","NASDAQ"),("^DJI","Dow Jones"),("^FCHI","CAC 40"),("^VIX","VIX")],
            "💎 CRYPTO":          [("BTC-USD","Bitcoin"),("ETH-USD","Ethereum"),("SOL-USD","Solana"),("BNB-USD","BNB"),("XRP-USD","XRP")],
            "🏭 MATIÈRES 1ÈRE":  [("GC=F","Or"),("SI=F","Argent"),("CL=F","Pétrole WTI"),("NG=F","Gaz Nat.")],
            "🏦 FOREX":           [("EURUSD=X","EUR/USD"),("GBPUSD=X","GBP/USD"),("JPY=X","USD/JPY"),("DX-Y.NYB","DXY")],
        }

        # Tente yfinance en premier, repli HTTP si manquant
        try:
            import yfinance as yf
            import pandas as pd
            _yf_ok = True
        except ImportError:
            _yf_ok = False

        lines = [f"## 🌐 Dashboard Marchés — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"]
        if not _yf_ok:
            lines.append("*⚠️ yfinance absent — données via Yahoo Finance HTTP (RSI non calculé)*\n")

        for category, items in groups.items():
            lines.append(f"### {category}")
            lines.append("| Actif | Prix | 24h | 1 mois | RSI | Signal |")
            lines.append("|---|---|---|---|---|---|")
            for sym, label in items:
                try:
                    if _yf_ok:
                        h = yf.Ticker(sym).history(period="1mo")
                        if h.empty:
                            raise ValueError("empty")
                        close  = h["Close"].squeeze().dropna()
                        price  = float(close.iloc[-1])
                        c24h   = (price / float(close.iloc[-2]) - 1) * 100 if len(close) > 1 else 0
                        c1m    = (price / float(close.iloc[0]) - 1) * 100
                        rv     = _rsi(close)
                        c24_s  = f"{'🟢' if c24h >= 0 else '🔴'} {c24h:+.2f}%"
                        c1m_s  = f"{c1m:+.1f}%"
                        rsi_s  = f"{rv:.0f}" if not pd.isna(rv) else "—"
                        sig    = "⚠️ Suracheté" if not pd.isna(rv) and rv > 70 else (
                                 "🔥 Survendu" if not pd.isna(rv) and rv < 30 else "✅ Neutre")
                    else:
                        d = _fetch_ticker_http(sym, "1mo")
                        if not d:
                            raise ValueError("no http data")
                        price  = d["price"]
                        c24h   = d["chg"]
                        c1m    = d.get("perf") or 0
                        c24_s  = f"{'🟢' if c24h >= 0 else '🔴'} {c24h:+.2f}%"
                        c1m_s  = f"{c1m:+.1f}%" if d.get("perf") is not None else "—"
                        rsi_s  = "—"
                        sig    = "✅ OK"

                    p_fmt = f"{price:,.2f}" if price >= 1 else f"{price:.6f}"
                    lines.append(f"| **{label}** | {p_fmt} | {c24_s} | {c1m_s} | {rsi_s} | {sig} |")
                except Exception:
                    # Ultime fallback HTTP si yfinance échoue pour ce ticker
                    try:
                        d = _fetch_ticker_http(sym, "1mo")
                        if d:
                            p_fmt = f"{d['price']:,.2f}" if d["price"] >= 1 else f"{d['price']:.6f}"
                            c24_s = f"{'🟢' if d['chg'] >= 0 else '🔴'} {d['chg']:+.2f}%"
                            c1m_s = f"{d['perf']:+.1f}%" if d.get("perf") is not None else "—"
                            lines.append(f"| **{label}** | {p_fmt} | {c24_s} | {c1m_s} | — | ✅ |")
                        else:
                            lines.append(f"| {label} | — | — | — | — | — |")
                    except Exception:
                        lines.append(f"| {label} | — | — | — | — | — |")
            lines.append("")

        return "\n".join(lines)


class MultiStockComparePlugin(Plugin):
    name = "compare_stocks"
    description = "Compare plusieurs actions/crypto — retour, volatilité, RSI, Sharpe simplifié."
    parameters = {
        "tickers": {"type": "string", "description": "Tickers séparés par virgule (ex: AAPL,MSFT,GOOGL)", "required": True},
        "period":  {"type": "string", "description": "Période: 1mo 3mo 6mo 1y", "required": False},
    }

    def run(self, tickers: str, period: str = "3mo") -> str:
        try:
            import yfinance as yf
            import pandas as pd
            _yf_ok = True
        except ImportError:
            _yf_ok = False
            pd = None

        symbols = [t.strip().upper() for t in tickers.split(",") if t.strip()][:8]
        rows = []

        for sym in symbols:
            try:
                if not _yf_ok:
                    raise ImportError("yfinance absent")
                h = yf.Ticker(sym).history(period=period)["Close"]
                if hasattr(h, "squeeze"):
                    h = h.squeeze().dropna()
                if h.empty:
                    rows.append({"ticker": sym, "error": True}); continue
                ret    = (h.iloc[-1] / h.iloc[0] - 1) * 100
                vol    = h.pct_change().std() * 100
                rv     = _rsi(h)
                sharpe = ret / (vol * (len(h) ** 0.5)) if vol > 0 else 0
                icon   = "🟢" if ret > 5 else ("🔴" if ret < -5 else "⚪")
                rows.append({"ticker": sym, "ret": ret, "vol": vol,
                             "rsi": rv, "sharpe": sharpe, "icon": icon})
            except Exception:
                # Fallback HTTP pour ce ticker
                try:
                    d = _fetch_ticker_http(sym, period)
                    if d and d.get("perf") is not None:
                        ret    = d["perf"]
                        icon   = "🟢" if ret > 5 else ("🔴" if ret < -5 else "⚪")
                        # ⚠️ C'etait « vol: 0, rsi: 50, sharpe: 0 » EN DUR — des chiffres
                        # inventes servis comme de vrais. Le repli rend pourtant les
                        # cotations : on calcule, et on avoue « N/D » si c'est trop court.
                        closes = d.get("closes") or []
                        vol = _volatilite_liste(closes)
                        rv = _rsi_liste(closes)
                        sharpe = (ret / (vol * (len(closes) ** 0.5))
                                  if vol and vol > 0 and closes else None)
                        rows.append({"ticker": sym, "ret": ret, "vol": vol,
                                     "rsi": rv, "sharpe": sharpe, "icon": icon,
                                     "http": True})
                    else:
                        rows.append({"ticker": sym, "error": True})
                except Exception:
                    rows.append({"ticker": sym, "error": True})

        valid = [r for r in rows if "error" not in r]
        if not valid:
            return "❌ Aucune donnée récupérée (yfinance absent et HTTP fallback échoué).\n\nInstalle yfinance : `pip install yfinance pandas`"

        valid.sort(key=lambda x: x["ret"], reverse=True)
        out = [f"## ⚖️ Comparaison — {period.upper()}\n"]
        out.append("| # | Ticker | Retour | Volatilité | RSI | Sharpe | Signal |")
        out.append("|---|---|---|---|---|---|---|")

        # ⚠️ « ✅ Neutre » etait affiche meme quand le RSI valait 50 parce qu'on l'avait
        # invente. Un indicateur qu'on n'a pas s'ecrit « N/D » — pas une valeur au milieu
        # de la fourchette, qui se lit comme une mesure rassurante.
        def _n(v, fmt, suffixe=""):
            return "N/D" if v is None else (format(v, fmt) + suffixe)

        degrade = False
        for i, r in enumerate(valid, 1):
            rsi = r.get("rsi")
            rsi_sig = ("N/D" if rsi is None else
                       "🔥 Survendu" if rsi < 30 else
                       "❄️ Suracheté" if rsi > 70 else "✅ Neutre")
            marque = ""
            if r.get("http"):
                degrade = True
                marque = " ⚠️"
            out.append(
                f"| {i} | **{r['ticker']}**{marque} | {r['icon']} {r['ret']:+.2f}% "
                f"| {_n(r.get('vol'), '.2f', '%/j')} | {_n(rsi, '.0f')} "
                f"| {_n(r.get('sharpe'), '.2f')} | {rsi_sig} |"
            )
        for r in rows:
            if "error" in r:
                out.append(f"| — | ❌ {r['ticker']} | — | — | — | — | Données indisponibles |")

        out.append("")
        if degrade:
            out.append("⚠️ Les lignes marquées viennent d'une source de secours "
                       "(la source principale n'a pas répondu) : les indicateurs y sont "
                       "calculés sur moins de données, et « N/D » signifie qu'ils n'ont "
                       "pas pu l'être du tout.")
            out.append("")
        out.append(f"**🏆 Meilleure performance :** {valid[0]['ticker']} ({valid[0]['ret']:+.2f}%)")
        out.append(f"**📉 Moins bonne :** {valid[-1]['ticker']} ({valid[-1]['ret']:+.2f}%)")
        return "\n".join(out)


class MarketNewsPlugin(Plugin):
    name = "get_market_news"
    description = "Dernières nouvelles financières. Paramètre ticker: ex 'AAPL', 'MC.PA', ou 'marché' pour les news générales."
    parameters = {
        "ticker": {"type": "string", "description": "Ticker boursier (ex: AAPL, MC.PA) ou 'marché' pour les news générales. Défaut: 'marché'", "required": False},
    }

    def run(self, ticker: str = "marché") -> str:
        try:
            import yfinance as yf
            _yf_ok = True
        except ImportError:
            _yf_ok = False
            yf = None

        def _parse(n: dict) -> tuple:
            """Gère l'ancien ET le nouveau schéma yfinance."""
            # Nouveau schéma : tout est sous 'content'
            c = n.get("content") if isinstance(n.get("content"), dict) else n
            title = c.get("title") or n.get("title") or "?"
            pub = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else None
            pub = pub or n.get("publisher") or "?"
            url = ""
            if isinstance(c.get("canonicalUrl"), dict):
                url = c["canonicalUrl"].get("url", "")
            url = url or c.get("clickThroughUrl", {}).get("url", "") if isinstance(c.get("clickThroughUrl"), dict) else url
            url = url or n.get("link") or ""
            summary = c.get("summary") or c.get("description") or ""
            return title, pub, url, summary

        err = ""
        if _yf_ok:
            try:
                news = yf.Ticker(ticker.upper()).news or []
                items = [_parse(n) for n in news[:8]]
                items = [it for it in items if it[0] and it[0] != "?"]

                if items:
                    lines = [f"## 📰 Actualités {ticker.upper()} — {len(items)} dernières\n"]
                    for title, pub, url, summary in items:
                        lines.append(f"**{title}**  \n*{pub}*" + (f" — {url}" if url else ""))
                        if summary:
                            lines.append(f"> {summary[:220]}")
                        lines.append("")
                    return "\n".join(lines)
            except Exception as e:
                err = str(e)

        # Fallback : recherche d'actualités web si yfinance ne renvoie rien
        try:
            from duckduckgo_search import DDGS
            q = f"{ticker.upper()} stock news"
            with DDGS() as ddgs:
                res = list(ddgs.news(q, max_results=6, region="fr-fr"))
            if res:
                lines = [f"## 📰 Actualités {ticker.upper()} (web)\n"]
                for r in res:
                    t = str(r.get("title", "?"))
                    s = str(r.get("source", ""))
                    u = r.get("url", "")
                    lines.append(f"**{t}**  \n*{s}* — {u}\n")
                return "\n".join(lines)
        except Exception:
            pass

        return f"Aucune actualité trouvée pour {ticker}." + (f" ({err})" if err else "")
