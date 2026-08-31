"""
Les conversations de Nova — les mêmes sur le téléphone ET sur l'ordinateur.

⚠️ LE DÉFAUT QUE CE MODULE SUPPRIME. La liste des conversations vivait dans le
`localStorage` du navigateur (clé « nova_sessions »). Un localStorage appartient à
UN navigateur sur UN appareil : l'iPhone et l'ordinateur avaient chacun leur
historique, et aucun des deux ne voyait celui de l'autre. Vider le cache du
navigateur, ou changer de téléphone, effaçait tout définitivement.

À ne pas confondre avec la mémoire de Nova (memory/manager.py), qui, elle, était
bien côté serveur : c'est ce qui explique que Nova pouvait se souvenir d'un fait
tout en n'affichant pas la conversation où il avait été dit.

Persistance : Supabase si SUPABASE_DB_URL, sinon fichier local — et le fichier
local d'un conteneur Render est effacé à chaque réveil, donc la synchronisation
entre appareils suppose vraiment Supabase. On le DIT plutôt que de le supposer
(voir `etat()`).
"""
import logging
import time

from agent.entrepot import Entrepot

logger = logging.getLogger(__name__)

_ENTREPOT = Entrepot("sessions_chat", "data/sessions.json", cle="id")

# Bornes volontairement larges mais fermes : une conversation ne doit pas pouvoir
# faire enfler la base sans limite (le Mode Cours colle des transcriptions entières).
MAX_SESSIONS = 60
MAX_MESSAGES = 120
MAX_CARACTERES = 40_000


def _propre(s: dict) -> dict:
    """Une session bornée et sans surprise, quelle que soit sa provenance."""
    msgs = []
    total = 0
    for m in (s.get("messages") or [])[-MAX_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        texte = str(m.get("text") or "")[:8000]
        total += len(texte)
        if total > MAX_CARACTERES:
            break
        msgs.append({"role": str(m.get("role") or "ai")[:12], "text": texte})
    return {
        "id": str(s.get("id") or "")[:40],
        "title": str(s.get("title") or "Conversation")[:80],
        "ts": float(s.get("ts") or time.time() * 1000),
        "messages": msgs,
    }


def lister() -> list:
    """Toutes les conversations, la plus récemment touchée en tête."""
    items, _ = _ENTREPOT.charge()
    return sorted(items, key=lambda s: float(s.get("ts") or 0), reverse=True)[:MAX_SESSIONS]


def enregistrer(session: dict) -> dict:
    """Ajoute ou met à jour UNE conversation.

    Écriture ciblée : elle ne peut pas faire disparaître les autres, même si la
    base n'a pas répondu au moment de la lecture (voir agent/entrepot.py).
    """
    s = _propre(session)
    if not s["id"]:
        return {}
    _ENTREPOT.ecrit_un(s)
    _elaguer()
    return s


def supprimer(sid: str) -> bool:
    sid = str(sid or "")[:40]
    if not sid:
        return False
    items, fiable = _ENTREPOT.charge()
    if not fiable or not any(str(x.get("id")) == sid for x in items):
        return False
    _ENTREPOT.supprime([sid])
    return True


def _elaguer() -> None:
    """Au-delà du plafond, on retire les conversations les PLUS ANCIENNES, nommément."""
    items, fiable = _ENTREPOT.charge()
    if not fiable or len(items) <= MAX_SESSIONS:
        return
    vieilles = sorted(items, key=lambda s: float(s.get("ts") or 0))[:len(items) - MAX_SESSIONS]
    _ENTREPOT.supprime([v.get("id") for v in vieilles if v.get("id")])


def etat() -> dict:
    """Les deux appareils voient-ils VRAIMENT la même chose ?

    Répondre « oui, c'est synchronisé » sans le vérifier serait exactement l'erreur
    qui a laissé passer le problème : sans Supabase, le fichier local est effacé à
    chaque réveil de Render, donc rien ne suit d'un appareil à l'autre.
    """
    items, fiable = _ENTREPOT.charge()
    d = {"conversations": len(items), "lecture_fiable": fiable,
         "ou": "Supabase" if _ENTREPOT.configure() else "fichier local (éphémère)"}
    if not _ENTREPOT.configure():
        d["resume"] = ("⚠️ Tes conversations ne sont PAS partagées entre le téléphone et "
                       "l'ordinateur : sans SUPABASE_DB_URL elles restent sur le disque de "
                       "Render, effacé à chaque réveil.")
        d["solution"] = ("Ajoute SUPABASE_DB_URL dans les variables Render "
                         "(Session pooler, pas Direct connection).")
    elif not fiable:
        d["resume"] = "⚠️ Supabase est configuré mais n'a pas répondu à l'instant."
    else:
        d["resume"] = (f"✅ {len(items)} conversation(s) partagées entre tous tes appareils.")
    return d
