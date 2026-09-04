"""
Deux façons de rendre une réponse inutilisable, vues le même jour, corrigées ici.

1. LE MUR DE CARACTÈRES.
   « et un schéma du cours de la bourse stp » →
       NASDAQ Composite 26 370,89 ──▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁… (un millier de fois)
   Le modèle, à qui on demandait un graphique sans lui donner de chiffres, a dessiné
   une ligne plate en répétant le même caractère jusqu'à épuisement. Ça remplit
   l'écran, ça noie le reste de la réponse, et ça ne veut rien dire. Aucune réponse
   légitime ne répète mille fois le même signe : ça se coupe sans rien perdre.

2. LE REFUS SEC.
   « fait des recherches pendant minimum 10 minutes sur une action PEA qui va
     exploser d'ici quelques mois et explique pourquoi » →
       « Je suis désolé, mais je ne peux pas répondre à cette demande. »
   Point final. La consigne de Nova dit pourtant, mot pour mot : « Jamais de "je ne
   peux pas" sans alternative. » Et il y avait tout à dire : personne ne sait quelle
   action va monter — c'est vrai et il faut le dire — mais chercher les sociétés
   éligibles au PEA qui ont des échéances connues dans les mois qui viennent, c'est
   exactement ce qu'il demandait, et c'est faisable.
   Un refus nu n'est pas de la prudence. C'est une porte fermée sur une question
   légitime, et ça se lit comme du mépris.

⚠️ RÈGLE APRÈS LE MODÈLE. Ces deux consignes existaient déjà dans les prompts. Le
modèle les a enfreintes quand même — un modèle saturé enfreint n'importe quelle
consigne. Ce qui compte se vérifie sur la sortie.
"""
import re

# Un caractère répété au-delà de ça n'est plus une figure, c'est un débordement.
_MAX_REPET = 40
_REPET_CAR = re.compile(r"(.)\1{" + str(_MAX_REPET) + r",}", re.S)
# Idem pour un petit motif (« ─▁─▁─▁… », « .-.-.- ») répété en boucle.
_REPET_MOTIF = re.compile(r"((?:[^\w\s]|_){2,8}?)\1{12,}")

_REFUS = re.compile(
    r"(je (?:ne )?(?:peux|puis) pas (?:répondre|vous aider|t'aider|traiter|faire)|"
    r"je suis (?:désolé|desole)e?,? mais je ne peux|"
    r"je ne suis pas en mesure de|"
    r"cette demande (?:ne peut pas|dépasse)|"
    r"je (?:ne )?suis pas (?:autorisé|habilité)e? [àa])", re.I)
# Ce qui transforme un refus en réponse : une porte de sortie.
_ALTERNATIVE = re.compile(
    r"(en revanche|par contre|ce que je peux|voici ce que|à la place|"
    r"je peux (?:en revanche|quand même|te|vous)|si tu veux|veux-tu que|"
    r"je te propose|on peut|il existe|voici|par exemple)", re.I)


# ⚠️ VU EN VRAI : « …c'est le consensus de prix cible sur un an (source : Fintel et
# ChartMill)【2†source】【3†source】. » Ces marqueurs viennent du format de citation de
# certains modèles ; ils ne renvoient à rien chez nous et s'affichent tels quels. Du
# bruit qui a l'air d'une référence — donc pire que du bruit.
_CITATION_MODELE = re.compile(r"【[^】]{0,40}】|\[\s*citation:[^\]]{0,40}\]|"
                              r"\[\s*\^?\d{1,2}\s*†[^\]]{0,30}\]")


def sans_citations_fantomes(texte: str) -> str:
    """Retire les marqueurs de citation qui ne pointent vers rien."""
    return _CITATION_MODELE.sub("", texte or "")


def sans_repetition(texte: str) -> str:
    """Coupe les débordements de caractères, sans toucher au reste.

    Volontairement prudent : on garde `_MAX_REPET` signes et on dit qu'on a coupé.
    Une ligne de tirets qui sépare deux sections reste intacte.
    """
    if not texte:
        return texte
    # Le motif AVANT le caractère : sinon « ▁▁▁… » est d'abord ramené à 40 signes,
    # que la règle du motif recoupe ensuite — et la coupure est annoncée deux fois.
    out = _REPET_MOTIF.sub(lambda m: m.group(1) * 6 + " […]", texte)
    return _REPET_CAR.sub(lambda m: m.group(1) * _MAX_REPET + " […]", out)


def refus_sec(texte: str) -> bool:
    """Un « je ne peux pas » sans la moindre porte de sortie ?"""
    t = (texte or "").strip()
    if not t or len(t) > 700:      # une explication longue n'est pas un refus sec
        return False
    return bool(_REFUS.search(t)) and not _ALTERNATIVE.search(t)


# Ce que Nova PEUT faire quand on lui demande de prédire. Écrit ici, en français, et
# pas laissé au modèle : c'est justement quand il cale qu'on en a besoin.
_PORTE_FINANCE = (
    "\n\n---\n\n**Ce que je peux faire à la place — et qui répond vraiment à ta "
    "question.**\n\n"
    "Personne ne sait quelle action va monter : si quelqu'un le savait, il ne "
    "l'écrirait pas sur Internet. Mais ce que tu cherches — repérer une société "
    "*avant* que ça bouge — se travaille avec des faits :\n\n"
    "- les **sociétés éligibles au PEA** dont une échéance connue tombe dans les "
    "prochains mois (publication de résultats, décision d'autorité, fin d'un "
    "contrat, essai clinique) ;\n"
    "- pour chacune : la **date exacte** de cette échéance, ce qui est en jeu, et ce "
    "qui se passe si ça rate ;\n"
    "- les **volumes** et les mouvements inhabituels, qui montrent quand quelque "
    "chose attire l'attention ;\n"
    "- et ce que je **ne sais pas** — dit clairement, plutôt que comblé.\n\n"
    "Je te décris ce qui existe, tu décides. Dis-moi : je pars sur quel secteur, ou "
    "je balaie large sur les valeurs PEA ?")


def porte_de_sortie(texte: str, demande: str = "") -> str:
    """Remplace un refus nu par ce qui est réellement faisable.

    On ne supprime pas le refus — s'il est justifié, il doit rester lisible. On lui
    ajoute ce qu'il manquait : la suite.
    """
    if not refus_sec(texte):
        return texte
    m = (demande or "").lower()
    if any(k in m for k in ("action", "bourse", "pea", "etf", "investir", "titre",
                            "cours", "placement", "crypto")):
        return texte.strip() + _PORTE_FINANCE
    return (texte.strip() +
            "\n\n---\n\nDis-moi ce qui te bloque exactement dans ta demande et je "
            "cherche l'angle que je peux traiter — je préfère te dire ce que je sais "
            "faire plutôt que m'arrêter là.")


def relis(texte: str, demande: str = "") -> str:
    """Le passage obligé de toute réponse : ni mur de signes, ni porte fermée,
    ni référence fantôme."""
    return porte_de_sortie(sans_repetition(sans_citations_fantomes(texte or "")), demande)
