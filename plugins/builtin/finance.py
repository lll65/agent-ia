"""
Plugin d'analyse financière — données réelles via yfinance.
RSI, MACD, Bollinger, ATR — recommandation achat/vente/hold.
Dashboard marchés, portefeuille P&L, comparaison multi-actifs.
"""
from plugins.base import Plugin


# ─── Helpers internes ─────────────────────────────────────────────────────────

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


def _generate_portfolio_chart(rows: list, total_value: float) -> str | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from pathlib import Path

        BG, BORDER = "#0d1117", "#30363d"
        TEXT = "#e6edf3"
        PALETTE = ["#58a6ff", "#3fb950", "#bc8cff", "#f0883e", "#ff7b72",
                   "#ffa657", "#79c0ff", "#56d364", "#d2a8ff", "#ffb3b3"]

        with_pnl = [r for r in rows if r.get("pnl_pct") is not None]
        n = 2 if with_pnl else 1
        fig, axes = plt.subplots(1, n, figsize=(13 if n == 2 else 7, 6), facecolor=BG)
        if n == 1:
            axes = [axes]

        # Pie
        ax_pie = axes[0]
        ax_pie.set_facecolor(BG)
        labels = [r["ticker"] for r in rows]
        sizes  = [r["cur_value"] / total_value * 100 for r in rows]
        colors = PALETTE[: len(labels)]

        wedges, texts, pcts = ax_pie.pie(
            sizes, labels=labels, colors=colors, autopct="%1.1f%%",
            startangle=90, textprops={"color": TEXT, "fontsize": 9},
            wedgeprops={"edgecolor": BG, "linewidth": 2}, pctdistance=0.75,
        )
        for pt in pcts:
            pt.set_color(TEXT); pt.set_fontsize(8)
        ax_pie.set_title("Allocation", color=TEXT, fontsize=12, pad=10)

        # Bar P&L
        if with_pnl:
            ax_bar = axes[1]
            ax_bar.set_facecolor(BG)
            ax_bar.tick_params(colors="#8b949e", labelsize=9)
            for sp in ax_bar.spines.values():
                sp.set_color(BORDER)
            ax_bar.grid(axis="y", color="#21262d", linewidth=0.5, alpha=0.6)
            ax_bar.axhline(y=0, color="#8b949e", linewidth=0.8)

            tks   = [r["ticker"] for r in with_pnl]
            vals  = [r["pnl_pct"] for r in with_pnl]
            bcolors = ["#3fb950" if v >= 0 else "#f85149" for v in vals]

            bars = ax_bar.bar(tks, vals, color=bcolors, edgecolor=BG, linewidth=1.5, width=0.6)
            ax_bar.set_title("Performance par Position (%)", color=TEXT, fontsize=12, pad=10)

            for bar, val in zip(bars, vals):
                off = 0.3 if val >= 0 else -1.2
                ax_bar.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + off,
                            f"{val:+.1f}%", ha="center", va="bottom", color=TEXT, fontsize=8)

        plt.tight_layout(pad=2)
        Path("output/charts").mkdir(parents=True, exist_ok=True)
        path = "output/charts/portfolio.png"
        fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        return path
    except Exception:
        return None


# ─── Portfolio public ─────────────────────────────────────────────────────────

def analyze_portfolio(positions_text: str) -> tuple:
    """
    Analyse un portefeuille depuis texte.
    Format: une ligne par position — TICKER QUANTITE [PRIX_ACHAT]
    Retourne (markdown_str, chart_path_or_None).
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return "❌ pip install yfinance pandas", None

    raw_lines = [l.strip() for l in positions_text.strip().split("\n")
                 if l.strip() and not l.strip().startswith("#")]
    positions, errors = [], []

    for line in raw_lines:
        parts = line.split()
        if len(parts) < 2:
            errors.append(f"Format invalide: `{line}`")
            continue
        try:
            positions.append({
                "ticker":    parts[0].upper(),
                "qty":       float(parts[1].replace(",", ".")),
                "buy_price": float(parts[2].replace(",", ".")) if len(parts) > 2 else None,
            })
        except ValueError:
            errors.append(f"Erreur de format: `{line}`")

    if not positions:
        return (
            "❌ Aucune position valide.\n\n"
            "**Format** (une par ligne) :\n```\nAAPL 10 150.00\nBTC-USD 0.5 42000\nMC.PA 3 650\n```",
            None,
        )

    rows, total_value, total_cost = [], 0.0, 0.0

    for p in positions:
        try:
            tk   = yf.Ticker(p["ticker"])
            info = tk.info or {}
            hist = tk.history(period="5d")
            if hist.empty:
                rows.append({**p, "error": "Données indisponibles"})
                continue

            cur_price = float(hist["Close"].iloc[-1])
            prev_p    = float(hist["Close"].iloc[-2]) if len(hist) > 1 else cur_price
            chg_24h   = (cur_price / prev_p - 1) * 100
            cur_value = cur_price * p["qty"]
            cost      = p["buy_price"] * p["qty"] if p["buy_price"] else None
            pnl       = cur_value - cost if cost else None
            pnl_pct   = (cur_price / p["buy_price"] - 1) * 100 if p["buy_price"] else None

            total_value += cur_value
            if cost:
                total_cost += cost

            rows.append({
                "ticker":    p["ticker"],
                "name":      (info.get("shortName") or p["ticker"])[:18],
                "qty":       p["qty"],
                "buy_price": p["buy_price"],
                "cur_price": cur_price,
                "chg_24h":   chg_24h,
                "cur_value": cur_value,
                "cost":      cost,
                "pnl":       pnl,
                "pnl_pct":   pnl_pct,
                "currency":  info.get("currency", ""),
            })
        except Exception as e:
            rows.append({**p, "error": str(e)})

    valid = [r for r in rows if "error" not in r]
    out   = ["## 💼 Mon Portefeuille\n"]

    # Résumé global
    if total_value > 0:
        out.append("### 📊 Résumé")
        if total_cost > 0:
            total_pnl     = total_value - total_cost
            total_pnl_pct = (total_value / total_cost - 1) * 100
            icon = "🟢" if total_pnl >= 0 else "🔴"
            out.append("| | |")
            out.append("|---|---|")
            out.append(f"| Valeur totale | **{total_value:,.2f}** |")
            out.append(f"| Coût total | {total_cost:,.2f} |")
            out.append(f"| **P&L Total** | {icon} **{total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)** |")
        else:
            out.append(f"**Valeur totale : {total_value:,.2f}**")
        out.append("")

    # Tableau positions
    out.append("### Positions")
    out.append("| Ticker | Nom | Qté | Px achat | Px actuel | 24h | Valeur | P&L | P&L% |")
    out.append("|---|---|---|---|---|---|---|---|---|")

    for r in rows:
        if "error" in r:
            out.append(f"| **{r.get('ticker','')}** | ❌ {r['error'][:20]} "
                       f"| {r.get('qty','')} | — | — | — | — | — | — |")
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

    # Allocation visuelle
    if valid and total_value > 0:
        out.append("\n### Allocation")
        for r in sorted(valid, key=lambda x: x["cur_value"], reverse=True):
            pct = r["cur_value"] / total_value * 100
            bar = _pct_bar(pct, 25)
            out.append(f"**{r['ticker']:<10}** `{bar}` {pct:.1f}%  ({r['cur_value']:,.2f})")

    if errors:
        out.append("\n### ⚠️ Erreurs de parsing")
        for e in errors:
            out.append(f"- {e}")

    chart_path = _generate_portfolio_chart(valid, total_value) if valid and total_value > 0 else None
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
            info  = stock.info or {}
            hist  = stock.history(period=period)
            if hist.empty:
                return f"❌ Aucune donnée pour {ticker}. Vérifie le symbole (ex: AAPL, BTC-USD, MC.PA)."

            close = hist["Close"]
            vol   = hist["Volume"]

            price = float(close.iloc[-1])
            prev  = float(close.iloc[-2]) if len(close) > 1 else price
            chg   = (price - prev) / prev * 100

            h52  = float(close.tail(252).max()) if len(close) >= 30 else float(close.max())
            l52  = float(close.tail(252).min()) if len(close) >= 30 else float(close.min())
            pos52 = (price - l52) / (h52 - l52) * 100 if h52 != l52 else 50
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

            avg_vol   = float(vol.rolling(20).mean().iloc[-1])
            cur_vol   = float(vol.iloc[-1])
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
            lines.append(f"| Plus haut 52s | {h52:,.2f} | Plus bas 52s | {l52:,.2f} |")
            lines.append(f"| Position 52s | {pos52:.0f}% | Volatilité ATR | {atr_pct:.1f}%/j |")
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
            lines.append(f"| 🔵 Plus bas 52 semaines | {l52:.4f} | {(l52/price-1)*100:+.2f}% |")
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
        try:
            import yfinance as yf
            import pandas as pd
        except ImportError:
            return "❌ pip install yfinance pandas"

        from datetime import datetime
        groups = {
            "📈 INDICES":         [("^GSPC","S&P 500"),("^IXIC","NASDAQ"),("^DJI","Dow Jones"),("^FCHI","CAC 40"),("^VIX","VIX")],
            "💎 CRYPTO":          [("BTC-USD","Bitcoin"),("ETH-USD","Ethereum"),("SOL-USD","Solana"),("BNB-USD","BNB"),("XRP-USD","XRP")],
            "🏭 MATIÈRES 1ÈRE":  [("GC=F","Or"),("SI=F","Argent"),("CL=F","Pétrole WTI"),("NG=F","Gaz Nat.")],
            "🏦 FOREX":           [("EURUSD=X","EUR/USD"),("GBPUSD=X","GBP/USD"),("JPY=X","USD/JPY"),("DX-Y.NYB","DXY")],
        }

        lines = [f"## 🌐 Dashboard Marchés — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"]

        for category, items in groups.items():
            lines.append(f"### {category}")
            lines.append("| Actif | Prix | 24h | 1 mois | RSI | Signal |")
            lines.append("|---|---|---|---|---|---|")
            for sym, label in items:
                try:
                    h = yf.Ticker(sym).history(period="1mo")
                    if h.empty:
                        lines.append(f"| {label} | — | — | — | — | — |"); continue
                    close  = h["Close"]
                    price  = float(close.iloc[-1])
                    c24h   = (price / float(close.iloc[-2]) - 1) * 100 if len(close) > 1 else 0
                    c1m    = (price / float(close.iloc[0]) - 1) * 100
                    rv     = _rsi(close)
                    c24_s  = f"{'🟢' if c24h >= 0 else '🔴'} {c24h:+.2f}%"
                    c1m_s  = f"{c1m:+.1f}%"
                    rsi_s  = f"{rv:.0f}" if not pd.isna(rv) else "—"
                    sig    = "⚠️ Suracheté" if not pd.isna(rv) and rv > 70 else (
                             "🔥 Survendu" if not pd.isna(rv) and rv < 30 else "✅ Neutre")
                    p_fmt  = f"{price:,.2f}" if price >= 1 else f"{price:.6f}"
                    lines.append(f"| **{label}** | {p_fmt} | {c24_s} | {c1m_s} | {rsi_s} | {sig} |")
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
        except ImportError:
            return "❌ pip install yfinance pandas"

        symbols = [t.strip().upper() for t in tickers.split(",") if t.strip()][:8]
        rows = []

        for sym in symbols:
            try:
                h = yf.Ticker(sym).history(period=period)["Close"]
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
                rows.append({"ticker": sym, "error": True})

        valid = [r for r in rows if "error" not in r]
        if not valid:
            return "❌ Aucune donnée récupérée."

        valid.sort(key=lambda x: x["ret"], reverse=True)
        out = [f"## ⚖️ Comparaison — {period.upper()}\n"]
        out.append("| # | Ticker | Retour | Volatilité | RSI | Sharpe | Signal |")
        out.append("|---|---|---|---|---|---|---|")

        for i, r in enumerate(valid, 1):
            import pandas as pd
            rsi_sig = "🔥 Survendu" if r["rsi"] < 30 else ("❄️ Suracheté" if r["rsi"] > 70 else "✅ Neutre")
            out.append(
                f"| {i} | **{r['ticker']}** | {r['icon']} {r['ret']:+.2f}% "
                f"| {r['vol']:.2f}%/j | {r['rsi']:.0f} | {r['sharpe']:.2f} | {rsi_sig} |"
            )
        for r in rows:
            if "error" in r:
                out.append(f"| — | ❌ {r['ticker']} | — | — | — | — | Données indisponibles |")

        out.append("")
        out.append(f"**🏆 Meilleure performance :** {valid[0]['ticker']} ({valid[0]['ret']:+.2f}%)")
        out.append(f"**📉 Moins bonne :** {valid[-1]['ticker']} ({valid[-1]['ret']:+.2f}%)")
        return "\n".join(out)


class MarketNewsPlugin(Plugin):
    name = "get_market_news"
    description = "Dernières nouvelles financières pour un ticker ou le marché."
    parameters = {
        "ticker": {"type": "string", "description": "Ticker (ex: AAPL) ou 'marché' pour les news générales", "required": True},
    }

    def run(self, ticker: str) -> str:
        try:
            import yfinance as yf
        except ImportError:
            return "❌ pip install yfinance"

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
        else:
            err = ""

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
