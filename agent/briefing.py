"""
Briefing du matin — compose un résumé à partir de DONNÉES RÉELLES :
agenda du jour (Composio), mails non lus (Composio), météo (open-meteo, gratuit, sans clé),
actu (recherche web). Aucune invention : si une source échoue, on le dit.
"""
import json
import logging
import re

from config import config

logger = logging.getLogger(__name__)

_WCODE = {0: "ciel clair", 1: "peu nuageux", 2: "nuageux", 3: "couvert", 45: "brouillard",
          48: "brouillard givrant", 51: "bruine", 61: "pluie", 63: "pluie", 65: "forte pluie",
          71: "neige", 80: "averses", 95: "orage"}


# ⚠️ Son briefing du matin annonçait la météo de PARIS. Il habite dans les
# Pyrénées-Atlantiques — il l'a dit à Nova, qui l'a retenu dans son profil, et qui
# lisait quand même une variable d'environnement figée à « Paris » par défaut.
# Une info fausse tous les matins, sur la seule ligne du briefing qu'on regarde
# vraiment avant de sortir. Ce que Nova SAIT de lui doit primer sur un réglage
# jamais changé : c'est tout l'intérêt de retenir quelque chose.
_INDICES_VILLE = ("habite", "vis à", "vis a", "vit à", "vit a", "réside", "reside",
                  "domicil", "j'habite", "ma ville", "chez moi")


def ville_de_lohan() -> str:
    """Sa ville : d'abord ce qu'il a dit à Nova, sinon le réglage, sinon Paris."""
    try:
        from agent.profile import list_facts
        for f in (list_facts() or []):
            texte = str(f.get("texte") or "")
            if str(f.get("cat") or "").lower() == "ville" or any(
                    i in texte.lower() for i in _INDICES_VILLE):
                # « j'habite à Pau » → « Pau ». On garde le dernier mot significatif.
                m = re.search(r"(?:à|a|de|sur)\s+([A-ZÀ-Ý][\wÀ-ÿ'’-]{2,}(?:[- ][A-ZÀ-Ý][\wÀ-ÿ'’-]+)*)",
                              texte)
                if m:
                    return m.group(1).strip()
                mots = [x for x in re.findall(r"[A-ZÀ-Ý][\wÀ-ÿ'’-]{2,}", texte)]
                if mots:
                    return mots[-1]
    except Exception as e:
        logger.info(f"[briefing] ville du profil illisible ({type(e).__name__})")
    return getattr(config, "BRIEFING_CITY", "Paris") or "Paris"


def weather_line() -> str:
    """Météo du jour pour SA ville (open-meteo, gratuit)."""
    import requests
    city = ville_de_lohan()
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
           "en une ligne. Termine par une phrase de motivation courte. "
           + __import__("agent.system_prompt", fromlist=["TUTOIEMENT"]).TUTOIEMENT)
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
            # Même piège : sans fuseau, le briefing « de 7h » partait à 9h à Paris.
            from agent.horloge import maintenant
            now = maintenant()
            target = now.replace(hour=config.BRIEFING_HOUR, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep(max(60, (target - now).total_seconds()))
            loop = asyncio.get_running_loop()
            txt = await loop.run_in_executor(None, build_briefing)
            # Même piège que pour les automatisations : exiger TELEGRAM_CHAT_ID faisait
            # disparaître le briefing en silence. send_message vise le chat du
            # propriétaire — avoir dit /start au bot une fois suffit.
            if config.TELEGRAM_TOKEN:
                from bots.telegram_push import send_message
                from agent.core import _off
                if await _off(send_message, txt[:4000]):
                    logger.info("[briefing] envoyé via Telegram.")
                else:
                    logger.warning("[briefing] produit mais NON envoyé : aucun chat "
                                   "Telegram connu (envoie /start au bot).")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[briefing] boucle: {e}")
            import asyncio as _a
            await _a.sleep(3600)
