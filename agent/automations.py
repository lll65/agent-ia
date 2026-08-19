"""
Automatisations — les tâches que Nova exécute TOUTE SEULE, même quand tu dors.

Chaque automatisation = {id, titre, prompt, heure (0-23), jours, active, dernier_run, dernier_resultat}.
Le planificateur tourne en tâche de fond : à l'heure dite, il exécute la demande via l'agent
(mêmes outils que le chat : agenda, mails, web…) et stocke le résultat, qui devient consultable
dans l'UI et poussé sur Telegram si configuré.

Persistance : Supabase si SUPABASE_DB_URL, sinon fichier local (data/automations.json).
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)
_FILE = Path("data/automations.json")
_LOCK = threading.Lock()

# Modèles proposés dans l'UI (l'utilisateur peut aussi écrire les siens)
TEMPLATES = [
    {"titre": "Briefing du matin", "icon": "🌅", "hour": 7,
     "prompt": "Fais-moi mon briefing du matin"},
    {"titre": "Résumé de mes mails", "icon": "📧", "hour": 18,
     "prompt": "Résume mes mails non lus d'aujourd'hui en 5 points maximum"},
    {"titre": "Préparation du lendemain", "icon": "🌙", "hour": 21,
     "prompt": "Regarde mon agenda de demain et dis-moi ce que je dois préparer ce soir"},
    {"titre": "Veille tech", "icon": "🔍", "hour": 12,
     "prompt": "Résume les 3 actualités tech les plus importantes du jour, avec leurs sources"},
    {"titre": "Bilan de la semaine", "icon": "📊", "hour": 19, "days": [6],
     "prompt": "Fais le bilan de ma semaine écoulée d'après mon agenda, et propose 3 priorités pour la semaine prochaine"},
]


# ── Persistance ───────────────────────────────────────────────────────────────
def _sb():
    if not getattr(config, "SUPABASE_DB_URL", ""):
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS automations "
                      "(id text PRIMARY KEY, data jsonb)")
        return conn
    except Exception:
        return None


def _load() -> list:
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                c.execute("SELECT data FROM automations")
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
                c.execute("DELETE FROM automations")
                for it in items:
                    c.execute("INSERT INTO automations (id, data) VALUES (%s, %s)",
                              (it["id"], json.dumps(it, ensure_ascii=False)))
            conn.close()
        except Exception:
            pass


# ── API interne ───────────────────────────────────────────────────────────────
def list_all() -> list:
    with _LOCK:
        return _load()


def add(titre: str, prompt: str, hour: int = 8, days=None, icon: str = "⚡") -> dict:
    item = {"id": uuid.uuid4().hex[:10], "titre": titre.strip()[:80],
            "prompt": prompt.strip()[:400], "hour": max(0, min(23, int(hour))),
            "days": days or [0, 1, 2, 3, 4, 5, 6], "icon": icon, "active": True,
            "last_run": None, "last_result": "", "runs": 0}
    with _LOCK:
        items = _load()
        items.append(item)
        _save(items)
    return item


def update(aid: str, **changes) -> bool:
    with _LOCK:
        items = _load()
        for it in items:
            if it["id"] == aid:
                it.update({k: v for k, v in changes.items() if v is not None})
                _save(items)
                return True
    return False


def delete(aid: str) -> bool:
    with _LOCK:
        items = _load()
        new = [i for i in items if i["id"] != aid]
        if len(new) == len(items):
            return False
        _save(new)
        return True


# ── Exécution ─────────────────────────────────────────────────────────────────
async def run_one(item: dict) -> str:
    """Exécute une automatisation via l'agent complet (mêmes capacités que le chat)."""
    from api.agent import _ask_agent
    try:
        answer = await _ask_agent(item["prompt"])
    except Exception as e:
        answer = f"❌ Échec : {type(e).__name__}: {str(e)[:200]}"
    with _LOCK:
        items = _load()
        for it in items:
            if it["id"] == item["id"]:
                it["last_run"] = time.time()
                it["last_result"] = (answer or "")[:4000]
                it["runs"] = int(it.get("runs", 0)) + 1
        _save(items)
    try:
        from agent.squad import record
        record("nova", "", f"⚡ {item['titre']}")
    except Exception:
        pass
    # Notification Telegram si configurée
    if config.TELEGRAM_TOKEN and getattr(config, "TELEGRAM_CHAT_ID", ""):
        try:
            import requests
            from agent.core import _off
            await _off(requests.post,
                       f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
                       json={"chat_id": config.TELEGRAM_CHAT_ID,
                             "text": f"⚡ {item['titre']}\n\n{answer[:3500]}"}, timeout=20)
        except Exception:
            pass
    logger.info(f"[automations] '{item['titre']}' exécutée.")
    return answer


async def scheduler_loop():
    """Boucle de fond : vérifie chaque minute s'il y a une automatisation à lancer."""
    logger.info("Automatisations : planificateur démarré.")
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now()
            for it in list_all():
                if not it.get("active", True):
                    continue
                if now.hour != int(it.get("hour", 8)) or now.weekday() not in it.get("days", list(range(7))):
                    continue
                last = it.get("last_run") or 0
                if time.time() - last < 3600:      # déjà lancée cette heure-ci
                    continue
                await run_one(it)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[automations] boucle : {e}")
            await asyncio.sleep(120)
