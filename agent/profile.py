"""
Profil utilisateur — ce que Nova retient VRAIMENT de toi.

Différence avec l'historique : ici on ne stocke pas des bouts de messages, mais des FAITS
structurés (prénom, âge, ville, goûts, objectifs…), dédoublonnés et modifiables.
Persistance : Supabase si SUPABASE_DB_URL, sinon fichier local.
"""
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from agent.entrepot import Entrepot
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


_ENTREPOT = Entrepot("profile_facts", "data/profile.json")


def _charge() -> tuple[list, bool]:
    """(faits, lecture fiable ?) — voir agent/entrepot.py pour le pourquoi."""
    return _ENTREPOT.charge()


# ── Le SUJET d'un fait ────────────────────────────────────────────────────────
# ⚠️ Le dédoublonnage comparait les 28 premiers caractères. « A 17 ans » et
# « A 18 ans » ne partagent pas ce préfixe : le jour de son anniversaire, Nova se
# retrouvait avec les DEUX affirmations dans son prompt système et choisissait au
# hasard. Pareil pour un déménagement (« Habite à Lyon » ; « Habite à Paris »).
# Un fait porte donc désormais un SUJET, et un nouveau fait sur le même sujet
# REMPLACE l'ancien.
_SUJETS = (
    ("age",           r"\b\d{1,2}\s*ans?\b|\bâge\b|\bage\b"),
    ("anniversaire",  r"anniversaire|né le|nee le|né en|nee en"),
    ("prenom",        r"s'appelle|se prénomme|se prenomme|prénom|prenom"),
    ("ville",         r"habite|vit à|vit a|vis à|vis a|domicili|réside|reside|déménag|demenag"),
    ("etablissement", r"lycée|lycee|collège|college|université|universite|école|ecole|étudie|etudie|en terminale|en seconde|en première|en premiere"),
    ("allergie",      r"allerg"),
    ("travail",       r"travaille|métier|metier|emploi|stage|apprenti"),
    ("telephone",     r"téléphone|telephone|portable|iphone|android"),
)


def _normalise(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", (t or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t)).strip()


def sujet_de(texte: str) -> str:
    """De quoi parle ce fait ? Deux faits de même sujet ne peuvent pas coexister."""
    bas = _normalise(texte)
    for nom, motif in _SUJETS:
        if re.search(motif, texte or "", re.I) or re.search(_normalise(motif), bas):
            return nom
    # À défaut : le texte sans ses chiffres. « Note de 15 en maths » et « Note de 18
    # en maths » se recouvrent ; « Aime le foot » et « Aime le tennis » non.
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", bas)).strip() or bas


def list_facts() -> list:
    with _LOCK:
        items, _ = _charge()
    order = list(CATEGORIES.keys())
    items.sort(key=lambda f: (order.index(f.get("cat", "autre")) if f.get("cat") in order else 99))
    return items


def add_fact(cat: str, texte: str) -> dict:
    """Ajoute un fait, en remplaçant celui qui portait déjà sur le même sujet."""
    texte = (texte or "").strip()[:160]
    if not texte:
        return {}
    cat = cat if cat in CATEGORIES else "autre"
    with _LOCK:
        items, fiable = _charge()
        # ⚠️ Écrire à partir d'une lecture qui a échoué, c'est effacer. On préfère
        # perdre UN fait que les soixante autres — et on le dit dans les journaux.
        if not fiable:
            logger.warning(f"[profil] « {texte[:40]} » NON mémorisé : la base n'a pas "
                           "répondu, et écrire maintenant effacerait le reste.")
            return {}
        sujet = sujet_de(texte)
        remplaces = [f["id"] for f in items
                     if f.get("cat") == cat and f.get("id")
                     and sujet_de(f.get("texte", "")) == sujet]
        items = [f for f in items if f.get("id") not in remplaces]
        item = {"id": uuid.uuid4().hex[:10], "cat": cat, "texte": texte,
                "sujet": sujet, "ts": time.time()}
        items.append(item)
        # Au-delà de 60 faits on jette les PLUS ANCIENS, pas les derniers arrivés.
        items.sort(key=lambda f: float(f.get("ts") or 0))
        trop = items[:-60]
        items = items[-60:]
        _ENTREPOT.ecrit(items, supprimes=remplaces + [f["id"] for f in trop if f.get("id")])
    return item


def delete_fact(fid: str) -> bool:
    with _LOCK:
        items, fiable = _charge()
        if not fiable:
            return False
        new = [f for f in items if f.get("id") != fid]
        if len(new) == len(items):
            return False
        _ENTREPOT.ecrit(new, supprimes=[fid])
        return True


def clear_all() -> None:
    """Tout effacer — demande explicite de l'utilisateur, donc DELETE assumé."""
    with _LOCK:
        _ENTREPOT.vide()


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
            fait = add_fact(f.get("cat", "autre"), t)
            if fait.get("id"):        # add_fact rend {} quand il refuse d'écrire
                out.append(fait)
    if out:
        logger.info(f"[profil] {len(out)} fait(s) mémorisé(s).")
    return out


def context_block() -> str:
    """Bloc à injecter dans les prompts pour personnaliser les réponses."""
    items = list_facts()
    if not items:
        return ""
    # ⚠️ On gardait « les 6 premiers » de chaque catégorie, dans l'ordre d'arrivée :
    # les six PLUS ANCIENS. Un fait récent et vital — « est allergique aux
    # arachides » — était mémorisé, visible dans le profil… et n'atteignait jamais
    # le prompt. On trie donc du plus RÉCENT au plus ancien, et on borne le bloc en
    # caractères pour ne rien jeter tant qu'il reste de la place.
    items = sorted(items, key=lambda f: float(f.get("ts") or 0), reverse=True)
    by = {}
    for f in items:
        by.setdefault(f.get("cat", "autre"), []).append(f.get("texte", ""))
    lines, budget = [], 1500
    for c, v in by.items():
        gardes = []
        for t in v:
            if budget - len(t) - 3 < 0:
                break
            gardes.append(t)
            budget -= len(t) + 3
        if gardes:
            lines.append(f"{CATEGORIES.get(c, c)} : " + " ; ".join(gardes))
    return "[Ce que je sais de l'utilisateur]\n" + "\n".join(lines)
