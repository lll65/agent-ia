"""
Aucun chiffre de marché sans source.

⚠️ Le 3 septembre 2026, à « cherche une action PEA qui va exploser », Nova a rendu
une réponse impeccablement écrite :

    GENFIT (ticker : GENF) — Cours actuel : 13,42 € · Variation du jour : +2,3 %
    Chiffre d'affaires 112 M€ (+18 %) · Bénéfice net 7,4 M€ · PER de 9,5
    (source : Les Échos) … (source : Boursorama) … (source : ZoneBourse)

La trace montre DEUX recherches web et pas une seule cotation. Aucun outil n'a
renvoyé de cours. Tous ces nombres — le prix, la variation, le PER, les millions —
ont été produits par le modèle, avec des noms de journaux accrochés dessus pour
faire vrai. C'est la faute la plus grave possible ici : il s'en sert pour décider
d'engager son argent, et rien à l'écran ne distingue un chiffre mesuré d'un chiffre
inventé.

⚠️ POURQUOI CE N'EST PAS UN PROBLÈME DE PROMPT. L'interdiction existe depuis le
début, en majuscules, à quatre endroits : « ZÉRO CHIFFRE INVENTÉ », « ne cite jamais
une source sans qu'un OUTIL te l'ait renvoyée ». Elle a été enfreinte quand même.
Une consigne est une intention ; ce qui coûte cher se vérifie sur la sortie.

LA MÉTHODE. On relit la réponse, on en extrait les chiffres de MARCHÉ (un prix, une
variation, un PER, une capitalisation, un montant en millions), et on regarde si
chacun apparaît dans ce que les outils ont réellement renvoyé. Ce qui n'y est pas
est signalé nommément — on ne l'efface pas : une réponse trouée serait illisible, et
c'est à lui de juger. Mais il saura lesquels sont mesurés et lesquels sont sortis de
nulle part.
"""
import re

# Ce qu'on surveille : les nombres sur lesquels on prend une décision d'argent.
# Volontairement étroit — un « 3 rendez-vous » ou une « page 12 » n'a rien à faire ici.
_PRIX = re.compile(r"\b\d{1,3}(?:[  ]\d{3})*(?:[.,]\d{1,4})?\s*(?:€|\$|EUR|USD|euros?|dollars?)", re.I)
_VARIATION = re.compile(r"[+-]\s?\d{1,3}(?:[.,]\d{1,2})?\s*%")
_RATIO = re.compile(r"\b(?:PER|P/E|PEG|ratio cours[^.\n]{0,20}|rendement)\s*(?:de|:|est de|s'établit à)?\s*"
                    r"(\d{1,4}(?:[.,]\d{1,2})?)", re.I)
_MONTANT = re.compile(r"\b\d{1,4}(?:[.,]\d{1,3})?\s*(?:M|Md|mds?|milliards?|millions?)\b", re.I)
_CAPI = re.compile(r"capitalisation[^.\n]{0,30}?(\d{1,4}(?:[.,]\d{1,3})?)", re.I)

_MOTIFS = (("prix", _PRIX), ("variation", _VARIATION), ("ratio", _RATIO),
           ("montant", _MONTANT), ("capitalisation", _CAPI))


def _valeurs(texte: str) -> set:
    """Tous les nombres d'un texte, en VALEURS.

    ⚠️ Première version : je comparais des suites de chiffres (« 13,42 » → « 1342 »).
    « 2,4 » face à une source qui dit « 2,39 » ressortait alors comme inventé, alors
    que c'est un arrondi honnête. On compare donc ce que les nombres VALENT, pas
    comment ils sont écrits — les séparateurs changent d'une source à l'autre.
    """
    out = set()
    for m in re.finditer(r"\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?", texte or ""):
        t = m.group(0).replace(" ", "").replace("\u00a0", "").replace(",", ".")
        # « 1.234.567 » : des points de milliers, pas une décimale.
        if t.count(".") > 1:
            t = t.replace(".", "")
        try:
            out.add(float(t))
        except ValueError:
            continue
    return out


def _valeur(libelle: str):
    """La valeur portée par un libellé, avec sa précision (« 2,4 » → 2.4, 1 décimale)."""
    m = re.search(r"\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?", libelle or "")
    if not m:
        return None, 0
    t = m.group(0).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if t.count(".") > 1:
        t = t.replace(".", "")
    try:
        v = float(t)
    except ValueError:
        return None, 0
    dec = len(t.split(".")[1]) if "." in t else 0
    return v, dec


def chiffres_de_marche(texte: str) -> list:
    """[(libellé, genre)] — les nombres du texte qui engagent une décision d'argent."""
    vus, out = set(), []
    for genre, motif in _MOTIFS:
        for m in motif.finditer(texte or ""):
            lib = m.group(0).strip()
            v, _dec = _valeur(lib)
            cle = (v, genre)
            if v is not None and cle not in vus:
                vus.add(cle)
                out.append((lib, genre))
    return out


def _adosse(libelle: str, sources: set) -> bool:
    """Ce nombre est-il appuyé par une source ? Arrondis et échelles compris.

    Trois façons légitimes d'écrire la même donnée :
      • à l'identique      — 2,39 € pour une source à 2.39
      • arrondie           — « environ 2,4 € » pour la même source
      • à une autre échelle — « 112 M€ » pour une source qui écrit 112000000
    """
    v, dec = _valeur(libelle)
    if v is None:
        return True
    for s in sources:
        if round(s, dec) == round(v, dec):
            return True
        # tolérance d'arrondi : 0,5 % ou le dernier chiffre significatif
        if abs(s - v) <= max(abs(v) * 0.005, 0.5 / (10 ** dec)):
            return True
        for ech in (1e3, 1e6, 1e9):
            if v and (abs(s - v * ech) <= abs(v * ech) * 0.005
                      or abs(s * ech - v) <= abs(v) * 0.005):
                return True
    return False


def non_sources(texte: str, observations) -> list:
    """Les chiffres de marché du texte qu'AUCUNE observation n'appuie."""
    sources = set()
    for o in (observations or []):
        sources |= _valeurs(str(o))
    return [(lib, genre) for lib, genre in chiffres_de_marche(texte)
            if not _adosse(lib, sources)]


# Les sources nommées : les citer sans outil, c'est fabriquer une caution.
_SOURCE_NOMMEE = re.compile(
    r"\(\s*sources?\s*:[^)]{2,60}\)|d'après\s+(?:Les Échos|Boursorama|Zonebourse|"
    r"ZoneBourse|Reuters|Bloomberg|Yahoo Finance|Investir|La Tribune|BFM)", re.I)


# ⚠️ MÊME FAUTE, EN IMAGE. « Voici un petit graphique du cours de GENFIT » — suivi
# d'une image cassée. Le data:URI faisait 188 octets pour une image annoncée en
# 640×640, et ses données compressées ne se décompressent même pas : le modèle avait
# inventé du base64 qui ressemble à un en-tête PNG. La légende, elle, citait
# « 2,30 € le 5 sept 2026 » — une date FUTURE — et « 2,39 € » alors qu'il venait
# d'annoncer 13,42 € pour la même action.
# Nova sait faire de vrais graphiques (agent/graphique.py) à partir de chiffres
# réellement récupérés. Un dessin que le modèle produit lui-même n'est pas une donnée :
# c'est la même invention, en plus convaincante parce que ça ressemble à une mesure.
_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(data:image/[^)\s]+)\s*\)|<img[^>]+src=[\"']\s*(data:image/[^\"']+)")


def images_inventees(texte: str, observations=None) -> list:
    """Les images du texte qu'aucun outil n'a produites."""
    sources = "\n".join(str(o) for o in (observations or []))
    out = []
    for m in _IMAGE.finditer(texte or ""):
        uri = m.group(1) or m.group(2) or ""
        # On compare un morceau du contenu : l'URI complet peut être coupé en chemin.
        noyau = uri.split("base64,", 1)[-1][:64]
        if len(noyau) >= 24 and noyau in sources:
            continue
        out.append(m.group(0))
    return out


def relis(texte: str, observations=None, demande: str = "") -> str:
    """Signale les chiffres que rien n'appuie. N'efface rien : il doit pouvoir juger."""
    if not texte:
        return texte

    # Une image fabriquée par le modèle est retirée, elle : contrairement à un chiffre,
    # elle ne se relit pas — elle s'affiche, ou elle casse, et dans les deux cas elle
    # prétend être une mesure.
    fausses = images_inventees(texte, observations)
    if fausses:
        for f in fausses:
            texte = texte.replace(f, "")
        texte = (texte.strip() + "\n\n> ⚠️ **J'ai retiré un « graphique » que j'avais "
                 "fabriqué moi-même.** Aucun outil ne me l'a donné : ce n'était pas une "
                 "mesure, juste une image inventée. Si tu veux une vraie courbe, "
                 "demande-moi la fiche de la valeur — elle trace les cours réellement "
                 "récupérés.")

    manquants = non_sources(texte, observations)
    if not manquants:
        return texte

    aucune_source = not [o for o in (observations or []) if _valeurs(str(o))]
    apercu = " · ".join(lib for lib, _g in manquants[:6])
    if len(manquants) > 6:
        apercu += f" · … ({len(manquants) - 6} autres)"

    if aucune_source:
        # Le cas GENFIT : pas UN seul chiffre mesuré dans toute la réponse.
        bandeau = (
            "> ⚠️ **Aucun de ces chiffres ne vient d'une source — je ne les ai pas "
            "vérifiés.**\n>\n"
            "> Aucun outil ne m'a renvoyé de cours ni de données chiffrées pendant "
            f"cette réponse. Les nombres ci-dessous ({apercu}) sortent de ma mémoire, "
            "pas d'une mesure : ils peuvent être faux, périmés, ou porter sur une autre "
            "société. **Ne prends aucune décision d'argent là-dessus** — demande-moi la "
            "fiche de la valeur, qui va chercher les chiffres réels.\n")
    else:
        bandeau = (
            "> ⚠️ **Certains chiffres ci-dessous ne viennent d'aucune source.**\n>\n"
            f"> {apercu} — je n'ai retrouvé ces valeurs dans aucun résultat d'outil. "
            "Le reste de la réponse s'appuie bien sur ce que j'ai lu, mais ces "
            "nombres-là sont à vérifier avant de t'en servir.\n")

    # Une caution inventée est pire qu'aucune caution : on retire les « (source : X) »
    # accrochés à une réponse dont les chiffres ne viennent de nulle part.
    corps = _SOURCE_NOMMEE.sub("", texte) if aucune_source else texte
    return bandeau + "\n" + corps.strip()
