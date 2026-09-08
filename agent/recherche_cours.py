"""
Retrouver un passage dans SES cours — sans API, sans index, sans dépendance.

« qu'est-ce que le prof a dit sur la dignité humaine ? »

Il a six cours enregistrés aujourd'hui, il en aura cinquante d'ici juin. Une synthèse ne
sert que si on la retrouve : à trente cours, personne n'ouvre les fichiers un par un.

⚠️ POURQUOI PAS D'EMBEDDINGS. Ce serait la réponse à la mode, et ce serait une erreur
ici : il faudrait une API (donc une clé de plus, un quota de plus, une panne de plus),
un index à reconstruire à chaque cours, et le tout sur l'offre gratuite de Render.
Une recherche par mots sur trente documents de 20 000 signes, c'est quelques
millisecondes en Python pur. La dépendance coûterait plus cher que le problème.

⚠️ CE QUI COMPTE DAVANTAGE QUE LE CLASSEMENT : NE PAS SE TROMPER DE SILENCE.
« Je n'ai rien trouvé dans tes cours » et « je n'ai pas pu lire tes cours » se lisent
pareil et disent le contraire. Le premier veut dire « le prof ne l'a pas dit », et il en
tirerait une conclusion pour son partiel. Les deux cas sont donc distingués ici.
"""
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# En dessous, un mot n'identifie rien : « le », « de », « et ».
_MIN = 3
_VIDES = set("""
les des une aux pour dans sur par avec sans sous entre vers chez que qui quoi dont
est sont etre ete avoir avait cette cet ces son sa ses leur leurs mon ma mes ton ta
tes nos vos plus moins tres bien tout tous toute toutes meme aussi donc mais car
quand comme alors ainsi cela ceci celui celle ceux dit dire prof professeur cours
parle parler explique expliquer sujet propos concernant est-ce note notes
""".split())


def _sansaccent(t: str) -> str:
    t = unicodedata.normalize("NFD", str(t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def mots_utiles(question: str) -> list:
    """Les mots qui identifient vraiment quelque chose dans la question."""
    # ⚠️ On découpe AUSSI sur l'apostrophe : sinon « qu'est-ce » passe pour un mot-clé
    # de six lettres et fait remonter n'importe quelle phrase interrogative du cours.
    bruts = re.split(r"[^a-z0-9-]+", _sansaccent(question).replace("'", " ").replace("’", " "))
    out = []
    for m in bruts:
        m = m.strip("'’-")
        if len(m) >= _MIN and m not in _VIDES and m not in out:
            out.append(m)
    return out


_PHRASE = re.compile(r"[^.!?\n]{15,400}[.!?]?")


def passages(texte: str, mots: list, marge: int = 1) -> list:
    """[(score, extrait)] — les phrases du texte qui portent le plus de mots cherchés.

    On rend la phrase ET ses voisines : une définition tient rarement sur une ligne, et
    un extrait coupé au milieu oblige à rouvrir le cours — ce qu'on voulait éviter.
    """
    phrases = [p.strip() for p in _PHRASE.findall(texte or "") if p.strip()]
    if not phrases:
        return []
    plats = [_sansaccent(p) for p in phrases]
    out = []
    for i, plat in enumerate(plats):
        # Le score compte les mots DISTINCTS présents : une phrase qui répète dix fois
        # « droit » n'en sait pas plus qu'une qui le dit une fois.
        n = sum(1 for m in mots if m in plat)
        if not n:
            continue
        d, f = max(0, i - marge), min(len(phrases), i + marge + 1)
        out.append((n, " ".join(phrases[d:f])[:600]))
    out.sort(key=lambda x: -x[0])
    # Deux phrases voisines donnent deux extraits qui se recouvrent : on dédoublonne
    # sur le début du texte, sinon la même chose s'affiche trois fois.
    vus, propres = set(), []
    for n, e in out:
        cle = _sansaccent(e)[:70]
        if cle in vus:
            continue
        vus.add(cle)
        propres.append((n, e))
    return propres


def cherche(question: str, limite: int = 4) -> dict:
    """{'ok', 'resultats'|'erreur'} — les passages de ses cours qui répondent."""
    mots = mots_utiles(question)
    if not mots:
        return {"ok": False, "erreur": "je n'ai pas compris ce que tu cherches"}
    try:
        from agent import cours as C
        sessions = C.lister()
    except Exception as e:
        # ⚠️ « Je n'ai pas pu lire tes cours » n'est PAS « le prof ne l'a pas dit ».
        logger.info(f"[recherche cours] liste illisible ({type(e).__name__})")
        return {"ok": False, "erreur": f"je n'ai pas pu ouvrir tes cours ({type(e).__name__})"}
    if not sessions:
        return {"ok": True, "resultats": [], "cours_lus": 0}

    trouves, lus, rates = [], 0, 0
    for s in sessions:
        try:
            plein = C._lire(s["id"])
        except Exception:
            rates += 1
            continue
        lus += 1
        # La synthèse d'abord : elle est relue et structurée. La transcription ensuite,
        # parce qu'elle contient ce que la synthèse a laissé de côté.
        for source, texte in (("synthèse", plein.get("synthese") or ""),
                              ("transcription", plein.get("transcript") or "")):
            for score, extrait in passages(texte, mots)[:2]:
                trouves.append({"cours": s.get("titre", "(sans titre)"),
                                "id": s["id"], "ou": source,
                                "score": score, "extrait": extrait,
                                "date": s.get("debut")})
            if trouves and source == "synthèse":
                break          # inutile de fouiller la transcription si la synthèse répond
    trouves.sort(key=lambda x: (-x["score"], -(x.get("date") or 0)))
    return {"ok": True, "resultats": trouves[:limite], "cours_lus": lus,
            "cours_illisibles": rates, "mots": mots}


def repond(question: str) -> str:
    """La réponse en français, avec les extraits et leur cours d'origine."""
    r = cherche(question)
    if not r.get("ok"):
        return (f"🎓 {r.get('erreur', 'recherche impossible')} — je ne te dis donc PAS "
                "que ce n'est pas dans tes cours.")
    if not r["resultats"]:
        # ⚠️ On dit COMBIEN de cours ont été lus : « rien trouvé » sur zéro cours lu et
        # « rien trouvé » sur trente cours lus n'ont pas du tout la même valeur.
        n = r.get("cours_lus", 0)
        if not n:
            return "🎓 Tu n'as encore aucun cours enregistré, il n'y a rien à chercher."
        return (f"🎓 Je n'ai rien trouvé sur « {' '.join(r.get('mots') or [])} » dans tes "
                f"**{n} cours**. Soit le sujet n'y est pas, soit il y est dit avec "
                "d'autres mots — réessaie avec un terme du cours.")
    L = [f"🎓 **{len(r['resultats'])} passage(s)** dans tes cours :", ""]
    for x in r["resultats"]:
        L.append(f"**{x['cours']}** _(dans la {x['ou']})_")
        L.append(f"> {x['extrait']}")
        L.append("")
    if r.get("cours_illisibles"):
        # Un cours qu'on n'a pas pu ouvrir peut contenir la réponse : on le dit.
        L.append(f"⚠️ {r['cours_illisibles']} cours n'ont pas pu être ouverts — la "
                 "réponse s'y trouve peut-être.")
    return "\n".join(L)


# ── Reconnaître la demande ───────────────────────────────────────────────────
_DANS_MES_COURS = re.compile(
    r"(dans (?:mes|le|mon|ce) cours|de mes cours|mes notes|dans mes notes|"
    r"(?:qu'?est-ce que|qu'?a) (?:le |la |mon |ma )?(?:prof|professeur|prof\w*)\s+"
    r"(?:a )?(?:dit|expliqu|racont|prcis)\w*|"
    r"cherche.{0,20}(?:dans )?(?:mes|le) cours|retrouve.{0,20}cours)", re.I)


def veut_chercher(message: str) -> bool:
    return bool(_DANS_MES_COURS.search(str(message or "")))
