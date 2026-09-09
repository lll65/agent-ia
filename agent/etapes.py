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


def _decoupe_sans_modele(question: str, n: int) -> list:
    """Repli SANS appel de modèle : on coupe sur la ponctuation forte et les « et ».

    ⚠️ Volontairement modeste. Le but n'est pas de bien découper — c'est de ne pas
    RENDRE LA MAIN VIDE quand aucun modèle ne répond. Une étape unique vaut mieux qu'une
    erreur, et un découpage grossier vaut mieux qu'un survol.
    """
    q = str(question or "").strip()
    if not q:
        return []
    bouts = re.split(r"[.?!\n]+|\bet (?:apr[èe]s|aussi|ensuite|sinon)\b", q)
    bouts = [b.strip(" ,;:") for b in bouts]
    bouts = [b for b in bouts if len(b) >= 20]
    return bouts[:n] if len(bouts) > 1 else [q]


def decoupe(question: str, n: int, appel_modele) -> list:
    """Les sujets de la question, au plus `n`. `appel_modele(system, user) -> str`."""
    if n <= 1:
        return [str(question or "").strip()]
    try:
        brut = appel_modele(_DECOUPE_SYSTEM.format(n=n), str(question or "")[:2000])
        sujets = _nettoie_sujets(brut, n, question)
        if sujets:
            return sujets
        logger.info("[étapes] découpage vide → repli sans modèle")
    except Exception as e:
        logger.info(f"[étapes] découpage impossible ({type(e).__name__}) → repli sans modèle")
    return _decoupe_sans_modele(question, n) or [str(question or "").strip()]


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
