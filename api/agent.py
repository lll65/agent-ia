import asyncio
import hmac

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from agent.core import run_agent
from memory import get_memory
from plugins import get_loader
from config import config

router = APIRouter()

# Identifiant de mémoire stable (même profil que l'UI web/Telegram)
_PROFILE_ID = "profil"

_FACTUAL_HINTS = ("actualité", "actualités", "actu", "news", "2024", "2025", "2026", "tendance",
                  "aujourd'hui", "récent", "dernier", "meilleur", "prix de", "cours de", "combien",
                  "qui est", "quand", "où", "statistiques", "chiffres", "météo", "cette semaine",
                  "quoi de neuf", "du jour")

# Petits messages sociaux / chitchat → réponse directe SANS outils (évite que
# "salut" déclenche une recherche web ou un réflexe finance).
_SMALLTALK = {
    "salut", "bonjour", "bonsoir", "coucou", "hello", "hi", "hey", "yo", "wsh",
    "ça va", "ca va", "cava", "comment ça va", "comment ca va", "quoi de neuf",
    "merci", "merci beaucoup", "ok", "d'accord", "daccord", "cool", "super",
    "bye", "au revoir", "à plus", "a plus", "bonne nuit", "bonne journée",
    "test", "tu es là", "tu es la", "t'es là", "tes la",
}


def _is_smalltalk(message: str) -> bool:
    """True si le message est un simple bonjour / remerciement / test court."""
    m = message.strip().lower().rstrip("!?. ")
    if not m:
        return False
    if m in _SMALLTALK:
        return True
    # Court (≤ 4 mots) et commence par une salutation → chitchat
    words = m.split()
    if len(words) <= 4 and words[0] in {"salut", "bonjour", "bonsoir", "coucou",
                                        "hello", "hi", "hey", "yo", "merci", "ok"}:
        return True
    return False


def _smalltalk_messages(message: str) -> list:
    return [
        {"role": "system", "content": (
            "Tu es Nova, l'assistant personnel de l'utilisateur. Réponds en français, "
            "de façon chaleureuse, brève et naturelle. Ne parle JAMAIS de bourse, "
            "d'actions, de crypto ou de finance sauf si on te le demande explicitement. "
            "Propose simplement ton aide.")},
        {"role": "user", "content": message},
    ]


def _smalltalk_reply(message: str) -> str:
    """Réponse conversationnelle directe via le LLM, sans aucun outil."""
    from llm.client import chat
    return chat(_smalltalk_messages(message), temperature=0.6)


# Intention "app connectée" → agenda/mails/calendar/slack/notion : on route vers
# l'outil Composio (connected_app), JAMAIS vers la recherche web.
_APP_HINTS = ("agenda", "calendrier", "calendar", "rendez-vous", "rendez vous", "rdv",
              "planning", "planifie", "événement", "evenement", "réunion", "reunion",
              "mail", "mails", "email", "e-mail", "gmail", "boîte mail", "boite mail",
              "slack", "notion", "mon calendrier", "mes messages")


def _app_intent(message: str) -> bool:
    m = message.lower()
    return any(h in m for h in _APP_HINTS)


def _build_agent_cfg(message: str, name: str = "Nova") -> dict:
    """Prépare la config de l'agent en priorisant le bon outil selon l'intention :
    - Intention app (agenda/mail/calendar/slack/notion) → connected_app en 1er, PAS de web.
    - Sinon question factuelle → recherche web forcée en 1er."""
    tools = list(get_loader().list_all().keys())
    app = _app_intent(message)
    factual = (not app) and any(h in message.lower() for h in _FACTUAL_HINTS)
    system = (f"Tu es {name}, l'assistant personnel de l'utilisateur. Français, concis et actionnable. "
              "Jamais de source inventée : ne cite une source que si un outil te l'a réellement fournie. "
              "N'utilise JAMAIS d'outil crypto/finance (crypto_market, fear_greed, etc.) sauf si la "
              "question porte EXPLICITEMENT sur la bourse, la crypto ou un actif financier.")
    if app and "connected_app" in tools:
        tools.remove("connected_app"); tools.insert(0, "connected_app")
        system += (" Pour l'agenda, le calendrier, les mails, Slack ou Notion, utilise TOUJOURS l'outil "
                   "connected_app (Composio) — ne cherche JAMAIS ces infos sur le web. "
                   'Exemple agenda : connected_app command="GOOGLECALENDAR_EVENTS_LIST". '
                   'Exemple mails : connected_app command="GMAIL_FETCH_EMAILS".')
    elif factual and "search_web" in tools:
        tools.remove("search_web"); tools.insert(0, "search_web")
    if factual:
        system += (" AUTO-VÉRIFICATION : pour chaque affirmation factuelle (chiffre, date, fait), indique "
                   "brièvement la source entre parenthèses (issue des résultats de recherche). Marque d'un ⚠️ "
                   "toute affirmation que les outils n'ont pas confirmée. N'invente jamais de source.")
    return {"id": _PROFILE_ID, "name": name, "system_prompt": system,
            "tools": tools, "force_search": factual, "model": config.LLM_MODEL}


# ── CHEMIN DÉTERMINISTE ANTI-HALLUCINATION (agenda / mails) ────────────────────
# Pour les données PERSONNELLES (agenda, mails), on NE passe PAS par la boucle ReAct
# (le LLM peut inventer un agenda crédible qui n'est jamais détecté comme "stub").
# À la place : appel Composio déterministe → si ça échoue, message honnête (jamais
# d'invention) ; si ça réussit, le LLM se contente de METTRE EN FORME les données réelles.

def _time_bounds(message: str):
    """Renvoie (timeMin, timeMax, libellé) ISO-8601 UTC selon la période demandée
    (aujourd'hui / demain / cette semaine / semaine prochaine / ce mois / mois prochain)."""
    from datetime import datetime, timezone, timedelta
    m = message.lower()
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    z = lambda d: d.isoformat().replace("+00:00", "Z")

    if "après-demain" in m or "apres-demain" in m or "surlendemain" in m:
        d0 = today + timedelta(days=2)
        return z(d0), z(d0 + timedelta(days=1)), "après-demain"
    if "demain" in m:
        d0 = today + timedelta(days=1)
        return z(d0), z(d0 + timedelta(days=1)), "demain"
    if "aujourd" in m or "ma journée" in m or "ma journee" in m or "ce soir" in m:
        return z(today), z(today + timedelta(days=1)), "aujourd'hui"

    # Mois
    if "mois" in m:
        first_this = today.replace(day=1)
        next_month = (first_this.replace(day=28) + timedelta(days=4)).replace(day=1)
        if "prochain" in m or "suivant" in m or "d'après" in m or "d apres" in m:
            start = next_month
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            return z(start), z(end), "le mois prochain"
        return z(first_this), z(next_month), "ce mois-ci"

    # Semaine (défaut). Lundi de la semaine courante.
    monday = today - timedelta(days=today.weekday())
    if ("prochaine" in m or "prochain" in m or "semaine pro" in m or "d'après" in m
            or "d apres" in m or "suivante" in m or "next week" in m):
        start = monday + timedelta(days=7)
        return z(start), z(start + timedelta(days=7)), "la semaine prochaine"
    return z(monday), z(monday + timedelta(days=7)), "cette semaine"


_CAL_CREATE = ("ajoute", "rajoute", "crée", "cree", "créer", "creer", "réserve", "reserve",
               "bloque", "programme", "note un", "note une", "mets un", "mets une",
               "mets-moi", "mets moi", "planifie un", "planifie une", "ajouter un", "ajouter une")


def _clean_event_text(message: str) -> str:
    """Nettoie la phrase pour Google Quick Add (retire les mots 'agenda' et le verbe d'ajout)."""
    import re
    t = " " + message + " "
    for w in ("sur mon agenda", "dans mon agenda", "à mon agenda", "a mon agenda", "sur l'agenda",
              "dans l'agenda", "dans le calendrier", "sur mon calendrier", "mon agenda", "l'agenda"):
        t = re.sub(re.escape(w), " ", t, flags=re.I)
    t = t.strip()
    for w in ("ajoute-moi", "ajoute moi", "ajouter", "ajoute", "rajoute", "crée", "cree", "créer",
              "creer", "réserve", "reserve", "bloque", "programme", "planifie", "mets-moi",
              "mets moi", "mets", "note"):
        t = re.sub(r"^\s*" + w + r"\b", "", t.strip(), flags=re.I)
    return t.strip(" ,:\"'").strip() or message


def _resolve_app_action(message: str):
    """Mappe une demande agenda/mail vers une action Composio + ses arguments. Sinon (None, None).
    IMPORTANT : on n'active l'agenda QUE sur un mot d'agenda EXPLICITE (agenda, rdv, réunion…)
    ou un verbe de planification. Les mots temporels seuls (semaine, aujourd'hui, demain) NE
    déclenchent PAS l'agenda → sinon « l'action X cette semaine » partirait à tort vers le calendrier."""
    import re
    m = message.lower()
    # Mots d'agenda EXPLICITES (déclencheurs forts)
    cal_strong = ("agenda", "calendrier", "calendar", "rendez-vous", "rendez vous", "rdv",
                  "réunion", "reunion", "meeting", "événement", "evenement", "planning",
                  "mes events", "mon planning")
    plan = ("planifie ma", "planifie mon", "organise ma", "organise mon", "prépare ma", "prepare ma")
    day_word = any(w in m for w in ("journée", "journee", "semaine", "jour", "mois"))
    mail = ("mail", "mails", "email", "e-mail", "gmail", "boîte mail", "boite mail",
            "inbox", "messagerie", "mes messages", "mes mails")
    has_clock = bool(re.search(r"\d{1,2}\s*h(\d{2})?\b", m)) or "midi" in m or "minuit" in m

    cal_ctx = any(c in m for c in cal_strong) or (any(p in m for p in plan) and day_word)

    # ➕ CRÉATION d'événement → Quick Add (langage naturel). Exige un mot d'agenda OU une heure précise.
    if any(v in m for v in _CAL_CREATE) and (cal_ctx or has_clock):
        return "GOOGLECALENDAR_QUICK_ADD", {"calendar_id": "primary", "text": _clean_event_text(message)}

    if cal_ctx:
        tmin, tmax, _ = _time_bounds(message)
        return "GOOGLECALENDAR_EVENTS_LIST", {
            "calendarId": "primary", "timeMin": tmin, "timeMax": tmax,
            "maxResults": 25, "singleEvents": True, "orderBy": "startTime",
        }
    if any(h in m for h in mail):
        return "GMAIL_FETCH_EMAILS", {"maxResults": 10, "query": "in:inbox"}
    return None, None


def _looks_like_failure(obs: str) -> bool:
    """True si l'observation Composio est un échec (et surtout PAS des données réelles)."""
    o = (obs or "").strip()
    if not o:
        return True
    low = o.lower()
    if '"successful": false' in low or '"successful":false' in low:
        return True
    if o.startswith("✅"):
        return False  # succès explicite du plugin (même si liste vide)
    hard = ("❌", "⚠️", "🔑", "[self-heal]", "[plugin", "échou", " error", "\"error\"",
            "401", "403", "404", "400", "not connected", "no connected", "not configured",
            "non configuré", "introuvable", "unauthor", "invalid", "not found",
            "permission", "tool_execution", "toolexecution")
    return any(b in low for b in hard)


def _honest_no_access(action: str, obs: str) -> str:
    """Message honnête quand l'accès échoue — JAMAIS d'invention de données."""
    app = ("ton agenda Google" if "CALENDAR" in action else
           "ta boîte Gmail" if "GMAIL" in action else "cette application")
    low = (obs or "").lower()
    key = getattr(config, "COMPOSIO_API_KEY", "") or ""
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ((key[:3] + "…") if key else "(vide)")
    # Cause : aucun compte connecté pour cette entity → on génère le lien OAuth direct
    if ("no connected account" in low or "connectedaccountnotfound" in low.replace("_", "")
            or "no active connection" in low):
        toolkit = ("googlecalendar" if "CALENDAR" in action else
                   "gmail" if "GMAIL" in action else "")
        link, dbg = _composio_connect_link(toolkit) if toolkit else (None, "app inconnue")
        if link:
            return (
                f"🔗 **Dernière étape !** Ta clé fonctionne, il ne reste qu'à **connecter {app}**.\n\n"
                f"👉 **Clique ici pour autoriser ton compte Google :**\n{link}\n\n"
                "Autorise l'accès, puis redemande-moi ton agenda. "
                "_(Ce lien connecte ton compte sous l'identité `default`, celle que j'utilise — tout sera aligné.)_"
            )
        return (
            f"🔌 Presque ! Ta clé marche, mais **aucun compte {app} n'est connecté** pour l'identité `default`.\n\n"
            "**À faire :** Composio → **Toolkits → Google Calendar → Connect account** → quand on te demande "
            "l'**entity / user id**, mets **`default`** → autorise ton compte Google.\n\n"
            f"_(Je n'ai pas pu générer le lien automatiquement : {dbg})_"
        )
    # Cause : clé VALIDE mais sans permission d'exécution (403 tool_execution)
    if ("tool_execution" in low or "toolexecution" in low.replace("_", "")
            or "insufficientpermission" in low.replace("_", "") or "permission" in low):
        return (
            f"🔑 Je n'ai pas pu accéder à {app} — **ta clé Composio est valide mais n'a pas le droit d'exécuter des outils**, donc je ne t'invente rien.\n\n"
            "**Ce qui manque :** la permission **`tool_execution`** (write) sur ta clé `ak_`. "
            "(Bonne nouvelle : la clé et le projet sont bons, il ne reste que ça.)\n\n"
            "**À faire (1 min) :**\n"
            "1. Composio → ton projet → **Clés API**.\n"
            "2. Ouvre ta clé (ou **crée-en une nouvelle en ACCÈS COMPLET**) et accorde-lui la permission "
            "**`tool_execution` = Write** (idéalement « full access » à tous les outils).\n"
            "3. Si tu as créé une **nouvelle** clé : remplace `COMPOSIO_API_KEY` sur Render → **Save** → redéploie.\n"
            "4. Redemande-moi ton agenda."
        )
    # Cause : clé API Composio refusée (401 / APIKey_InvalidAPIKey)
    if "invalid api key" in low or "invalidapikey" in low.replace("_", "") or "401" in low or "refus" in low:
        if key.startswith("ak_"):
            why = (f"La clé que **Render utilise en ce moment** est `{masked}` (type projet `ak_`), et "
                   "Composio la refuse. Causes probables :\n"
                   "  • Render n'a pas encore redéployé avec la nouvelle clé ;\n"
                   "  • la clé ne vient pas du **même projet** que celui où tu connectes Google Agenda ;\n"
                   "  • un espace / retour à la ligne s'est glissé au collage.")
            todo = ("1. **Render → Environment** : `COMPOSIO_API_KEY` = ta clé `ak_` du projet `…first_project` (sans espace).\n"
                    "2. **Manual Deploy → Deploy latest commit** pour forcer la prise en compte.\n"
                    "3. Dans le projet Composio → **Clés API**, confirme que c'est bien cette clé.")
        else:
            why = (f"La clé que **Render utilise en ce moment** est `{masked}` — c'est une clé de type "
                   "**consumer (`ck_`)** ou inconnue. L'API REST a besoin d'une clé de **projet `ak_`**. "
                   "Autrement dit : ta nouvelle clé n'a pas encore été prise en compte par Render.")
            todo = ("1. Récupère la clé **`ak_`** dans ton projet dev Composio → **Clés API**.\n"
                    "2. **Render → Environment** : remplace `COMPOSIO_API_KEY` par cette clé `ak_` → **Save**.\n"
                    "3. **Manual Deploy → Deploy latest commit**, puis attends ~2 min.")
        return (
            f"🔌 Je n'ai pas pu accéder à {app} — **clé Composio refusée**, donc je ne t'invente rien.\n\n"
            f"**Diagnostic :** {why}\n\n"
            f"**À faire :**\n{todo}\n\n"
            "Puis redemande-moi ton agenda. _(Pas besoin de Gmail pour l'agenda : Google Calendar seul suffit.)_"
        )
    return (
        f"🔌 Je n'ai pas pu accéder à {app}, donc je ne t'invente rien.\n\n"
        f"**Raison technique renvoyée par Composio :**\n> {obs[:400]}\n\n"
        "**Pour que ça marche :**\n"
        "1. Va sur composio.dev → vérifie que **Google Calendar / Gmail** est bien connecté (statut vert).\n"
        "2. Sur Render, vérifie que **COMPOSIO_USER_ID** correspond à l'`entity id` de ta connexion "
        "Composio (souvent `default`).\n"
        "3. Réessaie : dès que la connexion répond, je te sortirai tes **vrais** événements/mails."
    )


def _format_app_messages(message: str, action: str, obs: str, is_write: bool = False) -> list:
    if is_write:
        sys = (
            "Tu es Nova. On te fournit la RÉPONSE RÉELLE d'une API après une action (création/"
            "modification). Confirme brièvement et chaleureusement ce qui a été fait, en te basant "
            "UNIQUEMENT sur ces données réelles (titre, date, heure exacts renvoyés). "
            "N'invente rien. Si la réponse ne confirme pas clairement la création, dis-le honnêtement "
            "et propose de réessayer. Format court (1-3 lignes), avec un ✅ si c'est confirmé."
        )
    else:
        sys = (
            "Tu es Nova. On te fournit les DONNÉES RÉELLES renvoyées par une API (JSON brut). "
            "Résume-les clairement en français (tableau ou liste lisible). "
            "RÈGLE ABSOLUE : utilise UNIQUEMENT ces données. N'invente AUCUN événement, mail, "
            "date, heure, lieu ou personne. Si la liste est vide, dis simplement qu'il n'y a rien "
            "de prévu. Termine par au plus 2 suggestions utiles."
        )
    user = f"Demande de l'utilisateur : {message}\n\nDONNÉES RÉELLES [{action}] :\n{obs[:3500]}"
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def _format_app_result(message: str, action: str, obs: str, is_write: bool = False) -> str:
    """Met en forme les DONNÉES RÉELLES via le LLM — interdiction absolue d'inventer."""
    from llm.client import chat
    try:
        return chat(_format_app_messages(message, action, obs, is_write), temperature=0.2)
    except Exception:
        return f"Voici les données réelles récupérées :\n\n{obs[:2000]}"


def _direct_app_prepare(message: str):
    """Comme _direct_app_run mais SANS formater (pour le streaming). Renvoie un dict :
    {steps, done_answer} si terminé (échec → message honnête), ou {steps, action, obs, is_write}."""
    cx = _complex_app_flow(message)
    if cx is not None:
        return {"steps": cx["steps"], "done_answer": cx["done_answer"]}
    action, args = _resolve_app_action(message)
    if not action:
        return None
    import json as _json
    from plugins import get_loader
    from agent.self_heal import safe_tool_call
    obs = safe_tool_call(get_loader(), "connected_app",
                         {"command": action, "arguments": _json.dumps(args)})
    steps = [
        {"kind": "action", "tool": "connected_app", "label": action},
        {"kind": "obs", "tool": "connected_app", "text": str(obs)[:180]},
    ]
    if _looks_like_failure(obs):
        return {"steps": steps, "done_answer": _honest_no_access(action, obs)}
    is_write = any(k in action for k in ("CREATE", "QUICK_ADD", "UPDATE", "DELETE", "PATCH", "SEND"))
    return {"steps": steps, "action": action, "obs": obs, "is_write": is_write}


def _composio_connect_link(app_slug: str):
    """Crée un lien OAuth Composio pour connecter une app sous l'entity COMPOSIO_USER_ID.
    Renvoie (redirect_url|None, debug_str)."""
    import requests, json as _json
    ck = (getattr(config, "COMPOSIO_API_KEY", "") or "").strip()
    user = getattr(config, "COMPOSIO_USER_ID", "default") or "default"
    base = "https://backend.composio.dev"
    h = {"x-api-key": ck, "Content-Type": "application/json"}
    # 1) Trouver l'auth_config de l'app
    try:
        r = requests.get(f"{base}/api/v3/auth_configs?limit=100", headers=h, timeout=20)
        if r.status_code != 200:
            return None, f"auth_configs {r.status_code}: {r.text[:180]}"
        data = r.json()
        items = data.get("items", []) if isinstance(data, dict) else (data or [])
        acid = None
        for it in items:
            if app_slug.lower() in _json.dumps(it).lower():
                acid = it.get("id") or it.get("nano_id") or it.get("uuid")
                if acid:
                    break
        if not acid:
            return None, f"aucune auth config pour '{app_slug}' — crée-la dans Toolkits → {app_slug}"
    except Exception as e:
        return None, f"list exception: {type(e).__name__}: {str(e)[:150]}"
    # 2) Créer le lien de connexion OAuth
    try:
        r = requests.post(f"{base}/api/v3/connected_accounts/link", headers=h,
                          json={"auth_config_id": acid, "user_id": user}, timeout=20)
        if r.status_code in (200, 201):
            b = r.json()
            return (b.get("redirect_url") or b.get("redirectUrl") or b.get("url")), f"ok acid={acid}"
        return None, f"link {r.status_code}: {r.text[:180]}"
    except Exception as e:
        return None, f"link exception: {type(e).__name__}: {str(e)[:150]}"


def _direct_app_run(message: str):
    """Chemin déterministe agenda/mail. Renvoie {'steps','answer','ok'} ou None si non concerné."""
    cx = _complex_app_flow(message)
    if cx is not None:
        return {"steps": cx["steps"], "answer": cx["done_answer"], "ok": True}
    action, args = _resolve_app_action(message)
    if not action:
        return None
    import json as _json
    from plugins import get_loader
    from agent.self_heal import safe_tool_call
    obs = safe_tool_call(get_loader(), "connected_app",
                         {"command": action, "arguments": _json.dumps(args)})
    steps = [
        {"kind": "action", "tool": "connected_app", "label": action},
        {"kind": "obs", "tool": "connected_app", "text": str(obs)[:180]},
    ]
    if _looks_like_failure(obs):
        return {"steps": steps, "answer": _honest_no_access(action, obs), "ok": False}
    is_write = any(k in action for k in ("CREATE", "QUICK_ADD", "UPDATE", "DELETE", "PATCH", "SEND"))
    return {"steps": steps, "answer": _format_app_result(message, action, obs, is_write), "ok": True}


# ── FLUX MULTI-ÉTAPES (Gmail envoi, agenda supprimer, créneau libre) ────────────
def _llm_json(system: str, user: str, temperature: float = 0.1) -> dict:
    """Appelle le LLM et parse un objet JSON (robuste)."""
    from llm.client import chat
    import re, json as _json
    try:
        out = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=temperature)
    except Exception:
        return {}
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return {}
    for cand in (m.group(0), m.group(0).replace("'", '"')):
        try:
            return _json.loads(cand)
        except Exception:
            continue
    return {}


def _tool(name_cmd: str, args: dict) -> str:
    import json as _json
    from plugins import get_loader
    from agent.self_heal import safe_tool_call
    return safe_tool_call(get_loader(), "connected_app",
                          {"command": name_cmd, "arguments": _json.dumps(args)})


def _gmail_send_flow(message: str):
    ex = _llm_json(
        "Extrais du message un email à envoyer. Réponds en JSON STRICT : "
        '{"to":"email ou nom","subject":"...","body":"..."}. '
        "Si le corps n'est pas explicite, rédige-le poliment en français d'après l'intention.",
        message)
    to = (ex.get("to") or "").strip()
    subject = (ex.get("subject") or "").strip() or "(sans objet)"
    body = (ex.get("body") or "").strip()
    steps = [{"kind": "action", "tool": "connected_app", "label": "GMAIL_SEND_EMAIL"}]
    if not body:
        return {"steps": steps, "done_answer": "Je n'ai pas saisi le contenu du mail. Reformule : « envoie un mail à x@y.com pour dire que … »."}
    if "@" not in to and to:
        fetch = _tool("GMAIL_FETCH_EMAILS", {"maxResults": 20, "query": "in:anywhere"})
        addr = _llm_json('À partir de ces mails (JSON réel), donne l\'adresse email de la personne. '
                         'JSON STRICT {"email":"..."} ou {"email":""} si absente.',
                         f"Personne: {to}\n\nMAILS:\n{fetch[:2500]}").get("email", "")
        if addr and "@" in addr:
            to = addr
    if "@" not in to:
        return {"steps": steps, "done_answer": f"Je n'ai pas l'adresse email de « {to or '?'} ». Donne-la-moi (ex : prenom@domaine.com) et j'envoie tout de suite."}
    obs = _tool("GMAIL_SEND_EMAIL", {"recipient_email": to, "subject": subject, "body": body})
    steps.append({"kind": "obs", "tool": "connected_app", "text": str(obs)[:160]})
    if _looks_like_failure(obs):
        return {"steps": steps, "done_answer": _honest_no_access("GMAIL_SEND_EMAIL", obs)}
    return {"steps": steps, "done_answer": f"✅ Mail envoyé à **{to}**.\n\n**Objet :** {subject}\n\n{body[:500]}"}


def _calendar_delete_flow(message: str):
    m = message.lower()
    period = message if any(w in m for w in ("semaine", "mois", "demain", "aujourd", "prochaine")) else "ce mois"
    tmin, tmax, _ = _time_bounds(period)
    lst = _tool("GOOGLECALENDAR_EVENTS_LIST", {"calendarId": "primary", "timeMin": tmin, "timeMax": tmax,
                                               "maxResults": 30, "singleEvents": True, "orderBy": "startTime"})
    steps = [{"kind": "action", "tool": "connected_app", "label": "GOOGLECALENDAR_EVENTS_LIST"},
             {"kind": "obs", "tool": "connected_app", "text": str(lst)[:160]}]
    if _looks_like_failure(lst):
        return {"steps": steps, "done_answer": _honest_no_access("GOOGLECALENDAR_EVENTS_LIST", lst)}
    pick = _llm_json("À partir de la liste d'événements (JSON réel), identifie celui à SUPPRIMER. "
                     'JSON STRICT {"event_id":"...","title":"..."} ou {"event_id":""} si rien ne correspond.',
                     f"Demande: {message}\n\nÉVÉNEMENTS:\n{lst[:2600]}")
    eid = (pick.get("event_id") or "").strip()
    if not eid:
        return {"steps": steps, "done_answer": "Je n'ai pas trouvé l'événement à supprimer. Précise son intitulé ou sa date."}
    obs = _tool("GOOGLECALENDAR_DELETE_EVENT", {"calendar_id": "primary", "event_id": eid})
    steps.append({"kind": "obs", "tool": "connected_app", "text": str(obs)[:160]})
    if _looks_like_failure(obs):
        return {"steps": steps, "done_answer": _honest_no_access("GOOGLECALENDAR_DELETE_EVENT", obs)}
    return {"steps": steps, "done_answer": f"✅ Événement supprimé : **{pick.get('title', '(sans titre)')}**."}


def _free_slot_flow(message: str):
    tmin, tmax, label = _time_bounds(message)
    lst = _tool("GOOGLECALENDAR_EVENTS_LIST", {"calendarId": "primary", "timeMin": tmin, "timeMax": tmax,
                                               "maxResults": 40, "singleEvents": True, "orderBy": "startTime"})
    steps = [{"kind": "action", "tool": "connected_app", "label": "GOOGLECALENDAR_EVENTS_LIST"},
             {"kind": "obs", "tool": "connected_app", "text": str(lst)[:160]}]
    if _looks_like_failure(lst):
        return {"steps": steps, "done_answer": _honest_no_access("GOOGLECALENDAR_EVENTS_LIST", lst)}
    ans = _format_app_result(
        f"Donne mes créneaux LIBRES pour {label} (plage 8h–20h), en te basant UNIQUEMENT sur ces "
        f"événements réels. Liste les plages horaires libres, jour par jour.",
        "GOOGLECALENDAR_EVENTS_LIST", lst, is_write=False)
    return {"steps": steps, "done_answer": ans}


def _complex_app_flow(message: str):
    """Détecte et exécute les actions multi-étapes. Renvoie {steps, done_answer} ou None."""
    m = message.lower()
    mail_verb = any(v in m for v in ("envoie", "envoyer", "écris", "ecris", "rédige", "redige", "réponds", "reponds"))
    if (("mail" in m or "email" in m or "e-mail" in m or "courriel" in m) and mail_verb) \
            or m.startswith("réponds à") or m.startswith("reponds a"):
        return _gmail_send_flow(message)
    cal_words = ("agenda", "rdv", "rendez-vous", "rendez vous", "réunion", "reunion",
                 "événement", "evenement", "calendrier", "meeting")
    if any(v in m for v in ("supprime", "annule", "efface", "retire")) and any(c in m for c in cal_words):
        return _calendar_delete_flow(message)
    if any(w in m for w in ("libre", "créneau", "creneau", "disponible", "dispo", "quand suis-je")) \
            and any(c in m for c in ("agenda", "semaine", "journée", "journee", "aujourd", "demain", "créneau", "creneau", "rdv", "réunion")):
        return _free_slot_flow(message)
    return None


def _is_briefing(message: str) -> bool:
    m = message.lower()
    return ("briefing" in m or "brief du matin" in m or "quoi de neuf ce matin" in m
            or "résumé du matin" in m or "resume du matin" in m
            or ("résume" in m and "journée" in m) or ("resume" in m and "journee" in m))


def _check_key(provided: str):
    """Vérifie la clé de la passerelle /ask (comparaison à temps constant, bytes).
    En bytes → supporte les caractères accentués/non-ASCII dans la clé."""
    if not config.AGENT_API_KEY:
        raise HTTPException(status_code=501, detail="Passerelle désactivée : définis AGENT_API_KEY.")
    a = str(provided or "").encode("utf-8")
    b = str(config.AGENT_API_KEY).encode("utf-8")
    if not hmac.compare_digest(a, b):
        raise HTTPException(status_code=401, detail="Clé invalide.")


async def _ask_agent(message: str) -> str:
    """Fait tourner l'agent complet (outils + mémoire persistante + recherche web forcée).
    Robuste : toute erreur est renvoyée comme message lisible (jamais de 500)."""
    import logging
    try:
        if _is_smalltalk(message):
            return _smalltalk_reply(message)
        loop = asyncio.get_running_loop()
        if _is_briefing(message):
            from agent.briefing import build_briefing
            return await loop.run_in_executor(None, build_briefing)
        # Agenda / mails → chemin déterministe (données réelles ou aveu honnête, jamais d'invention)
        direct = await loop.run_in_executor(None, _direct_app_run, message)
        if direct is not None:
            return direct["answer"]
        cfg = _build_agent_cfg(message, "Nova")
        result = await run_agent(message, cfg, _PROFILE_ID)
        answer = (result or {}).get("answer", "") if isinstance(result, dict) else str(result)
        return answer or "(réponse vide)"
    except Exception as e:
        logging.getLogger(__name__).error("Erreur /ask", exc_info=True)
        return f"❌ Erreur agent : {type(e).__name__}: {str(e)[:400]}"


class AskRequest(BaseModel):
    message: str
    key: Optional[str] = None


@router.post("/ask")
async def ask_post(req: AskRequest, request: Request):
    """Passerelle universelle : parle à l'agent depuis n'importe quel appareil (Siri, n8n, webhook)."""
    key = req.key or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    _check_key(key)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message vide.")
    answer = await _ask_agent(req.message.strip())
    return {"answer": answer}


@router.get("/ask/stream")
async def ask_stream(q: str = "", key: str = ""):
    """Streaming SSE : émet en direct les étapes du raisonnement + la réponse (pour /nova)."""
    import json as _json
    from fastapi.responses import StreamingResponse

    _check_key(key)
    message = (q or "").strip()

    async def gen():
        def sse(obj):
            return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"
        if not message:
            yield sse({"type": "answer", "text": "Message vide."}); yield sse({"type": "done"}); return
        loop = asyncio.get_running_loop()

        async def _stream_llm(messages, temp):
            """Diffuse une réponse LLM token par token (dans un thread, non bloquant)."""
            from llm.client import chat_stream
            import queue as _q
            box = _q.Queue()
            def worker():
                try:
                    for tok in chat_stream(messages, temperature=temp):
                        box.put(("t", tok))
                except Exception as e:
                    box.put(("t", f"❌ {str(e)[:120]}"))
                box.put(("end", None))
            loop.run_in_executor(None, worker)
            acc = ""
            while True:
                kind, val = await loop.run_in_executor(None, box.get)
                if kind == "end":
                    break
                acc += val
                yield val
            yield_acc[0] = acc

        yield_acc = [""]
        try:
            # 1) Chitchat → streamé directement
            if _is_smalltalk(message):
                async for tok in _stream_llm(_smalltalk_messages(message), 0.6):
                    yield sse({"type": "token", "t": tok})
                yield sse({"type": "answer", "text": yield_acc[0], "final": True})
                yield sse({"type": "done"}); return
            # 1b) Briefing du matin (agenda + mails + météo + actu)
            if _is_briefing(message):
                yield sse({"type": "step", "kind": "action", "tool": "connected_app", "q": "briefing du matin"})
                from agent.briefing import build_briefing
                txt = await loop.run_in_executor(None, build_briefing)
                yield sse({"type": "answer", "text": txt})
                yield sse({"type": "done"}); return
            # 2) Agenda / mails → chemin déterministe (aucune invention), réponse streamée
            direct = await loop.run_in_executor(None, _direct_app_prepare, message)
            if direct is not None:
                for st in direct["steps"]:
                    if st["kind"] == "action":
                        yield sse({"type": "step", "kind": "action", "tool": st["tool"], "q": st.get("label", "")})
                    else:
                        yield sse({"type": "step", "kind": "obs", "tool": st["tool"], "text": st.get("text", "")})
                if direct.get("done_answer") is not None:
                    yield sse({"type": "answer", "text": direct["done_answer"]})
                    yield sse({"type": "done"}); return
                msgs = _format_app_messages(message, direct["action"], direct["obs"], direct["is_write"])
                async for tok in _stream_llm(msgs, 0.2):
                    yield sse({"type": "token", "t": tok})
                yield sse({"type": "answer", "text": yield_acc[0], "final": True})
                yield sse({"type": "done"}); return
            from agent.core import run_agent_stream
            cfg = _build_agent_cfg(message, "Nova")
            async for step in run_agent_stream(message, cfg, _PROFILE_ID):
                t = step.get("type")
                if t == "final":
                    yield sse({"type": "answer", "text": step.get("answer", "")})
                elif t == "action":
                    p = step.get("params", {}) or {}
                    q2 = p.get("query") or p.get("command") or ""
                    yield sse({"type": "step", "kind": "action", "tool": step.get("tool", ""), "q": str(q2)[:80]})
                elif t == "observation":
                    yield sse({"type": "step", "kind": "obs", "tool": step.get("tool", ""),
                               "text": str(step.get("result", ""))[:140]})
                elif t == "thought":
                    yield sse({"type": "step", "kind": "thought", "text": str(step.get("text", ""))[:140]})
            yield sse({"type": "done"})
        except Exception as e:
            yield sse({"type": "answer", "text": f"❌ Erreur : {type(e).__name__}: {str(e)[:300]}"})
            yield sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/briefing")
async def briefing_ep(key: str = ""):
    """Briefing du matin à la demande (agenda + mails + météo + actu)."""
    _check_key(key)
    loop = asyncio.get_running_loop()
    from agent.briefing import build_briefing
    txt = await loop.run_in_executor(None, build_briefing)
    return {"briefing": txt}


@router.get("/tts/status")
async def tts_status():
    """Indique si la voix premium (ElevenLabs) est active."""
    return {"enabled": bool(getattr(config, "ELEVENLABS_API_KEY", ""))}


@router.get("/tts")
async def tts(text: str = "", key: str = ""):
    """Voix premium ElevenLabs → renvoie un audio MP3. Repli navigateur si pas de clé."""
    _check_key(key)
    from fastapi.responses import Response, JSONResponse
    ek = getattr(config, "ELEVENLABS_API_KEY", "")
    if not ek:
        return JSONResponse({"ok": False, "reason": "no_key"})
    txt = (text or "").strip()[:900]
    if not txt:
        return JSONResponse({"ok": False, "reason": "empty"}, status_code=400)
    import requests
    vid = getattr(config, "ELEVENLABS_VOICE_ID", "")
    model = getattr(config, "ELEVENLABS_MODEL", "eleven_multilingual_v2")
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": ek, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": txt, "model_id": model,
                  "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.25}},
            timeout=30)
        if r.status_code == 200:
            return Response(content=r.content, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
        return JSONResponse({"ok": False, "status": r.status_code, "body": r.text[:200]}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)


@router.get("/diag/composio")
async def diag_composio(key: str = ""):
    """Auto-test Composio : appelle l'API et renvoie la réponse BRUTE (pour diagnostic).
    Ouvre /agent/diag/composio?key=TA_CLE_AGENT et colle le JSON obtenu."""
    _check_key(key)
    import requests
    ck = (getattr(config, "COMPOSIO_API_KEY", "") or "")
    ck_s = ck.strip()
    meta = {
        "key_present": bool(ck_s),
        "key_len_raw": len(ck),
        "key_len_stripped": len(ck_s),
        "key_prefix": ck_s[:6],
        "key_suffix": ck_s[-4:] if len(ck_s) >= 4 else "",
        "had_whitespace": ck != ck_s,
        "user_id": getattr(config, "COMPOSIO_USER_ID", "default"),
    }
    base = "https://backend.composio.dev"
    hdr_name = "x-consumer-api-key" if ck_s.startswith("ck_") else "x-api-key"
    probes = {}

    def _probe(label, method, url, headers, body=None):
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=20)
            else:
                r = requests.post(url, headers=headers, json=body, timeout=20)
            probes[label] = {"status": r.status_code, "body": r.text[:900]}
        except Exception as e:
            probes[label] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    h = {hdr_name: ck_s, "Content-Type": "application/json"}
    _probe("A_toolkits_GET", "GET", f"{base}/api/v3/toolkits?limit=1", h)
    _probe("B_execute_calendar", "POST", f"{base}/api/v3/tools/execute/GOOGLECALENDAR_EVENTS_LIST",
           h, {"user_id": meta["user_id"], "arguments": {}})
    # Essai en-tête alternatif (au cas où le serveur attend x-api-key même pour ck_)
    if hdr_name != "x-api-key":
        _probe("C_toolkits_xapikey", "GET", f"{base}/api/v3/toolkits?limit=1",
               {"x-api-key": ck_s, "Content-Type": "application/json"})
    return {"meta": meta, "header_used": hdr_name, "probes": probes}


@router.get("/usage")
async def usage():
    """Consommation de tokens du jour (Cerebras + Groq) — pour la barre sur /nova.
    Lecture durable (Supabase si configuré), donc fiable après redéploiement."""
    from llm import usage as U
    out, tu, tl = {}, 0, 0
    for p in ("cerebras", "groq"):
        try:
            used, limit = U.get_usage(p)
        except Exception:
            used, limit = 0, U.LIMITS.get(p, 0)
        out[p] = {"used": int(used), "limit": int(limit)}
        tu += int(used); tl += int(limit)
    out["total"] = {"used": tu, "limit": tl}
    return out


@router.get("/diag/connect")
async def diag_connect(key: str = "", app: str = "googlecalendar"):
    """Connexion OAuth en 1 clic : redirige direct vers l'écran d'autorisation Google.
    Ex : /agent/diag/connect?key=TA_CLE_AGENT&app=googlecalendar (ou app=gmail)."""
    _check_key(key)
    from fastapi.responses import RedirectResponse
    link, dbg = _composio_connect_link(app)
    if link:
        return RedirectResponse(url=link)
    return {"ok": False, "app": app, "detail": dbg}


@router.get("/ask")
async def ask_get(q: str = "", key: str = ""):
    """Version GET (pratique pour Siri Raccourcis / navigateur) : /agent/ask?q=...&key=..."""
    _check_key(key)
    if not q.strip():
        raise HTTPException(status_code=400, detail="Paramètre q vide.")
    answer = await _ask_agent(q.strip())
    return {"answer": answer}

DEFAULT_AGENT = {
    "id": "default",
    "name": "Agent Principal",
    "description": "Agent IA personnel polyvalent — peut créer d'autres agents, coder, chercher et créer des vidéos.",
    "system_prompt": "Tu es un agent IA personnel puissant. Tu aides à créer des projets, écrire du code, chercher des informations et créer du contenu.",
    "model": config.OLLAMA_MODEL,
}


class ChatRequest(BaseModel):
    message: str
    agent_id: Optional[str] = "default"
    show_steps: Optional[bool] = False


class ChatResponse(BaseModel):
    answer: str
    agent_id: str
    iterations: int
    steps: Optional[list] = None
    memory_count: Optional[int] = None


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    agent_id = req.agent_id or "default"

    if agent_id == "default":
        agent_config = {**DEFAULT_AGENT, "tools": list(get_loader().list_all().keys())}
    else:
        from orchestrator import get_registry
        agent_config = get_registry().get(agent_id)
        if not agent_config:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' introuvable.")

    result = await run_agent(req.message, agent_config, agent_id)
    mem = get_memory()

    # Compte les souvenirs quel que soit le backend (Supabase ou ChromaDB local)
    mem_count = None
    try:
        if mem.backend:
            mem_count = mem.backend.count(agent_id)
        elif mem.chroma and mem.chroma.available:
            mem_count = mem.chroma.count(agent_id)
    except Exception:
        mem_count = None

    return ChatResponse(
        answer=result["answer"],
        agent_id=agent_id,
        iterations=result["iterations"],
        steps=result["steps"] if req.show_steps else None,
        memory_count=mem_count,
    )


@router.get("/{agent_id}/history")
async def agent_history(agent_id: str, limit: int = 20):
    return {"agent_id": agent_id, "history": get_memory().recall_recent(agent_id, limit)}


@router.delete("/{agent_id}/memory")
async def clear_memory(agent_id: str):
    get_memory().clear(agent_id)
    return {"message": f"Mémoire de '{agent_id}' effacée."}


@router.get("/{agent_id}/summary")
async def get_summary(agent_id: str):
    summary = get_memory().get_summary(agent_id)
    return {"agent_id": agent_id, "summary": summary or "Aucun résumé disponible."}


@router.get("/tools/list")
async def available_tools():
    return {"tools": get_loader().list_all()}
