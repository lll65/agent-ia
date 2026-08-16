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


# Faits personnels : l'utilisateur se présente / donne une info sur lui.
# → réponse naturelle courte + mémorisation. JAMAIS d'analyse ni de rapport.
_PERSONAL_RE = (
    r"\bj'?ai\s+\d{1,3}\s*ans?\b", r"\bje\s+m'?appelle\b", r"\bmon\s+(pr[ée]nom|nom)\s+(c'?est|est)\b",
    r"\bj'?habite\b", r"\bje\s+vis\s+[àa]\b", r"\bje\s+suis\s+(un|une|en|au|à|a|étudiant|etudiant|lycéen|lyceen|développeur|developpeur)\b",
    r"\bje\s+travaille\b", r"\bj'?aime\b", r"\bje\s+pr[ée]f[èe]re\b", r"\bje\s+d[ée]teste\b",
    r"\bmon\s+(anniversaire|objectif|projet|but)\b", r"\bje\s+fais\s+(du|de la|des)\b",
)


def _is_personal_fact(message: str) -> bool:
    import re
    m = message.strip().lower()
    if len(m.split()) > 25:
        return False
    if "?" in m:
        return False  # une question n'est pas une simple confidence
    return any(re.search(p, m) for p in _PERSONAL_RE)


def _is_smalltalk(message: str) -> bool:
    """True si le message est un simple bonjour / remerciement / test court,
    ou une info personnelle que l'utilisateur partage (pas une demande de travail)."""
    m = message.strip().lower().rstrip("!?. ")
    if not m:
        return False
    if m in _SMALLTALK:
        return True
    if _is_personal_fact(message):
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
            "Tu es Nova, l'assistante personnelle de l'utilisateur. Réponds en français, "
            "de façon chaleureuse, BRÈVE (1 à 3 phrases maximum) et naturelle, comme un ami.\n"
            "INTERDICTIONS ABSOLUES :\n"
            "- Ne parle JAMAIS de bourse, actions, ETF, crypto, marchés, investissement, épargne "
            "ou placements. Même si l'utilisateur mentionne son âge ou de l'argent.\n"
            "- N'invente JAMAIS de chiffre, de cours, d'indice ou de statistique.\n"
            "- Pas de titres, pas de listes à puces, pas de plan d'action, pas de rapport.\n"
            "Si l'utilisateur te donne une info sur lui (âge, prénom, ville, goûts), accuse simplement "
            "réception avec chaleur et dis que tu le retiens. Tu peux poser UNE question courte ou "
            "proposer ton aide en une phrase.")},
        {"role": "user", "content": message},
    ]


def _has_invented_market_data(text: str) -> bool:
    """Filet de sécurité : dans le chemin conversationnel, AUCUN outil n'est appelé.
    Donc tout cours/indice/prix cité y est forcément inventé → on le détecte."""
    import re
    t = (text or "").lower()
    pats = (r"s&p\s*500", r"cac\s*40", r"nasdaq", r"dow jones", r"bitcoin", r"\bbtc\b",
            r"\beth\b", r"ethereum", r"\bpts\b", r"points?\b.{0,12}\d{3,}")
    hit = any(re.search(p, t) for p in pats)
    return hit and bool(re.search(r"\d[\d\s.,]{2,}", t))


def _remember_fact(message: str) -> None:
    """Mémorise une info personnelle donnée par l'utilisateur (non fatal)."""
    if not _is_personal_fact(message):
        return
    try:
        get_memory().remember(_PROFILE_ID, "user", message.strip()[:200])
    except Exception:
        pass


def _smalltalk_reply(message: str) -> str:
    """Réponse conversationnelle directe via le LLM, sans aucun outil."""
    from llm.client import chat
    _remember_fact(message)
    out = chat(_smalltalk_messages(message), temperature=0.6)
    if _has_invented_market_data(out) and not _finance_intent(message):
        # Dérive détectée (chiffres de marché non demandés et non sourcés) → on régénère.
        msgs = _smalltalk_messages(message) + [
            {"role": "assistant", "content": out},
            {"role": "user", "content": (
                "Ta réponse contient des chiffres de marché que personne ne t'a demandés et que "
                "tu ne peux pas connaître (aucun outil). Réponds à nouveau : 1 à 3 phrases, "
                "chaleureuses, SANS aucun chiffre, SANS parler de bourse/crypto/investissement.")},
        ]
        try:
            out2 = chat(msgs, temperature=0.3)
            if out2 and not _has_invented_market_data(out2):
                return out2
        except Exception:
            pass
    return out


# Apps connectées (Composio). Mots-clés → toolkit slug. Ajouter une app = ajouter une ligne :
# Nova découvre ensuite TOUTE seule les actions disponibles via l'API Composio.
_TOOLKITS = {
    "googlecalendar": ("agenda", "calendrier", "calendar", "rendez-vous", "rendez vous", "rdv",
                       "planning", "événement", "evenement", "réunion", "reunion", "meeting"),
    "gmail":          ("mail", "mails", "email", "e-mail", "gmail", "boîte mail", "boite mail",
                       "inbox", "messagerie", "courriel"),
    "linear":         ("linear", "ticket", "tickets", "issue", "issues", "bug tracker",
                       "sprint", "backlog", "tâche linear", "tache linear"),
    "canva":          ("canva", "design", "visuel", "affiche", "flyer", "présentation canva",
                       "presentation canva", "maquette"),
    "notion":         ("notion", "page notion", "base notion", "wiki"),
    "slack":          ("slack", "message slack", "canal slack", "channel"),
    "github":         ("github", "dépôt", "depot", "repo", "pull request", "commit"),
    "googledrive":    ("drive", "google drive", "mes fichiers", "document drive"),
    "googlesheets":   ("sheets", "google sheets", "tableur", "feuille de calcul"),
    "googledocs":     ("docs", "google docs", "document texte"),
    "trello":         ("trello", "board", "tableau trello"),
    "spotify":        ("spotify", "musique", "playlist"),
    "youtube":        ("youtube", "vidéo youtube", "video youtube"),
    "whatsapp":       ("whatsapp", "wa message"),
    "discord":        ("discord",),
    "twitter":        ("twitter", "tweet", "x.com"),
    "hubspot":        ("hubspot", "crm"),
    "airtable":       ("airtable",),
    "asana":          ("asana",),
    "jira":           ("jira",),
}

# Ordre de priorité : les slugs les plus spécifiques d'abord (évite que "message" prenne le pas)
_TOOLKIT_ORDER = ("linear", "canva", "notion", "slack", "github", "googlesheets", "googledocs",
                  "googledrive", "trello", "spotify", "youtube", "whatsapp", "discord", "twitter",
                  "hubspot", "airtable", "asana", "jira", "googlecalendar", "gmail")


def _detect_toolkit(message: str):
    """Renvoie le slug de l'app concernée par le message, ou None."""
    m = message.lower()
    for slug in _TOOLKIT_ORDER:
        if any(k in m for k in _TOOLKITS.get(slug, ())):
            return slug
    return None


def _app_intent(message: str) -> bool:
    return _detect_toolkit(message) is not None


# Outils finance : RETIRÉS de la boîte à outils sauf demande explicite de finance.
# (Les exposer en permanence poussait le modèle à parler bourse/crypto à tout propos.)
_FINANCE_TOOLS = {"analyze_stock", "market_dashboard", "compare_stocks", "get_market_news",
                  "crypto_market", "fear_greed", "currency_rates"}
_FINANCE_WORDS = ("bourse", "action ", "actions", "titre", "etf", "crypto", "bitcoin", "btc",
                  "ethereum", "marché", "marchés", "cac", "nasdaq", "s&p", "trading", "trader",
                  "investir", "investissement", "placement", "portefeuille", "pea", "dividende",
                  "cours de", "valneva", "ticker", "capitalisation", "boursier")


def _finance_intent(message: str) -> bool:
    m = message.lower()
    return any(w in m for w in _FINANCE_WORDS)


def _build_agent_cfg(message: str, name: str = "Nova") -> dict:
    """Prépare la config de l'agent en priorisant le bon outil selon l'intention :
    - Intention app (agenda/mail/calendar/slack/notion) → connected_app en 1er, PAS de web.
    - Sinon question factuelle → recherche web forcée en 1er.
    - Outils finance masqués sauf demande explicite (anti-dérive bourse)."""
    tools = list(get_loader().list_all().keys())
    app = _app_intent(message)
    fin = _finance_intent(message)
    if not fin:
        tools = [t for t in tools if t not in _FINANCE_TOOLS]
    factual = (not app) and any(h in message.lower() for h in _FACTUAL_HINTS)
    system = (f"Tu es {name}, l'assistante personnelle de l'utilisateur. Français, chaleureuse, "
              "concise. Réponds UNIQUEMENT à ce qui est demandé, avec une longueur proportionnée "
              "(remarque anodine → 1-2 phrases). "
              "Jamais de source inventée : ne cite une source que si un outil te l'a réellement fournie. "
              "N'invente JAMAIS un cours, un indice, un prix ou une statistique.")
    if not fin:
        system += (" INTERDIT : ne parle pas de bourse, actions, crypto, marchés, investissement, "
                   "épargne ou placements — l'utilisateur ne l'a pas demandé.")
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
    # Spécialiste mobilisé → constellation + ton adapté au domaine
    try:
        from agent.squad import pick_agent, get_agent, record
        aid = pick_agent(message)
        if aid != "nova":
            spec = get_agent(aid)
            record(aid, "", message[:50])
            system += f" Tu mobilises ton spécialiste **{spec['name']}** ({spec['desc']})."
    except Exception:
        pass
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
    # ⚠️ Composio renvoie parfois HTTP 200 avec l'erreur DANS le payload
    # (ex. {"http_error": "401 Client Error: Unauthorized for url: https://api.canva.com/..."}).
    # Il faut donc inspecter le contenu même quand le plugin a préfixé un ✅.
    embedded = ("http_error", '"error"', "client error", "unauthorized", "forbidden",
                "invalid_grant", "token expired", "insufficient", "\"status\": 4", "\"status\":4")
    if any(b in low for b in embedded):
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
    # Cause : l'APP elle-même refuse (token OAuth expiré ou scopes insuffisants).
    # Reconnaissable au fait que l'erreur cite l'API de l'app (api.canva.com, api.github.com…).
    head = (action or "").split("_", 1)[0].lower()
    tk = head if head in _TOOLKITS else ""
    if ("http_error" in low or "api." in low) and ("401" in low or "unauthorized" in low
                                                   or "403" in low or "forbidden" in low):
        nice = tk.capitalize() if tk else "l'application"
        scope_hint = ""
        if tk == "canva":
            scope_hint = ("\n\n💡 Pour Canva, la connexion doit inclure les autorisations d'**écriture** "
                          "(`design:content:write`) — une connexion en lecture seule renvoie exactement cette erreur.")
        link, _dbg = _composio_connect_link(tk) if tk else (None, "")
        reco = (f"\n\n👉 **Reconnecte {nice} en un clic :**\n{link}" if link else
                f"\n\n👉 Sur Composio → **Toolkits → {nice}** : supprime la connexion puis **reconnecte-la** "
                "en acceptant toutes les autorisations.")
        return (
            f"🔐 **{nice} a refusé l'accès** (erreur d'autorisation renvoyée par son API, pas par moi).\n\n"
            f"Ton compte est bien relié, mais le **jeton d'accès est expiré ou trop limité**.{scope_hint}{reco}\n\n"
            f"_Détail technique : {obs[:220]}_"
        )
    # Cause : aucun compte connecté pour cette entity → on génère le lien OAuth direct
    if ("no connected account" in low or "connectedaccountnotfound" in low.replace("_", "")
            or "no active connection" in low):
        # Déduit l'app depuis le nom d'action (CANVA_… → canva, GITHUB_… → github, etc.)
        head = (action or "").split("_", 1)[0].lower()
        toolkit = head if head in _TOOLKITS else ""
        nice = toolkit.capitalize() if toolkit else "cette application"
        # L'app est peut-être connectée sous une AUTRE identité → on le dit précisément.
        known = [(s, u) for s, u, _ in _connected_accounts()]
        same = [u for s, u in known if s == toolkit]
        used = _toolkit_user_id(toolkit)
        if same:
            return (
                f"🔌 **{nice}** est bien connecté, mais sous l'identité **`{same[0]}`** — or l'action a été "
                f"exécutée avec **`{used}`**.\n\n"
                "Je viens d'apprendre à détecter automatiquement la bonne identité : **redemande-moi**, "
                "ça devrait passer. Si l'erreur persiste, mets `COMPOSIO_USER_ID` = "
                f"**`{same[0]}`** sur Render puis redéploie."
            )
        link, dbg = _composio_connect_link(toolkit) if toolkit else (None, "app non reconnue")
        if link:
            return (
                f"🔗 **Dernière étape !** Ta clé fonctionne, il ne reste qu'à **connecter {nice}**.\n\n"
                f"👉 **Clique ici pour autoriser ton compte :**\n{link}\n\n"
                "Autorise l'accès, puis redemande-moi. "
                f"_(Ce lien connecte le compte sous l'identité `{used}`, celle que j'utilise.)_"
            )
        connectees = ", ".join(sorted({s for s, _ in known if s}) ) or "aucune"
        return (
            f"🔌 Ta clé marche, mais **aucun compte {nice} connecté** n'a été trouvé pour l'identité `{used}`.\n\n"
            f"**Apps actuellement connectées :** {connectees}\n\n"
            f"**À faire :** Composio → **Toolkits → {nice} → Connect account** → autorise ton compte.\n\n"
            f"_(Détail technique : {dbg})_"
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
        # Autre app connectée (Linear, Canva, Notion, Slack…) → routeur générique
        slug = _detect_toolkit(message)
        if slug and slug not in ("googlecalendar", "gmail"):
            g = _generic_app_flow(message, slug)
            _remember_user(message)   # pour comprendre un suivi vague au tour suivant
            return {"steps": g["steps"], "done_answer": g["done_answer"]}
        return None
    # ⚠️ Passe par _tool() : identité Composio résolue + activité enregistrée (constellation).
    obs = _tool(action, args)
    slug = (action or "").split("_", 1)[0].lower()
    steps = [
        {"kind": "action", "tool": slug, "label": action},
        {"kind": "obs", "tool": slug, "text": str(obs)[:180]},
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
        slug = _detect_toolkit(message)
        if slug and slug not in ("googlecalendar", "gmail"):
            g = _generic_app_flow(message, slug)
            _remember_user(message)   # pour comprendre un suivi vague au tour suivant
            return {"steps": g["steps"], "answer": g["done_answer"], "ok": True}
        return None
    obs = _tool(action, args)   # identité résolue + activité enregistrée
    slug = (action or "").split("_", 1)[0].lower()
    steps = [
        {"kind": "action", "tool": slug, "label": action},
        {"kind": "obs", "tool": slug, "text": str(obs)[:180]},
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


_USERID_CACHE = {}


def _connected_accounts():
    """Comptes réellement connectés sur Composio : [(toolkit_slug, user_id, status)]."""
    import requests
    ck = (getattr(config, "COMPOSIO_API_KEY", "") or "").strip()
    if not ck:
        return []
    try:
        r = requests.get("https://backend.composio.dev/api/v3/connected_accounts",
                         headers={"x-api-key": ck}, params={"limit": 100}, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else []) or []
        out = []
        for it in items:
            tk = it.get("toolkit") or {}
            slug = (tk.get("slug") if isinstance(tk, dict) else None) or it.get("toolkit_slug") or ""
            uid = it.get("user_id") or it.get("entity_id") or ""
            out.append((str(slug).lower(), str(uid), str(it.get("status", ""))))
        return out
    except Exception:
        return []


def _toolkit_user_id(slug: str) -> str:
    """Identité (entity) sous laquelle CETTE app est réellement connectée.

    Indispensable : une app connectée depuis le dashboard Composio n'utilise pas forcément
    'default'. Sans ça → 404 'No connected account found for user ID default'.
    """
    if not slug:
        return getattr(config, "COMPOSIO_USER_ID", "default") or "default"
    if slug in _USERID_CACHE:
        return _USERID_CACHE[slug]
    uid = ""
    accounts = _connected_accounts()
    for s, u, st in accounts:
        if s == slug.lower() and u and st.upper() not in ("FAILED", "EXPIRED", "INACTIVE"):
            uid = u
            break
    if not uid:  # repli : première identité connue, sinon la valeur configurée
        for s, u, st in accounts:
            if u:
                uid = u
                break
    uid = uid or (getattr(config, "COMPOSIO_USER_ID", "default") or "default")
    _USERID_CACHE[slug] = uid
    return uid


def _agent_for_slug(slug: str) -> str:
    """Sous-agent responsable d'une app (pour la constellation)."""
    try:
        from agent.squad import get_squad
        for a in get_squad():
            if slug in (a.get("apps") or []):
                return a["id"]
    except Exception:
        pass
    return "nova"


def _tool(name_cmd: str, args: dict, slug: str = "") -> str:
    """Exécute une action Composio sous la BONNE identité (résolue automatiquement)."""
    import json as _json
    from plugins import get_loader
    from agent.self_heal import safe_tool_call
    if not slug:  # déduit l'app depuis le préfixe de l'action (ex. CANVA_CREATE… → canva)
        head = (name_cmd or "").split("_", 1)[0].lower()
        slug = head if head in _TOOLKITS else ""
    try:
        from agent.squad import record
        record(_agent_for_slug(slug), slug, name_cmd)
    except Exception:
        pass
    params = {"command": name_cmd, "arguments": _json.dumps(args)}
    uid = _toolkit_user_id(slug)
    if uid:
        params["user_id"] = uid
    return safe_tool_call(get_loader(), "connected_app", params)


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


_TOOLS_CACHE = {}


def _composio_list_actions(slug: str):
    """Liste les actions Composio disponibles pour une app (mise en cache).
    Renvoie [{'name':..., 'desc':...}] — permet à Nova de gérer n'importe quelle app."""
    if slug in _TOOLS_CACHE:
        return _TOOLS_CACHE[slug]
    import requests
    ck = (getattr(config, "COMPOSIO_API_KEY", "") or "").strip()
    if not ck:
        return []
    h = {"x-api-key": ck, "Content-Type": "application/json"}
    out = []
    for url, params in (
        ("https://backend.composio.dev/api/v3/tools", {"toolkit_slug": slug, "limit": 100}),
        ("https://backend.composio.dev/api/v3/tools", {"toolkit_slugs": slug, "limit": 100}),
    ):
        try:
            r = requests.get(url, headers=h, params=params, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
            items = data.get("items", data if isinstance(data, list) else []) or []
            for it in items:
                nm = it.get("slug") or it.get("name") or it.get("enum")
                if not nm:
                    continue
                desc = (it.get("description") or "")[:110]
                out.append({"name": str(nm).upper(), "desc": desc})
            if out:
                break
        except Exception:
            continue
    _TOOLS_CACHE[slug] = out
    return out


def _log_activity(message: str) -> None:
    """Trace CHAQUE demande dans la constellation (même une simple discussion)."""
    try:
        from agent.squad import pick_agent, record
        record(pick_agent(message), "", message[:60])
    except Exception:
        pass


def _remember_user(message: str) -> None:
    """Trace le message dans la mémoire (le chemin direct ne passe pas par run_agent)."""
    try:
        get_memory().remember(_PROFILE_ID, "user", message.strip()[:300])
    except Exception:
        pass


def _recent_user_context(n: int = 4) -> str:
    """Derniers messages de l'utilisateur — permet de comprendre un suivi vague
    (« canva » seul après « crée-moi une slide »)."""
    try:
        recent = get_memory().recall_recent(_PROFILE_ID, n * 2) or []
    except Exception:
        return ""
    users = [m.get("content", "") for m in recent if (m.get("role") == "user")]
    return " | ".join(u[:120] for u in users[-n:])


_USEFUL_VERBS = ("CREATE", "LIST", "GET", "SEARCH", "SEND", "UPDATE", "ADD", "FETCH", "DELETE")
_NOISE = ("DEPRECATED", "OAUTH", "TOKEN", "INSTALLATION", "RESTRICTION", "COLLABORATOR_PERMISSION",
          "STARGAZER", "STARRED", "EMAIL_ADDRESS", "INVITATION", "WEBHOOK", "BLOB", "GIT_REF")


def _useful_actions(actions: list, limit: int = 8) -> list:
    """Garde les actions vraiment utiles (verbe d'action clair, pas d'admin/déprécié)."""
    scored = []
    for a in actions:
        nm, desc = a["name"], (a.get("desc") or "")
        if "deprecated" in desc.lower() or any(n in nm for n in _NOISE):
            continue
        verb = nm.split("_", 1)[-1].split("_")[0]
        score = 3 if verb in _USEFUL_VERBS else 0
        score += 1 if len(nm.split("_")) <= 5 else 0      # noms courts = actions principales
        if score:
            scored.append((score, nm, desc))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return [{"name": n, "desc": d} for _s, n, d in scored[:limit]]


def _friendly_actions(actions: list, limit: int = 8) -> str:
    """Traduit les noms d'actions techniques en capacités lisibles."""
    best = _useful_actions(actions, limit) or actions[:limit]
    out = []
    for a in best:
        label = a["name"].split("_", 1)[-1].replace("_", " ").lower()
        desc = (a.get("desc") or "").strip()
        out.append(f"• **{label}**" + (f" — {desc[:70]}" if desc else ""))
    return "\n".join(out)


_EXAMPLES = {
    "github": ("crée un projet GitHub : site portfolio en HTML", "liste mes dépôts GitHub"),
    "canva": ("crée un design Canva pour un flyer", "liste mes designs Canva"),
    "linear": ("crée un ticket Linear : corriger le login", "liste mes tickets Linear"),
    "notion": ("crée une page Notion « Idées »", "cherche dans mes pages Notion"),
    "slack": ("envoie un message Slack à l'équipe", "lis mes derniers messages Slack"),
    "gmail": ("envoie un mail à paul@x.com", "résume mes mails non lus"),
    "googlecalendar": ("ajoute un rdv dentiste lundi 14h", "mon agenda cette semaine"),
}


def _examples_for(slug: str) -> str:
    ex = _EXAMPLES.get(slug)
    if not ex:
        return f"_Exemple : « liste mes {slug} » ou « crée quelque chose sur {slug} »._"
    return f"_Exemples : « {ex[0]} » ou « {ex[1]} »._"


# Questions de CAPACITÉ (« as-tu accès à… », « tu peux… ? ») → on répond, on n'exécute pas.
def _is_capability_question(message: str) -> bool:
    m = message.lower().strip()
    pats = ("as-tu accès", "as tu acces", "as-tu acces", "tu as accès", "tu as acces",
            "est-ce que tu peux", "est ce que tu peux", "tu peux faire quoi", "que peux-tu",
            "que peux tu", "qu'est-ce que tu peux", "quest ce que tu peux", "tu sais faire",
            "tu es connecté", "tu es connectee", "tu as acces à", "peux-tu accéder",
            "tu peux accéder", "tu peux acceder")
    return any(p in m for p in pats)


def _capability_answer(slug: str) -> str:
    """Réponse conversationnelle à « as-tu accès à X ? » — basée sur l'état RÉEL."""
    nice = slug.capitalize()
    connected = {s for s, _u, _st in _connected_accounts() if s}
    if slug not in connected:
        link, _ = _composio_connect_link(slug)
        reco = (f"\n\n👉 **Connecte-le en un clic :**\n{link}" if link else
                f"\n\nConnecte-le sur Composio → **Toolkits → {nice}**.")
        return (f"Pas encore : **{nice}** n'est pas connecté à mon compte Composio.{reco}")
    actions = _composio_list_actions(slug)
    caps = _friendly_actions(actions, 6) if actions else ""
    return (f"Oui ✅ — **{nice}** est bien connecté, j'y ai accès.\n\n"
            f"Voici ce que je peux y faire :\n{caps}\n\n{_examples_for(slug)}")


def _generic_app_flow(message: str, slug: str):
    """Exécute une action sur N'IMPORTE QUELLE app connectée :
    1) découvre les actions réelles de l'app, 2) le LLM choisit l'action + arguments,
    3) exécution, 4) mise en forme des DONNÉES RÉELLES (jamais d'invention)."""
    steps = [{"kind": "action", "tool": slug, "label": slug}]
    # « as-tu accès à GitHub ? » → on RÉPOND, on n'essaie pas d'exécuter une action.
    if _is_capability_question(message):
        return {"steps": steps, "done_answer": _capability_answer(slug)}
    actions = _composio_list_actions(slug)
    if not actions:
        return {"steps": steps, "done_answer": (
            f"🔌 Je n'ai pas pu lister les actions disponibles pour **{slug}**. "
            "Vérifie que l'app est bien connectée sur Composio (et que ta clé `ak_` a la permission "
            "`tool_execution`), puis redemande-moi.")}
    catalog = "\n".join(f"- {a['name']}: {a['desc']}" for a in actions[:70])
    ctx = _recent_user_context()
    pick = _llm_json(
        "Tu choisis l'action d'API à exécuter pour l'utilisateur. Réponds en JSON STRICT : "
        '{"action":"NOM_EXACT_DE_LA_LISTE","arguments":{...}}.\n'
        "RÈGLES :\n"
        "- Choisis TOUJOURS l'action la PLUS PROCHE de l'intention, même si la formulation est vague "
        "ou incomplète. Sers-toi du contexte de conversation pour comprendre la demande.\n"
        "- Si l'utilisateur veut créer quelque chose, privilégie une action CREATE ; s'il veut "
        "consulter/voir, privilégie LIST/GET/FETCH/SEARCH.\n"
        "- Le nom doit être EXACTEMENT celui de la liste.\n"
        "- Les arguments : devine les noms standards attendus ; {} si aucun n'est requis.\n"
        '- Ne renvoie {"action":""} QUE si vraiment AUCUNE action de la liste ne peut convenir.',
        f"Contexte récent de la conversation : {ctx}\n\n"
        f"Demande actuelle : {message}\n\nACTIONS DISPONIBLES ({slug}) :\n{catalog}")
    action = (pick.get("action") or "").strip().upper()
    valid = {a["name"] for a in actions}
    if action and action not in valid:  # tolère un nom approchant
        near = [n for n in valid if action in n or n in action]
        action = near[0] if near else ""
    if not action:
        return {"steps": steps, "done_answer": (
            f"Dis-m'en un peu plus sur ce que tu veux faire avec **{slug.capitalize()}** 🙂\n\n"
            f"Voici ce que je peux y faire :\n{_friendly_actions(actions)}\n\n{_examples_for(slug)}")}
    args = pick.get("arguments")
    if not isinstance(args, dict):
        args = {}
    steps[0]["label"] = action
    obs = _tool(action, args)
    steps.append({"kind": "obs", "tool": "connected_app", "text": str(obs)[:180]})
    if _looks_like_failure(obs):
        return {"steps": steps, "done_answer": _honest_no_access(action, obs)}
    is_write = any(k in action for k in ("CREATE", "UPDATE", "DELETE", "SEND", "ADD", "POST", "PATCH"))
    return {"steps": steps, "done_answer": _format_app_result(message, action, obs, is_write)}


def _find_action(slug: str, *keywords):
    """Retrouve le nom exact d'une action Composio à partir de mots-clés (robuste aux renommages)."""
    for a in _composio_list_actions(slug):
        nm = a["name"]
        if all(k.upper() in nm for k in keywords):
            return nm
    return None


def _github_project_flow(message: str):
    """UNE commande = un projet complet sur GitHub :
    1) le LLM génère le code (plusieurs fichiers), 2) création du dépôt,
    3) push de chaque fichier. Aucune invention : on rapporte ce que GitHub renvoie vraiment."""
    import base64, json as _json, re as _re
    steps = [{"kind": "action", "tool": "github", "label": "GÉNÉRATION DU CODE"}]

    # 1) Génération du projet
    plan = _llm_json(
        "Tu es un développeur senior. Génère un projet complet, fonctionnel et propre. "
        "Réponds en JSON STRICT : "
        '{"repo":"nom-court-en-kebab-case","description":"une phrase","private":false,'
        '"files":{"README.md":"contenu","index.html":"contenu"}}. '
        "RÈGLES : 3 à 8 fichiers maximum, du code RÉEL et complet (pas de TODO ni de placeholder), "
        "toujours un README.md clair. Le contenu de chaque fichier est une chaîne JSON échappée.",
        f"Projet demandé : {message}", temperature=0.3)
    repo = _re.sub(r"[^A-Za-z0-9._-]", "-", (plan.get("repo") or "").strip())[:60].strip("-")
    files = plan.get("files") or {}
    if not repo or not isinstance(files, dict) or not files:
        return {"steps": steps, "done_answer": (
            "Je n'ai pas réussi à générer le projet. Redis-moi en une phrase ce que tu veux "
            "(ex. « crée un projet GitHub : site portfolio en HTML/CSS »).")}
    desc = (plan.get("description") or message)[:200]
    private = bool(plan.get("private"))
    steps.append({"kind": "obs", "tool": "github", "text": f"{len(files)} fichiers générés — dépôt « {repo} »"})

    # 2) Propriétaire du compte
    owner = ""
    act_me = _find_action("github", "AUTHENTICATED_USER") or ""
    if act_me and "REPOSITOR" not in act_me:
        me = _tool(act_me, {})
        m = _re.search(r'"login"\s*:\s*"([^"]+)"', me or "")
        owner = m.group(1) if m else ""

    # 3) Création du dépôt
    act_create = _find_action("github", "CREATE", "REPOSITORY") or "GITHUB_CREATE_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER"
    steps.append({"kind": "action", "tool": "github", "label": act_create})
    obs = _tool(act_create, {"name": repo, "description": desc, "private": private, "auto_init": False})
    if _looks_like_failure(obs):
        if "already exists" in (obs or "").lower():
            return {"steps": steps, "done_answer": (
                f"⚠️ Un dépôt **{repo}** existe déjà sur ton compte. Redemande-moi avec un autre nom "
                f"(ex. « …, appelle-le {repo}-v2 »).")}
        return {"steps": steps, "done_answer": _honest_no_access(act_create, obs)}
    if not owner:
        m = _re.search(r'"login"\s*:\s*"([^"]+)"', obs or "")
        owner = m.group(1) if m else ""
    m = _re.search(r'"html_url"\s*:\s*"([^"]+)"', obs or "")
    url = m.group(1) if m else (f"https://github.com/{owner}/{repo}" if owner else "")

    # 4) Push des fichiers
    act_put = _find_action("github", "CREATE_OR_UPDATE_FILE") or "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS"
    ok, failed = [], []
    for path, content in list(files.items())[:8]:
        if not isinstance(content, str):
            content = _json.dumps(content, ensure_ascii=False, indent=2)
        b64 = base64.b64encode(content.encode("utf-8")).decode()
        r = _tool(act_put, {"owner": owner, "repo": repo, "path": str(path),
                            "message": f"feat: ajout de {path}", "content": b64})
        (ok if not _looks_like_failure(r) else failed).append(str(path))
    steps.append({"kind": "obs", "tool": "github", "text": f"{len(ok)} fichier(s) poussé(s)"})

    lines = [f"✅ Projet **{repo}** créé sur GitHub !"]
    if url:
        lines.append(f"\n🔗 {url}")
    if ok:
        lines.append("\n**Fichiers ajoutés :**\n" + "\n".join(f"- `{p}`" for p in ok))
    if failed:
        lines.append("\n⚠️ Non poussés : " + ", ".join(f"`{p}`" for p in failed) +
                     "\n_(le dépôt existe, tu peux les ajouter à la main ou me redemander)_")
    lines.append("\nDis-moi si tu veux que j'ajoute des fonctionnalités ou un autre fichier.")
    return {"steps": steps, "done_answer": "\n".join(lines)}


_MAKE_VERBS = ("crée", "cree", "créer", "creer", "fais", "fais-moi", "génère", "genere",
               "générer", "generer", "développe", "developpe", "code", "construis", "build",
               "monte", "mets en place", "réalise", "realise")
_PROJECT_WORDS = ("projet", "repo", "dépôt", "depot", "repository", "site", "app", "application",
                  "jeu", "game", "script", "bot", "api", "landing", "portfolio", "dashboard",
                  "page web", "site web")


def _is_github_project(message: str) -> bool:
    """« crée un projet/site/app … sur GitHub » → génération de code + dépôt + push."""
    m = message.lower()
    if "github" not in m and "dépôt" not in m and "depot" not in m and "repo" not in m:
        return False
    return any(v in m for v in _MAKE_VERBS) and any(w in m for w in _PROJECT_WORDS)


def _complex_app_flow(message: str):
    """Détecte et exécute les actions multi-étapes. Renvoie {steps, done_answer} ou None."""
    m = message.lower()
    if _is_github_project(message):
        return _github_project_flow(message)
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
        _log_activity(message)
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
            _log_activity(message)   # visible immédiatement dans la constellation
            # 1) Chitchat / info personnelle → streamé directement (aucun outil)
            if _is_smalltalk(message):
                _remember_fact(message)
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


_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _analyze_upload(path: str, question: str) -> str:
    """Analyse un fichier déposé : image → vision, document → analyse texte."""
    from pathlib import Path as _P
    p = _P(path)
    if p.suffix.lower() in _IMG_EXT:
        try:
            from llm.client import chat_vision
            return chat_vision(str(p), question or "Décris cette image en détail, en français.")
        except Exception as e:
            return (f"❌ Analyse d'image indisponible : {str(e)[:200]}\n"
                    "_(Le modèle vision nécessite une clé Groq valide.)_")
    from plugins import get_loader
    from agent.self_heal import safe_tool_call
    return safe_tool_call(get_loader(), "analyze_document",
                          {"path": str(p), "question": question or ""})


@router.post("/upload")
async def upload(request: Request):
    """Réception d'un fichier/photo déposé depuis /nova → analyse et réponse.
    Multipart : file=<binaire>, key=<clé agent>, question=<consigne optionnelle>."""
    from fastapi import UploadFile
    form = await request.form()
    _check_key(str(form.get("key") or ""))
    up = form.get("file")
    if up is None or not hasattr(up, "filename"):
        raise HTTPException(status_code=400, detail="Aucun fichier reçu.")
    question = str(form.get("question") or "").strip()

    from pathlib import Path as _P
    import re as _re
    updir = _P("data/uploads"); updir.mkdir(parents=True, exist_ok=True)
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", (up.filename or "fichier"))[:80]
    dest = updir / safe
    try:
        content = await up.read()
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Fichier trop volumineux (max 20 Mo).")
        dest.write_bytes(content)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Écriture impossible : {str(e)[:150]}")

    loop = asyncio.get_running_loop()
    answer = await loop.run_in_executor(None, _analyze_upload, str(dest), question)
    try:
        get_memory().remember(_PROFILE_ID, "user", f"[fichier déposé] {safe} — {question[:120]}")
    except Exception:
        pass
    return {"answer": answer, "file": safe}


class AutoReq(BaseModel):
    key: Optional[str] = None
    titre: Optional[str] = None
    prompt: Optional[str] = None
    hour: Optional[int] = 8
    days: Optional[list] = None
    icon: Optional[str] = "⚡"
    active: Optional[bool] = None
    id: Optional[str] = None


@router.get("/automations")
async def automations_list(key: str = ""):
    """Liste les automatisations + les modèles proposés."""
    _check_key(key)
    from agent.automations import list_all, TEMPLATES
    return {"items": list_all(), "templates": TEMPLATES}


@router.post("/automations")
async def automations_add(req: AutoReq):
    """Crée une automatisation (tâche que Nova exécutera seule)."""
    _check_key(req.key or "")
    if not (req.titre and req.prompt):
        raise HTTPException(status_code=400, detail="titre et prompt requis.")
    from agent.automations import add
    return add(req.titre, req.prompt, req.hour or 8, req.days, req.icon or "⚡")


@router.post("/automations/toggle")
async def automations_toggle(req: AutoReq):
    """Active/désactive une automatisation."""
    _check_key(req.key or "")
    from agent.automations import update
    return {"ok": update(req.id or "", active=req.active)}


@router.post("/automations/run")
async def automations_run(req: AutoReq):
    """Lance une automatisation immédiatement (pour tester sans attendre l'heure)."""
    _check_key(req.key or "")
    from agent.automations import list_all, run_one
    item = next((i for i in list_all() if i["id"] == (req.id or "")), None)
    if not item:
        raise HTTPException(status_code=404, detail="Automatisation introuvable.")
    return {"answer": await run_one(item)}


@router.delete("/automations")
async def automations_delete(id: str = "", key: str = ""):
    """Supprime une automatisation."""
    _check_key(key)
    from agent.automations import delete
    return {"ok": delete(id)}


@router.get("/activity")
async def activity():
    """État de l'escouade + activité temps réel (alimente la constellation /nova/brain)."""
    from agent.squad import snapshot
    snap = snapshot()
    try:
        connected = {s for s, _u, _st in _connected_accounts() if s}
    except Exception:
        connected = set()
    assigned = set()
    for a in snap["squad"]:
        a["connected"] = [x for x in (a.get("apps") or []) if x in connected]
        assigned.update(a.get("apps") or [])
    # Toute app connectée mais non rattachée (ex. tu viens de brancher Notion/Spotify)
    # apparaît automatiquement — d'abord auprès du spécialiste connu, sinon dans « Autres ».
    extra = sorted(connected - assigned)
    if extra:
        snap["squad"].append({
            "id": "autres", "name": "Autres apps", "icon": "🧩", "color": "#94a3b8",
            "desc": "Apps connectées récemment", "apps": extra, "connected": extra,
        })
    snap["connected_apps"] = sorted(connected)
    return snap


@router.get("/apps")
async def apps_list(key: str = ""):
    """Liste les apps réellement CONNECTÉES sur Composio + celles que Nova sait piloter."""
    _check_key(key)
    import requests
    ck = (getattr(config, "COMPOSIO_API_KEY", "") or "").strip()
    connected, detail = [], ""
    if ck:
        h = {"x-api-key": ck, "Content-Type": "application/json"}
        try:
            r = requests.get("https://backend.composio.dev/api/v3/connected_accounts",
                             headers=h, params={"limit": 100}, timeout=20)
            if r.status_code == 200:
                data = r.json()
                items = data.get("items", data if isinstance(data, list) else []) or []
                for it in items:
                    tk = it.get("toolkit") or {}
                    slug = (tk.get("slug") if isinstance(tk, dict) else None) or it.get("toolkit_slug") or it.get("appName") or "?"
                    connected.append({"app": str(slug).lower(),
                                      "status": it.get("status", "?"),
                                      "user_id": it.get("user_id") or it.get("entity_id") or ""})
            else:
                detail = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            detail = f"{type(e).__name__}: {str(e)[:150]}"
    return {"connected": connected,
            "identite_utilisee": {c["app"]: _toolkit_user_id(c["app"]) for c in connected},
            "known_by_nova": sorted(_TOOLKITS.keys()), "detail": detail}


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
