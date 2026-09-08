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


def _parent(actions, appeler) -> str:
    """L'identifiant d'une page où créer. "" si aucune n'est accessible.

    ⚠️ D'abord son réglage s'il en a un : il sait mieux que moi où ranger ses cours.
    Sinon on DEMANDE à Notion quelles pages l'intégration voit — plutôt que d'inventer
    un identifiant, ce qui produirait une erreur incompréhensible.
    """
    try:
        from config import config
        fixe = (getattr(config, "NOTION_PARENT_ID", "") or "").strip()
        if fixe:
            return fixe
    except Exception:
        pass
    noms = [str((a or {}).get("name") or "") for a in (actions or [])]
    for motif in _CHERCHE:
        for n in noms:
            if not re.search(motif, n, re.I):
                continue
            try:
                obs = str(appeler(n, {"query": "", "page_size": 5}, "notion") or "")
            except Exception:
                continue
            m = _ID.search(obs)
            if m:
                return m.group(0)
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
    parent = _parent(actions, appeler)
    args = {"title": titre[:100], "children": corps}
    if parent:
        # On envoie les DEUX orthographes : Composio a changé de nom de champ selon les
        # versions, et une clé en trop est ignorée alors qu'une clé manquante bloque.
        args["parent_id"] = parent
        args["parent"] = {"page_id": parent}
    obs = appeler(nom, args, "notion")
    txt = str(obs or "")
    if not parent and "parent_id" in txt:
        return (
            "📝 Notion refuse de créer la page : il lui faut une **page parente**, et je "
            "n'en ai trouvé aucune que ton intégration puisse voir.\n\n"
            "C'est une particularité de Notion, pas une panne : une intégration ne voit "
            "que les pages qu'on lui a **explicitement partagées**.\n\n"
            "**Ce qu'il faut faire, une fois pour toutes :** dans Notion, ouvre la page "
            "où tu veux ranger tes cours → menu « … » en haut à droite → "
            "**Connexions / Ajouter des connexions** → choisis Composio. Ensuite je "
            "saurai créer dedans."
            + (f"\n\n_Réponse de {nom} : {' '.join(txt.split())[:200]}_" if txt else ""))
    if "✅" not in txt and '"successful": true' not in txt.lower():
        return ("📝 Notion a refusé la création de la page. Je ne te dis pas que c'est "
                f"fait.\n\n_Réponse de {nom} : {' '.join(txt.split())[:300]}_")
    url = ""
    m = re.search(r"https://www\.notion\.so/[\w-]+", txt)
    if m:
        url = m.group(0)
    fin = f"\n\n[Ouvrir dans Notion]({url})" if url else ""
    # Ce qui n'a pas été envoyé se DIT : une page tronquée qui a l'air complète, c'est
    # le défaut qu'on corrige depuis le début.
    reste = (f"\n\n⚠️ Notion n'accepte que {MAX_BLOCS} blocs par page : "
             f"**{laissees} ligne(s) n'ont pas été envoyées**. Le cours complet reste "
             "dans Nova, et le téléchargement, lui, est entier.") if laissees else ""
    return f"📝 **« {titre} »** est dans Notion." + fin + reste
