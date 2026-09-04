"""
Reconnaître ce dont il parle, même quand la dictée vocale l'a écorché — et ne jamais
répondre à côté d'une entreprise qu'il n'a pas nommée.

⚠️ DEUX DÉFAUTS VUS DANS LA MÊME CONVERSATION, 3 septembre 2026.

1. « est-ce que tu peux me faire résumé de Van Eva de l'action valneva aujourd'hui »
   La dictée avait écrit « Van Eva » pour « Valneva ». Nova a cherché littéralement
   « Van Eva Valneva 3 septembre 2026 », puis a répondu : « Je n'ai trouvé aucune
   information concernant un "Van Eva" lié à l'action Valneva. […] Je n'ai pas trouvé
   de passage précis sur "Van Eva". » Elle avait le vrai nom SOUS LES YEUX, dans la
   même phrase, et elle est partie chercher le fantôme.
   Il parle beaucoup à la voix : un nom propre écorché n'est pas un cas rare, c'est
   le cas NORMAL. Et Valneva est dans sa watchlist — on sait donc ce qu'il suit.

2. « Valneva cours euro 1er septembre 2026 » → « Actualité — 6 articles récents :
   1. Deux ex-directeurs du groupe Orpea bientôt jugés à Paris pour délit d'initié ».
   Le mode actualité interroge les flux RSS par THÈME (bourse) et rend les titres du
   jour, sans jamais vérifier qu'ils parlent de l'entreprise demandée. Des articles
   hors sujet présentés comme la réponse, c'est pire que pas de réponse : il croit
   que c'est ça, l'actualité de sa valeur.
"""
import json
import logging
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)


def _cle(nom: str) -> str:
    """« Van Eva » et « VALNEVA » deviennent comparables : sans accent, sans espace."""
    t = unicodedata.normalize("NFD", str(nom or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", t)


def _distance(a: str, b: str) -> int:
    """Damerau-Levenshtein — une INVERSION de deux lettres compte pour UNE faute.

    ⚠️ « Vanleva » pour « Valneva » : deux lettres interverties. En Levenshtein simple
    ça vaut 2 fautes, donc au-dessus du seuil, donc pas corrigé — alors que c'est
    l'erreur la plus fréquente de la dictée et du clavier. La transposition doit
    coûter ce qu'elle est : une seule faute.
    """
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cout = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cout)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def connues() -> list:
    """Ce qu'il suit VRAIMENT : sa watchlist, ses portefeuilles, l'univers PEA.

    ⚠️ On ne corrige que vers des noms qu'il a lui-même mis là. Réparer « Van Eva »
    vers une société au hasard qui sonne pareil serait pire que de ne rien réparer.
    """
    noms = []
    for chemin, extrait in ((Path("data/watchlist.txt"), None),
                            (Path("data/portfolios.json"), "portefeuille"),
                            (Path("data/pea_etf_universe.json"), "univers")):
        try:
            if not chemin.exists():
                continue
            if chemin.suffix == ".txt":
                for ligne in chemin.read_text(encoding="utf-8").splitlines():
                    ligne = ligne.split("#", 1)[0].strip()
                    if ligne:
                        noms.append(ligne)
            else:
                data = json.loads(chemin.read_text(encoding="utf-8"))
                if extrait == "univers" and isinstance(data, list):
                    for e in data:
                        for c in ("name", "ticker"):
                            if isinstance(e, dict) and e.get(c):
                                noms.append(str(e[c]))
                elif isinstance(data, dict):
                    for pf in data.values():
                        for ligne in str((pf or {}).get("positions") or "").splitlines():
                            mot = ligne.split("#", 1)[0].strip().split(" ")[0]
                            if mot:
                                noms.append(mot)
        except Exception as e:
            logger.info(f"[entites] {chemin} illisible ({type(e).__name__})")
    # Dédoublonnage en gardant l'écriture d'origine (c'est elle qu'on réinjecte).
    vus, out = set(), []
    for n in noms:
        n = n.strip()
        k = _cle(n)
        if len(k) >= 3 and k not in vus:
            vus.add(k)
            out.append(n)
    return out


# Assez de lettres pour qu'une ressemblance veuille dire quelque chose.
_MIN_LONGUEUR = 5
# ⚠️ Un groupe de deux mots ne doit pas COMMENCER par un mot vide. Sans ça,
# « le cours de Valnéva » formait le groupe « de Valnéva » — à deux lettres de
# « Valneva » — et la correction avalait la préposition ET le nom, laissant
# « le cours aujourd'hui ». Un correcteur qui mange le sujet de la phrase est pire
# que pas de correcteur.
_MOTS_VIDES = {"de", "du", "des", "la", "le", "les", "l", "un", "une", "et", "ou",
               "a", "au", "aux", "en", "sur", "pour", "dans", "par", "avec", "mon",
               "ma", "mes", "ton", "ta", "tes", "ce", "cet", "cette", "que", "qui",
               "action", "titre", "cours", "societe", "entreprise", "boite"}


def _tolerance(k: str) -> int:
    """Combien de lettres de travers on accepte. Court = strict, long = plus souple."""
    return 1 if len(k) <= 7 else 2


def repare(texte: str, reference=None) -> tuple:
    """(texte corrigé, [(écrit, voulu)]) — répare les noms écorchés par la dictée.

    On teste les groupes de UN et DEUX mots : « Van Eva » n'est un nom écorché qu'une
    fois recollé. Un mot déjà exact n'est jamais touché.
    """
    noms = list(reference if reference is not None else connues())
    if not noms or not texte:
        return texte, []
    index = {}
    for n in noms:
        index.setdefault(_cle(n), n)

    mots = re.findall(r"\S+", texte)
    corrections, sortie, i = [], [], 0
    while i < len(mots):
        remplace = None
        # Deux mots d'abord : « Van Eva » → « Valneva ».
        for taille in (2, 1):
            if i + taille > len(mots):
                continue
            if taille > 1 and _cle(mots[i]) in _MOTS_VIDES:
                continue                      # « de Valnéva » n'est pas un nom
            groupe = " ".join(mots[i:i + taille])
            k = _cle(groupe)
            if len(k) < _MIN_LONGUEUR or k in index:
                continue                      # trop court, ou déjà exact
            # Un groupe qui CONTIENT déjà un nom connu en entier n'est pas une faute
            # de frappe : c'est le nom, avec un mot collé à côté.
            if any(exact in k for exact in index if len(exact) >= _MIN_LONGUEUR):
                continue
            proche = min(
                (n for n in index if abs(len(n) - len(k)) <= 2),
                key=lambda n: _distance(k, n), default=None)
            if proche and _distance(k, proche) <= _tolerance(k):
                remplace = (groupe, index[proche], taille)
                break
        if remplace:
            ecrit, voulu, taille = remplace
            # ⚠️ Si le vrai nom est DÉJÀ dans la phrase, on ne le duplique pas : on
            # retire simplement le fantôme. C'est exactement son cas — « résumé de
            # Van Eva de l'action valneva » contenait les deux.
            if _cle(voulu) in {_cle(m) for m in mots}:
                corrections.append((ecrit, voulu))
            else:
                sortie.append(voulu)
                corrections.append((ecrit, voulu))
            i += taille
            continue
        sortie.append(mots[i])
        i += 1
    # Retirer le fantôme peut laisser « de de » ou « à à » : on recolle proprement.
    propre = []
    for mot in sortie:
        if propre and _cle(mot) in _MOTS_VIDES and _cle(mot) == _cle(propre[-1]):
            continue
        propre.append(mot)
    return " ".join(propre), corrections


def citee_dans(nom: str, texte: str) -> bool:
    """Ce texte parle-t-il de cette entreprise ? (comparaison insensible à la forme)"""
    k, t = _cle(nom), _cle(texte)
    return bool(k) and len(k) >= 3 and k in t


def filtre_sur_sujet(articles: list, entites: list) -> list:
    """Ne garde que les articles qui NOMMENT au moins une des entités demandées.

    ⚠️ « Valneva cours euro 1er septembre » rendait « Deux ex-directeurs du groupe
    Orpea bientôt jugés ». Les flux d'actualité sont interrogés par THÈME, pas par
    société : sans ce filtre, on sert les titres du jour comme s'ils répondaient.
    Rendre zéro article est une réponse ; en rendre six hors sujet n'en est pas une.
    """
    if not entites:
        return list(articles or [])
    gardes = []
    for a in (articles or []):
        blob = " ".join(str(a.get(c, "")) for c in ("titre", "title", "resume",
                                                    "summary", "description", "lien", "url"))
        if any(citee_dans(e, blob) for e in entites):
            gardes.append(a)
    return gardes

# ⚠️ Repérer l'entreprise d'une requête avec la regex des tickers ne marche pas :
# sur « Valneva cours euro 1er septembre 2026 » elle rend « 1er » et rate « Valneva »
# (qui n'est ni en capitales ni collé à un chiffre). On part donc de ce qu'il SUIT,
# puis, à défaut, des noms propres — en écartant ce qui n'en est jamais un.
_JAMAIS_UNE_SOCIETE = {
    "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout",
    "septembre", "octobre", "novembre", "decembre", "lundi", "mardi", "mercredi",
    "jeudi", "vendredi", "samedi", "dimanche", "aujourd", "hier", "demain", "matin",
    "soir", "cours", "action", "bourse", "euro", "euros", "prix", "resume", "actu",
    "actualite", "actualites", "nouvelles", "analyse", "objectif", "consensus",
    "pea", "etf", "cac", "nasdaq", "sbf", "euronext", "seance", "titre", "valeur",
}


def entites_citees(texte: str) -> list:
    """Les sociétés nommées dans ce texte. D'abord celles qu'il suit, sinon les noms propres."""
    if not texte:
        return []
    k = _cle(texte)
    suivies = [n for n in connues() if len(_cle(n)) >= 4 and _cle(n) in k]
    if suivies:
        return suivies[:4]
    out, vus = [], set()
    for mot in re.findall(r"\b[A-ZÀ-Ý][\wÀ-ÿ.&-]{2,}\b|\b\d[A-Za-zÀ-ÿ]{2,}\b", texte):
        c = _cle(mot)
        # ⚠️ « 1er » passait le filtre : un article « Le 1er septembre, Orpea… » aurait
        # alors été jugé « sur le sujet ». Un ordinal n'est pas une société.
        if (len(c) < 3 or c in _JAMAIS_UNE_SOCIETE or c.isdigit() or c in vus
                or re.fullmatch(r"\d+(er|e|eme|nd|nde|ere)", c)):
            continue
        vus.add(c)
        out.append(mot)
    return out[:4]
