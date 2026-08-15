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

_FACTUAL_HINTS = ("actualité", "news", "2024", "2025", "2026", "tendance", "aujourd'hui",
                  "récent", "dernier", "meilleur", "prix de", "cours de", "combien",
                  "qui est", "quand", "où", "statistiques", "chiffres", "météo", "cette semaine")

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


def _smalltalk_reply(message: str) -> str:
    """Réponse conversationnelle directe via le LLM, sans aucun outil."""
    from llm.client import chat
    msgs = [
        {"role": "system", "content": (
            "Tu es Nova, l'assistant personnel de l'utilisateur. Réponds en français, "
            "de façon chaleureuse, brève et naturelle. Ne parle JAMAIS de bourse, "
            "d'actions, de crypto ou de finance sauf si on te le demande explicitement. "
            "Propose simplement ton aide.")},
        {"role": "user", "content": message},
    ]
    return chat(msgs, temperature=0.6)


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
              "Jamais de source inventée : ne cite une source que si un outil te l'a réellement fournie.")
    if app and "connected_app" in tools:
        tools.remove("connected_app"); tools.insert(0, "connected_app")
        system += (" Pour l'agenda, le calendrier, les mails, Slack ou Notion, utilise TOUJOURS l'outil "
                   "connected_app (Composio) — ne cherche JAMAIS ces infos sur le web. "
                   'Exemple agenda : connected_app command="GOOGLECALENDAR_EVENTS_LIST". '
                   'Exemple mails : connected_app command="GMAIL_FETCH_EMAILS".')
    elif factual and "search_web" in tools:
        tools.remove("search_web"); tools.insert(0, "search_web")
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


def _resolve_app_action(message: str):
    """Mappe une demande agenda/mail vers une action Composio + ses arguments. Sinon (None, None)."""
    m = message.lower()
    cal = ("agenda", "calendrier", "calendar", "rendez-vous", "rendez vous", "rdv", "planning",
           "planifie", "événement", "evenement", "réunion", "reunion", "meeting", "semaine",
           "journée", "journee", "aujourd", "demain", "mois")
    mail = ("mail", "mails", "email", "e-mail", "gmail", "boîte mail", "boite mail",
            "inbox", "messagerie", "mes messages")
    if any(h in m for h in cal):
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


def _format_app_result(message: str, action: str, obs: str) -> str:
    """Met en forme les DONNÉES RÉELLES via le LLM — interdiction absolue d'inventer."""
    from llm.client import chat
    sys = (
        "Tu es Nova. On te fournit les DONNÉES RÉELLES renvoyées par une API (JSON brut). "
        "Résume-les clairement en français (tableau ou liste lisible). "
        "RÈGLE ABSOLUE : utilise UNIQUEMENT ces données. N'invente AUCUN événement, mail, "
        "date, heure, lieu ou personne. Si la liste est vide, dis simplement qu'il n'y a rien "
        "de prévu. Termine par au plus 2 suggestions utiles."
    )
    user = f"Demande de l'utilisateur : {message}\n\nDONNÉES RÉELLES [{action}] :\n{obs[:3500]}"
    try:
        return chat([{"role": "system", "content": sys}, {"role": "user", "content": user}], temperature=0.2)
    except Exception:
        return f"Voici les données réelles récupérées :\n\n{obs[:2000]}"


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
    return {"steps": steps, "answer": _format_app_result(message, action, obs), "ok": True}


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
        # Agenda / mails → chemin déterministe (données réelles ou aveu honnête, jamais d'invention)
        loop = asyncio.get_running_loop()
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
        try:
            if _is_smalltalk(message):
                yield sse({"type": "answer", "text": _smalltalk_reply(message)})
                yield sse({"type": "done"}); return
            # Agenda / mails → chemin déterministe (aucune invention possible)
            loop = asyncio.get_running_loop()
            direct = await loop.run_in_executor(None, _direct_app_run, message)
            if direct is not None:
                for st in direct["steps"]:
                    if st["kind"] == "action":
                        yield sse({"type": "step", "kind": "action",
                                   "tool": st["tool"], "q": st.get("label", "")})
                    else:
                        yield sse({"type": "step", "kind": "obs",
                                   "tool": st["tool"], "text": st.get("text", "")})
                yield sse({"type": "answer", "text": direct["answer"]})
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
