"""
Aucune DATE d'événement sans source — la même règle que pour les chiffres.

⚠️ VU EN VRAI, le 6 septembre 2026. « Des news de 2Crsi ? » →

    • 6 septembre 2026 : 2CRSi a annoncé la vente de serveurs Godì Blackwell Ultra
      en Allemagne pour 110 M€.
    • 26 mars 2026 : résultats du premier semestre 2025/26 (204,7 M€, ×9,8).
    • 29 juin 2026 : la société a lancé une procédure de vérification indépendante
      concernant son contrat allemand.

Tous les CHIFFRES sont exacts. `agent/chiffres.py` n'avait rien à redire, et il avait
raison. La faute est ailleurs : la vente des 110 M€ a été annoncée le **9 juin 2026**,
pas le 6 septembre. Le 6 septembre, c'était le jour de la conversation.

⚠️ POURQUOI C'EST GRAVE, ET PAS UN DÉTAIL DE MISE EN FORME.
Lu dans l'ordre, ce résumé dit : gros contrat tombé aujourd'hui. La réalité dit :
contrat vieux de trois mois, et placé depuis sous vérification indépendante — la
troisième puce, datée du 29 juin, parle d'un contrat que la première fait naître en
septembre. Les mêmes faits, redatés, disent le contraire. C'est exactement le genre de
phrase à partir de laquelle il décide d'acheter.

⚠️ D'OÙ VENAIT LA DATE. De la requête de Nova elle-même : « 2Crsi news 6 septembre
2026 ». Le modèle a relu sa propre question comme étant la date de la réponse. La cause
est corrigée dans `agent/core.py` (on n'écrit plus la date du jour dans une requête qui
ne la demande pas) ; ce module est la vérification de sortie, pour le jour où un modèle
se trompera de date sans qu'on lui ait tendu la perche.

LA MÉTHODE, identique à celle des chiffres. On relève les dates ANNONCÉES dans la
réponse, on regarde si chacune apparaît dans ce que les outils ont réellement renvoyé,
et on signale celles qui n'y sont pas. On n'efface rien : une date fausse retirée
laisserait un fait sans repère, ce qui est pire. Il doit pouvoir juger.
"""
import re

_MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre")
# Les formes réellement rencontrées dans les pages : « fevrier », « aout », « déc. ».
_MOIS_VARIANTES = {
    "janvier": 1, "janv": 1, "jan": 1,
    "février": 2, "fevrier": 2, "févr": 2, "fevr": 2, "feb": 2,
    "mars": 3, "mar": 3,
    "avril": 4, "avr": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juin": 6, "jun": 6,
    "juillet": 7, "juil": 7, "jul": 7,
    "août": 8, "aout": 8, "aug": 8,
    "septembre": 9, "sept": 9, "sep": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11,
    "décembre": 12, "decembre": 12, "déc": 12, "dec": 12,
}

# ⚠️ On n'attrape QUE les dates portant un jour ET une année. Une date sans année
# (« mardi 8 septembre je me réveille ») appartient le plus souvent à son agenda, pas à
# un fait de marché : la signaler serait du bruit, et le bruit finit par se sauter.
_TEXTUELLE = re.compile(
    r"\b(\d{1,2})(?:er)?\s+(" + "|".join(_MOIS_VARIANTES) + r")\.?\s+(\d{4})\b", re.I)
_NUMERIQUE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _annee(a: int) -> int:
    """« 26 » → 2026. Les pages de cotation écrivent l'année sur deux chiffres."""
    return a if a >= 1000 else (2000 + a if a < 70 else 1900 + a)


def dates_citees(texte: str) -> set:
    """Toutes les dates d'un texte, ramenées à des (jour, mois, année)."""
    out = set()
    t = texte or ""
    for m in _TEXTUELLE.finditer(t):
        mois = _MOIS_VARIANTES.get(m.group(2).lower().rstrip("."))
        if mois:
            out.add((int(m.group(1)), mois, _annee(int(m.group(3)))))
    for m in _NUMERIQUE.finditer(t):
        j, mo, a = int(m.group(1)), int(m.group(2)), _annee(int(m.group(3)))
        if 1 <= j <= 31 and 1 <= mo <= 12:
            out.add((j, mo, a))
    for m in _ISO.finditer(t):
        j, mo, a = int(m.group(3)), int(m.group(2)), int(m.group(1))
        if 1 <= j <= 31 and 1 <= mo <= 12:
            out.add((j, mo, a))
    return out


def _lisible(d) -> str:
    return f"{d[0]} {_MOIS[d[1] - 1]} {d[2]}"


def non_sourcees(texte: str, observations) -> list:
    """Les dates annoncées dans le texte qu'aucune observation ne confirme."""
    connues = set()
    for o in (observations or []):
        connues |= dates_citees(str(o))
    return sorted(d for d in dates_citees(texte) if d not in connues)


def _aujourdhui():
    from datetime import datetime
    d = datetime.now()
    return (d.day, d.month, d.year)


# ⚠️ « c'est quoi l'actualité de 2CRSi AUJOURD'HUI » → « DBV annoncera ses résultats du
# deuxième trimestre le 16 juillet 2026 », « présentera de nouvelles données au congrès
# EAACI ». Deux mois d'ancienneté, servis sous une question qui dit « aujourd'hui ».
# Aucune date n'était fausse — elles étaient toutes écrites, toutes exactes. Ce qui
# manquait, c'est ce qu'elles voulaient dire ENSEMBLE : il n'y a rien eu de récent.
# « Je n'ai rien trouvé de récent » et « voici du vieux » se lisent pareil quand on
# survole, et disent le contraire.
_MOTS_MAINTENANT = ("aujourd'hui", "aujourdhui", "du jour", "ce matin", "ce soir",
                    "maintenant", "en ce moment", "actuellement", "dernière", "derniere",
                    "récent", "recent", "quoi de neuf", "actualité", "actualite",
                    "actu", "news", "nouvelles")
_JOURS_FRAIS = 7


def trop_vieux(texte: str, demande: str, aujourdhui=None, jours: int = _JOURS_FRAIS):
    """La date la plus récente citée, si TOUT est plus vieux que `jours`. Sinon None."""
    from datetime import date
    m = " ".join(str(demande or "").lower().split()).replace("’", "'")
    if not any(k in m for k in _MOTS_MAINTENANT):
        return None
    dates = dates_citees(texte)
    if not dates:
        return None
    auj = date(*reversed(aujourdhui)) if aujourdhui else date.today()
    recentes = []
    for j, mo, a in dates:
        try:
            d = date(a, mo, j)
        except ValueError:
            continue
        # Une date FUTURE (une échéance annoncée) ne dit rien de la fraîcheur.
        if d <= auj:
            recentes.append(d)
    if not recentes:
        return None
    plus_recente = max(recentes)
    ecart = (auj - plus_recente).days
    return (plus_recente, ecart) if ecart > jours else None


def relis(texte: str, observations=None, aujourdhui=None, demande: str = "") -> str:
    """Signale les dates que rien n'appuie. N'efface rien : il doit pouvoir juger."""
    if not texte:
        return texte

    # Le plus RÉCENT de ce qu'on raconte est vieux de deux mois, alors qu'il demandait
    # aujourd'hui : on le dit en tête, avant qu'il lise la suite comme du frais.
    vieux = trop_vieux(texte, demande, aujourdhui)
    if vieux:
        d, ecart = vieux
        texte = (f"> ⚠️ **Rien de récent : la nouvelle la plus fraîche que j'aie trouvée "
                 f"date du {_lisible((d.day, d.month, d.year))}, il y a {ecart} jours.**\n>\n"
                 "> Tu m'as demandé aujourd'hui ; je n'ai rien trouvé d'aujourd'hui. Ce "
                 "qui suit est exact, mais ce n'est pas l'actualité du jour — et « je "
                 "n'ai rien trouvé de récent » n'est pas la même chose que « il ne s'est "
                 "rien passé ».\n\n" + texte.strip())

    manquantes = non_sourcees(texte, observations)
    if not manquantes:
        return texte

    auj = aujourdhui or _aujourdhui()
    # Aucune date confirmée nulle part : sans repère, on ne peut rien affirmer du tout.
    aucune = not any(dates_citees(str(o)) for o in (observations or []))

    if auj in manquantes:
        # LA signature de la contamination : la date du jour présentée comme la date
        # d'un fait, alors qu'aucune source ne la porte. C'est le cas 2CRSi.
        autres = [d for d in manquantes if d != auj]
        suite = ("" if not autres else
                 " J'ai aussi daté sans source : " + " · ".join(_lisible(d) for d in autres[:5]) + ".")
        return ("> ⚠️ **J'ai daté d'aujourd'hui (" + _lisible(auj) + ") un fait qu'aucune "
                "source ne date de ce jour.**\n>\n"
                "> C'est probablement MA date, pas celle de l'événement : je cherche avec "
                "la date du jour dans ma requête et je la relis ensuite comme si c'était "
                "la date de la nouvelle. **Une annonce de trois mois présentée comme "
                "tombée ce matin ne dit pas la même chose.** Vérifie la date avant de t'en "
                "servir." + suite + "\n\n" + texte.strip())

    apercu = " · ".join(_lisible(d) for d in manquantes[:5])
    if len(manquantes) > 5:
        apercu += f" · … ({len(manquantes) - 5} autres)"
    tete = ("> ⚠️ **Aucune de ces dates ne vient d'une source.**\n>\n> "
            if aucune else
            "> ⚠️ **Certaines dates ci-dessous ne viennent d'aucune source.**\n>\n> ")
    return (tete + apercu + " — je n'ai retrouvé ces dates dans aucun résultat d'outil. "
            "Le fait est peut-être exact ; c'est le QUAND dont je ne réponds pas.\n\n"
            + texte.strip())
