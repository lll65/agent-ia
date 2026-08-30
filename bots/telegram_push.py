"""
Push Telegram — envoi d'un message SORTANT (alerte proactive, résultat
d'automatisation), indépendant du bot de polling. Utilise l'API HTTP Telegram
directement (aucune dépendance async), donc appelable depuis n'importe quel
contexte (watcher, planificateur, superviseur).

⚠️ SÉCURITÉ — le bot est joignable par N'IMPORTE QUI sur Telegram. Sans filtre,
un inconnu qui tombe sur son @nom obtenait une conversation avec l'agent, donc
un accès aux applis connectées (mails, Drive, agenda), ET son chat était
enregistré comme cible de diffusion : il aurait reçu les résultats
d'automatisation et les alertes PEA. Ce module tient donc la notion de
PROPRIÉTAIRE, et lui seul est servi.

Le propriétaire est déterminé dans cet ordre :
  1. TELEGRAM_OWNER_ID / TELEGRAM_CHAT_ID s'ils sont définis (le plus sûr) ;
  2. sinon la PREMIÈRE personne qui parle au bot, mémorisée définitivement.

Persistance : Supabase si SUPABASE_DB_URL (le disque de Render est effacé à
chaque réveil — le propriétaire retenu en local serait perdu et le suivant
venu prendrait la place), sinon fichier local.
"""
import json
import logging
import threading
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)

_CHATS_FILE = Path("data/telegram_chats.json")
_LOCK = threading.Lock()


# ── Persistance ───────────────────────────────────────────────────────────────
def _sb():
    if not getattr(config, "SUPABASE_DB_URL", ""):
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS telegram_chats "
                      "(chat_id text PRIMARY KEY, proprietaire boolean DEFAULT false)")
        return conn
    except Exception:
        return None


def _lire() -> list[dict]:
    """[{chat_id, proprietaire}] — Supabase d'abord, fichier local en secours."""
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                c.execute("SELECT chat_id, proprietaire FROM telegram_chats")
                rows = c.fetchall()
            conn.close()
            return [{"chat_id": str(r[0]), "proprietaire": bool(r[1])} for r in rows]
        except Exception:
            pass
    if _CHATS_FILE.exists():
        try:
            data = json.loads(_CHATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # Ancien format : une simple liste d'identifiants, sans propriétaire.
                # Le premier de la liste était forcément l'utilisateur (le bot n'a
                # jamais été publié) — on le promeut pour ne pas le déconnecter.
                return [{"chat_id": str(c), "proprietaire": i == 0}
                        if not isinstance(c, dict)
                        else {"chat_id": str(c.get("chat_id")),
                              "proprietaire": bool(c.get("proprietaire"))}
                        for i, c in enumerate(data)]
        except Exception:
            pass
    return []


def _ecrire(chats: list[dict]) -> None:
    try:
        _CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CHATS_FILE.write_text(json.dumps(chats, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                for ch in chats:
                    c.execute(
                        "INSERT INTO telegram_chats (chat_id, proprietaire) VALUES (%s, %s) "
                        "ON CONFLICT (chat_id) DO UPDATE SET proprietaire = EXCLUDED.proprietaire",
                        (str(ch["chat_id"]), bool(ch.get("proprietaire"))))
            conn.close()
        except Exception:
            pass


# ── Propriétaire ──────────────────────────────────────────────────────────────
# Chaque messagerie a SON propriétaire. Un identifiant Discord est préfixé
# « discord: » ; un identifiant Telegram est nu. ⚠️ Sans cette séparation, le premier
# chat Telegram devenait propriétaire de tout, et le bot Discord ne pouvait plus
# JAMAIS se réclamer : il aurait refusé Lohan lui-même.
def _canal_de(chat_id) -> str:
    return "discord" if str(chat_id or "").startswith("discord:") else "telegram"


def _configure(canal: str = "telegram") -> str:
    """L'identifiant fixé à la main dans les variables d'environnement, s'il existe."""
    attrs = (("DISCORD_OWNER_ID",) if canal == "discord"
             else ("TELEGRAM_OWNER_ID", "TELEGRAM_CHAT_ID"))
    for attr in attrs:
        v = str(getattr(config, attr, "") or "").strip()
        if v:
            return f"discord:{v}" if canal == "discord" and not v.startswith("discord:") else v
    return ""


def proprietaire(canal: str = "telegram") -> str:
    """L'identifiant du seul compte autorisé sur ce canal, ou "" si personne n'a parlé."""
    fixe = _configure(canal)
    if fixe:
        return fixe
    for ch in _lire():
        if ch.get("proprietaire") and _canal_de(ch["chat_id"]) == canal:
            return ch["chat_id"]
    return ""


def est_proprietaire(chat_id) -> bool:
    """Ce compte a-t-il le droit de parler au bot ET de recevoir les diffusions ?

    Premier venu = propriétaire de SON canal (le bot n'est utilisable que par une
    personne). Tous les suivants sont refusés — et surtout jamais enregistrés
    comme cible de diffusion.
    """
    if chat_id is None:
        return False
    cid = str(chat_id)
    canal = _canal_de(cid)
    with _LOCK:
        actuel = proprietaire(canal)
        if actuel:
            return cid == actuel
        # Personne n'est encore propriétaire de ce canal : ce compte le devient.
        chats = [c for c in _lire() if c["chat_id"] != cid]
        chats.append({"chat_id": cid, "proprietaire": True})
        _ecrire(chats)
        logger.info(f"[TelegramPush] Propriétaire {canal} fixé : {cid}. "
                    "Les autres comptes seront refusés.")
        return True


def register_chat(chat_id) -> None:
    """Mémorise un chat_id — seulement s'il s'agit du propriétaire."""
    if chat_id is None:
        return
    est_proprietaire(chat_id)   # crée l'entrée au premier passage


# ── Envoi ─────────────────────────────────────────────────────────────────────
def _targets() -> list[str]:
    """Les cibles d'une diffusion — Telegram uniquement : c'est l'API qu'on appelle."""
    p = proprietaire("telegram")
    return [p] if p else []


def diagnostic() -> dict:
    """Pourquoi les messages n'arrivent-ils pas ? Réponse vérifiable, pas une supposition."""
    d = {"token": bool(config.TELEGRAM_TOKEN),
         "proprietaire": proprietaire("telegram"),
         "source": ("variable d'environnement" if _configure("telegram")
                    else "premier venu (mémorisé)"),
         "persistance": "Supabase" if getattr(config, "SUPABASE_DB_URL", "") else "fichier local"}
    if not d["token"]:
        d["resume"] = ("❌ TELEGRAM_TOKEN n'est pas défini : Nova ne peut envoyer aucun "
                       "message. Crée un bot avec @BotFather et pose le jeton sur Render.")
    elif not d["proprietaire"]:
        d["resume"] = ("⚠️ Le bot a un jeton mais personne ne lui a jamais parlé : Nova ne "
                       "sait pas où envoyer. Envoie /start au bot une fois, ça suffit.")
    elif d["persistance"] != "Supabase":
        d["resume"] = ("⚠️ Ça marche, mais le destinataire est retenu sur le disque de "
                       "Render, effacé à chaque réveil. Ajoute SUPABASE_DB_URL pour qu'il "
                       "tienne, ou fixe TELEGRAM_CHAT_ID.")
    else:
        d["resume"] = f"✅ Les résultats partent vers le chat {d['proprietaire']}."
    return d


def send_message(text: str, chat_id=None) -> bool:
    """Envoie un message Telegram. Retourne True si l'envoi a réussi.

    Sans chat_id explicite, envoie au propriétaire — et à lui seul.
    """
    if not config.TELEGRAM_TOKEN:
        logger.warning("[TelegramPush] TELEGRAM_TOKEN absent — impossible d'envoyer.")
        return False

    import requests

    targets = [str(chat_id)] if chat_id is not None else _targets()
    if not targets:
        logger.warning(
            "[TelegramPush] Aucune cible : envoie /start au bot une fois, "
            "ou définis TELEGRAM_CHAT_ID.")
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
