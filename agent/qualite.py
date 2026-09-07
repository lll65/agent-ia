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


# ⚠️ « quand nova enregistre des infos elles n'apparaissent pas dans mémoire ».
# Le stockage marche : on l'a rejoué, huit faits ajoutés, sept conservés (le huitième
# remplacé exprès, même sujet). Ce qui ne marche pas, c'est la PHRASE.
# La consigne dit pourtant, mot pour mot : « RIEN n'a été mémorisé de ce message. Ne dis
# donc NI "c'est noté", NI "je retiens", NI "je garde ça en tête" : ce serait faux. »
# Un modèle saturé l'enfreint quand même — c'est la leçon de toute la journée : une
# consigne est une intention, ce qui coûte cher se vérifie sur la sortie.
# Et ça coûte cher : il croit son information rangée, il ne la redit pas, elle est
# perdue. Une mémoire qui prétend retenir est pire qu'une mémoire qui avoue oublier.
_PRETEND_RETENIR = re.compile(
    r"((?:c'est|c est)\s+not[ée]|bien not[ée]"
    r"|je\s+(?:retiens|m[ée]morise|garde)\b"
    # ⚠️ « je note » a DEUX sens : « je mémorise » et « je remarque ». « Je note la
    # date du 9 juin dans ta réponse » ne promet rien — corriger là serait ajouter du
    # bruit sur une phrase juste. On n'attrape que la forme qui engage : « je note
    # que… », ou « je note » en fin de phrase.
    r"|je\s+note\s+(?:que\b|[çc]a\b|cela\b)|je\s+note\s*[.!…]"
    r"|(?:je\s+)?garde\s+(?:[çc]a|cela)\s+en\s+t[êe]te|j'?ai\s+(?:bien\s+)?"
    r"(?:not[ée]|retenu|m[ée]moris[ée]|enregistr[ée])|c'?est\s+enregistr[ée]"
    r"|(?:info|information)\s+(?:bien\s+)?(?:not[ée]e|enregistr[ée]e))", re.I)


def pretend_retenir(texte: str) -> bool:
    return bool(_PRETEND_RETENIR.search(texte or ""))


def sans_fausse_memoire(texte: str, appris) -> str:
    """Si RIEN n'a été mémorisé, une phrase qui dit le contraire est corrigée.

    On ne supprime pas la réponse : on retire l'affirmation fausse et on dit ce qui
    s'est réellement passé, avec la manière de s'y prendre.
    """
    if appris or not texte or not pretend_retenir(texte):
        return texte
    return (texte.rstrip() +
            "\n\n> ⚠️ **Correction : je n'ai rien enregistré.** Je viens de l'écrire "
            "au-dessus, et c'est faux — tu ne le retrouveras pas dans 🧠 Mémoire.\n>\n"
            "> Ça arrive quand je ne reconnais pas l'information comme durable, ou "
            "quand aucun modèle ne répond pour la reformuler. Redis-la-moi en "
            "commençant par **« retiens que… »** : là, je la range à coup sûr.")


def relis(texte: str, demande: str = "") -> str:
    """Le passage obligé de toute réponse : ni mur de signes, ni porte fermée,
    ni référence fantôme — ni marqueur technique laissé à l'écran.

    ⚠️ Le rapport de mails transporte sa version à dire dans un marqueur, que
    l'interface web détache pour la voix. Telegram, les automatisations et le briefing
    ne passent pas par là : sans ce filet, ils afficheraient le marqueur en clair.
    Il est retiré ICI parce que c'est le seul point par lequel TOUTES les réponses
    passent — un nettoyage placé ailleurs en manquerait forcément un.
    """
    t = texte or ""
    try:
        from agent.rapport_mail import separe
        t = separe(t)[0]
    except Exception:
        pass
    return porte_de_sortie(sans_repetition(sans_citations_fantomes(t)), demande)
