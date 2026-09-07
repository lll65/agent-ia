"""
Aucune adresse sans source — la même règle que pour les chiffres et les dates.

⚠️ VU EN VRAI, le 7 septembre 2026. « des news sur le cours boursier de Valneva » →

    Sources :
    1. Boursier.com – « Journée en baisse pour Valneva SE », publié le 04/09/2026
       …/valneva-se-termine-en-legere-hausse-898784.html
    2. Boursier.com – même titre, même date
       …/valneva-se-termine-en-forte-hausse-898784.html
    3. Boursier.com – même contenu
       …/journee-en-baisse-pour-valneva-se-898784.html

Trois adresses, le MÊME numéro d'article (898784), trois chemins différents. Une
seule existe. Le modèle a pris le lien réellement renvoyé par la recherche et en a
fabriqué deux variantes autour, en changeant les mots du chemin.

⚠️ POURQUOI C'EST PIRE QU'UN CHIFFRE FAUX. Un chiffre inventé, on peut en douter. Une
adresse, ça se clique : elle a l'air d'une preuve, et elle mène ailleurs — ou nulle
part. Et ici les deux fausses disent « termine en légère HAUSSE » et « termine en forte
HAUSSE » sous un titre qui annonce une BAISSE. Trois « sources » qui se contredisent
entre elles, sous une réponse qui a l'air sourcée.

LA MÉTHODE, identique à celle des chiffres et des dates. On relève les adresses citées,
on regarde si chacune apparaît dans ce que les outils ont réellement renvoyé, et celles
qui n'y sont pas sont RETIRÉES — pas seulement signalées. C'est la différence avec un
chiffre : un nombre douteux se relit et se juge, une adresse s'ouvre d'un clic avant
qu'on ait eu le temps de lire l'avertissement. Le texte du lien, lui, reste : il porte
l'information, et c'est la caution qui était fausse, pas le propos.
"""
import re

# Une adresse dans une réponse : en markdown, ou écrite nue.
_MARKDOWN = re.compile(r"\[([^\]]{1,200})\]\(\s*(https?://[^)\s]+)\s*\)")
# ⚠️ PREMIÈRE VERSION FAUSSE, et elle ratait précisément son cas. J'avais mis un
# « (?<![(\]]) » pour ne pas réattraper l'adresse d'un lien markdown — sauf que ses
# sources s'écrivent « Boursier.com – titre (https://…) » : une adresse entre
# parenthèses SANS être un lien markdown. Le garde-fou sautait donc les trois liens
# qu'il devait attraper. On neutralise les liens markdown AVANT de chercher les nues,
# au lieu de deviner d'après le caractère qui précède.
_NUE = re.compile(r"\bhttps?://[^\s<>()\[\]«»\"']+")

# La ponctuation de fin de phrase colle à l'adresse et n'en fait pas partie.
_QUEUE = ".,;:!?»\"'"


def _propre(url: str) -> str:
    return (url or "").strip().rstrip(_QUEUE)


def _cle(url: str) -> str:
    """Deux écritures de la MÊME adresse doivent se ressembler.

    On ignore le protocole, le « www. », la barre finale et la casse de l'hôte : une
    recherche renvoie « https://www.boursier.com/x » là où le modèle écrit
    « boursier.com/x », et ce n'est pas une invention.
    """
    u = _propre(url).lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def liens_cites(texte: str) -> list:
    """Toutes les adresses citées dans le texte, en clair."""
    out, vus = [], set()
    for m in _MARKDOWN.finditer(texte or ""):
        u = _propre(m.group(2))
        if u and _cle(u) not in vus:
            vus.add(_cle(u))
            out.append(u)
    # Les liens markdown sont déjà relevés : on les efface pour ne pas les compter deux
    # fois, puis on cherche ce qui reste.
    reste = _MARKDOWN.sub(" ", texte or "")
    for m in _NUE.finditer(reste):
        u = _propre(m.group(0))
        if u and _cle(u) not in vus:
            vus.add(_cle(u))
            out.append(u)
    return out


def _connus(observations) -> set:
    connus = set()
    for o in (observations or []):
        for m in re.finditer(r"https?://[^\s<>()\[\]\"'\\]+", str(o)):
            connus.add(_cle(m.group(0)))
    return connus


def non_sources(texte: str, observations) -> list:
    """Les adresses du texte qu'aucun outil n'a renvoyées."""
    connus = _connus(observations)
    return [u for u in liens_cites(texte) if _cle(u) not in connus]


def relis(texte: str, observations=None) -> str:
    """Retire les adresses inventées, garde ce qu'elles habillaient, et le dit."""
    if not texte:
        return texte
    faux = non_sources(texte, observations)
    if not faux:
        return texte
    # ⚠️ On ne retire une adresse QUE si un outil a réellement renvoyé quelque chose.
    # Sans aucune observation, on ne sait pas si elle est inventée ou simplement
    # recopiée d'ailleurs : retirer sur une ignorance serait aussi faux qu'inventer.
    if not _connus(observations):
        return texte

    faux_cles = {_cle(u) for u in faux}
    out = _MARKDOWN.sub(
        lambda m: m.group(1) if _cle(m.group(2)) in faux_cles else m.group(0), texte)
    out = _NUE.sub(
        lambda m: "" if _cle(m.group(0)) in faux_cles else m.group(0), out)
    # Une adresse retirée laisse sa ponctuation d'accueil : « (…) » vide, virgules
    # doublées, puce orpheline. Ce sont des restes de MON nettoyage, pas du texte.
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([,.;:])", r"\1", out)
    out = re.sub(r"^[ \t]*[-*\d.]+[ \t]*$", "", out, flags=re.M)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    apercu = "\n".join(f"> - `{u}`" for u in faux[:4])
    if len(faux) > 4:
        apercu += f"\n> - … ({len(faux) - 4} autres)"
    seul = len(faux) == 1
    entete = ("un lien que j'ai fabriqué" if seul else f"{len(faux)} liens que j'ai fabriqués")
    phrase = ("Aucun outil ne me l'a renvoyé : je l'ai écrit de moi-même"
              if seul else
              "Aucun outil ne me les a renvoyés : je les ai écrits de moi-même")
    return (out + f"\n\n> ⚠️ **J'ai retiré {entete}.**\n>\n"
            f"> {phrase}, et une adresse inventée mène ailleurs ou nulle part.\n"
            + apercu + "\n>\n"
            "> Le texte est resté ; c'est la source qui n'en était pas une.\n")
