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
                vus.append((v, unite, m.group(0).strip()))
            # ⚠️ On compare des valeurs, pas des écritures : « 28,60 € » et « 28.6 EUR »
            # sont la même chose, et une différence d'arrondi n'est pas un désaccord.
            distinctes = []
            for v, u, brut in vus:
                if not any(abs(v - w) <= max(abs(v) * 0.005, 0.01) for w, _u, _b in distinctes):
                    distinctes.append((v, u, brut))
            if len(distinctes) > 1:
                out.append((titre, nom, [b for _v, _u, b in distinctes]))
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
