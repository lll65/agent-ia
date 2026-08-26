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

from agent.horloge import maintenant
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


def add(titre: str, prompt: str, hour: int = 8, days=None, icon: str = "⚡",
        minute: int = 0) -> dict:
    item = {"id": uuid.uuid4().hex[:10], "titre": titre.strip()[:80],
            "prompt": prompt.strip()[:400], "hour": max(0, min(23, int(hour))),
            # Les minutes manquaient : on ne pouvait planifier qu'a l'heure pile.
            "minute": max(0, min(59, int(minute or 0))),
            # `days or [...]` transformait une liste VIDE en « tous les jours ».
            "days": list(range(7)) if days is None else list(days),
            "icon": icon, "active": True,
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
_JOURS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_JOURS_COURT = ("L", "M", "M", "J", "V", "S", "D")


def _jours(item: dict) -> list:
    """Les jours cochés. Absent = tous les jours ; VIDE = aucun.

    ⚠️ `item.get("days") or [0..6]` traitait une liste VIDE comme « pas de préférence » :
    décocher tous les jours faisait tourner l'automatisation TOUS les jours, exactement
    l'inverse de ce qu'on demandait.
    """
    j = item.get("days")
    return list(range(7)) if j is None else list(j)


def non_lus() -> list:
    """Les resultats produits pendant ton absence, jamais affiches."""
    return [{"id": i["id"], "titre": i.get("titre", ""), "icon": i.get("icon", "⚡"),
             "quand": i.get("last_run"), "resultat": i.get("last_result", "")}
            for i in list_all()
            if i.get("lu") is False and (i.get("last_result") or "").strip()]


def marquer_lus(ids=None) -> int:
    """Marque comme vus : ils ne seront plus represents a chaque ouverture."""
    n = 0
    with _LOCK:
        items = _load()
        for it in items:
            if it.get("lu") is False and (ids is None or it["id"] in ids):
                it["lu"] = True
                n += 1
        if n:
            _save(items)
    return n


def prochaine_execution(item: dict) -> str:
    """Quand cette automatisation partira-t-elle, en heure de l'utilisateur ?"""
    from datetime import timedelta
    if not item.get("active", True):
        return "désactivée"
    now = maintenant()
    jours = _jours(item)
    h, mi = int(item.get("hour", 8)), int(item.get("minute", 0) or 0)
    for d in range(8):
        cible = (now + timedelta(days=d)).replace(hour=h, minute=mi, second=0, microsecond=0)
        if cible <= now or cible.weekday() not in jours:
            continue
        # strftime rend « Mon », « Sun »… : la locale du conteneur est anglaise et on ne
        # peut pas compter dessus. On nomme les jours nous-mêmes.
        quand = f"{_JOURS_FR[cible.weekday()]} {cible.strftime('%d/%m à %Hh%M')}"
        if d == 0:
            quand = f"aujourd'hui à {cible.strftime('%Hh%M')}"
        elif d == 1:
            quand = f"demain à {cible.strftime('%Hh%M')}"
        return quand
    return "aucune (aucun jour coché)"


def etat_planificateur() -> dict:
    """Le planificateur tourne-t-il VRAIMENT, et à quelle heure ?

    Répondre « oui il est démarré » ne suffit pas : sur une offre gratuite l'instance
    s'endort, la boucle s'arrête, et rien ne part. Le battement le prouve ou l'infirme.
    """
    from agent.horloge import FUSEAU, decalage_h
    vu = float(BATTEMENT.get("ts") or 0)
    depuis = (time.time() - vu) if vu else None
    items = list_all()
    d = {"fuseau": FUSEAU, "heure_utilisateur": maintenant().strftime("%a %d/%m %H:%M"),
         "decalage_avec_le_serveur_h": decalage_h(),
         "automatisations": len(items),
         "actives": sum(1 for i in items if i.get("active", True)),
         "dernier_battement_il_y_a_s": round(depuis) if depuis is not None else None,
         "prochaines": [{"titre": i.get("titre"), "quand": prochaine_execution(i)}
                        for i in items if i.get("active", True)][:10]}
    if not BATTEMENT.get("demarre"):
        d["resume"] = "❌ Le planificateur n'a jamais démarré."
    elif depuis is None or depuis > 300:
        d["resume"] = ("⚠️ Le planificateur ne tourne plus : dernier passage il y a "
                       f"{round((depuis or 0) / 60)} min. C'est le symptôme d'un hébergement "
                       "gratuit qui met l'instance en veille faute de visites — pendant ce "
                       "temps AUCUNE automatisation ne part.")
        d["solution"] = ("Soit un hébergement qui ne s'endort pas, soit un réveil externe "
                         "qui appelle /health toutes les 10 min (cron-job.org, gratuit).")
    else:
        d["resume"] = (f"✅ Le planificateur tourne (vu il y a {round(depuis)} s) et raisonne "
                       f"en heure de {FUSEAU}.")
    return d


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
                # ⚠️ Le resultat n'etait POUSSE nulle part sans Telegram : il fallait
                # penser a ouvrir la fenetre Automatisations pour le decouvrir. Une
                # automatisation que personne ne voit ne sert a rien. On le marque donc
                # NON LU, et Nova le presente d'elle-meme a la prochaine ouverture.
                it["lu"] = False
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


# Dernier passage du planificateur. ⚠️ Sur une offre gratuite, l'hébergeur ENDORT
# l'instance après quelques minutes sans visite : la boucle s'arrête alors sans rien
# dire, et les automatisations ne partent jamais. Ce battement est la seule preuve
# vérifiable qu'elle tourne encore (voir /agent/diag/automatisations).
BATTEMENT = {"ts": 0.0, "demarre": 0.0}


async def scheduler_loop():
    """Boucle de fond : vérifie chaque minute s'il y a une automatisation à lancer."""
    logger.info("Automatisations : planificateur démarré.")
    BATTEMENT["demarre"] = time.time()
    while True:
        try:
            await asyncio.sleep(60)
            BATTEMENT["ts"] = time.time()
            # ⚠️ L'heure de L'UTILISATEUR, pas celle du serveur : le conteneur est en
            # UTC, donc « à 7h » se déclenchait à 9h heure de Paris.
            now = maintenant()
            for it in list_all():
                if not it.get("active", True):
                    continue
                if now.weekday() not in _jours(it):
                    continue
                # ⚠️ La boucle ne passe que toutes les 60 s : exiger la minute EXACTE
                # ferait rater le rendez-vous une fois sur deux. On accepte donc les
                # deux minutes qui suivent l'heure prevue, et on se protege du doublon
                # par la date du dernier lancement.
                cible = now.replace(hour=int(it.get("hour", 8)),
                                    minute=int(it.get("minute", 0) or 0),
                                    second=0, microsecond=0)
                retard = (now - cible).total_seconds()
                if not (0 <= retard < 120):
                    continue
                last = it.get("last_run") or 0
                if time.time() - last < 300:       # déjà lancée à l'instant
                    continue
                await run_one(it)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[automations] boucle : {e}")
            await asyncio.sleep(120)
