"""
Les tâches de fond de Nova — et surtout : est-ce qu'elles TOURNENT vraiment ?

⚠️ LE DÉFAUT QUE CE MODULE SUPPRIME. Le démarrage faisait :

    bot_tasks.append(asyncio.create_task(run_telegram_bot()))
    logger.info("Bot Telegram démarré.")

Le message était écrit AVANT que la tâche ait fait quoi que ce soit. Si le bot
mourait dans la seconde — jeton révoqué, dépendance absente, ou surtout le
« Conflict: terminated by other getUpdates request » que Telegram renvoie quand
DEUX instances interrogent le même bot (l'ancienne installation locale et
Render) —, l'exception partait dans une tâche que personne n'attendait. Python
l'avale jusqu'au ramasse-miettes. Résultat : les journaux affirmaient « démarré »,
Lohan ne recevait rien, et absolument rien n'expliquait pourquoi.

Ici, chaque tâche est surveillée : on sait si elle tourne, si elle s'est arrêtée,
et avec quelle erreur — et le diagnostic peut le dire au lieu de le supposer.
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# nom → {"etat": "en_cours"|"arretee"|"echouee", "depuis": ts, "erreur": str}
ETAT = {}


def _lisible(nom: str, e: BaseException) -> str:
    """L'erreur, traduite en quelque chose d'actionnable."""
    t = f"{type(e).__name__}: {e}"
    bas = t.lower()
    if "conflict" in bas or "getupdates" in bas:
        return ("Un AUTRE programme interroge le même bot Telegram en même temps "
                "(ton ancienne installation en local, ou un déploiement précédent). "
                "Telegram n'en autorise qu'un : arrête l'autre, ou crée un second bot "
                "avec @BotFather pour Render. Détail : " + t[:200])
    if "unauthorized" in bas or "401" in bas:
        return ("Le jeton Telegram est refusé (révoqué ou mal recopié). Régénère-le "
                "avec @BotFather et remets-le dans les variables Render. Détail : " + t[:200])
    if "modulenotfound" in bas or "no module" in bas:
        return ("Une dépendance manque sur le serveur. Détail : " + t[:200])
    return t[:400]


async def surveille(nom: str, coro):
    """Fait tourner une tâche de fond en RETENANT ce qui lui arrive."""
    ETAT[nom] = {"etat": "en_cours", "depuis": time.time(), "erreur": ""}
    try:
        await coro
        ETAT[nom] = {"etat": "arretee", "depuis": time.time(),
                     "erreur": "la tâche s'est terminée d'elle-même"}
        logger.warning(f"[taches] « {nom} » s'est arrêtée toute seule.")
    except asyncio.CancelledError:
        ETAT[nom] = {"etat": "arretee", "depuis": time.time(), "erreur": "arrêt du serveur"}
        raise
    except BaseException as e:
        msg = _lisible(nom, e)
        ETAT[nom] = {"etat": "echouee", "depuis": time.time(), "erreur": msg}
        # exc_info : sans la trace, une panne de démarrage reste indéchiffrable.
        logger.error(f"[taches] « {nom} » a échoué — {msg}", exc_info=True)


def lancer(nom: str, coro):
    """Crée la tâche surveillée. À utiliser à la place d'asyncio.create_task."""
    return asyncio.create_task(surveille(nom, coro), name=nom)


def etat(nom: str = "") -> dict:
    """L'état réel des tâches de fond. Vide = toutes."""
    if nom:
        return dict(ETAT.get(nom) or {"etat": "jamais_lancee", "erreur": ""})
    return {k: dict(v) for k, v in ETAT.items()}


def resume(nom: str) -> str:
    """Une phrase honnête sur cette tâche, destinée à l'utilisateur."""
    e = etat(nom)
    if e["etat"] == "en_cours":
        return f"✅ {nom} tourne depuis {round((time.time() - e['depuis']) / 60)} min."
    if e["etat"] == "jamais_lancee":
        return f"❌ {nom} n'a jamais été lancée."
    return f"❌ {nom} ne tourne plus : {e.get('erreur') or 'raison inconnue'}"
