"""
Ce que Nova a appris à faire — la FORME d'appel qui marche, action par action.

Quand un appel d'API échoue sur un problème de forme (« design_type doit être un objet,
pas une chaîne », « ranges attend une liste »), Nova corrige, réessaie, et finit par
réussir. Puis elle jette tout : à la demande suivante, même erreur, même aller-retour,
même attente. Elle galérait à chaque fois de la même façon.

Ici on garde le SQUELETTE de l'appel qui a réussi :

    ("canva", "CANVA_CREATE_DESIGN") → {"design_type": {"type": "…", "name": "…"},
                                         "title": "<texte>"}

Il est réinjecté comme exemple la fois d'après : le modèle voit la forme attendue au
lieu de la deviner.

⚠️ On ne garde QUE la forme, jamais le contenu. Le corps d'un mail, le nom d'un
destinataire, le texte d'une note n'ont rien à faire dans un fichier de compétences :
ils sont remplacés par « <texte> ». Une recette, pas une archive.

Persistance : Supabase si SUPABASE_DB_URL, sinon fichier local — comme le profil.
"""
import json
import logging
import re
import threading
import time

from pathlib import Path

from config import config

logger = logging.getLogger(__name__)
_FILE = Path("data/competences.json")
_LOCK = threading.Lock()
MAX_COMPETENCES = 150

# Valeurs qui décrivent un FORMAT, pas un contenu : un seul mot technique, sans espace
# (« preset », « primary », « A1:D50 », « UTC »). Elles aident le modèle et ne révèlent
# rien de personnel.
# ⚠️ Les espaces sont interdits À DESSEIN : sans cette règle, « Mon affiche » — donc un
# titre, et demain « RDV médecin Dr Dupont » — se retrouvait enregistré dans le fichier
# de compétences. Une recette décrit la forme ; le contenu ne doit jamais y entrer.
_FORMAT_OK = re.compile(r"^[A-Za-z0-9_:!$.\-+/]{1,24}$")
# …et même sans espace, ce qui ressemble à une donnée personnelle est écarté.
_PERSONNEL = re.compile(r"@|https?://|\+?\d{6,}")


def _valeur_squelette(v):
    """Garde la forme, jamais le contenu."""
    if isinstance(v, dict):
        return {k: _valeur_squelette(x) for k, x in list(v.items())[:12]}
    if isinstance(v, list):
        return [_valeur_squelette(v[0])] if v else []
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return 0
    s = str(v)
    if _FORMAT_OK.match(s) and not _PERSONNEL.search(s):
        return s
    return "<texte>"


def squelette(args: dict) -> dict:
    """La FORME d'un appel : quels champs, et de quel type — sans les données."""
    if not isinstance(args, dict):
        return {}
    return {k: _valeur_squelette(v) for k, v in list(args.items())[:20]}


def _sb():
    if not getattr(config, "SUPABASE_DB_URL", ""):
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS competences "
                      "(cle text PRIMARY KEY, data jsonb)")
        return conn
    except Exception:
        return None


def _load() -> list:
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                c.execute("SELECT data FROM competences")
                rows = c.fetchall()
            conn.close()
            return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]
        except Exception:
            pass
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save(items: list) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                c.execute("DELETE FROM competences")
                for it in items:
                    c.execute("INSERT INTO competences (cle, data) VALUES (%s, %s)",
                              (it["cle"], json.dumps(it, ensure_ascii=False)))
            conn.close()
        except Exception:
            pass


def apprendre(app: str, action: str, args: dict, corrections: int = 0,
              erreur: str = "") -> dict:
    """Retient la forme d'appel qui a RÉUSSI.

    `corrections` = combien d'allers-retours il a fallu. Une recette apprise après une
    galère vaut plus qu'une qui a marché du premier coup : c'est celle-là qui évitera
    de refaire l'erreur.
    """
    forme = squelette(args)
    if not (action and forme):
        return {}
    cle = f"{(app or '').lower()}|{action.upper()}"
    with _LOCK:
        items = _load()
        ancien = next((x for x in items if x.get("cle") == cle), None)
        item = {"cle": cle, "app": app, "action": action.upper(), "forme": forme,
                # On garde le PIRE cas rencontré, jamais le dernier : une recette qui a
                # coûté deux corrections reste une recette durement acquise, même si les
                # fois suivantes passent du premier coup (justement grâce à elle).
                "corrections": max(int(corrections), int((ancien or {}).get("corrections", 0))),
                "erreur_evitee": (ancien or {}).get("erreur_evitee", "") or (erreur or "")[:200],
                "usages": int((ancien or {}).get("usages", 0)) + 1, "ts": time.time()}
        items = [x for x in items if x.get("cle") != cle] + [item]
        _save(items[-MAX_COMPETENCES:])
    if corrections:
        logger.info(f"[competences] apprise après {corrections} correction(s) : {action}")
    return item


def recette(app: str, action: str) -> dict:
    """La forme d'appel connue pour cette action, ou {}."""
    cle = f"{(app or '').lower()}|{(action or '').upper()}"
    with _LOCK:
        return next((x for x in _load() if x.get("cle") == cle), {})


def indice(app: str, action: str) -> str:
    """Ce qu'on souffle au modèle pour qu'il n'ait plus à deviner la forme."""
    r = recette(app, action)
    if not r.get("forme"):
        return ""
    txt = ("\n\n✅ CETTE FORME A DÉJÀ FONCTIONNÉ pour cette action — reprends-la à "
           "l'identique en remplaçant seulement les valeurs :\n"
           + json.dumps(r["forme"], ensure_ascii=False))
    if r.get("erreur_evitee"):
        txt += f"\n(la première fois, l'erreur était : {r['erreur_evitee'][:160]})"
    return txt


def oublier(action: str = "") -> int:
    """Oublie une recette devenue fausse (l'API a changé de format)."""
    with _LOCK:
        items = _load()
        garde = [x for x in items
                 if not (not action or x.get("action") == (action or "").upper())]
        if len(garde) != len(items):
            _save(garde)
    n = len(items) - len(garde)
    if n:
        logger.info(f"[competences] {n} recette(s) oubliée(s) ({action or 'toutes'})")
    return n


def lister() -> list:
    """Les compétences, les plus durement acquises d'abord."""
    return sorted(_load(), key=lambda x: (-int(x.get("corrections", 0)),
                                          -float(x.get("ts") or 0)))


def effacer_tout() -> None:
    with _LOCK:
        _save([])
