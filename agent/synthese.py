"""
Relire une synthèse de cours — et rendre utile la section qui ne servait à rien.

⚠️ IL M'A ENVOYÉ SES DEUX COURS DU JOUR. Le second est très bon ; le premier montre
exactement ce qui manque.

Cours 1, section finale :
    ## Zones à éclaircir
    - **Euthanasie** : [passage peu clair]
    - **Morale vs Droit** : [passage peu clair]
    - **Exemple de ponctualité** : [passage peu clair]
    … treize lignes identiques.
Ça répète les titres et n'apprend rien : ni combien, ni où exactement, ni quoi faire.

Cours 2, même section :
    - **Nature du mariage catholique** : les notes disent que le mariage « n'est pas un
      sacrement » et qu'il ne peut pas être « détruit ». Cette formulation est
      théologiquement inexacte et contredit la logique de l'indissolubilité. Il est
      probable qu'il y ait une erreur de transcription.
    - **Terme « luthère »** : n'est pas standard en droit. Probablement une erreur
      d'audition (« adultère » mal entendu).
Là, c'est du travail fait : le doute est nommé, expliqué, et il sait quoi vérifier.

CE QUE FAIT CE MODULE. Le modèle produit parfois l'un, parfois l'autre — c'est la leçon
de toute la semaine : une consigne est une intention. Alors on VÉRIFIE la sortie. Quand
la section finale ne fait que répéter des titres, on la remplace par ce qu'on sait
réellement : combien de passages, dans quelles parties, et quoi faire ensuite.

⚠️ ON NE SUPPRIME PAS LES MARQUEURS DANS LE TEXTE. Ils disent OÙ, et c'est précisément
ce qui manque quand on ne garde qu'une liste à la fin.
"""
import re

_MARQUEUR = re.compile(r"\[passage peu clair\]|\[inaudible\]|\[non transcrit\]", re.I)
_TITRE = re.compile(r"^\s{0,3}(#{2,4})\s+(.+?)\s*$")
_SECTION_ZONES = re.compile(r"^\s{0,3}#{1,3}\s*Zones? [àa] [ée]claircir\s*$", re.I | re.M)


def passages_flous(md: str) -> list:
    """[(section, nombre)] — où se trouvent les passages non compris, dans le corps."""
    section, compte = "", {}
    ordre = []
    dans_zones = False
    for ligne in (md or "").splitlines():
        if _SECTION_ZONES.match(ligne):
            dans_zones = True
            continue
        m = _TITRE.match(ligne)
        if m:
            # Un titre de niveau 1-3 referme la section « Zones à éclaircir ».
            if len(m.group(1)) <= 3:
                dans_zones = False
            section = re.sub(r"[*_`]", "", m.group(2)).strip()
            continue
        # ⚠️ On ne compte QUE le corps : sinon la liste finale, qui répète un marqueur
        # par ligne, gonflerait le total et on annoncerait deux fois trop de trous.
        if dans_zones:
            continue
        n = len(_MARQUEUR.findall(ligne))
        if n:
            if section not in compte:
                ordre.append(section)
            compte[section] = compte.get(section, 0) + n
    return [(s, compte[s]) for s in ordre]


def _section_inutile(bloc: str) -> bool:
    """Cette section « Zones à éclaircir » n'apprend-elle rien ?

    Inutile = chaque ligne se réduit à un titre suivi du marqueur, sans explication.
    C'est le cas du premier cours : treize lignes, zéro information.
    """
    lignes = [l.strip(" -*\t") for l in bloc.splitlines() if l.strip(" -*\t")]
    if not lignes:
        return True
    utiles = 0
    for l in lignes:
        reste = _MARQUEUR.sub("", l)
        reste = re.sub(r"[*_`>]", "", reste)
        # Ce qui reste après le titre et le marqueur : « **Euthanasie** : » ne laisse
        # que « Euthanasie : », donc rien d'explicatif.
        reste = re.sub(r"^[^:]{0,60}:\s*", "", reste).strip(" .·—-")
        if len(reste) > 25:
            utiles += 1
    return utiles == 0


def _remplace_section(md: str, neuf: str) -> str:
    """Remplace le contenu de « Zones à éclaircir », ou l'ajoute à la fin."""
    m = _SECTION_ZONES.search(md or "")
    if not m:
        return (md or "").rstrip() + "\n\n" + neuf
    debut = m.start()
    suite = re.search(r"^\s{0,3}#{1,3}\s+\S", md[m.end():], re.M)
    fin = m.end() + suite.start() if suite else len(md)
    return md[:debut] + neuf + ("\n\n" + md[fin:].lstrip() if suite else "\n")


def relis(md: str, trous: int = 0) -> str:
    """Rend la section « Zones à éclaircir » informative quand elle ne l'est pas."""
    if not md:
        return md
    m = _SECTION_ZONES.search(md)
    if m:
        suite = re.search(r"^\s{0,3}#{1,3}\s+\S", md[m.end():], re.M)
        bloc = md[m.end(): m.end() + suite.start()] if suite else md[m.end():]
        if not _section_inutile(bloc):
            return md            # le modèle a fait le travail : on n'y touche pas

    flous = passages_flous(md)
    total = sum(n for _s, n in flous)
    if not total and not trous:
        # Rien à signaler, et la section le dit peut-être déjà mal : on la normalise.
        if m:
            return _remplace_section(
                md, "## Zones à éclaircir\nRien à signaler : tout le cours a été compris.\n")
        return md

    lignes = ["## Zones à éclaircir", ""]
    if total:
        ou = ", ".join(f"**{s or 'sans titre'}**" + (f" ({n})" if n > 1 else "")
                       for s, n in flous[:8])
        lignes.append(f"- **{total} passage(s) que je n'ai pas compris**, dans : {ou}.")
        # ⚠️ Les marqueurs restent dans le texte : ils disent OÙ, et c'est justement ce
        # qui manque quand on ne garde qu'une liste à la fin.
        lignes.append("  Ils sont marqués `[passage peu clair]` à l'endroit exact — "
                      "c'est là qu'il faut relire tes propres notes.")
    if trous:
        lignes.append(f"- **{trous} tranche(s) d'audio n'ont pas pu être transcrites.** "
                      "Ce qui s'y disait n'est nulle part : ni dans la synthèse, ni dans "
                      "la transcription.")
    lignes.append("")
    lignes.append("_Un mot qui te paraît étrange l'est probablement : la transcription "
                  "confond les termes qu'elle ne connaît pas. Vérifie-les avant de les "
                  "apprendre._")
    return _remplace_section(md, "\n".join(lignes) + "\n")
