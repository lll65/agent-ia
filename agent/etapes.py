"""
Réfléchir EN ÉTAPES — décomposer une grosse question, traiter chaque morceau, puis
rassembler.

« claude a vraiment une super intelligence, tu penses qu'avec Nova ça peut le faire ?
  sinon pourquoi on lui permettrait pas de créer plein de sous-agents ?
  mais pour ça faut vraiment que la limite dans la fiole soit fiable »

Il a raison sur les trois points, y compris le dernier — qui est le plus important.

⚠️ CE QUI SÉPARE VRAIMENT NOVA DE CLAUDE, ET CE N'EST PAS L'INTELLIGENCE.
Sur sa question de rentrée à Pau (sport, job étudiant, copine à Toulouse, organisation),
Claude a lancé 6 recherches et rappelé 6 souvenirs. Nova a lancé ZÉRO recherche : elle a
classé la demande en « agenda » et répondu de mémoire — d'où « le club de boxe anglaise
du Centre Sportif de Pau », qui n'existe pas. L'écart venait de la MÉTHODE, pas du
modèle : une question à cinq sujets recevait une seule passe, un seul angle, et deux
recherches au total.

Une étape ici, c'est un sujet : « le sport à l'UPPA », « où trouver un job étudiant ».
Chacune a droit à son propre appel et à ses propres recherches. C'est ce qui permet à
une réponse de tenir sur cinq sujets sans en survoler quatre.

⚠️ POURQUOI CE N'EST PAS « PLEIN DE SOUS-AGENTS ».
Sa proposition — un bouton, cinq sous-agents maximum — est la bonne intuition, et la
limite qu'il pose lui-même est la bonne aussi. Cinq sous-agents, c'est cinq appels de
plus : sur des quotas gratuits qui saturent déjà (« Mes modèles gratuits sont à leur
limite », groq vide, gemini 503, mistral 429 depuis deux jours), c'est multiplier par
cinq la fréquence de la panne. D'où :

  • les étapes sont SÉQUENTIELLES, pas parallèles — une étape qui échoue n'emporte pas
    les autres, et on peut s'arrêter en cours de route ;
  • le nombre d'étapes est décidé par L'ÉNERGIE RESTANTE, pas par l'envie ;
  • sous un certain seuil, on refuse et on le DIT, au lieu de lancer cinq appels qui
    finiront tous en « limite atteinte » ;
  • ce module ne prédit RIEN sur ce que les étapes vont trouver : il rapporte ce
    qu'elles ont réellement rendu, échecs compris.

⚠️ ET LA FIOLE EST APPROXIMATIVE. En streaming, la consommation est comptée en morceaux
reçus, pas en jetons réels : le pourcentage est un ordre de grandeur. C'est assez pour
décider « cinq étapes ou une seule » ; ce ne serait pas assez pour promettre un nombre
d'appels restants. On s'en sert donc comme d'un garde-fou, jamais comme d'une garantie.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Jamais plus, quoi qu'il demande : au-delà, le temps de Render et ses quotas partent
# avant que la réponse n'arrive. C'est le chiffre qu'il a proposé lui-même.
MAX_ETAPES = 5

# En dessous de ce reste d'énergie, on ne lance pas plusieurs étapes : elles échoueraient
# les unes après les autres et il aurait attendu pour rien.
SEUIL_REFUS = 0.15          # 15 % — plus rien à dépenser
SEUIL_PRUDENT = 0.40        # 40 % — on se limite à deux étapes


def budget() -> dict:
    """{'reste': 0..1, 'mesurable': bool, 'detail': str} — l'énergie réellement restante.

    ⚠️ On ne compte QUE les fournisseurs dont la clé existe. Additionner le quota d'une
    clé retirée gonfle le total et rend le pourcentage faux — c'est le défaut qu'il avait
    repéré sur la jauge de l'en-tête, et tout ce module s'appuie dessus.
    """
    try:
        from llm import usage as U
        from llm.client import cles_presentes, cles_secondaires  # noqa: F401
        presentes, secondes = cles_presentes(), cles_secondaires()
        tu = tl = 0
        for p in ("nvidia", "cerebras", "groq", "gemini"):
            if p not in presentes:
                continue
            used, limit = U.get_usage(p)
            if p in secondes:
                limit *= 2                 # deux comptes, deux quotas gratuits
            tu += int(used); tl += int(limit)
        if tl <= 0:
            # Aucun compteur : on ne PEUT pas mesurer. On ne suppose pas que tout va bien.
            return {"reste": 0.0, "mesurable": False,
                    "detail": "aucune clé dont je sache compter les jetons"}
        reste = max(0.0, min(1.0, 1 - tu / tl))
        # ⚠️ LE COMPTEUR EST-IL DURABLE ? Sans SUPABASE_DB_URL, il vit sur le disque
        # éphémère de Render et repart à zéro à chaque réveil. Il SOUS-ESTIME donc
        # toujours la consommation — c'est-à-dire qu'il dit « tu as de la marge » au
        # moment précis où c'est le plus faux. C'est le sens exact de sa condition :
        # « pour ça faut vraiment que la limite dans la fiole soit fiable ». Tant qu'elle
        # ne l'est pas, on ne s'autorise pas cinq appels sur sa foi.
        fiable = U.durable()
        return {"reste": reste, "mesurable": True, "fiable": fiable,
                "detail": (f"{int(reste * 100)} % sur {tl:,} jetons".replace(",", " ")
                           if fiable else
                           f"~{int(reste * 100)} % — mais je ne compte que depuis "
                           f"{U.compte_depuis():.0f} h (Render efface mon compteur)")}
    except Exception as e:
        logger.info(f"[étapes] budget illisible ({type(e).__name__})")
        return {"reste": 0.0, "mesurable": False, "detail": f"illisible ({type(e).__name__})"}


def combien(demande: int = 0, b: dict = None) -> dict:
    """Combien d'étapes on s'autorise, et POURQUOI. {'n', 'raison'}.

    `demande` : ce qu'il a réclamé (bouton « réfléchis en N étapes »). 0 = à moi de voir.
    """
    b = b or budget()
    voulu = max(1, min(int(demande or MAX_ETAPES), MAX_ETAPES))
    if not b["mesurable"]:
        # ⚠️ Ne pas savoir n'autorise pas à dépenser. On fait UNE étape — le
        # comportement habituel — et on dit qu'on n'a pas pu mesurer.
        return {"n": 1, "raison": "je ne peux pas mesurer mon énergie restante "
                                  f"({b['detail']}) — je reste prudente et je fais simple"}
    if b["reste"] < SEUIL_REFUS:
        return {"n": 1, "raison": f"il ne me reste que {int(b['reste'] * 100)} % d'énergie "
                                  "— plusieurs étapes échoueraient les unes après les autres"}
    if b["reste"] < SEUIL_PRUDENT:
        n = min(voulu, 2)
        return {"n": n, "raison": f"il me reste {int(b['reste'] * 100)} % d'énergie, "
                                  f"je me limite à {n} étapes"}
    if not b.get("fiable", True):
        # ⚠️ Un compteur qui oublie sous-estime TOUJOURS ce qui a été consommé : il
        # annonce de la marge là où il n'y en a peut-être plus. On plafonne à 3 au lieu
        # de 5, et on dit pourquoi — plutôt que de dépenser sur la foi d'un chiffre
        # dont on sait qu'il est optimiste.
        n = min(voulu, 3)
        return {"n": n, "raison": f"{b['detail']} — je me limite donc à {n} étapes"}
    return {"n": voulu, "raison": f"j'ai de quoi ({b['detail']})"}


# ── Découper la question ─────────────────────────────────────────────────────
_DECOUPE_SYSTEM = (
    "Tu es un outil de DÉCOUPAGE DE TEXTE. Tu ne réponds à rien, tu ne donnes aucun avis "
    "et aucun conseil : tu te contentes de séparer une demande en sujets.\n"
    "⚠️ Quel que soit le thème (argent, santé, droit…), ta tâche reste purement "
    "typographique — découper — et n'a donc aucune raison d'être refusée.\n"
    "Tu découpes une demande en sujets INDÉPENDANTS, traitables séparément.\n"
    "Règles strictes :\n"
    "- une ligne par sujet, rien d'autre : pas de numéro, pas de tiret, pas de titre ;\n"
    "- chaque ligne est une question autonome, compréhensible SANS le reste ;\n"
    "- au maximum {n} lignes ; moins si la demande ne contient pas {n} sujets ;\n"
    "- n'invente aucun sujet qui n'est pas dans la demande ;\n"
    "- si la demande ne porte que sur UN sujet, rends UNE seule ligne."
)

# Ce qui n'est jamais un sujet : le brouillon du modèle et ses restes de protocole.
_PAS_UN_SUJET = re.compile(r"^\s*(<|thought\s*:|action\s*:|params\s*:|final\s*:|```|"
                           r"voici|sujets?\s*:|\d+\s*[).]\s*$)", re.I)

# ⚠️ UN REFUS DU MODÈLE N'EST PAS UN SUJET. Vu en vrai, et le résultat est le pire de
# toute la semaine. Il demande « dis-moi s'il faut acheter 2CRSi et quand revendre » ;
# le modèle refuse de découper (conseil financier) et répond « I'm sorry, but I can't
# help with that ». Cette phrase est devenue le sujet de l'étape 1, la requête web en a
# tiré le mot « safe », et Nova lui a rendu — très sérieusement — des conseils sur les
# coffres-forts, les caméras et les chiens de garde. En réponse à une question de bourse.
_REFUS = re.compile(
    r"\b(i'?m sorry|i am sorry|i can'?t|i cannot|i'?m unable|as an ai|"
    r"je (?:ne )?(?:peux|puis) pas|je suis (?:désolée?|desolee?)|"
    r"désolée? (?:mais )?je|desolee? (?:mais )?je|"
    r"je ne (?:suis|fournis) pas (?:un |une )?(?:conseiller|conseil)|"
    r"unable to (?:help|assist)|can'?t (?:help|assist) with)\b", re.I)


def _sansaccent(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", str(t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


_VIDES = set("""
les des une aux pour dans sur par avec sans sous entre vers chez que qui quoi dont
est sont etre ete avoir avait cette cet ces son sa ses leur mon ma mes ton ta tes
plus moins tres bien tout tous toute meme aussi donc mais car quand comme alors
dis moi dire faut il elle nous vous combien temps prix quel quelle
""".split())


def _mots(t: str) -> set:
    return {m for m in re.split(r"[^a-z0-9]+", _sansaccent(t))
            if len(m) > 2 and m not in _VIDES}


def _parle_de_la_meme_chose(sujet: str, question: str) -> bool:
    """Ce texte partage-t-il un mot porteur avec la question ?

    ⚠️ CE TEST NE FILTRE PLUS LES SUJETS, ET C'EST DÉLIBÉRÉ. Je m'en servais pour écarter
    tout sujet ne reprenant aucun mot de la question. Ça a cassé le jour même : il demande
    5 étapes sur « faut-il acheter 2crsi et quand revendre », le modèle rend très
    correctement « Analyse du cours actuel / Perspectives à court terme / Niveaux de vente
    envisageables »… et je jette les trois, parce qu'aucune ne répète « 2crsi ». Il a eu
    une seule passe au lieu de cinq étapes.

    Le fond du problème : un bon découpage REFORMULE — c'est même ce qu'on lui demande.
    Une reformulation légitime et une invention hors sujet se ressemblent donc exactement
    du point de vue du vocabulaire. Aucun réglage de ce test ne peut les séparer, et un
    test qui ne sait pas trancher doit être retiré de la décision plutôt que réglé au
    jugé.

    Ce qui protège vraiment est ailleurs, et ça suffit :
      • _REFUS attrape les formules de refus, qui sont la panne réellement observée ;
      • _tire_de_la_demande (agent/core.py) empêche un refus de partir comme REQUÊTE WEB
        — c'est là qu'était le dégât, celui qui a rendu des conseils sur les
        coffres-forts en réponse à une question de bourse.
    Cette fonction reste utilisée par ce second garde-fou.
    """
    mq = _mots(question)
    if not mq:
        return True                 # rien à comparer : on ne bloque pas
    return bool(_mots(sujet) & mq)


def _nettoie_sujets(brut: str, n: int, question: str = "") -> list:
    out = []
    for ligne in str(brut or "").splitlines():
        t = ligne.strip()
        t = re.sub(r"^\s*(?:\d+[).\-]|[-*•])\s*", "", t).strip()
        if len(t) < 8 or _PAS_UN_SUJET.match(t) or _REFUS.search(t):
            continue
        if t not in out:
            out.append(t)
    return out[:n]


# Les mots qui ouvrent une DEMANDE distincte dans une phrase française. « et quand »,
# « et à quel prix », « dans combien de temps » : chacun ajoute une question à la
# précédente, sans ponctuation ni majuscule pour le signaler.
_INTERROGATIFS = (r"quand|combien|comment|pourquoi|o[ùu]\b|"
                  r"[àa] quel(?:le)?s?\b|de quel(?:le)?s?\b|quel(?:le)?s?\b|"
                  r"lequel|laquelle|lesquel(?:le)?s|jusqu'?[àa]")
# Une coupure : « et » / « puis » / une virgule, SUIVI d'un interrogatif.
_COUPURE = re.compile(r"\s*(?:,|\bet\b|\bpuis\b|\bensuite\b|\baussi\b|\bsinon\b)\s+"
                      r"(?=(?:" + _INTERROGATIFS + r"))", re.I)
# … et les coupures franches, qui restent valables.
_COUPURE_FRANCHE = re.compile(r"[.?!\n]+|\bet (?:apr[èe]s|aussi|ensuite|sinon)\b", re.I)


def _decoupe_sans_modele(question: str, n: int) -> list:
    """Repli SANS appel de modèle : on coupe là où une NOUVELLE demande commence.

    ⚠️ PREMIÈRE VERSION TROP MODESTE, ET ÇA S'EST VU. Elle ne coupait que sur « . ? ! »
    et sur « et après / et aussi / et ensuite ». Or sa question — « dis moi si il faut
    acheter 2crsi ET QUAND ET revendre À QUEL PRIX DANS COMBIEN DE TEMPS » — n'en contient
    aucun : le repli rendait donc la question entière en un seul morceau, et ses cinq
    étapes se réduisaient à une passe. Quatre demandes, aucune ponctuation.

    En français, une demande de plus s'ouvre par un mot interrogatif accroché à « et »,
    « puis » ou une virgule. C'est cela qu'on coupe — sans modèle, donc sans refus
    possible et sans coût.
    """
    q = str(question or "").strip()
    if not q:
        return []
    bouts = []
    for gros in _COUPURE_FRANCHE.split(q):
        for bout in _COUPURE.split(gros or ""):
            b = (bout or "").strip(" ,;:—-")
            if b:
                bouts.append(b)
    # ⚠️ Un morceau de deux mots (« quand ») ne veut rien dire tout seul : on le recolle
    # au SUIVANT plutôt que de lancer une étape sur un fragment vide de sens.
    fusionnes = []
    for b in bouts:
        if fusionnes and len(fusionnes[-1].split()) < 4:
            fusionnes[-1] = fusionnes[-1] + " " + b
        else:
            fusionnes.append(b)
    fusionnes = [b for b in fusionnes if len(b.split()) >= 3]
    return fusionnes[:n] if len(fusionnes) > 1 else [q]


# ── Un sujet ne doit jamais perdre DE QUOI on parle ──────────────────────────
# ⚠️ VU EN VRAI, ET C'EST LE MÊME DÉFAUT QUE LES COFFRES-FORTS, SOUS UNE AUTRE FORME.
# Sa question « dis moi si il faut acheter 2crsi et quand et revendre à quel prix dans
# combien de temps » a été correctement coupée en deux. Mais le second morceau — « quand
# et revendre à quel prix dans combien de temps » — ne contenait plus « 2crsi ». La
# recherche est donc partie sur « revendre prix optimal délai », et Nova lui a répondu,
# très sérieusement, qu'on revend une MAISON au bout de cinq ans et une VOITURE au bout
# de trois. En réponse à une question sur une action.
#
# Découper une phrase, c'est facile ; ne pas perdre son sujet en la découpant, c'est tout
# le travail. Chaque morceau doit pouvoir être lu SEUL — c'est d'ailleurs ce qu'on demande
# au modèle, et ce que le repli doit garantir lui aussi.

# Les mots trop courants pour identifier un sujet. On ne cherche pas l'exhaustivité :
# juste à ne pas prendre « acheter » ou « combien » pour le nom d'une entreprise.
_BANAL = set("""
acheter achete achat vendre revendre vends vente prix temps combien quand comment
pourquoi faut faire dire dis moi mon ma mes ton ta tes son sa ses leur nos vos
quel quelle quels quelles lequel laquelle dans pour avec sans sous entre vers chez
que qui quoi dont est sont etre ete avoir avait cette cet ces plus moins tres bien
tout tous toute toutes meme aussi donc mais car alors ainsi cela ceci celui celle
maintenant aujourd hui demain hier bientot encore deja jamais toujours peut peux
penses pense avis bonne bonnes bon idee sais savoir voudrais aimerais veux
""".split())


def entites(question: str) -> list:
    """Les mots qui disent DE QUOI on parle — ceux qu'un morceau ne doit pas perdre.

    On ne fait pas d'analyse grammaticale : on repère ce qui ne peut pas être un mot
    courant — un code boursier (2crsi, AL2SI), un sigle en majuscules (DBV, UPPA), un nom
    propre, ou simplement un mot assez rare pour identifier quelque chose.
    """
    out = []
    for mot in re.findall(r"[\wÀ-ÿ0-9]{3,}", str(question or "")):
        plat = _sansaccent(mot)
        if plat in _BANAL or plat in _VIDES:
            continue
        distinctif = (any(c.isdigit() for c in mot)            # 2crsi, AL2SI, CAC40
                      or (mot.isupper() and len(mot) <= 6)     # DBV, UPPA, LVMH
                      or mot[:1].isupper()                     # Valneva, Pau, Toulouse
                      or len(plat) >= 6)                       # un mot assez rare
        if distinctif and plat not in [_sansaccent(x) for x in out]:
            out.append(mot)
    return out


def rattache_le_sujet(sujets: list, question: str) -> list:
    """Rend chaque morceau lisible SEUL, en lui remettant le sujet s'il l'a perdu."""
    ents = entites(question)
    if not ents:
        return sujets
    out = []
    for s in sujets:
        plat = _sansaccent(s)
        if any(_sansaccent(e) in plat for e in ents):
            out.append(s)                       # le sujet y est déjà : on n'alourdit pas
        else:
            # On remet les deux mots les plus identifiants, pas toute la question :
            # une étape doit rester une question, pas un paragraphe.
            out.append(f"{s.rstrip(' ?.')} — {' '.join(ents[:2])}")
    return out


def decoupe(question: str, n: int, appel_modele) -> list:
    """Les sujets de la question, au plus `n`. `appel_modele(system, user) -> str`."""
    if n <= 1:
        return [str(question or "").strip()]
    # Le découpage SANS modèle est calculé d'abord : il ne coûte rien, il ne peut pas
    # être refusé, et il sert d'arbitre si le modèle rend une seule ligne.
    sans_modele = _decoupe_sans_modele(question, n)
    sujets = []
    try:
        brut = appel_modele(_DECOUPE_SYSTEM.format(n=n), str(question or "")[:2000])
        sujets = _nettoie_sujets(brut, n, question)
        if not sujets:
            logger.info("[étapes] découpage vide → repli sans modèle")
    except Exception as e:
        logger.info(f"[étapes] découpage impossible ({type(e).__name__}) → repli sans modèle")
    # ⚠️ UNE SEULE LIGNE RENDUE N'EST PAS UNE PREUVE QU'IL N'Y A QU'UN SUJET.
    # « dis moi si il faut acheter 2crsi et quand et revendre à quel prix dans combien de
    # temps » : quatre demandes, et le modèle a répondu une ligne — donc une passe, alors
    # qu'il avait explicitement demandé cinq étapes. Quand la simple lecture de sa phrase
    # y trouve plusieurs demandes et que le modèle n'en voit qu'une, on croit la phrase :
    # elle, au moins, ne peut ni se tromper de tâche ni refuser.
    if len(sujets) <= 1 and len(sans_modele) > 1:
        logger.info(f"[étapes] le modèle n'a vu qu'un sujet, sa phrase en contient "
                    f"{len(sans_modele)} → on suit la phrase")
        sujets = sans_modele
    else:
        sujets = sujets or sans_modele or [str(question or "").strip()]
    # ⚠️ DERNIER PASSAGE, ET IL S'APPLIQUE AUX DEUX CHEMINS. Le modèle aussi rend parfois
    # un morceau qui a perdu son sujet — le garde-fou ne peut pas ne couvrir que le repli,
    # sinon c'est la correction asymétrique habituelle.
    return rattache_le_sujet(sujets, question)


# ── Rassembler ───────────────────────────────────────────────────────────────
def resultats_utiles(resultats: list) -> list:
    """Les étapes qui ont VRAIMENT abouti. Une étape en échec n'est pas une réponse."""
    out = []
    for r in resultats or []:
        t = str((r or {}).get("texte") or "").strip()
        if not t or t.lstrip().startswith(("[ERREUR]", "❌", "⏱️", "🔌", "✂️", "🤔")):
            continue
        out.append(r)
    return out


def consigne_synthese(question: str, resultats: list) -> str:
    """Ce qu'on donne au modèle pour rassembler — uniquement du matériau RÉEL."""
    L = [f"Question de départ : {question}", "",
         "Voici ce que chaque étape a réellement trouvé. Rédige UNE réponse suivie qui "
         "couvre tous ces sujets, en français, en tutoyant.",
         "⚠️ N'ajoute AUCUN fait qui ne figure pas ci-dessous. Si une étape n'a rien "
         "trouvé, dis-le franchement plutôt que de combler.", ""]
    for i, r in enumerate(resultats, 1):
        L.append(f"--- ÉTAPE {i} : {r.get('sujet', '')} ---")
        L.append(str(r.get("texte", ""))[:2200])
        L.append("")
    return "\n".join(L)


def rapport(question: str, resultats: list, raison: str = "") -> str:
    """Le repli quand la synthèse ne peut pas être rédigée : les étapes, telles quelles.

    ⚠️ On rend le TRAVAIL RÉEL plutôt qu'une excuse. Chaque étape a coûté un appel ; les
    jeter parce que la dernière passe a échoué serait doublement dommage.
    """
    bons = resultats_utiles(resultats)
    rates = [r for r in (resultats or []) if r not in bons]
    if not bons:
        return ("🧩 Aucune de mes étapes n'a abouti"
                + (f" — {raison}" if raison else "")
                + ". Je préfère te le dire plutôt que d'inventer une réponse.")
    L = [f"🧩 **{len(bons)} étape(s) sur {len(resultats or [])}** ont abouti. "
         "Je n'ai pas pu les rassembler en une seule réponse, alors voici chacune telle "
         "quelle :", ""]
    for r in bons:
        L.append(f"### {r.get('sujet', 'Étape')}")
        L.append(str(r.get("texte", "")).strip())
        L.append("")
    if rates:
        L.append("⚠️ **Sans réponse :** " + ", ".join(
            f"« {r.get('sujet', '?')} »" for r in rates)
            + " — je ne te dis donc pas qu'il n'y a rien à en dire.")
    return "\n".join(L)


# ── Quand ça vaut le coup ────────────────────────────────────────────────────
# Une question à plusieurs sujets se reconnaît : elle est longue, elle enchaîne les
# « et », elle pose plusieurs interrogations.
def merite_des_etapes(question: str) -> bool:
    """Cette demande contient-elle assez de sujets pour justifier plusieurs appels ?

    ⚠️ On reste EXIGEANT. Découper « quelle heure il est » en cinq étapes brûlerait son
    quota pour rien. Le mode n'a de sens que sur les demandes qui en portent vraiment
    plusieurs — celle de sa rentrée à Pau en comptait sept.
    """
    q = str(question or "").strip()
    if len(q) < 180:
        return False
    liens = len(re.findall(r"\bet\b|\bpuis\b|\bensuite\b|\baussi\b|\bde plus\b|\bsinon\b",
                           q, re.I))
    questions = q.count("?")
    return (liens + questions) >= 4
