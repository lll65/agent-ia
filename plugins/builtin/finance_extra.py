"""
Outils finance supplémentaires — sources 100% GRATUITES et SANS CLÉ API :
  - CoinGecko        → marché crypto complet (prix, cap, dominance BTC, top coins)
  - alternative.me   → Fear & Greed Index (sentiment du marché)
  - Frankfurter (BCE)→ taux de change officiels

Chaque plugin se charge automatiquement et devient dispo en Mode Agent + Telegram.
Tolérant aux pannes : si une API ne répond pas, message clair au lieu d'un crash.
"""
import requests
from plugins.base import Plugin

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MasterAgent/1.0)"}
_TIMEOUT = 15

# Symboles courants → identifiants CoinGecko
_COIN_IDS = {
    "BTC": "bitcoin", "BITCOIN": "bitcoin",
    "ETH": "ethereum", "ETHEREUM": "ethereum",
    "SOL": "solana", "SOLANA": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "RIPPLE": "ripple",
    "ADA": "cardano", "CARDANO": "cardano",
    "DOGE": "dogecoin", "DOGECOIN": "dogecoin",
    "AVAX": "avalanche-2", "AVALANCHE": "avalanche-2",
    "DOT": "polkadot", "POLKADOT": "polkadot",
    "MATIC": "matic-network", "POLYGON": "matic-network",
    "LINK": "chainlink", "CHAINLINK": "chainlink",
    "LTC": "litecoin", "TRX": "tron", "SHIB": "shiba-inu",
    "TON": "the-open-network", "ATOM": "cosmos", "UNI": "uniswap",
}


def _fmt(n, dec=2):
    try:
        return f"{float(n):,.{dec}f}".replace(",", " ")
    except Exception:
        return str(n)


class CryptoMarketPlugin(Plugin):
    name = "crypto_market"
    description = (
        "Marché crypto en direct via CoinGecko (gratuit) : cap globale, dominance BTC, "
        "et prix/variation des principales cryptos. Paramètre optionnel: coins (ex: 'BTC,ETH,SOL')."
    )
    parameters = {
        "coins": {"type": "string", "description": "Cryptos séparées par virgule (optionnel)", "required": False},
    }

    def run(self, coins: str = "", **_) -> str:
        out = ["## 🪙 Marché Crypto (CoinGecko, temps réel)\n"]

        # Vue globale
        try:
            g = requests.get("https://api.coingecko.com/api/v3/global",
                             headers=_HEADERS, timeout=_TIMEOUT).json().get("data", {})
            mcap = g.get("total_market_cap", {}).get("eur")
            chg = g.get("market_cap_change_percentage_24h_usd")
            dom = g.get("market_cap_percentage", {})
            if mcap:
                icon = "🟢" if (chg or 0) >= 0 else "🔴"
                out.append(f"**Cap totale :** {_fmt(mcap/1e9,1)} Md€ {icon} {chg:+.2f}%/24h")
                out.append(f"**Dominance :** BTC {dom.get('btc',0):.1f}% · ETH {dom.get('eth',0):.1f}%\n")
        except Exception as e:
            out.append(f"⚠️ Vue globale indisponible ({str(e)[:60]})\n")

        # Coins demandés OU top 8
        try:
            if coins and coins.strip():
                ids = []
                for c in coins.replace(" ", "").split(","):
                    if not c:
                        continue
                    ids.append(_COIN_IDS.get(c.upper(), c.lower()))
                url = ("https://api.coingecko.com/api/v3/coins/markets"
                       f"?vs_currency=eur&ids={','.join(ids)}"
                       "&price_change_percentage=24h,7d")
            else:
                url = ("https://api.coingecko.com/api/v3/coins/markets"
                       "?vs_currency=eur&order=market_cap_desc&per_page=8&page=1"
                       "&price_change_percentage=24h,7d")
            data = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT).json()
            if isinstance(data, list) and data:
                out.append("| Crypto | Prix | 24h | 7j |")
                out.append("|---|---|---|---|")
                for c in data:
                    p = c.get("current_price")
                    d1 = c.get("price_change_percentage_24h_in_currency")
                    d7 = c.get("price_change_percentage_7d_in_currency")
                    s24 = f"{d1:+.1f}%" if d1 is not None else "N/D"
                    s7 = f"{d7:+.1f}%" if d7 is not None else "N/D"
                    name = c.get("symbol", "").upper()
                    out.append(f"| {name} | {_fmt(p, 2 if (p or 0) < 100 else 0)}€ | {s24} | {s7} |")
            else:
                out.append("⚠️ Aucune donnée coin (vérifie les symboles).")
        except Exception as e:
            out.append(f"⚠️ Prix indisponibles ({str(e)[:60]})")

        return "\n".join(out)


class FearGreedPlugin(Plugin):
    name = "fear_greed"
    description = (
        "Indice Fear & Greed crypto (peur/avidité du marché, gratuit via alternative.me). "
        "0 = peur extrême (souvent opportunité d'achat), 100 = avidité extrême (prudence)."
    )
    parameters = {}

    def run(self, **_) -> str:
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=1",
                             headers=_HEADERS, timeout=_TIMEOUT).json()
            item = (d.get("data") or [{}])[0]
            val = int(item.get("value", 0))
            label = item.get("value_classification", "?")
        except Exception as e:
            return f"⚠️ Fear & Greed indisponible ({str(e)[:60]})"

        if val <= 25:
            reading = "😱 PEUR EXTRÊME — historiquement une zone d'accumulation (les autres paniquent)"
        elif val <= 45:
            reading = "😟 Peur — prudence ambiante, opportunités possibles"
        elif val <= 55:
            reading = "😐 Neutre — marché indécis"
        elif val <= 75:
            reading = "🙂 Avidité — le marché est optimiste, reste sélectif"
        else:
            reading = "🤑 AVIDITÉ EXTRÊME — euphorie, risque de correction élevé"

        bar = "█" * (val // 10) + "░" * (10 - val // 10)
        return (
            f"## 😨/🤑 Fear & Greed Index (crypto)\n\n"
            f"**{val}/100** — {label}\n\n"
            f"`{bar}`\n\n{reading}"
        )


class CurrencyRatesPlugin(Plugin):
    name = "currency_rates"
    description = (
        "Taux de change officiels (BCE, gratuit via Frankfurter). "
        "Paramètres optionnels: base (défaut EUR), to (ex: 'USD,GBP,JPY')."
    )
    parameters = {
        "base": {"type": "string", "description": "Devise de base (défaut EUR)", "required": False},
        "to":   {"type": "string", "description": "Devises cibles séparées par virgule", "required": False},
    }

    def run(self, base: str = "EUR", to: str = "USD,GBP,JPY,CHF", **_) -> str:
        base = (base or "EUR").upper().strip()
        to = (to or "USD,GBP,JPY,CHF").upper().replace(" ", "")
        try:
            d = requests.get(f"https://api.frankfurter.app/latest?from={base}&to={to}",
                             headers=_HEADERS, timeout=_TIMEOUT).json()
            rates = d.get("rates", {})
            date = d.get("date", "")
            if not rates:
                return f"⚠️ Aucun taux pour {base}→{to}"
            lines = [f"## 💱 Taux de change ({base}, source BCE — {date})\n"]
            for cur, rate in rates.items():
                lines.append(f"- 1 {base} = **{_fmt(rate, 4)} {cur}**")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ Taux de change indisponibles ({str(e)[:60]})"
