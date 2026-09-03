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


def relis(texte: str, noms: list = None, actu_verifiee: bool = True) -> str:
    """Retire les causes inventées et dit franchement ce qui n'a pas été trouvé.

    `actu_verifiee` : False quand la recherche d'actualité a ÉCHOUÉ (réseau, quota).
    Ce cas-là ne doit surtout pas se lire « rien de neuf » — voir plus bas.
    """
    if not texte:
        return texte
    phrases, gardees, retirees = _phrases(texte), [], 0
    for p in phrases:
        if cause_creuse(p):
            retirees += 1
            continue
        gardees.append(p)
    sortie = " ".join(x for x in gardees if x.strip())

    fortes = [v for v in variations(texte) if v >= SEUIL_NOTABLE]
    if not (retirees or (fortes and not actu_verifiee)):
        return sortie

    bloc = ["", "---", ""]
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
