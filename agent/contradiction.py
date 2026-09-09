"""
Deux cours différents pour la même action, ce n'est pas deux informations : c'est zéro.

⚠️ VU EN VRAI, le 8 septembre 2026, dans son automatisation « Suivi action » :

    2CRSI (AL2SI)
    • Cours : 28,60 €
    • Variation : ‑1,72 % (baisse)
    • Source : Boursorama (pas de date précise indiquée)
    • Autre cotation trouvée : 26,74 €, +1,27 % (hausse) — analyse XTB, sans date

Une baisse à 28,60 € ET une hausse à 26,74 €, l'une sous l'autre, présentées comme deux
résultats de recherche. Lui : « les infos des automatisations sont fausses ».

⚠️ POURQUOI agent/chiffres.py NE POUVAIT RIEN VOIR. Il vérifie qu'un nombre vient d'une
source. Ici les deux viennent d'une source — elles sont toutes les deux « adossées ».
Ce qui cloche n'est pas leur origine, c'est qu'elles ne peuvent pas être vraies
ensemble. Une valeur invérifiable, on la signale ; deux valeurs incompatibles, on ne
choisit pas à sa place et on ne les empile pas non plus : on dit qu'on ne sait pas.

Empiler les deux, c'est se couvrir en lui laissant le travail. Il lit la première,
décide, et découvre la seconde trop tard — ou pas du tout.
"""
import re

# Ce qu'on surveille : les grandeurs pour lesquelles DEUX valeurs sont contradictoires.
# Une « note » ou un « nombre d'articles » peut légitimement varier d'un paragraphe à
# l'autre ; un cours de Bourse à un instant donné, non.
_GRANDEURS = (
    ("cours", r"(?:cours|prix|cotation|derni[èe]re? cotation|clôture|cloture)"),
    ("variation", r"(?:variation|évolution|evolution|performance du jour)"),
    ("capitalisation", r"capitalisation"),
)
_NOMBRE = r"([+-]?\d{1,4}(?:[  ]\d{3})*(?:[.,]\d{1,4})?)\s*(%|€|\$|EUR|USD|euros?)"

# Un titre de section : c'est lui qui dit DE QUELLE valeur on parle.
_TITRE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|\*\*)?([A-Z0-9][^\n*#|]{2,60}?)(?:\*\*)?\s*$")


def _valeur(t: str):
    t = str(t).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _sections(texte: str):
    """[(titre, contenu)] — le texte découpé à ses intitulés."""
    titre, corps, out = "", [], []
    for ligne in (texte or "").splitlines():
        nu = ligne.strip()
        # Une ligne courte, sans ponctuation de phrase, qui ouvre un bloc.
        if nu and len(nu) < 70 and not nu.startswith(("-", "*", "•", ">", "|")) \
                and not re.search(r"[.:;]\s*\S", nu) and _TITRE.match(nu):
            if titre or corps:
                out.append((titre, "\n".join(corps)))
            titre, corps = _TITRE.match(nu).group(1).strip(), []
            continue
        corps.append(ligne)
    out.append((titre, "\n".join(corps)))
    return out


# ⚠️ DE QUELLE SOCIÉTÉ PARLE CETTE LIGNE ? C'est la question qui manquait, et son écran
# l'a montré crûment. Son suivi d'actions affichait :
#
#     ⚠️ Deux valeurs qui ne peuvent pas être vraies ensemble.
#       cours : « Cours : 2,38 € » / « Cours : 28,64 € »
#     Ne décide rien là-dessus.
#
# Sauf que 2,38 € est le cours de DBV et 28,64 € celui de 2CRSi. Les deux sont JUSTES.
# Les sections par société étaient bien reconnues — mais le paragraphe « Actualités
# récentes » cite les deux sociétés, une par puce, et le module comparait leurs cours.
#
# Un garde-fou qui crie au loup est pire qu'un garde-fou absent : il apprend à ignorer
# le bandeau, et le jour où la contradiction est vraie, elle ne sera pas lue.
_SIGLE = re.compile(r"\b[A-Z]{2,6}\d*\b")
_TICKER = re.compile(r"\b(?:\d{1,2}[A-Za-z]{2,6}|[A-Za-z]{2,6}\d{1,3}[A-Za-z]*)\b")
_PROPRE = re.compile(r"\b[A-ZÀ-Ý][\wÀ-ÿ'’-]{2,}\b")
# Les majuscules qui ne désignent aucune société : début de puce, mots de liaison.
_PAS_UN_NOM = {"Cours", "Variation", "Actualité", "Actualités", "Le", "La", "Les", "Un",
               "Une", "Des", "Selon", "Source", "Sources", "Prix", "Objectif", "Titre",
               "En", "Au", "Aux", "Ce", "Cette", "Il", "Elle", "Je", "Tu", "Nous", "Vous",
               "Article", "Analyse", "Avis", "Perspectives", "Volatilité", "Résultats"}


def sujet_de_ligne(ligne: str) -> set:
    """Les noms de sociétés cités dans cette ligne — sigles, codes, noms propres."""
    t = str(ligne or "")
    noms = set(_SIGLE.findall(t)) | set(_TICKER.findall(t))
    noms |= {m for m in _PROPRE.findall(t) if m not in _PAS_UN_NOM}
    return {n.lower().strip(".,;:()") for n in noms if len(n) >= 2}


def _memes_sujets(a: set, b: set) -> bool:
    """Ces deux valeurs peuvent-elles porter sur la MÊME chose ?

    Si les deux lignes nomment des sociétés et qu'elles n'ont aucune en commun, elles
    parlent d'autre chose : il n'y a pas de contradiction à signaler. Si l'une au moins
    ne nomme personne, on ne peut pas trancher — et dans le doute on signale, parce que
    manquer une vraie contradiction sur un cours coûte plus cher qu'un bandeau de trop.
    """
    if not a or not b:
        return True
    return bool(a & b)


def desaccords(texte: str) -> list:
    """[(section, grandeur, [valeurs])] — les grandeurs annoncées deux fois, autrement."""
    out = []
    for titre, corps in _sections(texte):
        for nom, motif in _GRANDEURS:
            vus = []
            for m in re.finditer(motif + r"[^\n]{0,24}?" + _NOMBRE, corps, re.I):
                v, unite = _valeur(m.group(1)), m.group(2)
                if v is None:
                    continue
                # La LIGNE entière, pas seulement l'extrait : c'est elle qui dit de
                # quelle société il s'agit.
                debut = corps.rfind("\n", 0, m.start()) + 1
                fin = corps.find("\n", m.end())
                ligne = corps[debut:fin if fin > 0 else len(corps)]
                vus.append((v, unite, m.group(0).strip(), sujet_de_ligne(ligne)))
            # ⚠️ On compare des valeurs, pas des écritures : « 28,60 € » et « 28.6 EUR »
            # sont la même chose, et une différence d'arrondi n'est pas un désaccord.
            distinctes = []
            for v, u, brut, suj in vus:
                proche = any(abs(v - w) <= max(abs(v) * 0.005, 0.01)
                             and _memes_sujets(suj, s2) for w, _u, _b, s2 in distinctes)
                if not proche:
                    distinctes.append((v, u, brut, suj))
            # Et on ne retient que les valeurs qui portent VRAIMENT sur la même chose.
            for i, (v, u, brut, suj) in enumerate(distinctes):
                concurrentes = [b2 for w, _u2, b2, s2 in distinctes[i + 1:]
                                if _memes_sujets(suj, s2)]
                if concurrentes:
                    out.append((titre, nom, [brut] + concurrentes))
                    break
    return out


def relis(texte: str) -> str:
    """Signale les grandeurs qui se contredisent, en tête, sans choisir à sa place."""
    if not texte:
        return texte
    trouves = desaccords(texte)
    if not trouves:
        return texte
    lignes = []
    for titre, grandeur, valeurs in trouves[:5]:
        ou = f"**{titre}** — " if titre else ""
        lignes.append(f"> - {ou}{grandeur} : " + " / ".join(f"`{v}`" for v in valeurs))
    return ("> ⚠️ **Deux valeurs qui ne peuvent pas être vraies ensemble.**\n>\n"
            + "\n".join(lignes) + "\n>\n"
            "> Je ne sais pas laquelle est la bonne, et je ne choisis pas à ta place : "
            "elles viennent de sources différentes, relevées à des moments différents. "
            "**Ne décide rien là-dessus** — ouvre la fiche de la valeur, qui va chercher "
            "une cotation datée.\n\n" + texte.strip())
