"""
Profil utilisateur — ce que Nova retient VRAIMENT de toi.

Différence avec l'historique : ici on ne stocke pas des bouts de messages, mais des FAITS
structurés (prénom, âge, ville, goûts, objectifs…), dédoublonnés et modifiables.
Persistance : Supabase si SUPABASE_DB_URL, sinon fichier local.
"""
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)
_FILE = Path("data/profile.json")
_LOCK = threading.Lock()

CATEGORIES = {
    "identite": "🪪 Identité",
    "lieu": "📍 Lieu",
    "gouts": "❤️ Goûts",
    "travail": "💼 Travail & études",
    "objectifs": "🎯 Objectifs",
    "preferences": "⚙️ Préférences",
    "autre": "💡 Autre",
}


def _sb():
    if not getattr(config, "SUPABASE_DB_URL", ""):
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS profile_facts "
                      "(id text PRIMARY KEY, data jsonb)")
        return conn
    except Exception:
        return None


def _load() -> list:
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                c.execute("SELECT data FROM profile_facts")
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
                c.execute("DELETE FROM profile_facts")
                for it in items:
                    c.execute("INSERT INTO profile_facts (id, data) VALUES (%s, %s)",
                              (it["id"], json.dumps(it, ensure_ascii=False)))
            conn.close()
        except Exception:
            pass


def list_facts() -> list:
    with _LOCK:
        items = _load()
    order = list(CATEGORIES.keys())
    items.sort(key=lambda f: (order.index(f.get("cat", "autre")) if f.get("cat") in order else 99))
    return items


def add_fact(cat: str, texte: str) -> dict:
    """Ajoute un fait en évitant les doublons (même catégorie + début identique)."""
    texte = (texte or "").strip()[:160]
    if not texte:
        return {}
    cat = cat if cat in CATEGORIES else "autre"
    with _LOCK:
        items = _load()
        key = texte.lower()[:28]
        items = [f for f in items if not (f.get("cat") == cat and f.get("texte", "").lower()[:28] == key)]
        item = {"id": uuid.uuid4().hex[:10], "cat": cat, "texte": texte, "ts": time.time()}
        items.append(item)
        _save(items[-60:])
    return item


def delete_fact(fid: str) -> bool:
    with _LOCK:
        items = _load()
        new = [f for f in items if f.get("id") != fid]
        if len(new) == len(items):
            return False
        _save(new)
        return True


def clear_all() -> None:
    with _LOCK:
        _save([])


def learn_from(message: str) -> list:
    """Extrait les faits durables d'un message (appelé quand l'utilisateur parle de lui)."""
    from api.agent import _llm_json
    data = _llm_json(
        "Extrais UNIQUEMENT les informations durables sur l'utilisateur (à retenir sur le long terme). "
        'Réponds en JSON STRICT : {"faits":[{"cat":"identite|lieu|gouts|travail|objectifs|preferences|autre",'
        '"texte":"phrase courte à la 3e personne"}]}\n'
        "Exemples : « A 17 ans », « Habite à Lyon », « Aime le football », « Préfère les réponses courtes ».\n"
        "Ignore les demandes ponctuelles (« ouvre Canva », « quel temps fait-il »). "
        'Si rien de durable : {"faits":[]}.',
        f"Message : {message}")
    out = []
    for f in (data.get("faits") or [])[:4]:
        t = (f.get("texte") or "").strip()
        if t:
            out.append(add_fact(f.get("cat", "autre"), t))
    if out:
        logger.info(f"[profil] {len(out)} fait(s) mémorisé(s).")
    return out


def context_block() -> str:
    """Bloc à injecter dans les prompts pour personnaliser les réponses."""
    items = list_facts()
    if not items:
        return ""
    by = {}
    for f in items:
        by.setdefault(f.get("cat", "autre"), []).append(f.get("texte", ""))
    lines = [f"{CATEGORIES.get(c, c)} : " + " ; ".join(v[:6]) for c, v in by.items()]
    return "[Ce que je sais de l'utilisateur]\n" + "\n".join(lines)
