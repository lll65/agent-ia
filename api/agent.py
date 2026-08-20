import asyncio
import hmac
import re

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


def _profile_ctx() -> str:
    """Ce que Nova sait de l'utilisateur, à injecter pour personnaliser ses réponses."""
    try:
        from agent.profile import context_block
        blk = context_block()
        return ("\n\n" + blk) if blk else ""
    except Exception:
        return ""


def _smalltalk_messages(message: str) -> list:
    return [
        {"role": "system", "content": _profile_ctx().strip() + "\n" + (
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
    """Mémorise une info personnelle : fait structuré dans le profil + trace dans l'historique."""
    if not _is_personal_fact(message):
        return
    try:
        get_memory().remember(_PROFILE_ID, "user", message.strip()[:200])
    except Exception:
        pass
    try:
        from agent.profile import learn_from
        learn_from(message)
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
    "googlemaps":     ("maps", "google maps", "itinéraire", "itineraire", "trajet", "adresse",
                       "comment aller", "temps de route", "restaurant près", "restaurant pres"),
    "googletasks":    ("google tasks", "ma liste de tâches", "ma liste de taches", "mes tâches",
                       "mes taches", "todo", "to-do"),
    "todoist":        ("todoist",),
    "outlook":        ("outlook",),
    "dropbox":        ("dropbox",),
    "figma":          ("figma",),
    "reddit":         ("reddit",),
    "perplexity":     ("perplexity",),
}

# Ordre de priorité : les slugs les plus spécifiques d'abord (évite que "message" prenne le pas)
_TOOLKIT_ORDER = ("linear", "canva", "notion", "slack", "github", "figma", "googlesheets",
                  "googledocs", "googledrive", "dropbox", "trello", "spotify", "youtube",
                  "whatsapp", "discord", "twitter", "reddit", "hubspot", "airtable", "asana",
                  "jira", "todoist", "googletasks", "googlemaps", "outlook", "perplexity",
                  "googlecalendar", "gmail")


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
        system += (
            " AUTO-VÉRIFICATION : pour chaque affirmation factuelle (chiffre, date, fait), indique "
            "brièvement la source entre parenthèses (issue des résultats de recherche). Marque d'un ⚠️ "
            "toute affirmation que les outils n'ont pas confirmée. N'invente jamais de source.\n"
            "RECHERCHE TENACE — ne renonce pas au premier essai :\n"
            "1. Reformule la requête à chaque tentative (mots-clés différents, synonymes, sigle ET nom "
            "complet, ajoute l'année, ajoute « site officiel » ou le nom du site attendu).\n"
            "2. Fais jusqu'à 4 recherches successives tant que l'information précise manque.\n"
            "3. Si après ces tentatives tu n'as toujours pas la réponse exacte : dis-le franchement, "
            "donne ce que tu as trouvé de plus proche, ET termine par le LIEN le plus officiel/pertinent "
            "issu des résultats (page du site concerné) pour que l'utilisateur vérifie lui-même. "
            "Ne donne jamais un lien que les résultats ne contiennent pas.\n"
            "FRAÎCHEUR DES INFOS : indique la date de chaque information quand elle est disponible. "
            "Si les résultats datent de plusieurs semaines ou mois alors que la demande portait sur "
            "« aujourd'hui » / « du jour », signale-le explicitement en tête de réponse "
            "(ex. « ⚠️ Je n'ai pas trouvé d'actualité datée d'aujourd'hui ; voici les plus récentes "
            "que j'ai, du 29 juillet »). Ne présente jamais une information ancienne comme récente.")
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
    system += _profile_ctx()          # personnalisation à partir des faits retenus
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
    """Nettoie la phrase pour Google Quick Add (retire l'appel à Nova, le verbe et les mots 'agenda')."""
    import re
    t = " " + message + " "
    # 1) On enlève l'interpellation ("Nova, ...", "dis Nova ...") — sinon elle finit dans le titre.
    t = re.sub(r"^[\s,]*(h?ey\s+|ok\s+|dis\s+)?nova[\s,:!]*", " ", t.strip(), flags=re.I)
    # 2) Les mentions de l'agenda
    for w in ("sur mon agenda", "dans mon agenda", "à mon agenda", "a mon agenda", "sur l'agenda",
              "dans l'agenda", "dans le calendrier", "sur mon calendrier", "mon agenda", "l'agenda",
              "mon calendrier", "agenda"):
        t = re.sub(re.escape(w), " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    # 3) Le verbe d'ajout, puis les mots creux ("une tâche", "un événement"…)
    for w in ("ajoute-moi", "ajoute moi", "ajouter", "ajoute", "rajoute", "crée", "cree", "créer",
              "creer", "réserve", "reserve", "bloque", "programme", "planifie", "mets-moi",
              "mets moi", "mets", "note"):
        t = re.sub(r"^\s*" + w + r"\b", "", t.strip(), flags=re.I)
    t = re.sub(r"^\s*(une?\s+)?(t[âa]che|[ée]v[ée]nement|rappel|truc|chose)\s+(de\s+|pour\s+)?",
               "", t.strip(), flags=re.I)
    return t.strip(" ,:\"'").strip() or message


def _event_text(message: str) -> str:
    """Titre + moment prêts pour Quick Add. Le LLM extrait l'essentiel (robuste à l'oral),
    avec repli sur le nettoyage par règles si l'extraction échoue."""
    base = _clean_event_text(message)
    ex = _llm_json(
        "Extrais l'événement à mettre dans un agenda. JSON STRICT : "
        '{"titre":"…","quand":"…"}. '
        "titre = 2 à 4 mots MAX décrivant l'activité, sans verbe d'ajout, sans le mot « agenda », "
        "sans le mot « Nova », première lettre en majuscule (ex. « Travail », « Rendez-vous dentiste »). "
        "quand = l'expression temporelle telle quelle (ex. « demain 14h », « lundi 9h »), "
        "chaîne vide si aucune n'est donnée.",
        f"Phrase : {message}")
    titre = (ex.get("titre") or "").strip(" .\"'")
    quand = (ex.get("quand") or "").strip(" .\"'")
    if titre and len(titre) <= 60:
        return (titre + (" " + quand if quand else "")).strip()
    return base


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
        return "GOOGLECALENDAR_QUICK_ADD", {"calendar_id": "primary", "text": _event_text(message)}

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


def _is_param_error(obs: str) -> bool:
    """Erreur due aux PARAMÈTRES envoyés (400) — donc corrigeable automatiquement,
    par opposition à un problème d'accès (401/403/404)."""
    low = (obs or "").lower()
    if "401" in low or "403" in low or "no connected account" in low:
        return False
    return ("400" in low or "invalid_field" in low or "bad request" in low
            or "must be defined" in low or "is required" in low or "missing" in low
            or "invalid" in low or "validation" in low)


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
    # Erreur de paramètres (400) : l'accès fonctionne, c'est la requête qui n'allait pas.
    if _is_param_error(obs):
        import re as _re
        # Le message utile est souvent dans un JSON imbriqué et échappé → on déséchappe d'abord
        flat = (obs or "").replace('\\"', '"').replace("\\\\", "\\")
        cands = _re.findall(r'"message"\s*:\s*"([^"]{6,300})"', flat)
        detail = next((c for c in reversed(cands) if not c.lstrip().startswith("{")), "")[:220]
        nice = tk.capitalize() if tk else "cette application"
        return (
            f"⚠️ **{nice} a refusé la demande** — l'accès fonctionne, mais il manquait une précision.\n\n"
            + (f"**Ce que {nice} répond :** {detail}\n\n" if detail else "")
            + "J'ai tenté de corriger automatiquement, sans succès. **Sois plus précis** et je réessaie "
              "(ex. « crée une **présentation** Canva intitulée Projet X », « crée un **document** Canva »).")
    nice = tk.capitalize() if tk else "cette application"
    return (
        f"🔌 Je n'ai pas pu accéder à {nice}, donc je ne t'invente rien.\n\n"
        f"**Raison technique :**\n> {obs[:350]}\n\n"
        f"**Pistes :** vérifie que **{nice}** est bien connecté sur composio.dev (statut vert), "
        "puis redemande-moi."
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
        out = chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                   temperature=temperature) or ""      # le modèle peut renvoyer un contenu vide
    except Exception:
        return {}
    m = re.search(r"\{.*\}", str(out), re.DOTALL)
    if not m:
        return {}
    for cand in (m.group(0), m.group(0).replace("'", '"')):
        try:
            return _json.loads(cand)
        except Exception:
            continue
    return {}


# Caches AVEC EXPIRATION : sans TTL, connecter une nouvelle app (ou en reconnecter une
# avec de nouvelles autorisations) restait sans effet jusqu'au redémarrage du serveur.
_CACHE_TTL = 600.0          # 10 minutes
_USERID_CACHE = {}          # slug -> (valeur, horodatage)
_ACCOUNTS_CACHE = {"data": None, "ts": 0.0}


def _cache_get(store: dict, key: str):
    import time as _t
    hit = store.get(key)
    if hit and (_t.monotonic() - hit[1]) < _CACHE_TTL:
        return hit[0]
    return None


def _cache_put(store: dict, key: str, val):
    import time as _t
    store[key] = (val, _t.monotonic())


def invalidate_caches(slug: str = "") -> None:
    """Vide les caches — appelé quand une app semble avoir changé de connexion."""
    if slug:
        _USERID_CACHE.pop(slug, None)
        _TOOLS_CACHE.pop(slug, None)
    else:
        _USERID_CACHE.clear()
        _TOOLS_CACHE.clear()
    _ACCOUNTS_CACHE["data"] = None
    _ACCOUNTS_CACHE["ts"] = 0.0


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


def _norm_slug(s: str) -> str:
    """Compare les identifiants d'app en ignorant tirets/underscores.
    Composio écrit « google_maps » là où nous écrivons « googlemaps »."""
    return (s or "").lower().replace("_", "").replace("-", "").strip()


def _real_slug(slug: str) -> str:
    """Renvoie l'identifiant EXACT utilisé par Composio pour cette app (sinon le nôtre)."""
    if not slug:
        return slug
    cible = _norm_slug(slug)
    try:
        for s, _u, _st in _connected_accounts():
            if s and _norm_slug(s) == cible:
                return s
    except Exception:
        pass
    return slug


def _toolkit_user_id(slug: str) -> str:
    """Identité (entity) sous laquelle CETTE app est réellement connectée.

    Indispensable : une app connectée depuis le dashboard Composio n'utilise pas forcément
    'default'. Sans ça → 404 'No connected account found for user ID default'.
    """
    if not slug:
        return getattr(config, "COMPOSIO_USER_ID", "default") or "default"
    cached = _cache_get(_USERID_CACHE, slug)
    if cached:
        return cached
    uid = ""
    accounts = _connected_accounts()
    for s, u, st in accounts:
        if _norm_slug(s) == _norm_slug(slug) and u and st.upper() not in ("FAILED", "EXPIRED", "INACTIVE"):
            uid = u
            break
    if not uid:  # repli : première identité connue, sinon la valeur configurée
        for s, u, st in accounts:
            if u:
                uid = u
                break
    uid = uid or (getattr(config, "COMPOSIO_USER_ID", "default") or "default")
    _cache_put(_USERID_CACHE, slug, uid)
    return uid


def _agent_pour_outil(outil: str) -> str:
    """Quel spécialiste de l'escouade est derrière cet outil (pour l'afficher au travail)."""
    o = (outil or "").lower()
    if "search" in o or "web" in o:
        return "veille"
    if "visual" in o or "image" in o:
        return "crea"
    if "document" in o or "file" in o or "read" in o or "write" in o:
        return "fichiers"
    if "exec" in o or "code" in o or "project" in o:
        return "dev"
    if "connected" in o:
        return "nova"
    return _agent_for_slug(o)


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
    if not slug:  # déduit l'app depuis le préfixe de l'action (ex. CANVA_CREATE… → canva)
        head = (name_cmd or "").split("_", 1)[0].lower()
        slug = head if head in _TOOLKITS else ""
    try:
        from agent.squad import record
        record(_agent_for_slug(slug), slug, name_cmd)
    except Exception:
        pass
    return _tool_call(name_cmd, args, slug)


def _tool_call(name_cmd: str, args: dict, slug: str) -> str:
    """Exécution + reprise automatique si l'identité en cache est périmée
    (cas typique : tu viens juste de connecter l'app)."""
    import json as _json
    from plugins import get_loader
    from agent.self_heal import safe_tool_call

    def _run(uid):
        params = {"command": name_cmd, "arguments": _json.dumps(args)}
        if uid:
            params["user_id"] = uid
        return safe_tool_call(get_loader(), "connected_app", params)

    obs = _run(_toolkit_user_id(slug))
    low = str(obs or "").lower()
    if "no connected account" in low or "connectedaccountnotfound" in low.replace("_", ""):
        invalidate_caches(slug)                     # l'app a peut-être été (re)connectée
        obs2 = _run(_toolkit_user_id(slug))
        if not _looks_like_failure(obs2):
            return obs2
        return obs2 if obs2 else obs
    return obs
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


def _trim_schema(schema, depth: int = 0):
    """Réduit un JSON Schema à l'essentiel (type, forme imbriquée, valeurs autorisées, exemple).
    Objectif : le modèle voit la STRUCTURE exacte attendue et ne se trompe plus de forme."""
    if not isinstance(schema, dict) or depth > 3:
        return {}
    keep = {}
    t = schema.get("type")
    if t:
        keep["type"] = t
    for k in ("enum", "default", "example", "format"):
        if schema.get(k) is not None:
            keep[k] = schema[k]
    if schema.get("required"):
        keep["required"] = schema["required"][:10]
    props = schema.get("properties")
    if isinstance(props, dict):
        keep["properties"] = {k: _trim_schema(v, depth + 1) for k, v in list(props.items())[:14]}
    items = schema.get("items")
    if isinstance(items, dict):
        keep["items"] = _trim_schema(items, depth + 1)
    if schema.get("description") and depth > 0:
        keep["description"] = str(schema["description"])[:90]
    return keep


def _build_args(action: str, spec: dict, message: str, ctx: str = "", error: str = "") -> dict:
    """Construit les arguments d'une action à partir de son SCHÉMA réel.
    Étape séparée du choix de l'action : le modèle se concentre sur la forme attendue."""
    import json as _j
    schema = _j.dumps(spec.get("schema") or {}, ensure_ascii=False)[:2200]
    sys = ("Tu construis les ARGUMENTS d'un appel d'API à partir de son schéma JSON.\n"
           'Réponds en JSON STRICT : {"arguments":{…}}.\n'
           "RÈGLES :\n"
           "- RESPECTE EXACTEMENT la structure du schéma : si un champ est un objet, envoie un objet "
           '(ex. {"design_type":{"type":"preset","name":"doc"}}), jamais une chaîne.\n'
           "- Remplis TOUS les champs obligatoires ; ignore les champs inutiles.\n"
           "- Si un champ a une liste de valeurs autorisées (enum), choisis-en une.\n"
           "- Déduis les valeurs de la demande de l'utilisateur ; sinon mets une valeur par défaut sensée.")
    usr = (f"Action : {action}\nObligatoires : {spec.get('required')}\n"
           f"SCHÉMA :\n{schema}\n\nDemande : {message}")
    if ctx:
        usr += f"\nContexte récent : {ctx}"
    if error:
        usr += f"\n\n⚠️ L'appel précédent a ÉCHOUÉ avec cette erreur — corrige-la :\n{error[:600]}"
    out = _llm_json(sys, usr)
    args = out.get("arguments")
    return args if isinstance(args, dict) else {}


def _composio_list_actions(slug: str):
    """Liste les actions Composio disponibles pour une app (mise en cache).
    Renvoie [{'name':..., 'desc':...}] — permet à Nova de gérer n'importe quelle app."""
    cached = _cache_get(_TOOLS_CACHE, slug)
    if cached is not None:
        return cached
    import requests
    ck = (getattr(config, "COMPOSIO_API_KEY", "") or "").strip()
    if not ck:
        return []
    h = {"x-api-key": ck, "Content-Type": "application/json"}
    vrai = _real_slug(slug)      # identifiant exact chez Composio (ex. « google_maps »)
    out = []
    for url, params in (
        ("https://backend.composio.dev/api/v3/tools", {"toolkit_slug": vrai, "limit": 100}),
        ("https://backend.composio.dev/api/v3/tools", {"toolkit_slugs": vrai, "limit": 100}),
        ("https://backend.composio.dev/api/v3/tools", {"toolkit_slug": slug, "limit": 100}),
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
                # Schéma d'entrée COMPLET : indispensable pour connaître la FORME attendue
                # (ex. Canva : design_type est un objet imbriqué, pas une chaîne).
                schema = it.get("input_parameters") or it.get("parameters") or {}
                props = list((schema.get("properties") or {}).keys())[:14]
                req = (schema.get("required") or [])[:10]
                out.append({"name": str(nm).upper(), "desc": desc, "props": props,
                            "required": req, "schema": _trim_schema(schema)})
            if out:
                break
        except Exception:
            continue
    _cache_put(_TOOLS_CACHE, slug, out)
    return out


# ── NIVEAU DE TÂCHE ───────────────────────────────────────────────────────────
# Règles DÉTERMINISTES (pas d'appel LLM) : le routage est instantané, gratuit et
# ne peut pas échouer — condition posée pour qu'il soit fiable.
_LOURD = ("code", "coder", "programme", "script", "fonction", "algorithme", "débogue", "debug",
          "corrige le bug", "analyse", "analyser", "compare", "comparer", "explique en détail",
          "rédige", "redige", "écris un", "ecris un", "dissertation", "rapport", "synthèse",
          "synthese", "stratégie", "strategie", "plan détaillé", "traduis", "resume ce",
          "résume ce", "projet", "architecture", "optimise", "refactor")
_LEGER = ("salut", "bonjour", "merci", "ok", "oui", "non", "ça va", "ca va", "quelle heure",
          "quel jour", "coucou", "hello", "bye", "à plus", "bonne nuit")


def _modele_utilise() -> str:
    """Quel fournisseur/modèle vient de répondre (affiché discrètement sous la réponse)."""
    try:
        from llm.client import DERNIER
        return DERNIER.get("") or ""
    except Exception:
        return ""


def _niveau_tache(message: str) -> str:
    """« rapide » (discuter), « equilibre » (défaut), « puissant » (code, analyse, rédaction)."""
    m = (message or "").lower().strip()
    mots = len(m.split())
    if _is_smalltalk(message) or (mots <= 4 and any(k in m for k in _LEGER)):
        return "rapide"
    if any(k in m for k in _LOURD) or mots > 60 or len(m) > 500:
        return "puissant"
    return "equilibre"


_NIVEAU_FR = {"rapide": "modèle rapide", "equilibre": "modèle équilibré", "puissant": "modèle puissant"}

# Verbes → intention, sans le moindre appel réseau (affiché en sous-bulle avant le streaming).
_INTENTIONS = (
    (("ajoute", "ajouter", "crée", "creer", "créer", "planifie", "programme", "note ", "réserve", "reserve"),
     "tu veux ajouter quelque chose"),
    (("supprime", "annule", "efface", "retire"), "tu veux supprimer"),
    (("modifie", "déplace", "deplace", "change", "reporte"), "tu veux modifier"),
    (("envoie", "envoi", "réponds", "reponds", "écris", "ecris", "partage"), "tu veux envoyer"),
    (("cherche", "trouve", "recherche"), "tu veux chercher dedans"),
)


def _intention_app(message: str) -> str:
    """Décrit l'intention en français clair, par simple analyse lexicale."""
    m = (message or "").lower()
    for mots, libelle in _INTENTIONS:
        if any(k in m for k in mots):
            return libelle
    return "je vais aller regarder"


# Nom lisible des applications connectées (« googlecalendar » ne parle à personne).
_APP_FR = {"googlecalendar": "ton agenda", "gmail": "tes mails", "googlemaps": "Maps",
           "googledrive": "ton Drive", "googledocs": "Docs", "googlesheets": "Sheets",
           "notion": "Notion", "slack": "Slack", "github": "GitHub", "linear": "Linear",
           "canva": "Canva", "youtube": "YouTube", "spotify": "Spotify", "trello": "Trello",
           "discord": "Discord", "twitter": "X", "linkedin": "LinkedIn"}

_NIVEAU_BULLE = {"rapide": "je réponds vite",
                 "equilibre": "mon modèle habituel",
                 "puissant": "je sors mon modèle le plus costaud"}


def _route_bulles(message: str) -> list:
    """Ce que Nova a RÉELLEMENT décidé, en français clair : une phrase par décision.

    Ces phrases s'affichent UNE PAR UNE pendant qu'elle travaille. Elles disaient avant
    « question factuelle · recherche web requise · modèle équilibré » — du jargon, envoyé
    d'un seul bloc. Ce sont les mêmes décisions, dites comme à un humain.
    """
    if _is_smalltalk(message):
        b = ["tu me parles simplement"]
        if _is_personal_fact(message):
            b.append("je note ça sur toi")
        b.append("pas besoin d'outil")
        return b
    if _is_briefing(message):
        return ["tu veux ton briefing", "je regarde agenda, mails, météo et actu"]
    slug = _detect_toolkit(message)
    if slug:
        b = [f"ça concerne {_APP_FR.get(slug, slug)}"]
        if _is_capability_question(message):
            b.append("tu me demandes ce que je sais faire")
        elif _wants_visual(message):
            b.append("tu veux une image")
        else:
            # ⚠️ Purement lexical, AUCUN appel réseau : cette description est calculée
            # avant le streaming, sur la boucle asyncio.
            b.append(_intention_app(message))
        return b
    if _wants_visual(message):
        return ["tu veux une image", "je prépare le texte à écrire dessus"]
    b = []
    if _finance_intent(message):
        b.append("sujet finance")
    if any(h in message.lower() for h in _FACTUAL_HINTS):
        b.append("il me faut des infos à jour")
        b.append("je vais chercher sur le web")
    else:
        b.append("je réfléchis avec ce que je sais déjà")
    return b


def _route_detail(message: str) -> str:
    """Les mêmes décisions d'un seul tenant (journal, diagnostics, chemins non streamés)."""
    return " · ".join(_route_bulles(message)) or "j'analyse ta demande"


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


_VERBES_FR = {
    "create": "Créer", "get": "Consulter", "list": "Lister", "update": "Modifier",
    "delete": "Supprimer", "send": "Envoyer", "search": "Rechercher", "fetch": "Récupérer",
    "add": "Ajouter", "remove": "Retirer", "download": "Télécharger", "upload": "Envoyer",
    "move": "Déplacer", "copy": "Copier", "find": "Trouver", "read": "Lire", "write": "Écrire",
}


def _friendly_actions(actions: list, limit: int = 8) -> str:
    """Capacités en FRANÇAIS. Repli utilisé si la traduction par le modèle échoue :
    on ne montre plus les descriptions anglaises tronquées de l'API."""
    best = _useful_actions(actions, limit) or actions[:limit]
    out = []
    for a in best:
        mots = a["name"].split("_")[1:]                     # on retire le préfixe de l'app
        if not mots:
            continue
        verbe = _VERBES_FR.get(mots[0].lower())
        reste = " ".join(m.lower() for m in (mots[1:] if verbe else mots))
        # on enlève les mots parasites fréquents dans les noms d'actions
        for parasite in ("the", "a", "an", "for", "authenticated", "user", "specific", "by", "id"):
            reste = re.sub(rf"\b{parasite}\b", " ", reste)
        reste = re.sub(r"\s+", " ", reste).strip()
        out.append(f"• {verbe} {reste}".strip() if verbe else f"• {reste.capitalize()}")
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


def _plain_capabilities(slug: str, actions: list) -> str:
    """Traduit les actions techniques en phrases claires, en français."""
    from llm.client import chat
    liste = "\n".join(f"- {a['name']}: {(a.get('desc') or '')[:90]}" for a in actions[:40])
    try:
        out = chat([
            {"role": "system", "content": (
                "On te donne les actions d'API disponibles pour une application. Résume en FRANÇAIS "
                "ce que l'assistant peut concrètement y faire, du point de vue de l'utilisateur.\n"
                "RÈGLES : 4 à 6 puces maximum, une ligne chacune, commençant par un verbe à l'infinitif "
                "(ex. « Créer un ticket avec un titre et une description »). Regroupe les actions "
                "similaires. Pas de noms techniques, pas d'anglais, pas de descriptions tronquées. "
                "Réponds UNIQUEMENT par les puces.")},
            {"role": "user", "content": f"Application : {slug}\n\nACTIONS :\n{liste}"},
        ], temperature=0.2)
        lines = [l.strip() for l in (out or "").splitlines() if l.strip().startswith(("•", "-", "*"))]
        if lines:
            return "\n".join("• " + l.lstrip("•-* ").strip() for l in lines[:6])
    except Exception:
        pass
    return _friendly_actions(actions, 6)


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
    caps = _plain_capabilities(slug, actions) if actions else ""
    return (f"Oui ✅ — **{nice}** est bien connecté, j'y ai accès.\n\n"
            f"Voici ce que je peux y faire :\n{caps}\n\n{_examples_for(slug)}")


def _canva_design_args(message: str) -> dict:
    """Canva attend un OBJET imbriqué, pas une chaîne :
       {"design_type": {"type": "preset", "name": "doc"}}
    Les presets valides sont : doc, presentation, whiteboard."""
    m = message.lower()
    if any(w in m for w in ("présentation", "presentation", "slide", "diapo", "powerpoint", "deck")):
        preset = "presentation"
    elif any(w in m for w in ("tableau blanc", "whiteboard", "brainstorm", "mind map")):
        preset = "whiteboard"
    else:
        preset = "doc"
    # Titre = ce que l'utilisateur veut voir écrit, débarrassé de la commande
    import re
    t = message
    # 1) si une consigne d'écriture est présente, on garde ce qui suit
    m2 = re.search(r"(?:[ée]cri[stre]*|marqu[eé]|intitul[ée]e?|appelle|nomm[ée]e?|titre)\s*"
                   r"(?:dessus|dedans|:|«|\"|')?\s*(.+)$", t, flags=re.I)
    if m2:
        t = m2.group(1)
    else:
        # 2) sinon on retire les formules de commande et le nom de l'app
        t = re.sub(r"\b(va sur|ouvre|cr[ée]e?r?|fais(-moi)?|g[ée]n[èe]re)\b", " ", t, flags=re.I)
        t = re.sub(r"\b(un|une|le|la|les|des|sur|dans|avec|moi)\b", " ", t, flags=re.I)
        t = re.sub(r"\b(canva|design|document|doc|pr[ée]sentation|slide|diapo|tableau blanc|whiteboard)\b",
                   " ", t, flags=re.I)
    t = re.sub(r"^\s*(et|puis)\s+", "", t.strip(), flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" .,:;«»\"'")
    return {"design_type": {"type": "preset", "name": preset},
            "title": (t[:60] if len(t) >= 3 else "Nouveau design")}


# Paramètres connus pour les actions capricieuses (évite un aller-retour d'erreur)
def _known_args(action: str, message: str):
    if "CANVA" in action and "CREATE" in action and "DESIGN" in action:
        return _canva_design_args(message)
    return None


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
    # ── ÉTAPE 2 : construire les arguments À PARTIR DU SCHÉMA RÉEL de l'action.
    # Séparer « quelle action » de « quels arguments » évite les erreurs de forme
    # (objet imbriqué vs chaîne), quelle que soit l'app — donc aussi pour les futures.
    spec = next((a for a in actions if a["name"] == action), {})
    args = _build_args(action, spec, message, ctx)
    if not args:
        a2 = pick.get("arguments")
        args = a2 if isinstance(a2, dict) else {}
    known = _known_args(action, message)      # formes vérifiées à la main (filet de sécurité)
    if known:
        args = {**args, **known}
    steps[0]["label"] = action
    obs = _tool(action, args)
    steps.append({"kind": "obs", "tool": slug, "text": str(obs)[:180]})

    # ── AUTO-CORRECTION : on renvoie l'erreur de l'API au constructeur d'arguments,
    # qui la lit et corrige (jusqu'à 2 tentatives).
    tries = 0
    while _looks_like_failure(obs) and _is_param_error(obs) and tries < 2:
        tries += 1
        new_args = _build_args(action, spec, message, ctx, error=str(obs))
        if not new_args or new_args == args:
            break
        args = new_args
        steps.append({"kind": "action", "tool": slug, "label": f"{action} (correction {tries})"})
        obs = _tool(action, args)
        steps.append({"kind": "obs", "tool": slug, "text": str(obs)[:180]})

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


def _visual_flow(message: str):
    """« écris X sur une slide/visuel/image » → génère une vraie image avec le texte.
    L'API Canva gratuite ne sait pas écrire dans un design (l'autofill exige un plan
    Enterprise) : on produit donc le visuel nous-mêmes, gratuitement et instantanément."""
    from plugins.builtin.visual_maker import make_visual
    steps = [{"kind": "action", "tool": "create_visual", "label": "CREATE_VISUAL"}]
    ex = _llm_json(
        "Extrais ce qu'il faut mettre sur un visuel. JSON STRICT : "
        '{"texte":"…","sous_titre":"","theme":"nova|sombre|chaud|nature|clair",'
        '"format":"carre|story|slide|banniere|post"}. '
        "texte = la phrase à afficher (sans les mots de commande). "
        "format : « slide/présentation » → slide, « story/insta » → story, sinon carre.",
        f"Demande : {message}")
    texte = (ex.get("texte") or "").strip()
    if not texte:
        texte = _canva_design_args(message).get("title", "")
    if not texte or texte == "Nouveau design":
        return {"steps": steps, "done_answer": "Dis-moi le texte à écrire (ex. « fais un visuel avec écrit Bienvenue »)."}
    try:
        path = make_visual(texte, (ex.get("sous_titre") or "").strip(),
                           ex.get("theme") or "nova", ex.get("format") or "carre")
    except Exception as e:
        return {"steps": steps, "done_answer": f"❌ Création du visuel impossible : {str(e)[:160]}"}
    steps.append({"kind": "obs", "tool": "create_visual", "text": path})
    url = f"/agent/file?p={path}&key=__KEY__"
    return {"steps": steps, "done_answer": (
        f"✅ Visuel créé avec « **{texte}** » 🎨\n\n"
        f"![visuel]({url})\n\n"
        f"[Ouvrir en grand]({url})\n\n"
        "_Astuce : je peux changer le thème (nova, sombre, chaud, nature, clair) et le format "
        "(carré, story, slide, bannière). Tu peux ensuite l'importer dans Canva._")}


def _wants_visual(message: str) -> bool:
    """Écrire un texte SUR quelque chose de visuel → générateur d'image."""
    m = message.lower()
    verbe = any(v in m for v in ("écri", "ecri", "marque", "affiche", "mets"))
    support = any(w in m for w in ("visuel", "image", "slide", "affiche", "carte", "story",
                                   "bannière", "banniere", "post", "citation", "miniature"))
    if verbe and support:
        return True
    # « fais un visuel avec écrit … » / « crée une image avec le texte … »
    return bool(("visuel" in m or "image" in m) and any(v in m for v in ("cré", "cre", "fais", "génère", "genere")))


def _complex_app_flow(message: str):
    """Détecte et exécute les actions multi-étapes. Renvoie {steps, done_answer} ou None."""
    m = message.lower()
    if _wants_visual(message):
        return _visual_flow(message)
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

        async def _stream_llm(messages, temp, deadline: float = 150.0, niveau: str = "equilibre"):
            """Diffuse une réponse LLM token par token (dans UN seul thread, non bloquant).

            ⚠️ La version précédente relançait un `run_in_executor` par attente de token :
            sur une petite instance (pool de 5 threads) plusieurs conversations simultanées
            saturaient le pool et le thread producteur ne démarrait même plus → Nova
            « réfléchissait » sans jamais rien renvoyer. Ici le producteur pousse ses tokens
            dans une file asyncio via `call_soon_threadsafe` : 1 thread par réponse, point.
            """
            from llm.client import chat_stream
            box: asyncio.Queue = asyncio.Queue()

            def worker():
                def push(item):
                    try:
                        loop.call_soon_threadsafe(box.put_nowait, item)
                    except RuntimeError:
                        pass                     # boucle fermée (client parti) : on abandonne
                try:
                    for tok in chat_stream(messages, temperature=temp, niveau=niveau):
                        push(("t", tok))
                except Exception as e:
                    push(("t", f"❌ {str(e)[:120]}"))
                finally:
                    push(("end", None))          # garanti, même en cas d'erreur

            loop.run_in_executor(None, worker)
            acc = ""
            fin = loop.time() + deadline
            while True:
                reste = fin - loop.time()
                if reste <= 0:
                    acc += "\n\n⚠️ (réponse interrompue : délai dépassé)"
                    yield "\n\n⚠️ (réponse interrompue : délai dépassé)"
                    break
                try:
                    kind, val = await asyncio.wait_for(box.get(), timeout=reste)
                except asyncio.TimeoutError:
                    continue                     # on repasse par le contrôle de délai
                if kind == "end":
                    break
                acc += val
                yield val
            yield_acc[0] = acc

        yield_acc = [""]
        try:
            _log_activity(message)   # visible immédiatement dans la constellation
            # Le serveur annonce ses VRAIES décisions de routage → sous-bulles authentiques
            # (et non des mots-clés extraits de la question, qui simulaient un raisonnement).
            # Les décisions arrivent UNE PAR UNE, en français clair. Elles partaient
            # auparavant d'un seul bloc, en jargon (« question factuelle · recherche web
            # requise · modèle équilibré ») : illisible, et sans aucune sensation de direct.
            _niv = _niveau_tache(message)
            for _b in _route_bulles(message) + [_NIVEAU_BULLE[_niv]]:
                yield sse({"type": "step", "kind": "route", "tool": "analyse", "text": _b})
                await asyncio.sleep(0.3)      # le temps de les lire défiler
            # 1) Chitchat / info personnelle → streamé directement (aucun outil)
            if _is_smalltalk(message):
                _remember_fact(message)
                async for tok in _stream_llm(_smalltalk_messages(message), 0.6, niveau="rapide"):
                    yield sse({"type": "token", "t": tok})
                yield sse({"type": "answer", "text": yield_acc[0], "final": True})
                yield sse({"type": "model", "name": _modele_utilise()})
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
                        yield sse({"type": "step", "kind": "action", "tool": st["tool"],
                                   "agent": _agent_for_slug(st["tool"]), "q": st.get("label", "")})
                    else:
                        yield sse({"type": "step", "kind": "obs", "tool": st["tool"],
                                   "agent": _agent_for_slug(st["tool"]), "text": st.get("text", "")})
                if direct.get("done_answer") is not None:
                    yield sse({"type": "answer", "text": direct["done_answer"]})
                    yield sse({"type": "done"}); return
                msgs = _format_app_messages(message, direct["action"], direct["obs"], direct["is_write"])
                async for tok in _stream_llm(msgs, 0.2, niveau=_niveau_tache(message)):
                    yield sse({"type": "token", "t": tok})
                yield sse({"type": "answer", "text": yield_acc[0], "final": True})
                yield sse({"type": "model", "name": _modele_utilise()})
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
                    yield sse({"type": "step", "kind": "action", "tool": step.get("tool", ""),
                               "agent": _agent_pour_outil(step.get("tool", "")), "q": str(q2)[:80]})
                elif t == "observation":
                    yield sse({"type": "step", "kind": "obs", "tool": step.get("tool", ""),
                               "agent": _agent_pour_outil(step.get("tool", "")),
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


def _split_tts(text: str, limit: int = 180):
    """Découpe le texte en morceaux ≤ limit, en coupant sur la ponctuation."""
    import re
    parts, cur = [], ""
    for piece in re.split(r"(?<=[.!?;:,])\s+", text):
        while len(piece) > limit:                      # phrase très longue → coupe dure
            parts.append(piece[:limit]); piece = piece[limit:]
        if len(cur) + len(piece) + 1 <= limit:
            cur = (cur + " " + piece).strip()
        else:
            if cur:
                parts.append(cur)
            cur = piece
    if cur:
        parts.append(cur)
    return [p for p in parts if p.strip()][:12]


def _gtts_mp3(text: str) -> bytes:
    """Voix gratuite sans clé (endpoint TTS de Google Traduction). Renvoie du MP3 ou b''.
    Les trames MP3 se concatènent : on assemble simplement les morceaux."""
    import requests
    out = b""
    for i, chunk in enumerate(_split_tts(text)):
        try:
            r = requests.get("https://translate.google.com/translate_tts",
                             params={"ie": "UTF-8", "q": chunk, "tl": "fr",
                                     "client": "tw-ob", "idx": i, "total": 1, "textlen": len(chunk)},
                             headers={"User-Agent": "Mozilla/5.0", "Referer": "https://translate.google.com/"},
                             timeout=15)
            if r.status_code == 200 and r.content[:2] in (b"\xff\xfb", b"\xff\xf3", b"ID", b"\xff\xf2"):
                out += r.content
            else:
                break
        except Exception:
            break
    return out


@router.get("/tts/status")
async def tts_status(key: str = ""):
    _check_key(key)
    """La voix serveur est-elle disponible ? (ElevenLabs si clé, sinon voix gratuite)"""
    return {"enabled": True, "premium": bool(getattr(config, "ELEVENLABS_API_KEY", ""))}


@router.get("/tts")
async def tts(text: str = "", key: str = ""):
    """Voix premium ElevenLabs → renvoie un audio MP3. Repli navigateur si pas de clé."""
    _check_key(key)
    from fastapi.responses import Response, JSONResponse
    ek = getattr(config, "ELEVENLABS_API_KEY", "")
    txt = (text or "").strip()[:900]
    if not txt:
        return JSONResponse({"ok": False, "reason": "empty"}, status_code=400)
    from agent.core import _off
    if not ek:
        # Pas de clé premium → voix gratuite côté serveur (indispensable sur iPhone, où la
        # synthèse du navigateur est bloquée, surtout en app installée).
        mp3 = await _off(_gtts_mp3, txt)
        if mp3:
            return Response(content=mp3, media_type="audio/mpeg",
                            headers={"Cache-Control": "no-store"})
        return JSONResponse({"ok": False, "reason": "tts_unavailable"}, status_code=502)
    import requests
    vid = getattr(config, "ELEVENLABS_VOICE_ID", "")
    model = getattr(config, "ELEVENLABS_MODEL", "eleven_multilingual_v2")
    try:
        # ⚠️ requests est SYNCHRONE : appelé tel quel, il gèlerait toute la boucle asyncio
        # (donc le flux SSE en cours) pendant la génération audio. → thread dédié.
        r = await _off(
            requests.post,
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


@router.get("/selftest")
async def selftest(key: str = ""):
    """Auto-diagnostic complet : vérifie chaque brique et dit ce qui marche / ce qui manque."""
    _check_key(key)
    loop = asyncio.get_running_loop()

    def run() -> dict:
        res = {}

        def check(nom, fn):
            try:
                ok, det = fn()
                res[nom] = {"ok": bool(ok), "detail": str(det)[:200]}
            except Exception as e:
                res[nom] = {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"}

        # 1) Modèle de langage
        def _llm():
            from llm.client import chat
            out = chat([{"role": "user", "content": "Réponds juste : ok"}], temperature=0)
            return bool(out and out.strip()), f"{config.LLM_PROVIDER}/{config.LLM_MODEL} → {str(out)[:40]}"
        check("llm", _llm)

        # 2) Recherche web
        def _web():
            from plugins import get_loader
            from agent.self_heal import safe_tool_call
            o = safe_tool_call(get_loader(), "search_web", {"query": "test", "mode": "web"})
            return (not _looks_like_failure(o)), str(o)[:120]
        check("recherche_web", _web)

        # 3) Vision (photos) — avec contrôle du FORMAT des clés
        def _vision():
            gq = (config.GROQ_API_KEY or "").strip()
            gm = (getattr(config, "GEMINI_API_KEY", "") or "").strip()
            notes = []
            if gq:
                notes.append("Groq ✓" if gq.startswith("gsk_")
                             else f"Groq ⚠ format inattendu (devrait commencer par « gsk_ », ici « {gq[:4]}… »)")
            if gm:
                # Google émet DEUX formats : anciennes clés « AIza… » et nouvelles « AQ.… »
                notes.append("Gemini ✓" if (gm.startswith("AIza") or gm.startswith("AQ."))
                             else f"Gemini ⚠ format inattendu (attendu « AIza… » ou « AQ.… », ici « {gm[:4]}… »)")
            nv = (getattr(config, "NVIDIA_API_KEY", "") or "").strip()
            if nv:
                notes.append("NVIDIA ✓" if nv.startswith("nvapi-")
                             else f"NVIDIA ⚠ format inattendu (devrait commencer par « nvapi- », ici « {nv[:6]}… »)")
            if not notes:
                return False, "aucune clé vision (ajoute GROQ_API_KEY ou GEMINI_API_KEY)"
            ok = any("✓" in n for n in notes)
            return ok, " · ".join(notes)
        check("analyse_photos", _vision)

        # 4) Générateur de visuels
        def _vis():
            from plugins.builtin.visual_maker import make_visual
            p = make_visual("Auto-test Nova", "", "nova", "post")
            return True, p
        check("generateur_visuels", _vis)

        # 5) Voix serveur
        def _tts():
            if getattr(config, "ELEVENLABS_API_KEY", ""):
                return True, "ElevenLabs (premium)"
            mp3 = _gtts_mp3("test")
            return bool(mp3), f"voix gratuite ({len(mp3)} octets)"
        check("voix", _tts)

        # 6) Composio : clé, identité, apps
        def _composio():
            if not (getattr(config, "COMPOSIO_API_KEY", "") or "").strip():
                return False, "COMPOSIO_API_KEY absente"
            accs = _connected_accounts()
            apps = sorted({s for s, _u, _st in accs if s})
            return bool(apps), ("apps connectées : " + ", ".join(apps) if apps else "aucune app connectée")
        check("composio", _composio)

        # 7) Mémoire / profil
        def _mem():
            from agent.profile import list_facts
            n = len(list_facts())
            try:
                get_memory().recall_recent(_PROFILE_ID, 1)
            except Exception:
                from agent.memory import init_db      # table absente → on la crée
                init_db()
                get_memory().recall_recent(_PROFILE_ID, 1)
            return True, f"{n} fait(s) mémorisé(s)"
        check("memoire", _mem)

        # 8) Automatisations
        def _auto():
            from agent.automations import list_all
            items = list_all()
            return True, f"{len(items)} automatisation(s), {sum(1 for i in items if i.get('active'))} active(s)"
        check("automatisations", _auto)

        # 9) Compteur d'énergie
        def _usage():
            from llm import usage as U
            u, l = U.get_usage("cerebras")
            return True, f"cerebras {u}/{l}"
        check("energie", _usage)

        # 10) Actions par app connectée (détecte un connecteur cassé)
        try:
            for slug in sorted({s for s, _u, _st in _connected_accounts() if s})[:6]:
                acts = _composio_list_actions(slug)
                res[f"app_{slug}"] = {"ok": bool(acts), "detail": f"{len(acts)} action(s) disponibles"}
        except Exception as e:
            res["apps_detail"] = {"ok": False, "detail": str(e)[:150]}

        ko = [k for k, v in res.items() if not v["ok"]]
        return {"resultats": res, "ok": len(res) - len(ko), "ko": len(ko), "a_corriger": ko}

    return await loop.run_in_executor(None, run)


def _diag_un_llm(nom: str) -> dict:
    """Teste UN fournisseur pour de vrai : format de clé, appel minimal, latence, erreur exacte."""
    import time as _t
    from llm.client import (MODELES, _fournisseur_hs, _MODELES_OK, _BUDGET_APPEL,
                            _providers_disponibles)
    # Préfixes attendus : une clé collée de travers (nom de modèle, espace en trop…) est
    # la panne la plus fréquente, et la plus invisible.
    prefixes = {"nvidia": "nvapi-", "groq": "gsk_", "openrouter": "sk-or-",
                "gemini": ("AIza", "AQ."), "cerebras": "csk-", "xai": "xai-"}
    cle = (getattr(config, f"{nom.upper()}_API_KEY", "") or "").strip()
    d = {"fournisseur": nom, "cle_presente": bool(cle), "ok": False,
         "modele": "", "latence_s": None, "erreur": "", "conseil": ""}
    if not cle:
        d["conseil"] = "aucune clé configurée sur Render"
        return d
    d["cle_apercu"] = cle[:6] + "…" + cle[-4:] if len(cle) > 12 else "trop courte"
    att = prefixes.get(nom)
    if att and not cle.startswith(att):
        d["conseil"] = (f"la clé ne commence pas par « {att if isinstance(att, str) else ' ou '.join(att)} » "
                        "— vérifie que tu as bien collé la CLÉ et pas autre chose")
    if cle != cle.strip() or " " in cle:
        d["conseil"] = "la clé contient un espace — recolle-la sans espace ni retour à la ligne"
    d["ecarte_pour_le_moment"] = _fournisseur_hs(nom)

    fn = dict((n, f) for n, f, _m in _providers_disponibles("equilibre")).get(nom)
    if fn is None:
        d["erreur"] = "fournisseur non pris en charge"
        return d
    modele = MODELES.get(nom, {}).get("equilibre") or config.LLM_MODEL
    t0 = _t.monotonic()
    jeton = _BUDGET_APPEL.set(45.0)          # test large : on veut MESURER, pas juger vite
    try:
        out = fn([{"role": "user", "content": "Réponds juste : ok"}], modele, 0.0, "equilibre")
        d.update(ok=bool(out and out.strip()), modele=_MODELES_OK.get(nom) or modele,
                 reponse=(out or "")[:60])
    except TypeError:
        try:
            out = fn([{"role": "user", "content": "Réponds juste : ok"}], modele, 0.0)
            d.update(ok=bool(out and out.strip()), modele=_MODELES_OK.get(nom) or modele,
                     reponse=(out or "")[:60])
        except Exception as e:
            d["erreur"] = str(e)[:300]
    except Exception as e:
        d["erreur"] = str(e)[:300]
    finally:
        _BUDGET_APPEL.reset(jeton)
    d["latence_s"] = round(_t.monotonic() - t0, 1)
    # Le seuil qui compte n'est pas un chiffre arbitraire : c'est le délai réellement
    # appliqué en usage normal. Au-delà, ce fournisseur sera abandonné même s'il marche.
    from llm.client import TIMEOUT_LLM as _TL
    if d["ok"] and d["latence_s"] > _TL:
        d["conseil"] = (f"répond mais LENTEMENT ({d['latence_s']} s) — c'est plus que "
                        f"LLM_TIMEOUT ({_TL} s), donc il est abandonné en usage normal. "
                        "Augmente LLM_TIMEOUT sur Render si tu veux l'utiliser.")
    elif not d["ok"] and not d["conseil"]:
        e = d["erreur"].lower()
        if "401" in e or "unauthorized" in e or "invalid" in e:
            d["conseil"] = "clé refusée — régénère-la"
        elif "403" in e:
            d["conseil"] = "accès refusé — l'API n'est peut-être pas activée pour ce compte"
        elif "429" in e or "rate" in e:
            d["conseil"] = "limite atteinte pour le moment — ça se débloque tout seul"
        elif "timed out" in e or "timeout" in e:
            d["conseil"] = (f"n'a pas répondu en {d['latence_s']} s — service lent ou injoignable "
                            "depuis Render")
        elif "402" in e or "payment" in e:
            d["conseil"] = "offre gratuite épuisée sur ce compte"
    return d


@router.get("/diag/llm")
async def diag_llm(key: str = ""):
    """Teste CHAQUE fournisseur séparément et dit ce qui cloche, précisément.

    Ouvre /agent/diag/llm?key=TA_CLE — plus besoin de deviner quelle clé pose problème.
    Les fournisseurs sont testés EN PARALLÈLE : le diagnostic complet prend le temps du
    plus lent, pas la somme.
    """
    _check_key(key)
    from llm.client import _providers_disponibles, TIMEOUT_LLM, TIMEOUT_CHAINE
    noms = [n for n, _f, _m in _providers_disponibles("equilibre")]
    loop = asyncio.get_running_loop()
    res = await asyncio.gather(*[loop.run_in_executor(None, _diag_un_llm, n) for n in noms])

    ok = [r for r in res if r["ok"]]
    lents = [r for r in ok if (r["latence_s"] or 0) > TIMEOUT_LLM]
    if not noms:
        resume = "Aucune clé de modèle n'est configurée sur Render."
    elif not ok:
        resume = ("❌ Aucun fournisseur ne répond. Détail par fournisseur ci-dessous — "
                  "regarde le champ « conseil ».")
    else:
        resume = (f"✅ {len(ok)} fournisseur(s) sur {len(noms)} répondent : "
                  + ", ".join(f"{r['fournisseur']} ({r['latence_s']} s)" for r in ok))
        if lents:
            resume += (f" — ⚠️ {', '.join(r['fournisseur'] for r in lents)} dépasse(nt) "
                       f"LLM_TIMEOUT ({TIMEOUT_LLM} s) et sera(ont) abandonné(s) en usage normal.")
    return {"resume": resume,
            "reglages": {"LLM_TIMEOUT": TIMEOUT_LLM, "LLM_TIMEOUT_TOTAL": TIMEOUT_CHAINE},
            "fournisseurs": res}


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
async def usage(key: str = ""):
    _check_key(key)
    """Consommation de tokens du jour (Cerebras + Groq) — pour la barre sur /nova.
    Lecture durable (Supabase si configuré), donc fiable après redéploiement."""
    from llm import usage as U
    out, tu, tl = {}, 0, 0
    for p in ("nvidia", "cerebras", "groq", "gemini"):
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
    # Nom de fichier assaini : on retire tout séparateur ET les « .. » (traversée de dossier)
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", (up.filename or "fichier"))[:80]
    safe = safe.replace("..", "_").lstrip(".") or "fichier"
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


# ═══ MODE COURS ═══════════════════════════════════════════════════════════════
# Nova écoute un cours entier, transcrit au fil de l'eau (Whisper/Groq), efface
# l'audio aussitôt, et rend à la fin une synthèse + des fiches de révision.
# Tout est exécuté dans un thread : une transcription ne doit jamais figer le serveur.

class CoursReq(BaseModel):
    key: Optional[str] = None
    titre: Optional[str] = None
    matiere: Optional[str] = None
    id: Optional[str] = None


@router.get("/cours/dispo")
async def cours_dispo(key: str = ""):
    """Vraie vérification de bout en bout AVANT d'enregistrer 2 h pour rien : Nova envoie
    un bip de 0,4 s à Whisper et n'autorise le départ que s'il répond."""
    _check_key(key)
    from agent import cours
    from agent.core import _off
    ok, raison = await _off(cours.verifier_transcription)
    return {"ok": ok, "raison": raison}


@router.post("/cours/start")
async def cours_start(req: CoursReq):
    _check_key(req.key or "")
    from agent import cours
    from agent.core import _off
    if not cours.transcription_dispo():
        raise HTTPException(status_code=503, detail="Transcription indisponible : GROQ_API_KEY absente.")
    s = await _off(cours.demarrer, req.titre or "", req.matiere or "")
    return {"id": s["id"], "titre": s["titre"]}


@router.post("/cours/chunk")
async def cours_chunk(request: Request):
    """Une tranche d'audio (multipart : id, key, file, secondes). L'audio n'est jamais stocké."""
    from agent import cours
    from agent.core import _off
    form = await request.form()
    _check_key(str(form.get("key") or ""))
    sid = str(form.get("id") or "")
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(status_code=400, detail="Aucune tranche audio reçue.")
    try:
        secondes = float(form.get("secondes") or 0)
    except ValueError:
        secondes = 0.0
    data = await up.read()
    if not data:
        raise HTTPException(status_code=400, detail="Tranche vide.")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Tranche trop volumineuse (max 25 Mo).")
    nom = str(getattr(up, "filename", "") or "tranche.webm")[:60]
    try:
        return await _off(cours.ajouter_tranche, sid, data, secondes, nom)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session inconnue.")
    except Exception as e:
        # 502 = « réessaie » : le navigateur remet la tranche en file, aucun mot n'est perdu.
        raise HTTPException(status_code=502, detail=str(e)[:200])


def _vue_cours(s: dict) -> dict:
    """Ce que l'UI a besoin de connaître d'une session."""
    return {"id": s["id"], "titre": s["titre"], "etat": s.get("etat", ""),
            "synthese": s.get("synthese", ""), "fiches": s.get("fiches", []),
            "trous": s.get("trous", []), "erreurs": s.get("erreurs", []),
            "mots": len(s.get("transcript", "").split()), "secondes": s.get("secondes", 0)}


@router.post("/cours/stop")
async def cours_stop(req: CoursReq):
    """Lance la synthèse EN TÂCHE DE FOND et rend la main tout de suite.

    Une synthèse peut durer plusieurs minutes quand les modèles gratuits sont saturés :
    garder la requête ouverte la ferait couper par le navigateur ou le proxy. L'UI suit
    l'avancement via /cours/detail, et le travail aboutit même si l'onglet est fermé.
    """
    _check_key(req.key or "")
    from agent import cours
    from agent.core import _off
    try:
        s = await _off(cours.lancer_synthese, req.id or "")
    except KeyError:
        raise HTTPException(status_code=404, detail="Session inconnue.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:250])
    return _vue_cours(s)


@router.get("/cours")
async def cours_liste(key: str = ""):
    _check_key(key)
    from agent import cours
    from agent.core import _off
    return {"sessions": await _off(cours.lister)}


@router.get("/cours/detail")
async def cours_detail(id: str = "", key: str = ""):
    _check_key(key)
    from agent import cours
    from agent.core import _off
    try:
        s = await _off(cours._lire, id)
    except Exception:
        raise HTTPException(status_code=404, detail="Session inconnue.")
    return {k: v for k, v in s.items() if k not in ("en_attente", "condenses")}


@router.get("/cours/export")
async def cours_export(id: str = "", key: str = ""):
    """Le cours en Markdown — à garder chez toi : le disque du serveur est effacé aux redémarrages."""
    _check_key(key)
    from fastapi.responses import Response
    from agent import cours
    from agent.core import _off
    try:
        md = await _off(cours.markdown, id)
        s = await _off(cours._lire, id)
    except Exception:
        raise HTTPException(status_code=404, detail="Session inconnue.")
    nom = re.sub(r"[^A-Za-z0-9À-ÿ _-]", "", s.get("titre", "cours"))[:60].strip() or "cours"
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nom}.md"'})


@router.delete("/cours")
async def cours_supprimer(id: str = "", key: str = ""):
    _check_key(key)
    from agent import cours
    from agent.core import _off
    return {"ok": await _off(cours.supprimer, id)}


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


# ── Réveil vocal à distance (PC → téléphone) ──────────────────────────────────
_WAKE = {"ts": 0.0, "from": ""}


@router.post("/voice/wake")
async def voice_wake(req: AutoReq):
    """Demande à tes autres appareils d'ouvrir le mode vocal (page Nova ouverte)."""
    _check_key(req.key or "")
    import time as _t
    _WAKE["ts"] = _t.time()
    _WAKE["from"] = (req.titre or "un autre appareil")[:40]
    return {"ok": True}


@router.get("/voice/pending")
async def voice_pending(since: float = 0.0, key: str = ""):
    _check_key(key)
    """Un réveil a-t-il été demandé depuis ce timestamp ? (interrogé par les appareils)"""
    import time as _t
    fresh = _WAKE["ts"] > since and (_t.time() - _WAKE["ts"]) < 60
    return {"wake": bool(fresh), "ts": _WAKE["ts"], "from": _WAKE["from"]}


@router.get("/file")
async def serve_file(p: str = "", key: str = ""):
    """Sert un fichier produit par Nova (visuels, exports). Chemin restreint à output/ et data/."""
    _check_key(key)
    from fastapi.responses import FileResponse
    from pathlib import Path as _P
    try:
        target = _P(p).resolve()
        roots = [_P("output").resolve(), _P("data").resolve()]
        # is_relative_to : vrai confinement (startswith laisserait passer « output-secret/ »)
        inside = any(target == r or r in target.parents for r in roots)
        if not inside or not target.is_file():
            raise HTTPException(status_code=404, detail="Fichier introuvable.")
        return FileResponse(str(target))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")


@router.get("/profile")
async def profile_get(key: str = ""):
    """Faits que Nova retient sur toi (structurés, pas des bouts de messages)."""
    _check_key(key)
    from agent.profile import list_facts, CATEGORIES
    return {"facts": list_facts(), "categories": CATEGORIES}


@router.delete("/profile")
async def profile_delete(id: str = "", key: str = ""):
    """Supprime un fait, ou tout le profil si id est vide."""
    _check_key(key)
    from agent.profile import delete_fact, clear_all
    if id:
        return {"ok": delete_fact(id)}
    clear_all()
    return {"ok": True}


class TitleReq(BaseModel):
    key: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None


@router.post("/title")
async def make_title(req: TitleReq):
    """Titre court et propre pour une conversation (3-5 mots), généré après le 1er échange."""
    _check_key(req.key or "")
    from llm.client import chat
    from agent.core import _off
    q = (req.question or "").strip()[:400]
    a = (req.answer or "").strip()[:400]
    if not q:
        raise HTTPException(status_code=400, detail="question requise.")
    try:
        # ⚠️ L'UI demande un titre juste après chaque 1ère réponse. En appelant `chat()`
        # directement sur la boucle, ce titre figeait le serveur (et donc le streaming
        # de la conversation suivante) le temps d'un aller-retour modèle.
        out = await _off(chat, [
            {"role": "system", "content": (
                "Donne un TITRE de conversation en français : 3 à 5 mots, clair et descriptif, "
                "première lettre en majuscule, SANS guillemets, SANS ponctuation finale, "
                "SANS reprendre la phrase mot pour mot. "
                "Exemples : « Création d'un design Canva », « Agenda de la semaine », "
                "« Bilan des points GPS ». Réponds UNIQUEMENT par le titre.")},
            {"role": "user", "content": f"Question : {q}\nRéponse : {a}"},
        ], temperature=0.3)
        titre = (out or "").strip().strip('"«».').split("\n")[0][:48]
    except Exception:
        titre = ""
    return {"title": titre or q[:42]}


@router.get("/activity")
async def activity(key: str = ""):
    _check_key(key)          # l'activité contient des extraits de tes messages
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
        from agent.core import _off
        h = {"x-api-key": ck, "Content-Type": "application/json"}
        try:
            r = await _off(requests.get, "https://backend.composio.dev/api/v3/connected_accounts",
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
    key: Optional[str] = None          # requis : la route fait parler l'agent


class ChatResponse(BaseModel):
    answer: str
    agent_id: str
    iterations: int
    steps: Optional[list] = None
    memory_count: Optional[int] = None


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    # ⚠️ Route protégée : sans clé, n'importe qui pourrait faire parler l'agent
    # (et consommer tes jetons, voire déclencher des actions sur tes apps).
    _check_key(req.key or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
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
async def agent_history(agent_id: str, limit: int = 20, key: str = ""):
    # ⚠️ Contient tes conversations : jamais accessible sans clé.
    _check_key(key)
    return {"agent_id": agent_id, "history": get_memory().recall_recent(agent_id, limit)}


@router.delete("/{agent_id}/memory")
async def clear_memory(agent_id: str, key: str = ""):
    # ⚠️ Destructif : sans clé, n'importe qui pourrait effacer ta mémoire.
    _check_key(key)
    get_memory().clear(agent_id)
    return {"message": f"Mémoire de '{agent_id}' effacée."}


@router.get("/{agent_id}/summary")
async def get_summary(agent_id: str, key: str = ""):
    _check_key(key)          # résumé de tes échanges
    summary = get_memory().get_summary(agent_id)
    return {"agent_id": agent_id, "summary": summary or "Aucun résumé disponible."}


@router.get("/tools/list")
async def available_tools(key: str = ""):
    _check_key(key)          # ne pas divulguer les capacités de l'agent
    return {"tools": get_loader().list_all()}
