"""
L'escouade Nova — sous-agents spécialisés + suivi d'activité en temps réel.

Chaque sous-agent a son domaine, ses outils/apps, son icône et sa couleur.
Nova joue le chef d'orchestre : elle route la demande vers le bon spécialiste.
L'activité est enregistrée en mémoire pour alimenter la constellation (/nova/brain).
"""
import time
import threading

# ── Définition de l'escouade ──────────────────────────────────────────────────
SQUAD = [
    {
        "id": "agenda", "name": "Agenda", "icon": "📅", "color": "#22d3ee",
        "apps": ["googlecalendar"], "tools": ["connected_app"],
        "desc": "Événements, rendez-vous, créneaux libres, planification",
        "keywords": ("agenda", "calendrier", "calendar", "rendez-vous", "rdv", "réunion",
                     "reunion", "événement", "evenement", "planning", "créneau", "creneau"),
    },
    {
        "id": "mails", "name": "Mails", "icon": "📧", "color": "#a78bfa",
        "apps": ["gmail"], "tools": ["connected_app"],
        "desc": "Lecture, tri, résumé et envoi d'emails",
        "keywords": ("mail", "mails", "email", "e-mail", "gmail", "inbox", "messagerie",
                     "courriel", "boîte mail", "boite mail"),
    },
    {
        "id": "dev", "name": "Dev", "icon": "💻", "color": "#34d399",
        "apps": ["github"], "tools": ["connected_app", "build_full_project", "exec_python"],
        "desc": "Code, dépôts GitHub, projets, débogage",
        "keywords": ("github", "code", "coder", "dépôt", "depot", "repo", "projet", "bug",
                     "script", "api", "fonction", "programme", "développe", "developpe"),
    },
    {
        "id": "crea", "name": "Créa", "icon": "🎨", "color": "#f472b6",
        "apps": ["canva"], "tools": ["connected_app"],
        "desc": "Designs, visuels, présentations",
        "keywords": ("canva", "design", "visuel", "affiche", "flyer", "maquette", "logo",
                     "présentation", "presentation", "slide"),
    },
    {
        "id": "veille", "name": "Veille", "icon": "🔍", "color": "#fbbf24",
        "apps": [], "tools": ["search_web"],
        "desc": "Recherche web, actualité, vérification des faits",
        "keywords": ("actu", "actualité", "actualités", "news", "cherche", "recherche",
                     "tendance", "météo", "meteo", "qui est", "c'est quoi", "информация"),
    },
    {
        "id": "projets", "name": "Projets", "icon": "📋", "color": "#60a5fa",
        "apps": ["linear", "notion", "trello", "asana", "jira"], "tools": ["connected_app"],
        "desc": "Tickets, tâches, notes et bases de connaissances",
        "keywords": ("linear", "notion", "trello", "asana", "jira", "ticket", "issue",
                     "tâche", "tache", "backlog", "sprint", "note"),
    },
    {
        "id": "fichiers", "name": "Fichiers", "icon": "📎", "color": "#c084fc",
        "apps": ["googledrive", "googledocs", "googlesheets"],
        "tools": ["analyze_document", "read_file", "write_file"],
        "desc": "Documents, PDF, images, feuilles de calcul",
        "keywords": ("document", "pdf", "fichier", "drive", "sheets", "docs", "image",
                     "photo", "tableur"),
    },
]

_BY_ID = {a["id"]: a for a in SQUAD}


def get_squad() -> list:
    return SQUAD


def get_agent(agent_id: str):
    return _BY_ID.get(agent_id)


def pick_agent(message: str) -> str:
    """Choisit le sous-agent le plus pertinent (id) — 'nova' si aucun ne se détache."""
    m = (message or "").lower()
    best, score = "nova", 0
    for a in SQUAD:
        s = sum(1 for k in a["keywords"] if k in m)
        if s > score:
            best, score = a["id"], s
    return best


# ── Suivi d'activité (alimente la constellation) ──────────────────────────────
_LOCK = threading.Lock()
_EVENTS = []            # derniers événements : {agent, tool, label, ts}
_COUNTS = {}            # nombre d'activations par agent
_MAX_EVENTS = 60


def record(agent_id: str, tool: str = "", label: str = "") -> None:
    """Enregistre une activation (non bloquant, jamais fatal)."""
    try:
        with _LOCK:
            _EVENTS.append({"agent": agent_id or "nova", "tool": tool or "",
                            "label": (label or "")[:60], "ts": time.time()})
            if len(_EVENTS) > _MAX_EVENTS:
                del _EVENTS[:-_MAX_EVENTS]
            _COUNTS[agent_id] = _COUNTS.get(agent_id, 0) + 1
    except Exception:
        pass


def snapshot() -> dict:
    """État courant pour l'UI : escouade, compteurs, activité récente."""
    now = time.time()
    with _LOCK:
        events = list(_EVENTS[-20:])
        counts = dict(_COUNTS)
    # Un agent est "actif" s'il a bougé dans les 20 dernières secondes
    active = {}
    for e in events:
        if now - e["ts"] < 20:
            active[e["agent"]] = max(active.get(e["agent"], 0), 1 - (now - e["ts"]) / 20)
    return {
        "squad": [{k: a[k] for k in ("id", "name", "icon", "color", "apps", "desc")} for a in SQUAD],
        "counts": counts,
        "active": active,
        "events": [{"agent": e["agent"], "label": e["label"], "tool": e["tool"],
                    "ago": round(now - e["ts"], 1)} for e in reversed(events)],
        "total": sum(counts.values()),
    }
