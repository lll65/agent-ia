"""
Plugin d'analyse financière — données réelles via yfinance.
RSI, MACD, Bollinger, SMA, volume — recommandation achat/vente/hold.
"""
from plugins.base import Plugin


class StockAnalysisPlugin(Plugin):
    name = "analyze_stock"
    description = "Analyse technique complète d'une action/crypto: prix, RSI, MACD, Bollinger. Recommandation achat/vente."
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
            info = stock.info or {}
            hist = stock.history(period=period)
            if hist.empty:
                return f"❌ Aucune donnée pour {ticker}. Vérifie le symbole (ex: AAPL, BTC-USD, MC.PA)."

            close = hist["Close"]
            vol = hist["Volume"]

            price = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) > 1 else price
            chg = (price - prev) / prev * 100

            # RSI 14
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            loss_val = loss.iloc[-1]
            gain_val = gain.iloc[-1]
            if loss_val == 0:
                rsi = 100.0 if gain_val > 0 else float("nan")
            else:
                rsi = float(100 - 100 / (1 + gain_val / loss_val))

            # MACD
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            sig_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_v = float(macd_line.iloc[-1])
            sig_v = float(sig_line.iloc[-1])
            hist_v = macd_v - sig_v

            # SMA
            sma20 = float(close.rolling(20).mean().iloc[-1])
            sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
            sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

            # Bollinger
            std20 = float(close.rolling(20).std().iloc[-1])
            bb_up = sma20 + 2 * std20
            bb_lo = sma20 - 2 * std20

            # Volume
            avg_vol = float(vol.rolling(20).mean().iloc[-1])
            cur_vol = float(vol.iloc[-1])
            vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

            # Score haussier/baissier
            score = 0.0
            bull, bear = [], []

            if not pd.isna(rsi):
                if rsi < 30:
                    score += 2.5; bull.append(f"RSI survendu ({rsi:.1f}) — rebond probable")
                elif rsi < 45:
                    score += 1; bull.append(f"RSI neutre ({rsi:.1f})")
                elif rsi > 70:
                    score -= 2.5; bear.append(f"RSI suracheté ({rsi:.1f}) — correction probable")
                elif rsi > 55:
                    score -= 1; bear.append(f"RSI élevé ({rsi:.1f})")

            if not pd.isna(hist_v):
                if hist_v > 0 and macd_v > 0:
                    score += 1.5; bull.append("MACD positif + momentum haussier")
                elif hist_v > 0 and macd_v < 0:
                    score += 0.5; bull.append("MACD croisement haussier")
                elif hist_v < 0 and macd_v < 0:
                    score -= 1.5; bear.append("MACD négatif + momentum baissier")
                else:
                    score -= 0.5; bear.append("MACD croisement baissier")

            if not pd.isna(sma20):
                if price > sma20:
                    score += 1; bull.append(f"Prix > SMA20 ({sma20:.2f})")
                else:
                    score -= 1; bear.append(f"Prix < SMA20 ({sma20:.2f})")

            if sma50:
                if price > sma50:
                    score += 1; bull.append(f"Tendance MT haussière (> SMA50 {sma50:.2f})")
                else:
                    score -= 1; bear.append(f"Tendance MT baissière (< SMA50 {sma50:.2f})")

            if sma200:
                if price > sma200:
                    score += 1; bull.append(f"Tendance LT haussière (> SMA200 {sma200:.2f})")
                else:
                    score -= 1; bear.append(f"Tendance LT baissière (< SMA200 {sma200:.2f})")

            if price < bb_lo:
                score += 2; bull.append("Sous la bande de Bollinger inf. (survente technique)")
            elif price > bb_up:
                score -= 2; bear.append("Au-dessus de la bande de Bollinger sup. (surachat)")

            if vol_ratio > 2:
                if chg > 0:
                    score += 0.5; bull.append(f"Volume x{vol_ratio:.1f} — acheteurs agressifs")
                else:
                    score -= 0.5; bear.append(f"Volume x{vol_ratio:.1f} — vendeurs agressifs")

            # Recommandation
            if score >= 4:
                rec = "🟢 ACHAT FORT"
            elif score >= 1.5:
                rec = "🟡 ACHAT MODÉRÉ"
            elif score >= -1.5:
                rec = "⚪ NEUTRE / TENIR"
            elif score >= -4:
                rec = "🟠 VENTE MODÉRÉE"
            else:
                rec = "🔴 VENTE FORTE"

            currency = info.get("currency", "")
            name = info.get("shortName") or info.get("longName") or ticker
            market_cap = info.get("marketCap")
            cap_str = f"  • Capitalisation: ${market_cap/1e9:.1f}B\n" if market_cap else ""

            out = [
                f"═══ ANALYSE {ticker.upper()} — {name} ═══",
                f"",
                f"💰 Prix: {price:.4f} {currency}  ({chg:+.2f}%)",
                cap_str.rstrip(),
                f"",
                f"📊 INDICATEURS TECHNIQUES ({period})",
                f"  • RSI(14): {rsi:.1f}" + ("  🔥 SURVENDU" if rsi < 30 else "  ❄️ SURACHETÉ" if rsi > 70 else ""),
                f"  • MACD: {macd_v:+.4f} | Signal: {sig_v:+.4f} | Histo: {hist_v:+.4f}" + ("  📈" if hist_v > 0 else "  📉"),
                f"  • SMA20: {sma20:.4f}" + (f" | SMA50: {sma50:.4f}" if sma50 else "") + (f" | SMA200: {sma200:.4f}" if sma200 else ""),
                f"  • Bollinger: [{bb_lo:.4f} ↔ {bb_up:.4f}]",
                f"  • Volume: {cur_vol:,.0f}  ({vol_ratio:.1f}x la moyenne)",
                f"",
                f"🎯 RECOMMANDATION: {rec}  (score={score:+.1f})",
                f"",
            ]

            if bull:
                out.append("✅ Points HAUSSIERS:")
                out += [f"   + {r}" for r in bull]
                out.append("")
            if bear:
                out.append("⚠️ Points BAISSIERS:")
                out += [f"   - {r}" for r in bear]
                out.append("")

            # Niveaux clés
            out += [
                "📍 NIVEAUX CLÉS:",
                f"   Support: {bb_lo:.4f}  (Bollinger inf.)",
                f"   Résistance: {bb_up:.4f}  (Bollinger sup.)",
                "",
                "⚠️  Analyse algorithmique — pas un conseil financier.",
            ]
            return "\n".join(out)

        except Exception as e:
            return f"Erreur analyse {ticker}: {e}"


class MultiStockComparePlugin(Plugin):
    name = "compare_stocks"
    description = "Compare plusieurs actions côte à côte — retour, volatilité, force relative."
    parameters = {
        "tickers": {"type": "string", "description": "Tickers séparés par virgule (ex: AAPL,MSFT,GOOGL)", "required": True},
        "period": {"type": "string", "description": "Période: 1mo 3mo 6mo 1y", "required": False},
    }

    def run(self, tickers: str, period: str = "3mo") -> str:
        try:
            import yfinance as yf
            import pandas as pd
        except ImportError:
            return "❌ pip install yfinance pandas"

        symbols = [t.strip().upper() for t in tickers.split(",") if t.strip()][:6]
        rows = []
        for sym in symbols:
            try:
                h = yf.Ticker(sym).history(period=period)["Close"]
                if h.empty:
                    continue
                ret = (h.iloc[-1] / h.iloc[0] - 1) * 100
                vol = h.pct_change().std() * 100
                delta = h.diff()
                gain = delta.where(delta > 0, 0).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rs = gain / loss.replace(0, float("nan"))
                rsi = float(100 - 100 / (1 + rs.iloc[-1]))
                icon = "🟢" if ret > 5 else "🔴" if ret < -5 else "⚪"
                rows.append(f"{icon} {sym:<8} Ret: {ret:+6.1f}%  Vol: {vol:.2f}%  RSI: {rsi:.0f}")
            except Exception:
                rows.append(f"❌ {sym}: données indisponibles")

        if not rows:
            return "❌ Aucune donnée récupérée."
        header = f"COMPARAISON {period.upper()} — {len(rows)} actifs\n" + "─" * 50
        return header + "\n" + "\n".join(rows)


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
        try:
            news = yf.Ticker(ticker.upper()).news or []
            if not news:
                return f"Aucune news pour {ticker}."
            lines = [f"📰 NEWS {ticker.upper()} (dernières {min(5, len(news))})\n"]
            for n in news[:5]:
                title = n.get("title", "?")
                pub = n.get("publisher", "?")
                url = n.get("link", "")
                lines.append(f"• **{title}** ({pub})\n  {url}")
            return "\n".join(lines)
        except Exception as e:
            return f"Erreur news {ticker}: {e}"
