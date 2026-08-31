"""
Où passent les secondes ?

⚠️ « Ça met 40 s à me donner mes dispos » ne se corrige pas en optimisant au
hasard. Compter les allers-retours ne suffit pas non plus : sur une demande
d'agenda il n'y en a que deux (un appel Composio, un appel modèle). Le temps est
donc dans leur DURÉE, ou avant eux — un démarrage à froid de Render coûte à lui
seul 30 à 60 s, et ressemble exactement au même symptôme.

Ce module chronomètre chaque étape d'une demande et garde les dernières. On peut
alors répondre « c'est le modèle », « c'est Composio » ou « c'est le réveil de
l'instance » avec des chiffres, au lieu de trois hypothèses.
"""
import threading
import time
from collections import deque

_LOCK = threading.Lock()
_HISTORIQUE = deque(maxlen=20)      # dernières demandes chronométrées
_COURANT = {}                       # étapes de la demande en cours


def demarre(question: str = "") -> None:
    with _LOCK:
        _COURANT.clear()
        _COURANT["_question"] = (question or "")[:80]
        _COURANT["_t0"] = time.monotonic()


def ajoute(etape: str, secondes: float) -> None:
    """Cumule le temps passé dans une étape (plusieurs appels s'additionnent)."""
    with _LOCK:
        if "_t0" not in _COURANT:
            return
        d = _COURANT.setdefault(etape, {"s": 0.0, "n": 0})
        d["s"] += max(0.0, secondes)
        d["n"] += 1


def termine() -> dict:
    with _LOCK:
        if "_t0" not in _COURANT:
            return {}
        total = time.monotonic() - _COURANT.pop("_t0")
        entree = {"question": _COURANT.pop("_question", ""),
                  "total_s": round(total, 1),
                  "etapes": {k: {"s": round(v["s"], 1), "n": v["n"]}
                             for k, v in _COURANT.items() if isinstance(v, dict)},
                  "quand": time.time()}
        # Ce qui n'est mesuré nulle part : démarrage à froid, mémoire, sérialisation…
        mesure = sum(v["s"] for v in entree["etapes"].values())
        entree["non_mesure_s"] = round(max(0.0, total - mesure), 1)
        _COURANT.clear()
        _HISTORIQUE.append(entree)
        return entree


class mesure:
    """`with mesure("composio"):` — chronomètre un bloc, même s'il lève."""

    def __init__(self, etape: str):
        self.etape = etape

    def __enter__(self):
        self.t = time.monotonic()
        return self

    def __exit__(self, *a):
        ajoute(self.etape, time.monotonic() - self.t)
        return False


_LIBELLES = {
    "composio": "les applis connectées (Google, Notion…)",
    "modele": "les modèles de langage",
    "recherche": "la recherche web",
    "memoire": "la mémoire",
}


def etat() -> dict:
    """Ce qui prend du temps, chiffres à l'appui — et le coupable désigné."""
    with _LOCK:
        hist = list(_HISTORIQUE)
    if not hist:
        return {"resume": "Aucune demande mesurée depuis le démarrage.", "demandes": []}
    recentes = hist[-10:]
    moyenne = sum(h["total_s"] for h in recentes) / len(recentes)
    cumul = {}
    for h in recentes:
        for k, v in h["etapes"].items():
            cumul[k] = cumul.get(k, 0.0) + v["s"]
        cumul["non mesuré"] = cumul.get("non mesuré", 0.0) + h["non_mesure_s"]
    pire = max(cumul.items(), key=lambda kv: kv[1]) if cumul else ("", 0)

    d = {"demandes_mesurees": len(hist),
         "duree_moyenne_s": round(moyenne, 1),
         "temps_par_etape_s": {k: round(v / len(recentes), 1) for k, v in cumul.items()},
         "dernieres": [{"question": h["question"], "total_s": h["total_s"],
                        "etapes": {k: v["s"] for k, v in h["etapes"].items()}}
                       for h in reversed(recentes[-5:])]}
    if moyenne < 6:
        d["resume"] = f"✅ Nova répond en {round(moyenne, 1)} s en moyenne."
    else:
        quoi = _LIBELLES.get(pire[0], pire[0])
        part = round(100 * pire[1] / max(1e-9, sum(cumul.values())))
        d["resume"] = (f"⚠️ {round(moyenne, 1)} s en moyenne, dont {part} % dans "
                       f"{quoi}.")
        if pire[0] == "non mesuré":
            d["solution"] = ("Ce temps-là n'est pas dans le code : c'est presque "
                             "toujours le réveil de Render (30 à 60 s à froid). "
                             "Vérifie le bloc « Réveil de Render » juste au-dessus.")
        elif pire[0] == "modele":
            d["solution"] = ("Un fournisseur lent en tête de chaîne. Lance le "
                             "diagnostic des modèles et mets en premier celui qui "
                             "répond le plus vite.")
        elif pire[0] == "composio":
            d["solution"] = ("Les allers-retours vers tes applis dominent. C'est le "
                             "réseau, pas Nova — mais le catalogue est mis en cache "
                             "au démarrage pour ne le payer qu'une fois.")
    return d
