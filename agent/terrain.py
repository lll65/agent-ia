"""
Aucune ADRESSE, DISTANCE ou DURÉE DE TRAJET sans source.

⚠️ VU EN VRAI, le 8 septembre 2026. « trouve-moi le kiné le plus proche à Pau, sachant
que j'habite dans la résidence UXCO » →

    « Le kiné le plus proche de la résidence XCO se trouve à la clinique du Parc,
      12 rue du Parc, à deux minutes à pied. »

AUCUNE RECHERCHE N'A ÉTÉ LANCÉE. Pas un outil appelé. Le cabinet, la rue, le numéro et
le temps de marche ont été écrits de mémoire. Plus loin dans la même conversation :

    « Adresse de la résidence XCO : 10 rue du 8 Mai 1945, 64000 Pau
      (source : recherche d'adresse). »          ← la caution aussi était inventée
    « Distance ≈ 650 m, soit 8 minutes de marche. »
    « Environ 1,2 km, 15-20 minutes à pied. »    ← lui : « c'est faux, je suis à 700 m »

⚠️ POURQUOI C'EST PIRE QUE TOUT CE QU'ON A CORRIGÉ JUSQU'ICI.
Un chiffre de bourse faux lui coûte de l'argent, et il peut le recouper. Une adresse
fausse, il s'y REND. Il a 17 ans, il vient d'emménager dans une ville qu'il ne connaît
pas, et il marche jusqu'à un cabinet qui n'existe pas. `agent/chiffres.py` surveille les
cours et les capitalisations ; personne ne surveillait ce sur quoi il pose les pieds.

LA MÉTHODE, la même que pour les chiffres, les dates et les liens : on relève ce que la
réponse AFFIRME — une adresse postale, une distance, une durée de marche ou de trajet —
et on vérifie que ça figure dans ce qu'un outil a réellement renvoyé.

Le bandeau est mis EN TÊTE, pas en bas : une mise en garde placée après l'adresse arrive
quand il a déjà noté la rue.
"""
import re

# Une adresse postale affirmée : « 12 rue du Parc », « 15 place de la Libération ».
_VOIES = (r"rue|avenue|av\.|boulevard|bd|place|pl\.|impasse|chemin|route|all[ée]e|"
          r"quai|cours|square|esplanade|passage|voie|faubourg")
# ⚠️ Première version : « [\wÀ-ÿ'’-] » pour le premier mot de la voie, au SINGULIER.
# « 12 rue du Parc » ressortait « 12 rue d » — un extrait tronqué, illisible dans le
# bandeau, et qui ne se retrouvait jamais dans une source. Le premier mot est un mot,
# pas une lettre.
_ADRESSE = re.compile(r"\b\d{1,4}\s*(?:bis|ter)?\s*,?\s*(?:" + _VOIES + r")\s+"
                      r"[\wÀ-ÿ'’-]+(?:[\s'’-][\wÀ-ÿ'’-]+){0,4}", re.I)
# Un code postal + ville : « 64000 Pau ».
_CODE_POSTAL = re.compile(r"\b\d{5}\s+[A-ZÀ-Ý][\wÀ-ÿ'’-]+(?:[- ][A-ZÀ-Ý]?[\wÀ-ÿ'’-]+){0,3}")
# Une distance : « 650 m », « 1,2 km », « environ 700 mètres ».
_DISTANCE = re.compile(r"\b\d{1,4}(?:[.,]\d{1,3})?\s*(?:m|km|m[èe]tres?|kilom[èe]tres?)\b", re.I)
# Une durée de DÉPLACEMENT — pas n'importe quelle durée : « 8 minutes de marche »,
# « à deux minutes à pied », « 15-20 minutes à pied », « 1 h de route ».
_MOTS_TRAJET = r"(?:[àa]\s+pied|de\s+marche|de\s+route|de\s+trajet|en\s+voiture|en\s+v[ée]lo|en\s+bus)"
_DUREE = re.compile(
    r"\b(?:\d{1,3}(?:\s*[-–à]\s*\d{1,3})?|une?|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|"
    r"quinze|vingt|trente)\s*"
    r"(?:min\b|minutes?|h\b|heures?)\s*(?:de\s+)?" + _MOTS_TRAJET, re.I)
# … et la forme inversée : « à deux minutes à pied » est déjà prise ; « à pied, 8 min ».
_DUREE_INV = re.compile(_MOTS_TRAJET + r"[^.\n]{0,12}?\b\d{1,3}\s*(?:min\b|minutes?|h\b|heures?)", re.I)

_GENRES = (("adresse", _ADRESSE), ("adresse", _CODE_POSTAL),
           ("distance", _DISTANCE), ("durée de trajet", _DUREE), ("durée de trajet", _DUREE_INV))


def _cle(t: str) -> str:
    """Deux écritures du même fait doivent se ressembler : sans accent, sans ponctuation."""
    import unicodedata
    t = unicodedata.normalize("NFD", str(t or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", t)


def affirmations(texte: str) -> list:
    """[(extrait, genre)] — ce que la réponse affirme du terrain."""
    vus, out = set(), []
    for genre, motif in _GENRES:
        for m in motif.finditer(texte or ""):
            brut = " ".join(m.group(0).split()).strip(" ,;:.")
            c = _cle(brut)
            if len(c) < 3 or c in vus:
                continue
            vus.add(c)
            out.append((brut, genre))
    return out


def _appuye(extrait: str, sources: str) -> bool:
    """Cet extrait figure-t-il dans ce qu'un outil a renvoyé ?

    ⚠️ On compare sans accents ni ponctuation : une page écrit « 15, place de la
    Libération » là où le modèle écrit « 15 place de la Liberation ». Ce n'est pas une
    invention, c'est la même adresse.
    """
    c = _cle(extrait)
    if not c:
        return True
    if c in sources:
        return True
    # Un nombre suivi d'un mot : on accepte que la source les sépare autrement
    # (« 650 mètres » vs « 650 m »). On exige le nombre ET le premier mot significatif.
    m = re.match(r"^(\d+)([a-z]+)", c)
    if m and (m.group(1) + m.group(2)[:2]) in sources:
        return True
    return False


def non_sourcees(texte: str, observations) -> list:
    src = _cle("\n".join(str(o) for o in (observations or [])))
    return [(e, g) for e, g in affirmations(texte) if not _appuye(e, src)]


def _aucun_outil(observations) -> bool:
    """Aucun outil n'a rien renvoyé de substantiel ce tour-ci."""
    return not any(len(str(o).strip()) > 40 for o in (observations or []))


def relis(texte: str, observations=None, demande: str = "") -> str:
    """Signale ce que la réponse affirme du terrain sans qu'aucune source l'appuie."""
    if not texte:
        return texte
    manquantes = non_sourcees(texte, observations)
    if not manquantes:
        return texte

    apercu = " · ".join(f"{e}" for e, _g in manquantes[:5])
    if len(manquantes) > 5:
        apercu += f" · … ({len(manquantes) - 5} autres)"
    genres = sorted({g for _e, g in manquantes})

    if _aucun_outil(observations):
        # LE cas du kiné : aucune recherche lancée, tout écrit de mémoire.
        tete = ("> ⚠️ **Je n'ai lancé aucune recherche : ce qui suit sort de ma mémoire, "
                "pas d'une carte.**\n>\n"
                f"> {apercu}\n>\n"
                "> **N'y va pas sur cette base.** Une adresse inventée ressemble "
                "exactement à une vraie, et c'est toi qui fais le trajet. Redemande-moi "
                "en disant « cherche » : j'irai voir pour de bon.\n")
    else:
        tete = ("> ⚠️ **" + " et ".join(genres).capitalize() +
                " : rien ne vient d'une source.**\n>\n"
                f"> {apercu} — aucun outil ne me l'a renvoyé, je l'ai écrit moi-même. "
                "Vérifie avant de te déplacer.\n")
    return tete + "\n" + texte.strip()
