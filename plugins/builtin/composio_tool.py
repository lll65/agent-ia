"""
Composio — connecteur universel (Gmail, Google Agenda, Slack, Notion… 1000+ apps).

Nova exécute une "action" Composio (ex: GMAIL_FETCH_EMAILS) sur un compte que
l'utilisateur a connecté sur composio.dev (l'auth OAuth est gérée par Composio).

Utilise l'API HTTP Composio directement (pas de dépendance en plus → ne casse pas le
déploiement). Essaie l'API v3 puis v2. Sans COMPOSIO_API_KEY : outil inactif (message clair).
"""
import json
from plugins.base import Plugin

_BASE = "https://backend.composio.dev"

# Actions courantes (pour guider le modèle)
_COMMON = (
    "GMAIL_FETCH_EMAILS (lire mails), GMAIL_SEND_EMAIL, "
    "GOOGLECALENDAR_EVENTS_LIST (agenda), GOOGLECALENDAR_CREATE_EVENT, "
    "GOOGLECALENDAR_QUICK_ADD, SLACK_SEND_MESSAGE, NOTION_CREATE_PAGE"
)


def _to_dict(arguments):
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        return json.loads(arguments)
    except Exception:
        return {}


class ComposioPlugin(Plugin):
    name = "connected_app"
    description = (
        "Agit sur les apps connectées de l'utilisateur via Composio (Gmail, Google Agenda, "
        "Slack, Notion…). PARAMS obligatoires — exemple EXACT :\n"
        'PARAMS: {"command": "GOOGLECALENDAR_EVENTS_LIST", "arguments": {"maxResults": 10}}\n'
        "command = nom d'action Composio en MAJUSCULES (ex: " + _COMMON + "). "
        "arguments = objet JSON des paramètres de l'action."
    )
    parameters = {
        "command": {"type": "string", "description": "Nom de l'action Composio en MAJUSCULES (ex: GOOGLECALENDAR_EVENTS_LIST)", "required": False},
        "arguments": {"type": "string", "description": "Paramètres de l'action en JSON (optionnel)", "required": False},
    }

    def run(self, command: str = "", arguments="", action: str = "",
            name: str = "", tool: str = "", app: str = "", **kw) -> str:
        from config import config
        # Ultra-tolérant : récupère le nom d'action dans n'importe quelle clé raisonnable
        command = (command or action or name or tool or app or kw.get("query") or "").strip()
        if not arguments and isinstance(kw, dict):
            arguments = kw.get("arguments") or kw.get("input") or kw.get("params") or ""
        key = getattr(config, "COMPOSIO_API_KEY", "")
        if not key:
            return ("⚠️ Composio non configuré. Crée un compte gratuit sur composio.dev, "
                    "connecte tes apps (Gmail, Agenda…), et mets COMPOSIO_API_KEY dans les variables Render.")
        if not command:
            return f"⚠️ Précise 'command'. Ex : {_COMMON}"
        action = command

        import requests
        args = _to_dict(arguments)
        user = getattr(config, "COMPOSIO_USER_ID", "default") or "default"
        headers = {"x-api-key": key, "Content-Type": "application/json"}

        # 1) API v3
        try:
            r = requests.post(f"{_BASE}/api/v3/tools/execute/{action}",
                              headers=headers, json={"user_id": user, "arguments": args}, timeout=30)
            if r.status_code == 200:
                return _fmt(action, r.json())
            v3_err = f"v3 {r.status_code}: {r.text[:200]}"
        except Exception as e:
            v3_err = f"v3 exception: {str(e)[:150]}"

        # 2) API v2 (fallback)
        try:
            r = requests.post(f"{_BASE}/api/v2/actions/{action}/execute",
                              headers=headers, json={"entityId": user, "input": args}, timeout=30)
            if r.status_code == 200:
                return _fmt(action, r.json())
            v2_err = f"v2 {r.status_code}: {r.text[:200]}"
        except Exception as e:
            v2_err = f"v2 exception: {str(e)[:150]}"

        return (f"❌ Action Composio '{action}' échouée.\n{v3_err}\n{v2_err}\n"
                "Vérifie que l'app est bien connectée sur composio.dev et que le nom d'action est exact.")


def _fmt(action: str, data) -> str:
    """Rend la réponse Composio lisible pour le LLM."""
    try:
        payload = data.get("data", data) if isinstance(data, dict) else data
        txt = json.dumps(payload, ensure_ascii=False, indent=1)
    except Exception:
        txt = str(data)
    return f"✅ [{action}] résultat :\n{txt[:2500]}"
