"""
Composio — connecteur universel (Gmail, Google Agenda, Slack, Notion… 1000+ apps).

Nova exécute une "action" Composio (ex: GMAIL_FETCH_EMAILS) sur un compte que
l'utilisateur a connecté sur composio.dev (l'auth OAuth est gérée par Composio).

Utilise l'API HTTP Composio directement (pas de dépendance en plus → ne casse pas le
déploiement). Essaie l'API v3 puis v2. Sans COMPOSIO_API_KEY : outil inactif (message clair).
"""
import json
import re
from plugins.base import Plugin

_BASE = "https://backend.composio.dev"

# Repli si Composio est injoignable. La VRAIE liste est découverte en direct :
# une liste figée devenait fausse dès que l'utilisateur connectait une nouvelle app
# (constaté avec Google Sheets — Nova répondait « donne-moi la commande » à quelqu'un
# qui venait justement de connecter l'app pour ne plus avoir à la connaître).
_COMMON = (
    "GMAIL_FETCH_EMAILS (lire mails), GMAIL_SEND_EMAIL, "
    "GOOGLECALENDAR_EVENTS_LIST (agenda), GOOGLECALENDAR_CREATE_EVENT, "
    "GOOGLECALENDAR_QUICK_ADD, SLACK_SEND_MESSAGE, NOTION_CREATE_PAGE"
)


def catalogue(indice: str = "", budget: int = 1800, max_actions: int = 12) -> str:
    """Actions RÉELLEMENT disponibles sur les apps que l'utilisateur a connectées.

    C'est la réponse à « pourquoi il n'a pas les commandes lui-même ? » : dès qu'une app
    est connectée, ses actions sont découvertes chez Composio et données au modèle. Rien
    n'est codé en dur, donc rien ne devient périmé.
    """
    try:
        from api.agent import _connected_accounts, _composio_list_actions
    except Exception:
        return _COMMON
    try:
        apps = sorted({s for s, _u, _st in _connected_accounts() if s})
    except Exception:
        return _COMMON
    if not apps:
        return ("aucune app n'est connectée pour l'instant — connecte-la sur composio.dev, "
                "ses actions seront disponibles immédiatement")
    # L'app évoquée dans la demande passe en premier : c'est celle qui intéresse.
    # L'app évoquée passe devant. On compare sans espaces ni tirets bas, et on tolère
    # le singulier : l'utilisateur écrit « google sheet », le slug est « googlesheets ».
    ind = re.sub(r"[^a-z0-9]", "", (indice or "").lower())

    def _evoquee(app: str) -> bool:
        nu = re.sub(r"[^a-z0-9]", "", app.lower())
        return bool(nu) and (nu in ind or (len(nu) > 4 and nu[:-1] in ind))

    apps.sort(key=lambda a: (not _evoquee(a), a))

    # ⚠️ AUCUNE app connectée ne doit disparaître de cette liste. Auparavant deux coupes
    # s'additionnaient — un plafond de 6 apps, puis une troncature brutale du texte — et
    # le modèle ne voyait que les premières. Il répondait alors, en toute bonne foi,
    # « je n'ai pas accès à Notion » à quelqu'un dont Notion ÉTAIT connecté.
    # Ici le budget est RÉPARTI : on raccourcit la liste d'actions de chacune, jamais
    # la liste des apps elle-même.
    catalogues = []
    for app in apps:
        try:
            actes = [a["name"] for a in (_composio_list_actions(app) or [])]
        except Exception:
            actes = []
        if actes:
            catalogues.append((app, actes))
    if not catalogues:
        return _COMMON

    # L'app évoquée dans la demande reçoit une part plus large : c'est celle qui sert.
    parts = {app: (3 if _evoquee(app) else 1) for app, _ in catalogues}
    total = sum(parts.values())
    blocs = []
    for app, actes in catalogues:
        part = max(120, int(budget * parts[app] / total))
        gardes, taille = [], 0
        for nom in actes[:max_actions]:
            if gardes and taille + len(nom) + 2 > part:
                break
            gardes.append(nom)
            taille += len(nom) + 2
        reste = len(actes) - len(gardes)
        blocs.append(f"• {app} : {', '.join(gardes)}"
                     + (f" (+{reste} autres — demande-les si besoin)" if reste > 0 else ""))
    return "\n".join(blocs)


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
            name: str = "", tool: str = "", app: str = "", user_id: str = "", **kw) -> str:
        from config import config
        # Ultra-tolérant : récupère le nom d'action dans n'importe quelle clé raisonnable
        command = (command or action or name or tool or app or kw.get("query") or "").strip()
        if not arguments and isinstance(kw, dict):
            arguments = kw.get("arguments") or kw.get("input") or kw.get("params") or ""
        # .strip() : élimine espaces/retours-ligne collés depuis Render (piège classique)
        key = (getattr(config, "COMPOSIO_API_KEY", "") or "").strip()
        if not key:
            return ("⚠️ Composio non configuré. Crée un compte gratuit sur composio.dev, "
                    "connecte tes apps (Gmail, Agenda…), et mets COMPOSIO_API_KEY dans les variables Render.")
        if not command:
            # On ne renvoie plus une liste figée : on va CHERCHER les actions réelles des
            # apps connectées. Le modèle peut alors relancer avec le bon nom tout seul.
            indice = " ".join(str(v) for v in kw.values() if isinstance(v, str))
            return ("⚠️ Il manque le nom de l'action ('command'). Voici les actions "
                    "réellement disponibles sur tes apps connectées :\n"
                    + catalogue(indice) +
                    "\n\nRelance connected_app avec la bonne action, par exemple :\n"
                    'PARAMS: {"command": "GOOGLESHEETS_BATCH_GET", "arguments": {}}')
        action = command

        import requests
        args = _to_dict(arguments)
        # Identité : celle résolue par l'appelant (compte réellement connecté), sinon la config.
        user = (user_id or "").strip() or getattr(config, "COMPOSIO_USER_ID", "default") or "default"
        # En-tête selon le TYPE de clé :
        #   ck_ → clé "consumer/MCP"  → en-tête x-consumer-api-key
        #   ak_ → clé de projet dev   → en-tête x-api-key
        # On envoie les deux en-têtes possibles : le serveur utilise celui qu'il reconnaît.
        headers = {"Content-Type": "application/json"}
        if key.startswith("ck_"):
            headers["x-consumer-api-key"] = key
        else:
            headers["x-api-key"] = key

        # API v3 (la v2 est fermée : renvoie 410 "upgrade to v3").
        try:
            r = requests.post(f"{_BASE}/api/v3/tools/execute/{action}",
                              headers=headers, json={"user_id": user, "arguments": args}, timeout=30)
            if r.status_code == 200:
                return _fmt(action, r.json())
            tl = r.text.lower().replace("_", "")
            # 403 : clé VALIDE mais sans permission d'exécution (tool_execution)
            if r.status_code == 403 or "insufficientpermission" in tl or "toolexecution" in tl:
                return ("🔑 Ta clé Composio est VALIDE mais n'a pas la permission d'exécuter des outils "
                        "(il lui manque « tool_execution » en écriture). "
                        "Composio → projet → Clés API → accorde « tool_execution » (write) à ta clé, "
                        "OU crée une nouvelle clé de projet en ACCÈS COMPLET, mets-la dans COMPOSIO_API_KEY, redéploie.")
            # 401 : clé refusée
            if r.status_code == 401 or "invalidapikey" in tl:
                kind = "consumer/MCP (ck_)" if key.startswith("ck_") else "projet (ak_)"
                return (f"❌ Clé API Composio refusée (type {kind}, variable COMPOSIO_API_KEY). "
                        "Prends une clé de PROJET « ak_ » en accès complet sur la plateforme développeurs, "
                        "colle-la sans espace puis redéploie.")
            # 404 / nom inconnu : le modèle a inventé une action. On lui donne la VRAIE
            # liste plutôt que de lui demander de deviner une deuxième fois.
            if r.status_code == 404 or "not found" in tl or "notfound" in tl:
                dispo = catalogue(action)
                # ⚠️ Le message se CONTREDISAIT : « GOOGLE_MAPS_GET_DIRECTION n'existe
                # pas », suivi d'une liste où il figure. Quand le nom est bel et bien au
                # catalogue, ce n'est pas une action inventée — c'est l'app qui refuse
                # (droits manquants, API non activée côté Google, compte incomplet).
                if action and action.upper() in (dispo or "").upper():
                    return (f"🔒 **{action}** existe bien, mais l'application la refuse "
                            f"(erreur 404 de son côté, pas du nôtre).\n"
                            "C'est presque toujours une autorisation ou une API non "
                            "activée sur le compte : reconnecte l'app sur composio.dev en "
                            "acceptant TOUTES les autorisations, et vérifie que l'API "
                            "correspondante est activée chez le fournisseur.")
                return (f"❌ L'action « {action} » n'existe pas. Actions réellement "
                        f"disponibles sur tes apps connectées :\n{dispo}\n"
                        "Relance connected_app avec l'un de ces noms exacts.")
            return (f"❌ Action Composio '{action}' échouée (HTTP {r.status_code}).\n{r.text[:220]}\n"
                    "Vérifie que l'app est connectée sur composio.dev et que le nom d'action est exact.")
        except Exception as e:
            return f"❌ Composio injoignable : {type(e).__name__}: {str(e)[:180]}"


_MAX_RESULTAT = 2500
_MAX_LISTE = 8000        # pour un simple index « identifiant + nom », bien plus utile
# Ce qui sert vraiment à identifier un élément : tout le reste peut sauter quand la
# réponse est trop longue. Mieux vaut 200 fichiers réduits à l'essentiel que 6 complets.
_CHAMPS_ESSENTIELS = ("id", "name", "title", "spreadsheetId", "spreadsheetTitle", "fileId",
                      "documentId", "pageId", "databaseId", "filename", "displayName",
                      "summary", "subject", "email", "status", "url", "start", "end")


def _essentiel(element):
    """Ne garde d'un enregistrement que de quoi le reconnaître et le rouvrir."""
    if not isinstance(element, dict):
        return element
    court = {k: v for k, v in element.items()
             if k in _CHAMPS_ESSENTIELS and not isinstance(v, (dict, list))}
    return court or element


def _reduit(payload, limite: int = _MAX_RESULTAT):
    """Raccourcit une réponse en retirant des ENREGISTREMENTS ENTIERS, jamais des lettres.

    ⚠️ Couper le texte à 2500 caractères tronquait le JSON au milieu d'un fichier : il
    devenait illisible, et Nova annonçait « aucun document trouvé » alors que la liste
    était bien arrivée. Ici on enlève des éléments de liste jusqu'à tenir dans la limite,
    et on dit combien ont été omis — le JSON reste toujours valide.
    """
    def taille(o):
        return len(json.dumps(o, ensure_ascii=False, indent=1))

    if taille(payload) <= limite:
        return payload
    # La plus longue liste du document est celle qui coûte : c'est elle qu'on raccourcit.
    if isinstance(payload, dict):
        cle = max((k for k, v in payload.items() if isinstance(v, list)),
                  key=lambda k: len(payload[k]), default=None)
        if cle:
            # 1) D'ABORD alléger chaque élément — un fichier Drive traîne son type MIME,
            # sa date, son lien, ses propriétaires… alors que seuls son identifiant et son
            # nom servent. Supprimer des fichiers entiers en premier faisait disparaître
            # celui que l'utilisateur cherchait (son PEA était le 8e de la liste).
            garde = [_essentiel(e) for e in payload[cle]]
            # Une fois réduite à « identifiant + nom », une liste coûte peu et vaut cher :
            # c'est elle qui permet de retrouver le bon fichier. On lui laisse donc plus
            # de place qu'à une réponse ordinaire — sinon un Drive un peu fourni faisait
            # disparaître le fichier cherché passé la 20e position.
            plafond = _MAX_LISTE if garde != payload[cle] else limite
            while garde and taille({**payload, cle: garde}) > plafond:
                garde.pop()
            reduit = {**payload, cle: garde}
            omis = len(payload[cle]) - len(garde)
            if omis > 0:
                reduit["_note"] = f"{omis} élément(s) supplémentaire(s) non affiché(s)"
            return reduit
    if isinstance(payload, list):
        garde = list(payload)
        while garde and taille(garde) > limite:
            garde.pop()
        return garde
    return payload


def _fmt(action: str, data) -> str:
    """Rend la réponse Composio lisible pour le LLM, SANS jamais casser le JSON."""
    try:
        payload = data.get("data", data) if isinstance(data, dict) else data
        txt = json.dumps(_reduit(payload), ensure_ascii=False, indent=1)
    except Exception:
        txt = str(data)[:_MAX_RESULTAT]
    return f"✅ [{action}] résultat :\n{txt}"
