"""
Ce que Nova a appris de TES documents — pour ne pas les rechercher deux fois.

Nova sait extraire un fait quand tu le déclares (« j'ai 17 ans »). Mais elle n'apprenait
rien de ce qu'elle DÉCOUVRE : à chaque fois que tu demandais ton PEA, elle relançait une
recherche dans Google Sheets, comparait les noms, et pouvait retomber sur le mauvais
fichier. Elle refaisait le même travail, avec le même risque, indéfiniment.

Ici on garde le lien « ce que tu dis » → « le document que c'était vraiment » :

    ("googlesheets", "pea") → ("11qGW0rhYzR…", "Suivi_PEA_Lohan_Pere")

La fois suivante, elle y va directement. Et si l'identifiant ne répond plus (document
renommé, supprimé, ou droits retirés), on l'oublie et on repart chercher : un raccourci
faux serait pire que pas de raccourci.

Persistance : Supabase si SUPABASE_DB_URL, sinon fichier local — comme le profil.
⚠️ Sans Supabase, le fichier local vit sur le disque du conteneur, remis à zéro par
Render à chaque redéploiement (voir /agent/diag/memoire).
"""
import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path

from agent.entrepot import Entrepot
from config import config

logger = logging.getLogger(__name__)
_FILE = Path("data/documents.json")
_LOCK = threading.Lock()
MAX_RACCOURCIS = 200        # largement de quoi couvrir les documents d'une vraie vie
_PERIME_S = 90 * 24 * 3600  # au-delà de 3 mois sans usage, on préfère revérifier


def _norme(t: str) -> str:
    """Compare sans accent, sans ponctuation, sans casse : « PEA » == « pea » == « p.e.a »."""
    t = unicodedata.normalize("NFD", (t or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    # Les sigles s'écrivent avec ou sans points : « P.E.A » doit valoir « PEA ».
    # Une suite d'au moins deux lettres isolées est un sigle, jamais des mots.
    return re.sub(r"\b(?:[a-z] ){1,}[a-z]\b", lambda m: m.group(0).replace(" ", ""), t)


_ENTREPOT = Entrepot("documents_connus", "data/documents.json", cle="cle")


def _charge() -> tuple[list, bool]:
    """(éléments, lecture fiable ?) — voir agent/entrepot.py.

    ⚠️ L'ancien couple _load/_save reconstruisait la table entière (DELETE puis
    INSERT) a partir d'une lecture qui avait le droit d'echouer en silence : une
    coupure Supabase de quelques secondes effacait tout, definitivement.
    """
    return _ENTREPOT.charge()


def _load() -> list:
    return _charge()[0]


def _cle(app: str, quoi: str) -> str:
    return f"{(app or '').lower()}|{_norme(quoi)}"


def retenir(app: str, quoi: str, ident: str, nom: str) -> dict:
    """Mémorise « quand il dit ÇA, c'est CE document-là »."""
    quoi, ident = (quoi or "").strip(), (ident or "").strip()
    if not (app and quoi and ident) or len(_norme(quoi)) < 2:
        return {}
    item = {"cle": _cle(app, quoi), "app": app, "quoi": quoi.strip()[:80],
            "id": ident[:200], "nom": (nom or "")[:160], "ts": time.time()}
    with _LOCK:
        _ENTREPOT.ecrit_un(item)
        _elague()
    logger.info(f"[documents] retenu : {app} « {quoi} » → {nom or ident[:12]}")
    return item


def retrouver(app: str, quoi: str) -> tuple:
    """(identifiant, nom) déjà connu pour cette demande, ou ("", "").

    On accepte aussi une correspondance PARTIELLE : « mon suivi pea » retrouve ce qui a
    été retenu sous « pea ». Sinon le moindre mot en plus rendrait le raccourci inutile.
    """
    q = _norme(quoi)
    if len(q) < 2:
        return ("", "")
    app = (app or "").lower()
    with _LOCK:
        items = _load()
    frais = [x for x in items if time.time() - float(x.get("ts") or 0) < _PERIME_S]
    candidats = [x for x in frais if (x.get("app") or "").lower() == app]
    exact = next((x for x in candidats if x.get("cle") == _cle(app, quoi)), None)
    if exact:
        return (exact.get("id", ""), exact.get("nom", ""))
    # Correspondance partielle : le plus SPÉCIFIQUE d'abord (le libellé le plus long),
    # pour que « pea pere » ne soit pas éclipsé par « pea ».
    for x in sorted(candidats, key=lambda x: -len(_norme(x.get("quoi", "")))):
        n = _norme(x.get("quoi", ""))
        if n and (n in q or q in n):
            return (x.get("id", ""), x.get("nom", ""))
    return ("", "")


def oublier(app: str = "", ident: str = "") -> int:
    """Oublie un raccourci devenu faux (document renommé, supprimé, droits retirés)."""
    with _LOCK:
        items = _load()
        garde = [x for x in items
                 if not ((not app or (x.get("app") or "").lower() == (app or "").lower())
                         and (not ident or x.get("id") == ident))]
        gardees = {x.get("cle") for x in garde}
        _ENTREPOT.supprime([x.get("cle") for x in items if x.get("cle") not in gardees])
    n = len(items) - len(garde)
    if n:
        logger.info(f"[documents] {n} raccourci(s) oublié(s) ({app} {ident[:12]})")
    return n


def lister() -> list:
    """Tous les raccourcis connus, du plus récemment utilisé au plus ancien."""
    return sorted(_load(), key=lambda x: -float(x.get("ts") or 0))


def effacer_tout() -> None:
    with _LOCK:
        _ENTREPOT.vide()


def _elague() -> None:
    """Au-delà du plafond, on jette les raccourcis les PLUS ANCIENS — nommément."""
    items, fiable = _charge()
    if not fiable or len(items) <= MAX_RACCOURCIS:
        return
    items.sort(key=lambda x: float(x.get("ts") or 0))
    _ENTREPOT.supprime([x.get("cle") for x in items[:-MAX_RACCOURCIS]])
