"""
« Déplace tel cours dans Notion » — reconnaître QUEL cours, et le dire quand on doute.

⚠️ LE RISQUE PROPRE À CETTE DEMANDE. Toutes les autres corrections de ce projet portent
sur des réponses fausses ; ici, une erreur ENVOIE quelque chose quelque part. Se tromper
de cours, c'est publier le mauvais document. On préfère donc demander lequel plutôt que
de parier — et surtout, on ne prétend jamais avoir envoyé sans preuve.

⚠️ ET ON NE DEVINE PAS À MOITIÉ. S'il dit « déplace mon cours de droit dans Notion » et
qu'il a trois cours de droit, on les LISTE et on lui demande. Choisir « le plus récent »
serait un pari silencieux : il ne verrait l'erreur qu'une fois la page créée.
"""
import re
import unicodedata

_VERBES = r"(?:d[ée]place|envoie|exporte|met[s]?|copie|ajoute|balance|pousse|transf[èe]re)"
_DEMANDE = re.compile(
    _VERBES + r"[^.\n]{0,80}?\bcours\b[^.\n]{0,80}?\b(?:dans|sur|vers|[àa])\s+notion", re.I)
# … et l'ordre inverse, tout aussi naturel : « dans Notion, mets mon cours de droit ».
_DEMANDE_INV = re.compile(
    r"\bnotion\b[^.\n]{0,60}?" + _VERBES + r"[^.\n]{0,60}?\bcours\b", re.I)


def _cle(t: str) -> str:
    t = unicodedata.normalize("NFD", str(t or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def veut_exporter(message: str) -> bool:
    m = str(message or "")
    return bool(_DEMANDE.search(m) or _DEMANDE_INV.search(m))


# Ce qui n'aide pas à reconnaître un cours : les mots de la demande elle-même.
_VIDES = set("deplace envoie exporte met mets copie ajoute balance pousse transfere "
             "cours mon ma mes le la les de du des dans sur vers a notion stp s il te "
             "plait dernier derniere ce cet cette".split())


def indice(message: str) -> str:
    """Ce qui, dans la phrase, désigne UN cours en particulier. "" s'il n'y a rien."""
    mots = [m for m in _cle(message).split() if m not in _VIDES and len(m) > 2]
    return " ".join(mots)


def choisit(message: str, sessions) -> dict:
    """{'id':…} si un seul cours correspond, {'ambigu': [...]} sinon, {} si aucun."""
    liste = list(sessions or [])
    if not liste:
        return {}
    ind = indice(message)
    if not ind:
        # Aucun indice : « déplace mon dernier cours » est la seule lecture raisonnable.
        if re.search(r"\bderni[èe]re?\b|\bce cours\b|\bcelui[- ]l[àa]\b", message or "", re.I):
            return {"id": liste[0]["id"]}
        return {"ambigu": liste[:8]}
    mots = ind.split()
    trouves = []
    for s in liste:
        foin = _cle(f"{s.get('titre','')} {s.get('matiere','')} {s.get('dossier','')}")
        if any(m in foin for m in mots):
            trouves.append(s)
    if len(trouves) == 1:
        return {"id": trouves[0]["id"]}
    if trouves:
        # ⚠️ Plusieurs candidats : on demande. Prendre « le plus récent » serait un pari
        # silencieux, et il ne verrait l'erreur qu'une fois la page publiée.
        return {"ambigu": trouves[:8]}
    return {"ambigu": liste[:8]}


def cours_a_exporter(message: str):
    """Les paramètres de la demande, ou None si ce n'en est pas une."""
    if not veut_exporter(message):
        return None
    return {"message": message}


def _liste_lisible(sessions) -> str:
    lignes = []
    for s in sessions:
        d = s.get("dossier") or s.get("matiere") or ""
        lignes.append(f"- **{s.get('titre','(sans titre)')}**" + (f" — _{d}_" if d else ""))
    return "\n".join(lignes)


def execute(args: dict, lister_actions, appeler) -> str:
    """Choisit le cours, l'envoie, et rend un message honnête."""
    from agent import cours as C
    from agent.vers_notion import envoie
    message = (args or {}).get("message", "")
    try:
        sessions = C.lister()
    except Exception as e:
        return f"📝 Je n'ai pas pu lire tes cours ({type(e).__name__}) — je n'ai rien envoyé."
    if not sessions:
        return "📝 Tu n'as aucun cours enregistré pour l'instant, il n'y a rien à envoyer."

    choix = choisit(message, sessions)
    if "ambigu" in choix:
        return ("📝 **Lequel ?** Je ne veux pas envoyer le mauvais cours dans Notion.\n\n"
                + _liste_lisible(choix["ambigu"])
                + "\n\nRedis-moi avec son titre, par exemple « envoie le cours de droit "
                  "dans Notion ».")
    if "id" not in choix:
        return "📝 Je n'ai pas trouvé de quel cours tu parles — je n'ai rien envoyé."

    sid = choix["id"]
    try:
        md = C.markdown(sid)
        s = C._lire(sid)
    except Exception as e:
        return f"📝 Ce cours est introuvable ({type(e).__name__}) — rien n'a été envoyé."
    return envoie(s.get("titre", "Cours"), md, lister_actions, appeler)
