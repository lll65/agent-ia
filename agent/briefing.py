"""
Briefing du matin — compose un résumé à partir de DONNÉES RÉELLES :
agenda du jour (Composio), mails non lus (Composio), météo (open-meteo, gratuit, sans clé),
actu (recherche web). Aucune invention : si une source échoue, on le dit.
"""
import json
import logging

from config import config

logger = logging.getLogger(__name__)

_WCODE = {0: "ciel clair", 1: "peu nuageux", 2: "nuageux", 3: "couvert", 45: "brouillard",
          48: "brouillard givrant", 51: "bruine", 61: "pluie", 63: "pluie", 65: "forte pluie",
          71: "neige", 80: "averses", 95: "orage"}


def weather_line() -> str:
    """Météo du jour pour la ville configurée (open-meteo, gratuit)."""
    import requests
    city = getattr(config, "BRIEFING_CITY", "Paris") or "Paris"
    try:
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1, "language": "fr"}, timeout=10).json()
        res = (g.get("results") or [{}])[0]
        lat, lon = res.get("latitude"), res.get("longitude")
        if lat is None:
            return ""
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        }, timeout=10).json()
        d = w.get("daily", {})
        tmax = d.get("temperature_2m_max", [None])[0]
        tmin = d.get("temperature_2m_min", [None])[0]
        pp = d.get("precipitation_probability_max", [None])[0]
        code = (d.get("weather_code", [0]) or [0])[0]
        desc = _WCODE.get(code, "")
        return f"{city} : {desc}, {tmin}–{tmax}°C, pluie {pp}%."
    except Exception as e:
        logger.warning(f"[briefing] météo KO: {e}")
        return ""


def build_briefing() -> str:
    """Compose le briefing du matin (bloquant → appeler dans un thread)."""
    from plugins import get_loader
    from agent.self_heal import safe_tool_call
    from llm.client import chat
    from api.agent import _time_bounds  # réutilise le calcul de période

    loader = get_loader()
    tmin, tmax, _ = _time_bounds("aujourd'hui")
    ag = safe_tool_call(loader, "connected_app", {"command": "GOOGLECALENDAR_EVENTS_LIST",
        "arguments": json.dumps({"calendarId": "primary", "timeMin": tmin, "timeMax": tmax,
                                 "maxResults": 15, "singleEvents": True, "orderBy": "startTime"})})
    ml = safe_tool_call(loader, "connected_app", {"command": "GMAIL_FETCH_EMAILS",
        "arguments": json.dumps({"maxResults": 8, "query": "in:inbox is:unread"})})
    weather = weather_line()
    news = safe_tool_call(loader, "search_web", {"query": "principales actualités du jour France", "mode": "news"})

    sys = ("Tu es Nova. Rédige un BRIEFING DU MATIN chaleureux et concis en français, à partir "
           "UNIQUEMENT des données réelles fournies. Structure claire :\n"
           "🌅 (petit mot d'accueil)\n📅 Agenda du jour\n📧 Mails (nombre de non-lus + expéditeurs clés)\n"
           "🌤️ Météo\n📰 Actu (3 puces max)\nN'invente RIEN. Si une section est vide ou en erreur, dis-le "
           "en une ligne. Termine par une phrase de motivation courte.")
    user = (f"AGENDA (réel):\n{ag[:1500]}\n\nMAILS (réel):\n{ml[:1200]}\n\n"
            f"MÉTÉO (réel):\n{weather or '(indisponible)'}\n\nACTU (réel):\n{news[:1200]}")
    try:
        return chat([{"role": "system", "content": sys}, {"role": "user", "content": user}], temperature=0.4)
    except Exception as e:
        return f"🌅 Briefing partiel (LLM indisponible : {str(e)[:100]}).\n\nAgenda:\n{ag[:600]}"


async def morning_loop():
    """Boucle proactive : envoie le briefing chaque matin via Telegram (si configuré)."""
    import asyncio
    from datetime import datetime, timedelta
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=config.BRIEFING_HOUR, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep(max(60, (target - now).total_seconds()))
            loop = asyncio.get_running_loop()
            txt = await loop.run_in_executor(None, build_briefing)
            if config.TELEGRAM_TOKEN and getattr(config, "TELEGRAM_CHAT_ID", ""):
                import requests
                from agent.core import _off
                await _off(requests.post,
                           f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
                           json={"chat_id": config.TELEGRAM_CHAT_ID, "text": txt[:4000]}, timeout=20)
                logger.info("[briefing] envoyé via Telegram.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[briefing] boucle: {e}")
            import asyncio as _a
            await _a.sleep(3600)
