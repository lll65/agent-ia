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
from agent.entrepot import Entrepot
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
_ENTREPOT = Entrepot("automations", "data/automations.json", cle="id")


def _charge() -> tuple[list, bool]:
    """(éléments, lecture fiable ?) — voir agent/entrepot.py.

    ⚠️ L'ancien couple _load/_save reconstruisait la table entière (DELETE puis
    INSERT) a partir d'une lecture qui avait le droit d'echouer en silence : une
    coupure Supabase de quelques secondes effacait tout, definitivement.
    """
    return _ENTREPOT.charge()


def _load() -> list:
    return _charge()[0]


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
            "icon": icon, "active": True, "cree": time.time(),
            "last_run": None, "last_result": "", "runs": 0}
    with _LOCK:
        _ENTREPOT.ecrit_un(item)
    return item


def update(aid: str, **changes) -> bool:
    with _LOCK:
        for it in _load():
            if it["id"] == aid:
                it.update({k: v for k, v in changes.items() if v is not None})
                _ENTREPOT.ecrit_un(it)
                return True
    return False


def delete(aid: str) -> bool:
    with _LOCK:
        items = _load()
        if not any(i["id"] == aid for i in items):
            return False
        _ENTREPOT.supprime([aid])
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
        for it in _load():
            if it.get("lu") is False and (ids is None or it["id"] in ids):
                it["lu"] = True
                _ENTREPOT.ecrit_un(it)
                n += 1
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


def _il_y_a(ts) -> str:
    """« il y a 3 h » — sans ça, un constat ancien se lit comme l'état actuel."""
    try:
        ecart = time.time() - float(ts or 0)
    except Exception:
        return ""
    if ecart < 120:
        return "à l'instant"
    if ecart < 7200:
        return f"il y a {round(ecart / 60)} min"
    if ecart < 172800:
        return f"il y a {round(ecart / 3600)} h"
    return f"il y a {round(ecart / 86400)} j"


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
                        for i in items if i.get("active", True)][:10],
         # « J'en ai fait une a 17h et je ne recois rien » : sans ca, impossible de
         # distinguer « elle n'a pas tourne » de « elle a tourne mais l'envoi a rate ».
         # ⚠️ L'issue du dernier envoi s'affichait SANS DATE : un « non envoyé :
         # TELEGRAM_TOKEN absent » vieux de plusieurs jours se lisait comme l'état
         # actuel, et contredisait le bloc juste au-dessus qui disait « ✅ les
         # résultats partent ». Deux affirmations opposées, aucune fausse : l'une
         # décrivait le passé, l'autre le présent. On date donc le constat.
         "derniers_envois": [{"titre": i.get("titre"),
                              "issue": i.get("dernier_envoi") or "—",
                              "quand": _il_y_a(i.get("last_run"))}
                             for i in items if i.get("last_run")][:10]}
    try:
        from bots.telegram_push import diagnostic as _diag_tg
        d["telegram"] = _diag_tg()
    except Exception as e:
        d["telegram"] = {"resume": f"diagnostic Telegram indisponible ({type(e).__name__})"}
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
        # Mode fond : personne ne regarde l'écran à 17 h, donc on cherche plus loin.
        answer = await _ask_agent(item["prompt"], fond=True)
    except Exception as e:
        answer = f"❌ Échec : {type(e).__name__}: {str(e)[:200]}"
    # Notification Telegram. ⚠️ Ce push exigeait TELEGRAM_CHAT_ID, une variable que
    # personne ne pense a definir : sans elle le resultat ne partait NULLE PART, en
    # silence. On passe par send_message, qui sait aussi viser le chat du proprietaire
    # (celui qui a parle au bot) — un /start suffit donc. Et on ENREGISTRE l'issue :
    # une notification qui echoue doit se voir dans le diagnostic, pas disparaitre.
    envoi = ""
    if config.TELEGRAM_TOKEN:
        try:
            from bots.telegram_push import send_message, proprietaire
            from agent.core import _off
            if not proprietaire():
                envoi = ("non envoye : Nova ne sait pas a quel chat Telegram ecrire. "
                         "Envoie /start au bot une fois.")
            elif await _off(send_message, f"⚡ {item['titre']}\n\n{(answer or '')[:3500]}"):
                envoi = "envoye sur Telegram"
            else:
                envoi = "echec de l'envoi Telegram (voir les journaux)"
        except Exception as e:
            envoi = f"echec de l'envoi Telegram ({type(e).__name__})"
    else:
        envoi = "non envoye : TELEGRAM_TOKEN absent"

    with _LOCK:
        for it in _load():
            if it["id"] == item["id"]:
                it["last_run"] = time.time()
                it["dernier_envoi"] = envoi
                # ⚠️ 4 000 caractères coupaient une synthèse bourse en plein milieu.
                # Le travail de fond peut désormais durer 20 min : autant en garder le
                # résultat entier. (Telegram, lui, reste borné par son propre plafond.)
                it["last_result"] = (answer or "")[:12000]
                it["runs"] = int(it.get("runs", 0)) + 1
                # ⚠️ Le resultat n'etait POUSSE nulle part sans Telegram : il fallait
                # penser a ouvrir la fenetre Automatisations pour le decouvrir. Une
                # automatisation que personne ne voit ne sert a rien. On le marque donc
                # NON LU, et Nova le presente d'elle-meme a la prochaine ouverture.
                it["lu"] = False
                _ENTREPOT.ecrit_un(it)
    try:
        from agent.squad import record
        record("nova", "", f"⚡ {item['titre']}")
    except Exception:
        pass
    logger.info(f"[automations] '{item['titre']}' exécutée — {envoi}.")
    return answer


# Dernier passage du planificateur. ⚠️ Sur une offre gratuite, l'hébergeur ENDORT
# l'instance après quelques minutes sans visite : la boucle s'arrête alors sans rien
# dire, et les automatisations ne partent jamais. Ce battement est la seule preuve
# vérifiable qu'elle tourne encore (voir /agent/diag/automatisations).
BATTEMENT = {"ts": 0.0, "demarre": 0.0}

# ⚠️ La boucle n'acceptait que les 2 minutes qui suivent l'heure prevue. Sur une offre
# gratuite l'instance dort : elle se reveille a 17h20 pour un rendez-vous de 17h00, la
# fenetre est passee, et l'automatisation ne part JAMAIS — sans le moindre message.
# On rattrape donc jusqu'a 3 h de retard : mieux vaut un resultat en retard que rien.
RATTRAPAGE = 3 * 3600


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
                if not (0 <= retard < RATTRAPAGE):
                    continue
                last = it.get("last_run") or 0
                # Deja lancee pour CE rendez-vous ? `retard` et l'age du dernier
                # lancement sont deux durees en secondes reelles : si le dernier
                # lancement est plus recent que le rendez-vous, c'etait celui-ci.
                # Le plancher de 300 s evite un doublon dans la minute qui suit.
                if last and (time.time() - last) < max(retard, 300):
                    continue
                # Rattrapage, mais pas de rendez-vous ANTERIEUR a la creation :
                # une automatisation de 7h creee a 9h ne doit pas partir aussitot.
                if not last and float(it.get("cree") or 0) > time.time() - retard:
                    continue
                if retard > 120:
                    logger.info(f"[automations] '{it.get('titre')}' rattrapee avec "
                                f"{round(retard / 60)} min de retard (instance endormie ?).")
                await run_one(it)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[automations] boucle : {e}")
            await asyncio.sleep(120)
