"""
Envoyer un cours dans Notion — sans deviner le nom de l'action.

« j'aimerais que dans la conv avec Nova je puisse lui dire "déplace tel cours dans
Notion", et aussi dans le mode cours il faut un import Notion. »

⚠️ POURQUOI ON NE CODE PAS UN NOM D'ACTION EN DUR.
Composio renomme ses actions d'une version à l'autre, et ce projet en a déjà payé le
prix trois fois : « NOTION_CREATE_COMMENT » choisi pour « crée un nouveau projet »
(un commentaire n'est pas un projet), les quatre modèles de vision Groq devenus 404 en
bloc, et le modèle NVIDIA retiré avec un 410. Chaque fois, la même cause : un nom écrit
dans le code, qui a cessé d'exister ailleurs.
On DEMANDE donc à Composio ce que le compte sait faire, et on choisit dans sa liste.

⚠️ ET ON REFUSE DE CHOISIR N'IMPORTE QUOI. Si aucune action de création de page n'est
proposée, on ne se rabat pas sur la première venue : on le dit. Créer un commentaire
en croyant créer une page, c'est exactement ce qui s'est produit la dernière fois.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Ce qui ressemble à « créer une page ». Du plus précis au plus large : on prend le
# premier qui existe, jamais « la première action de la liste ».
_PREFERENCES = (
    r"^NOTION_CREATE_NOTION_PAGE$",
    r"^NOTION_CREATE_PAGE$",
    r"^NOTION_ADD_PAGE",
    r"^NOTION_.*CREATE.*PAGE",
    r"^NOTION_.*PAGE.*CREATE",
)
# Ce qui n'est PAS créer une page, même si le nom contient « CREATE ».
_JAMAIS = re.compile(r"COMMENT|DATABASE_ITEM|BLOCK|DUPLICATE|DELETE|ARCHIVE|SEARCH|"
                     r"FETCH|GET|LIST|UPDATE|APPEND", re.I)


def choisit_action(actions) -> str:
    """Le nom de l'action « créer une page », ou "" si le compte n'en propose aucune."""
    noms = [str((a or {}).get("name") or "") for a in (actions or [])]
    noms = [n for n in noms if n and not _JAMAIS.search(n)]
    for motif in _PREFERENCES:
        for n in noms:
            if re.search(motif, n, re.I):
                return n
    return ""


def _bloc_texte(ligne: str) -> dict:
    """Une ligne de markdown → un bloc Notion. Titres et puces conservés."""
    t = ligne.rstrip()
    m = re.match(r"^(#{1,3})\s+(.*)$", t)
    if m:
        niveau = len(m.group(1))
        return {"object": "block", "type": f"heading_{niveau}",
                f"heading_{niveau}": {"rich_text": [
                    {"type": "text", "text": {"content": m.group(2)[:1900]}}]}}
    m = re.match(r"^\s*[-*+]\s+(.*)$", t)
    if m:
        return {"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [
                    {"type": "text", "text": {"content": m.group(1)[:1900]}}]}}
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": t[:1900]}}]}}


# ⚠️ Notion refuse au-delà de 100 blocs par appel, et une ligne de plus de 2000
# caractères. Un cours de deux heures dépasse les deux : on borne, et on DIT ce qui n'a
# pas été envoyé plutôt que de laisser croire que tout est passé.
MAX_BLOCS = 95
# Notion refuse aussi un contenu démesuré d'un seul tenant : on borne le markdown.
_MAX_MD = 40000


def blocs(md: str):
    """(blocs Notion, nombre de lignes laissées de côté)."""
    lignes = [l for l in (md or "").splitlines() if l.strip()]
    gardees = lignes[:MAX_BLOCS]
    return [_bloc_texte(l) for l in gardees], max(0, len(lignes) - len(gardees))


# Une action qui sait CHERCHER une page existante : c'est elle qui nous donne un parent.
_CHERCHE = (r"^NOTION_SEARCH", r"^NOTION_.*SEARCH", r"^NOTION_LIST.*PAGE",
            r"^NOTION_FETCH.*PAGE", r"^NOTION_GET.*PAGE")
# Un identifiant Notion : 32 hexadécimaux, avec ou sans tirets.
_ID = re.compile(r"\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b", re.I)


def pages_candidates(obs: str) -> list:
    """Les identifiants qui sont VRAIMENT des pages (ou des bases) dans une réponse Notion.

    ⚠️ LE DÉFAUT QUE CETTE FONCTION SUPPRIME. On prenait le PREMIER identifiant croisé
    dans la réponse brute, avec une simple expression régulière. Une réponse de recherche
    Notion en contient une douzaine : l'intégration elle-même, l'auteur, le bloc parent,
    chaque propriété… Le premier n'est presque jamais une page. D'où, à l'écran :
    « Parent id 'aa92e55a-7284-4a57-9741-6712586d8608' is neither a page nor a database ».
    Le message était juste ; l'identifiant venait de nous.

    On lit donc la structure : seul un objet qui se déclare `"object": "page"` (ou
    `"database"`) et porte un `id` est retenu — dans l'ordre où Notion les rend.
    """
    import json as _json
    trouves = []

    def visite(n):
        if isinstance(n, dict):
            genre = str(n.get("object") or "").lower()
            ident = str(n.get("id") or "")
            # `parent` porte lui aussi un id, mais il désigne le CONTENANT, pas l'objet
            # rendu : on ne le prend jamais pour la page cherchée.
            if genre in ("page", "database") and _ID.fullmatch(ident) and ident not in trouves:
                trouves.append(ident)
            for c, v in n.items():
                if c != "parent":
                    visite(v)
        elif isinstance(n, list):
            for v in n:
                visite(v)

    txt = str(obs or "")
    # La réponse arrive souvent enrobée de texte : on tente le JSON le plus large.
    for d, f in ((txt.find("{"), txt.rfind("}")), (txt.find("["), txt.rfind("]"))):
        if d >= 0 and f > d:
            try:
                visite(_json.loads(txt[d:f + 1]))
                if trouves:
                    return trouves
            except Exception:
                pass
    # Pas de JSON exploitable : on exige au moins que « "object": "page" » précède l'id
    # de près.
    for m in re.finditer(r'"object"\s*:\s*"(page|database)"', txt, re.I):
        fenetre = txt[m.end():m.end() + 400]
        mi = re.search(r'"id"\s*:\s*"([^"]+)"', fenetre)
        if mi and _ID.fullmatch(mi.group(1)) and mi.group(1) not in trouves:
            trouves.append(mi.group(1))
    if trouves:
        return trouves
    # ⚠️ DERNIER RECOURS, ET IL RESTE NÉCESSAIRE. Certaines actions Composio rendent
    # simplement { "id": "…" } sans dire de quel objet il s'agit : exiger « object »
    # ferait perdre le parent là où l'ancien code, lui, le trouvait. On accepte donc les
    # identifiants nus — mais JAMAIS ceux qui sont annoncés comme autre chose (le robot,
    # l'auteur, l'espace de travail), car c'est précisément l'un d'eux qui produisait
    # « aa92e55a-… is neither a page nor a database ».
    interdits = set()
    for m in re.finditer(r'"object"\s*:\s*"(?!page"|database")[a-z_]+"', txt, re.I):
        mi = re.search(r'"id"\s*:\s*"([^"]+)"', txt[m.end():m.end() + 400])
        if mi:
            interdits.add(mi.group(1))
    for m in re.finditer(r'"(?:bot_?id|user_?id|owner|workspace_?id|parent_?id)"\s*:\s*"([^"]+)"',
                         txt, re.I):
        interdits.add(m.group(1))
    for m in _ID.finditer(txt):
        v = m.group(0)
        if v not in interdits and v not in trouves:
            trouves.append(v)
    if trouves:
        return trouves
    # ⚠️ Dernier endroit où un identifiant se cache : l'URL de la page. Notion écrit
    # « https://www.notion.so/Mon-titre-<32 hexa> ». Une création qui ne rend que son
    # lien nous laissait sans page où écrire la suite — donc sans contenu.
    for m in re.finditer(r"notion\.so/[^\s)\"']*?([0-9a-f]{32})", txt, re.I):
        v = m.group(1)
        if v not in trouves:
            trouves.append(v)
    return trouves


def _parents_possibles(actions, appeler) -> list:
    """Les pages où créer, la meilleure d'abord. Vide si aucune n'est accessible.

    ⚠️ D'abord son réglage s'il en a un : il sait mieux que moi où ranger ses cours.
    Sinon on DEMANDE à Notion quelles pages l'intégration voit — plutôt que d'inventer
    un identifiant, ce qui produirait une erreur incompréhensible.
    """
    try:
        from config import config
        fixe = (getattr(config, "NOTION_PARENT_ID", "") or "").strip()
        if fixe:
            return [fixe]
    except Exception:
        pass
    noms = [str((a or {}).get("name") or "") for a in (actions or [])]
    out = []
    for motif in _CHERCHE:
        for n in noms:
            if not re.search(motif, n, re.I):
                continue
            try:
                obs = str(appeler(n, {"query": "", "page_size": 5}, "notion") or "")
            except Exception:
                continue
            for ident in pages_candidates(obs):
                if ident not in out:
                    out.append(ident)
            if out:
                return out
    return out


def _parent(actions, appeler) -> str:
    """La meilleure page où créer, ou "" — conservée pour les appelants existants."""
    lot = _parents_possibles(actions, appeler)
    return lot[0] if lot else ""


# Les refus qui parlent du PARENT, et eux seuls : réessayer ailleurs n'a de sens que
# pour ceux-là. Un quota dépassé ou un titre invalide se reproduiraient à l'identique.
_REFUS_PARENT = re.compile(
    r"neither a page nor a database|parent_?id|could not find (page|database)|"
    r"make sure the relevant pages and databases are shared|"
    r"missing.{0,40}parent|invalid parent", re.I)


def _parent_refuse(txt: str) -> bool:
    return bool(_REFUS_PARENT.search(str(txt or "")))


# ── Où mettre le CONTENU, selon ce que l'action accepte vraiment ──────────────
# ⚠️ « Nova n'importe que le titre du cours dans Notion. » Exact, et la cause est la
# même que pour le nom de l'action : on écrivait `children` en dur, avec des blocs
# Notion bruts. Composio n'expose PAS l'API Notion telle quelle — selon les versions et
# les actions, le contenu s'appelle `content` (du markdown), `child_blocks`, `blocks` ou
# `children`. Un champ que l'action ne connaît pas est IGNORÉ EN SILENCE : la page était
# donc créée, l'appel réussissait, et il ne restait que le titre.
#
# On ne devine plus : la liste d'actions porte déjà le schéma d'entrée (`props`). On y
# lit le champ du contenu et sa FORME, et on s'y conforme.
_CHAMPS_MD = ("content", "markdown", "markdown_content", "page_content", "text", "body")
_CHAMPS_BLOCS = ("children", "child_blocks", "blocks", "content_blocks", "children_blocks")


def champ_contenu(props) -> tuple:
    """(nom du champ, "md" ou "blocs") — où placer le cours. ("", "") si nulle part."""
    noms = [str(p) for p in (props or [])]
    bas = {n.lower(): n for n in noms}
    for c in _CHAMPS_MD:
        if c in bas:
            return bas[c], "md"
    for c in _CHAMPS_BLOCS:
        if c in bas:
            return bas[c], "blocs"
    return "", ""


def _action_par_nom(actions, nom):
    for a in (actions or []):
        if str((a or {}).get("name") or "").upper() == str(nom).upper():
            return a or {}
    return {}


# Une action qui AJOUTE du contenu à une page existante. Repli quand la création ne sait
# poser qu'un titre : mieux vaut deux appels qu'une page vide.
_AJOUTE = (r"^NOTION_ADD_.*CONTENT", r"^NOTION_APPEND", r"^NOTION_ADD_.*BLOCK",
           r"^NOTION_.*ADD.*CHILD", r"^NOTION_UPDATE_.*BLOCK")


def choisit_ajout(actions) -> str:
    """Le nom de l'action « ajouter du contenu à une page », ou ""."""
    noms = [str((a or {}).get("name") or "") for a in (actions or [])]
    for motif in _AJOUTE:
        for n in noms:
            if re.search(motif, n, re.I):
                return n
    return ""


def envoie(titre: str, md: str, lister_actions, appeler) -> str:
    """Crée la page et rend un message en français. Jamais un succès supposé.

    `lister_actions(slug)` et `appeler(action, args, slug)` sont injectés : ce module
    ne connaît ni Composio ni le réseau, ce qui le rend testable sans rien simuler.
    """
    actions = lister_actions("notion") or []
    if not actions:
        return ("📝 Je n'ai pas pu joindre Notion : aucune action ne m'est proposée. "
                "Vérifie que Notion est bien connecté dans Composio.")
    nom = choisit_action(actions)
    if not nom:
        dispo = ", ".join(str(a.get("name") or "") for a in actions[:6])
        # ⚠️ On ne se rabat PAS sur la première action venue. « NOTION_CREATE_COMMENT »
        # avait déjà été choisi pour « crée un projet » : un commentaire n'est pas une page.
        return ("📝 Notion est connecté, mais aucune action de **création de page** ne "
                "m'est proposée sur ce compte — je préfère ne rien faire plutôt que "
                f"d'utiliser une action au hasard.\n\n_Actions vues : {dispo}_")

    corps, laissees = blocs(md)
    # ⚠️ VU EN VRAI : « Invalid request data provided — Following fields are missing:
    # {'parent_id'} ». Notion ne sait pas créer une page « quelque part » : il lui faut
    # une page ou une base PARENTE. Et une intégration Notion ne voit que ce qu'on lui a
    # explicitement partagé — d'où l'échec, alors que tout est « connecté » côté Composio.
    # Ce n'était donc ni lui, ni Composio : il manquait un paramètre.
    candidats = _parents_possibles(actions, appeler)
    # ⚠️ On ESSAIE plusieurs pages plutôt qu'une seule. La recherche Notion rend d'abord
    # ce qu'elle veut, et une page à laquelle l'intégration n'a qu'un accès en lecture
    # refuse la création. Deux essais coûtent une seconde ; un échec lui coûte sa séance.
    # Où va le CONTENU, d'après le schéma que Composio publie pour CETTE action.
    creation = _action_par_nom(actions, nom)
    props = creation.get("props") or []
    champ, forme = champ_contenu(props)
    # ⚠️ SCHÉMA INCONNU ≠ PAS DE CONTENU. Si Composio n'a pas publié la liste des champs
    # (cache d'une ancienne version, réponse incomplète), n'envoyer aucun contenu serait
    # pire que l'ancien comportement. On envoie alors TOUTES les orthographes connues :
    # une clé que l'action ignore ne coûte rien, une clé manquante coûte le cours.
    a_l_aveugle = not props
    essais = candidats[:3] or [""]
    txt, parent = "", ""
    for parent in essais:
        args = {"title": titre[:100]}
        if champ:
            args[champ] = (md[:_MAX_MD] if forme == "md" else corps)
        elif a_l_aveugle:
            args["content"] = md[:_MAX_MD]
            args["children"] = corps
        if parent:
            # On envoie les DEUX orthographes : Composio a changé de nom de champ selon
            # les versions, et une clé en trop est ignorée alors qu'une clé manquante
            # bloque.
            args["parent_id"] = parent
            args["parent"] = {"page_id": parent}
        txt = str(appeler(nom, args, "notion") or "")
        if "✅" in txt or '"successful": true' in txt.lower():
            break
        if not _parent_refuse(txt):
            break      # l'échec ne vient pas du parent : réessayer ailleurs n'aiderait pas
    if "✅" not in txt and '"successful": true' not in txt.lower() and (
            not parent or _parent_refuse(txt)):
        return (
            "📝 Notion refuse de créer la page : il lui faut une **page parente** à "
            "laquelle ton intégration a accès, et "
            + (f"aucune des {len(candidats)} pages que je vois ne l'accepte"
               if candidats else "je n'en ai trouvé aucune qu'elle puisse voir") + ".\n\n"
            "C'est une particularité de Notion, pas une panne : une intégration ne voit "
            "que les pages qu'on lui a **explicitement partagées**.\n\n"
            "**Ce qu'il faut faire, une fois pour toutes :** dans Notion, ouvre la page "
            "où tu veux ranger tes cours → menu « … » en haut à droite → "
            "**Connexions / Ajouter des connexions** → choisis Composio. Ensuite je "
            "saurai créer dedans.\n\n"
            "_Tu peux aussi coller l'identifiant de cette page dans la variable "
            "`NOTION_PARENT_ID` sur Render : je n'aurai plus à la chercher._"
            + (f"\n\n_Réponse de {nom} : {' '.join(txt.split())[:200]}_" if txt else ""))
    if "✅" not in txt and '"successful": true' not in txt.lower():
        return ("📝 Notion a refusé la création de la page. Je ne te dis pas que c'est "
                f"fait.\n\n_Réponse de {nom} : {' '.join(txt.split())[:300]}_")
    url = ""
    m = re.search(r"https://www\.notion\.so/[\w-]+", txt)
    if m:
        url = m.group(0)
    fin = f"\n\n[Ouvrir dans Notion]({url})" if url else ""

    # ── Le contenu est-il VRAIMENT parti ? ───────────────────────────────────
    # ⚠️ « Nova n'importe que le titre du cours dans Notion. » L'appel réussissait, la
    # page se créait, et le cours n'y était pas : un champ que l'action ne connaît pas
    # est ignoré sans un mot. Si la création ne sait pas porter de contenu, on l'AJOUTE
    # en second appel — et si on n'y arrive pas non plus, on le DIT au lieu d'annoncer
    # un import complet.
    contenu_ok, note = bool(champ) or a_l_aveugle, ""
    if not champ and not a_l_aveugle:
        ajout = choisit_ajout(actions)
        page = ""
        for ident in pages_candidates(txt):
            page = ident
            break
        cible = _action_par_nom(actions, ajout)
        champ2, forme2 = champ_contenu(cible.get("props") or [])
        if ajout and page and champ2:
            a2 = {champ2: (md[:_MAX_MD] if forme2 == "md" else corps)}
            # L'identifiant de la page se nomme différemment selon l'action : on renseigne
            # les orthographes connues, les clés en trop étant ignorées.
            for k in ("page_id", "block_id", "parent_id", "parent_block_id", "id"):
                a2[k] = page
            t2 = str(appeler(ajout, a2, "notion") or "")
            contenu_ok = "✅" in t2 or '"successful": true' in t2.lower()
            if not contenu_ok:
                note = ("\n\n⚠️ **La page est créée mais elle est VIDE** : je n'ai pas "
                        "réussi à y écrire le cours.\n\n_Réponse de "
                        f"{ajout} : {' '.join(t2.split())[:200]}_")
        else:
            note = ("\n\n⚠️ **La page est créée mais elle ne contient que le titre.** "
                    f"L'action **{nom}** de ton compte Composio n'accepte aucun champ de "
                    "contenu, et aucune action « ajouter du contenu » ne m'est proposée. "
                    "Le cours complet reste dans Nova — utilise le téléchargement en "
                    "attendant.")
    # Ce qui n'a pas été envoyé se DIT : une page tronquée qui a l'air complète, c'est
    # le défaut qu'on corrige depuis le début.
    reste = (f"\n\n⚠️ Notion n'accepte que {MAX_BLOCS} blocs par page : "
             f"**{laissees} ligne(s) n'ont pas été envoyées**. Le cours complet reste "
             "dans Nova, et le téléchargement, lui, est entier.") if laissees and contenu_ok else ""
    tete = (f"📝 **« {titre} »** est dans Notion." if contenu_ok
            else f"📝 J'ai créé **« {titre} »** dans Notion.")
    return tete + fin + note + reste
