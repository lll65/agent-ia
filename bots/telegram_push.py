"""
Push Telegram — envoi d'un message SORTANT (alerte proactive), indépendant du bot
de polling. Utilise l'API HTTP Telegram directement (aucune dépendance async), donc
robuste et appelable depuis n'importe quel contexte (watcher, superviseur, cron).

Cibles :
  - config.TELEGRAM_CHAT_ID si défini
  - sinon tous les chat_id connus (data/telegram_chats.json), remplis quand un
    utilisateur envoie /start au bot.
"""
import json
import logging
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)

_CHATS_FILE = Path("data/telegram_chats.json")


def register_chat(chat_id) -> None:
    """Mémorise un chat_id (appelé par le bot sur /start ou tout message)."""
    if chat_id is None:
        return
    chat_id = str(chat_id)
    known = _load_chats()
    if chat_id not in known:
        known.append(chat_id)
        _CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CHATS_FILE.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[TelegramPush] Nouveau chat enregistré: {chat_id}")


def _load_chats() -> list[str]:
    if _CHATS_FILE.exists():
        try:
            data = json.loads(_CHATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(c) for c in data]
        except Exception:
            pass
    return []


def _targets() -> list[str]:
    if config.TELEGRAM_CHAT_ID:
        return [str(config.TELEGRAM_CHAT_ID)]
    return _load_chats()


def send_message(text: str, chat_id=None) -> bool:
    """
    Envoie un message Telegram. Retourne True si au moins un envoi a réussi.
    Sans chat_id explicite, diffuse à toutes les cibles connues.
    """
    if not config.TELEGRAM_TOKEN:
        logger.warning("[TelegramPush] TELEGRAM_TOKEN absent — impossible d'envoyer.")
        return False

    import requests

    targets = [str(chat_id)] if chat_id is not None else _targets()
    if not targets:
        logger.warning(
            "[TelegramPush] Aucune cible : définis TELEGRAM_CHAT_ID dans .env "
            "ou envoie /start au bot au moins une fois."
        )
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    ok_any = False
    # Telegram limite à 4096 caractères par message
    body = text if len(text) <= 4000 else text[:3950] + "\n…(tronqué)"
    for cid in targets:
        try:
            r = requests.post(
                url,
                json={"chat_id": cid, "text": body, "disable_web_page_preview": True},
                timeout=15,
            )
            if r.status_code == 200:
                ok_any = True
            else:
                logger.warning(f"[TelegramPush] HTTP {r.status_code} vers {cid}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"[TelegramPush] Erreur envoi vers {cid}: {e}")
    return ok_any
