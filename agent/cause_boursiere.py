"""
« Aucune nouvelle annonce n'est tombée aujourd'hui pour expliquer ces variations :
les mouvements reflètent la dynamique de marché et la digestion des actualités
de l'été. »

C'est ce que Nova a écrit sur 2CRSi le 2 septembre 2026, jour où le titre a pris
+9,25 %. Zonebourse avait publié à 12h37 le même jour : « 2CRSi, toujours dans les
petits papiers de Portzamparc, progresse en Bourse » — Portzamparc maintenait la
valeur dans sa liste High Five. La cause existait, elle était datée, elle était
publique. Nova ne l'avait pas trouvée, et elle a comblé le trou.

⚠️ DEUX FAUTES, ET LA SECONDE EST LA PIRE.
  1. Ne pas avoir trouvé l'article. C'est une limite de la recherche — ça arrive.
  2. Avoir présenté « je n'ai rien trouvé » comme « il n'y a rien », puis avoir
     fourni une explication de remplacement. « La dynamique de marché » n'est pas
     une cause : c'est une façon de ne pas dire « je ne sais pas » qui a l'air
     d'un diagnostic. Lohan lit ça et croit qu'il n'y a rien à chercher.

Il avait demandé exactement ça : « les infos de base qui pourraient me permettre
de m'informer AVANT une baisse ou une hausse ». Une explication inventée fait
précisément l'inverse : elle referme la question.

⚠️ RÈGLE AVANT LE MODÈLE. On ne se contente pas de l'interdire dans le prompt : un
modèle saturé reproduira la formule. Le texte produit est relu ici, et une cause
creuse est REMPLACÉE par l'aveu et par les endroits où aller vérifier.
"""
import re

# Les formules qui font passer une absence de réponse pour une réponse. Aucune n'est
# vérifiable, toutes sont interchangeables — c'est ce qui les trahit.
_CREUX = re.compile(
    r"(dynamique(?:s)? de march[ée]|digestion des actualit[ée]s|sentiment de march[ée]|"
    r"contexte (?:g[ée]n[ée]ral|de march[ée])|mouvement technique|rotation sectorielle|"
    r"prises? de b[ée]n[ée]fices?|effet de march[ée]|humeur du march[ée]|"
    r"tendance g[ée]n[ée]rale du march[ée]|sp[ée]culation)", re.I)

# …employées comme CAUSE. « Le marché est nerveux » est une observation ; « la hausse
# s'explique par la dynamique de marché » est une explication inventée.
_CAUSAL = re.compile(
    r"(expliqu\w+|s'explique|refl[èe]t\w+|traduis\w+|d[ûu]e? [àa]|en raison de|"
    r"[àa] cause de|r[ée]sult\w+ de|proviennent? de|li[ée]s? [àa]|justifi\w+|"
    r"attribu\w+|correspond\w* [àa])", re.I)

# Une variation de cette ampleur a presque toujours une cause publiée quelque part.
SEUIL_NOTABLE = 3.0


def _phrases(texte: str) -> list:
    """Découpe en phrases sans casser les nombres (« +9,25 % », « 2,37 € »)."""
    return re.split(r"(?<=[.!?])\s+", texte or "")


def cause_creuse(phrase: str) -> bool:
    """Cette phrase explique-t-elle un mouvement par une formule qui ne dit rien ?"""
    return bool(_CREUX.search(phrase or "") and _CAUSAL.search(phrase or ""))


def variations(texte: str) -> list:
    """Les variations en pourcentage citées dans le rapport, en valeur absolue."""
    out = []
    for m in re.finditer(r"([+-]\s?\d{1,3}(?:[.,]\d+)?)\s*%", texte or ""):
        try:
            out.append(abs(float(m.group(1).replace(" ", "").replace(",", "."))))
        except ValueError:
            continue
    return out


def ou_verifier(noms: list) -> list:
    """Où Lohan peut aller voir lui-même — des liens, pas un conseil de « surveiller ».

    ⚠️ Ces adresses sont construites, pas récupérées : elles ne prétendent donc pas
    qu'un article existe. Elles disent seulement où chercher.
    """
    liens = []
    for nom in (noms or [])[:3]:
        q = re.sub(r"[^\w\s.-]", "", str(nom)).strip()
        if not q:
            continue
        enc = q.replace(" ", "+")
        liens.append(f"- **{q}** — "
                     f"[Zonebourse](https://www.zonebourse.com/recherche/?q={enc}) · "
                     f"[Boursorama](https://www.boursorama.com/recherche/{enc}) · "
                     f"[Google Actualités](https://news.google.com/search?q={enc}&hl=fr)")
    return liens


def _corrige_fraicheur(texte: str, fenetre: int, perimees: list) -> str:
    """Réécrit le titre menteur là où il est, plutôt que de le contredire plus bas.

    Deux phrases qui se contredisent dans une même réponse, c'est la rassurante qu'on
    retient : le titre doit cesser de mentir à l'endroit exact où il est lu.
    """
    plus_vieux = max(age for _lib, age in perimees)
    return re.sub(r"\(\s*moins de\s+\d{1,3}\s+jours?\s*\)",
                  f"(⚠️ pas toutes : la plus ancienne a {plus_vieux} jours)",
                  texte, flags=re.I)


def relis(texte: str, noms: list = None, actu_verifiee: bool = True) -> str:
    """Retire les causes inventées et dit franchement ce qui n'a pas été trouvé.

    `actu_verifiee` : False quand la recherche d'actualité a ÉCHOUÉ (réseau, quota).
    Ce cas-là ne doit surtout pas se lire « rien de neuf » — voir plus bas.
    """
    if not texte:
        return texte
    # ⚠️ DÉFAUT QUE J'AVAIS INTRODUIT ICI : on découpait le texte ENTIER en phrases puis
    # on le recollait avec des espaces. Toute réponse finance dont les lignes finissent
    # par un point — donc presque toutes — ressortait APLATIE : listes numérotées mises
    # bout à bout, titres collés au paragraphe suivant. Exactement la « liste de course,
    # police plate » qu'il avait déjà signalée, réintroduite par le garde-fou censé
    # améliorer la qualité. On travaille donc LIGNE PAR LIGNE : la mise en forme est
    # une donnée, pas un détail.
    retirees = 0
    lignes = []
    for ligne in (texte or "").split("\n"):
        if not ligne.strip():
            lignes.append(ligne)
            continue
        gardees = []
        for p in _phrases(ligne):
            if cause_creuse(p):
                retirees += 1
                continue
            gardees.append(p)
        reste = " ".join(x for x in gardees if x.strip())
        # Une ligne vidée de sa seule phrase disparaît ; sinon on garde son indentation.
        if reste:
            lignes.append(re.match(r"^[ \t>*\-\d.)]*", ligne).group(0) + reste
                          if not reste.startswith(ligne[:1]) else reste)
    sortie = "\n".join(lignes).strip("\n")

    # ⚠️ La fraîcheur ANNONCÉE contre les dates réellement citées. Le titre rassure et
    # personne ne recalcule trois dates en lisant — surtout pas avant d'acheter.
    fenetre, perimees = fraicheur_contredite(sortie)
    if perimees:
        sortie = _corrige_fraicheur(sortie, fenetre, perimees)

    fortes = [v for v in variations(texte) if v >= SEUIL_NOTABLE]
    if not (retirees or perimees or (fortes and not actu_verifiee)):
        return sortie

    bloc = ["", "---", ""]
    if perimees:
        detail = " · ".join(f"« {lib} » a {age} jours" for lib, age in perimees[:4])
        bloc.append(f"⚠️ **Attention à la fraîcheur.** J'ai présenté comme « récent » "
                    f"(moins de {fenetre} jours) ce qui ne l'est pas : {detail}. "
                    "Sur une décision d'achat, une info d'il y a trois semaines n'a pas "
                    "la même valeur qu'une info d'hier — vérifie la date avant de t'en servir.")
    if not actu_verifiee:
        # ⚠️ « Je n'ai pas pu chercher » et « il n'y a rien » se ressemblent à l'écran
        # et n'ont rien à voir. Confondre les deux, c'est rassurer à tort.
        bloc.append("⚠️ **Je n'ai pas pu vérifier l'actualité** de ces valeurs — ce "
                    "n'est pas la même chose que « il ne s'est rien passé ».")
    elif retirees:
        bloc.append("⚠️ **Je n'ai pas trouvé ce qui explique ce mouvement.** Ce n'est "
                    "pas pareil que « il n'y a rien » : une variation de cette ampleur "
                    "a presque toujours une cause publiée quelque part, et c'est ma "
                    "recherche qui n'est pas allée la chercher.")
    liens = ou_verifier(noms or [])
    if liens:
        bloc.append("")
        bloc.append("**À vérifier toi-même :**")
        bloc.extend(liens)
    return (sortie + "\n" + "\n".join(bloc)).strip()

# ═══ LA FRAÎCHEUR ANNONCÉE CONTRE LES DATES RÉELLEMENT CITÉES ═══
#
# ⚠️ Réponse rendue le 3 septembre 2026, sur une valeur qu'il envisageait d'acheter :
#     « Actualités récentes (moins de 7 jours)
#       1. 12 août 2026 – Résultat du deuxième trimestre… »
# Le 12 août, ce jour-là, a VINGT-DEUX JOURS. La consigne de fraîcheur existe pourtant
# depuis longtemps et elle est explicite. Le modèle a écrit le titre, puis rangé
# dessous ce qu'il avait — sans jamais faire la soustraction.
#
# C'est le genre d'erreur qu'un texte bien écrit rend invisible : le titre rassure, et
# on ne recalcule pas trois dates en lisant. Or lui s'en sert pour décider d'acheter.
# Une soustraction, elle, ne se trompe jamais — donc on la fait ici.
_MOIS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre")
_DATE_LONGUE = re.compile(
    r"\b(\d{1,2})(?:er)?\s+(janvier|f[ée]vrier|mars|avril|mai|juin|juillet|"
    r"ao[uû]t|septembre|octobre|novembre|d[ée]cembre)\s+(\d{4})\b", re.I)
_DATE_COURTE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
# « moins de 7 jours », « actualités récentes », « ces derniers jours »…
_PROMESSE_FRAICHEUR = re.compile(
    r"(moins de\s+(\d{1,3})\s+jours?"
    r"|actualit[ée]s?\s+r[ée]centes?|nouvelles?\s+r[ée]centes?"
    r"|ces derniers jours|tout r[ée]cemment|derni[èe]res? actualit[ée]s?)", re.I)


def _sans_accent_mois(mot: str) -> str:
    m = (mot or "").lower().replace("û", "u").replace("é", "e").replace("è", "e")
    for i, nom in enumerate(_MOIS_FR):
        n = nom.replace("û", "u").replace("é", "e").replace("è", "e")
        if m == n:
            return i + 1
    return 0


def dates_citees(texte: str) -> list:
    """Les dates écrites dans le texte, en (libellé, date). Ignore ce qui n'en est pas."""
    from datetime import date
    out = []
    for m in _DATE_LONGUE.finditer(texte or ""):
        mois = _sans_accent_mois(m.group(2))
        if not mois:
            continue
        try:
            out.append((m.group(0), date(int(m.group(3)), mois, int(m.group(1)))))
        except ValueError:
            continue
    for m in _DATE_COURTE.finditer(texte or ""):
        try:
            out.append((m.group(0), date(int(m.group(3)), int(m.group(2)), int(m.group(1)))))
        except ValueError:
            continue
    return out


def fraicheur_contredite(texte: str, aujourdhui=None) -> tuple:
    """(fenêtre annoncée en jours, [(libellé, âge)]) pour les dates qui la dépassent.

    Rend (0, []) si le texte ne promet aucune fraîcheur — on ne reproche rien à un
    texte qui n'a rien promis.
    """
    if aujourdhui is None:
        try:
            from agent.horloge import maintenant
            aujourdhui = maintenant().date()
        except Exception:
            return 0, []
    p = _PROMESSE_FRAICHEUR.search(texte or "")
    if not p:
        return 0, []
    fenetre = 7
    if p.group(2):
        try:
            fenetre = max(1, min(365, int(p.group(2))))
        except ValueError:
            fenetre = 7
    trop_vieilles = []
    for libelle, d in dates_citees(texte):
        age = (aujourdhui - d).days
        # Une date FUTURE n'est pas une actualité périmée (« commercialisation en 2027 »).
        if age > fenetre:
            trop_vieilles.append((libelle, age))
    return fenetre, trop_vieilles
