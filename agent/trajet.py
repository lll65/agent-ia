"""
Un temps de trajet, sans clé, sans compte, sans facture.

⚠️ POURQUOI ON N'UTILISE PAS GOOGLE MAPS.
« Utilise Google Maps alors » → « Distance Matrix API returned status: REQUEST_DENIED.
You must use an API key to authenticate each request to Google Maps Platform APIs. »

Ce message vient de GOOGLE, pas de Composio. Les API Google Maps Platform (Distance
Matrix, Directions, Places) s'authentifient par une CLÉ liée à un projet Google Cloud
avec facturation activée. L'OAuth que Composio a connecté ne marche pas pour elles, et
ne marchera jamais : ce n'est pas une connexion mal faite, c'est le mauvais type
d'authentification. Reconnecter l'app cent fois n'y changera rien — et c'est
exactement ce que Nova lui disait de faire.

Lohan s'interdit les API payantes. Google Maps Platform demande une carte bancaire
même pour son palier gratuit. Donc on change de fournisseur, on ne répare pas.

CE QU'ON UTILISE À LA PLACE, tout gratuit et sans inscription :
  • open-meteo pour trouver les coordonnées d'un lieu. Déjà utilisé pour sa météo
    tous les matins, donc joignable depuis Render : c'est prouvé, pas supposé.
  • OSRM (router.project-osrm.org) pour la route et la durée en voiture.

⚠️ CE QUE JE N'AI PAS PU VÉRIFIER. Toutes les sorties réseau sont bloquées depuis mon
environnement de développement : je n'ai jamais vu OSRM répondre. Le découpage et la
lecture de sa réponse sont testés sur des réponses fabriquées ; l'appel réel, non. Si
OSRM ne répond pas depuis Render, le message le dira franchement au lieu d'inventer
une durée.
"""
import logging
import re

logger = logging.getLogger(__name__)

_GEO = "https://geocoding-api.open-meteo.com/v1/search"
_OSRM = "https://router.project-osrm.org/route/v1/driving/{}"


class Introuvable(Exception):
    """Un lieu qu'on n'a pas su placer sur la carte, avec son nom."""


def coordonnees(lieu: str, session=None):
    """(latitude, longitude, nom affiché) — lève Introuvable si le lieu est inconnu."""
    import requests
    s = session or requests
    r = s.get(_GEO, params={"name": lieu, "count": 1, "language": "fr", "format": "json"},
              timeout=12)
    res = ((r.json() or {}).get("results") or [])
    if not res:
        raise Introuvable(lieu)
    p = res[0]
    nom = p.get("name") or lieu
    region = p.get("admin1") or ""
    return p["latitude"], p["longitude"], (f"{nom} ({region})" if region else nom)


def _duree_lisible(secondes: float) -> str:
    m = int(round(secondes / 60.0))
    if m < 60:
        return f"{m} min"
    h, reste = divmod(m, 60)
    return f"{h} h" if reste == 0 else f"{h} h {reste:02d}"


def itineraire(depart: str, arrivee: str, session=None) -> str:
    """Le trajet en voiture entre deux lieux, en français. Jamais une durée inventée."""
    import requests
    s = session or requests
    try:
        la1, lo1, nom1 = coordonnees(depart, s)
        la2, lo2, nom2 = coordonnees(arrivee, s)
    except Introuvable as e:
        return (f"🗺️ Je n'ai pas trouvé « {e.args[0]} » sur la carte. Précise la "
                "commune et le département, et je réessaie.")
    except Exception as e:
        logger.info(f"[trajet] géocodage impossible ({type(e).__name__})")
        return ("🗺️ Je n'ai pas pu placer ces lieux sur la carte à l'instant — je ne "
                "te donne donc PAS une durée au hasard. Redemande-moi dans un moment.")

    try:
        r = s.get(_OSRM.format(f"{lo1},{la1};{lo2},{la2}"),
                  params={"overview": "false", "alternatives": "false"}, timeout=15)
        d = r.json() or {}
        routes = d.get("routes") or []
        if d.get("code") != "Ok" or not routes:
            raise ValueError(d.get("code") or "réponse vide")
        route = routes[0]
        km = float(route["distance"]) / 1000.0
        duree = _duree_lisible(float(route["duration"]))
    except Exception as e:
        logger.info(f"[trajet] OSRM indisponible ({type(e).__name__})")
        # ⚠️ Surtout pas d'estimation « à vol d'oiseau » maquillée en temps de trajet :
        # ce serait un chiffre inventé, exactement ce qu'on passe la journée à traquer.
        return (f"🗺️ Je n'ai pas pu calculer le trajet **{nom1} → {nom2}** : le service "
                "de calcul d'itinéraire ne répond pas. Je préfère te le dire plutôt que "
                "de t'annoncer une durée que je n'ai pas mesurée.")

    return (f"🚗 **{nom1} → {nom2}**\n\n"
            f"- Durée en voiture : **{duree}**\n"
            f"- Distance : **{km:.0f} km**\n\n"
            "_Trajet routier sans trafic (OSRM, gratuit). Compte un peu plus aux heures "
            "de pointe._")


# ── Reconnaître la demande et en extraire les deux lieux ─────────────────────
# ⚠️ « Combien de temps j'ai pour de Saint-Agne aller à Hèches » — sa vraie phrase,
# dictée à la voix, avec un « pour de » qui ne veut rien dire.
# Première version : je cherchais « de X à Y » directement dans la phrase entière. Elle
# attrapait « de temps j'ai pour » et rendait le départ « temps j'ai ». On RETIRE donc
# d'abord la question elle-même, puis on lit les lieux dans ce qui reste — la question
# et les lieux ne se lisent pas avec la même grammaire.
_MOTS_TRAJET = re.compile(
    r"(combien de temps|temps de (?:trajet|route)|dur[ée]e du trajet|"
    r"[àa] quelle distance|distance entre|itin[ée]raire|trajet|comment aller|"
    r"en combien de temps|pour rejoindre)", re.I)
# La question, retirée en entier avant de chercher les lieux.
_PREAMBULE = re.compile(
    r"^.*?(?:combien de temps(?:\s+j'?ai)?|temps de (?:trajet|route)|"
    r"dur[ée]e du trajet|[àa] quelle distance|itin[ée]raire|trajet|"
    r"comment (?:aller|se rendre)|pour rejoindre)\s*(?:pour\s+)?(?:aller\s+)?", re.I)
_QUEUE = re.compile(r"\s*(?:en (?:voiture|train|bus|v[ée]lo)|combien de temps|"
                    r"stp|s'il te pla[îi]t|\?|\.|!)+\s*$", re.I)

_PAIRE = re.compile(
    r"\b(?:de|depuis|d'|du)\s+(?P<a>[\wÀ-ÿ'’\- ]{2,40}?)\s+"
    r"(?:aller\s+)?(?:jusqu'?[àa]u?|[àa]|vers|pour)\s+(?P<b>[\wÀ-ÿ'’\- ]{2,40}?)\s*$", re.I)
_ENTRE = re.compile(
    r"\bentre\s+(?P<a>[\wÀ-ÿ'’\- ]{2,40}?)\s+et\s+(?P<b>[\wÀ-ÿ'’\- ]{2,40}?)\s*$", re.I)

# Ce qui n'est jamais un lieu qu'on sait placer sur une carte. « Chez moi » et « mon
# lycée » en font partie : mieux vaut rendre None et laisser l'agent demander, que
# géocoder « mon lycée » et tomber sur un lycée à l'autre bout du pays.
_PAS_UN_LIEU = re.compile(
    r"^(mon|ma|mes|ton|ta|tes|le|la|les|un|une|ce|cette|moi|toi|nous|vous|ici|"
    r"chez\s+\w+|(?:mon|ma)\s+\w+|maintenant|demain|hier|aujourd'?hui|midi|minuit)$",
    re.I)


def lieux_demandes(message: str):
    """(départ, arrivée) si la phrase demande un trajet entre deux endroits, sinon None."""
    texte = " ".join(str(message or "").split())
    if not _MOTS_TRAJET.search(texte):
        return None
    reste = _QUEUE.sub("", _PREAMBULE.sub("", texte)).strip(" ,;")
    for motif in (_ENTRE, _PAIRE):
        m = motif.search(reste)
        if not m:
            continue
        a = " ".join(m.group("a").split()).strip(" '’-")
        b = " ".join(m.group("b").split()).strip(" '’-")
        if not a or not b:
            continue
        if _PAS_UN_LIEU.match(a) or _PAS_UN_LIEU.match(b):
            return None
        # « de 14h à 16h » n'est pas un trajet, c'est un créneau.
        if re.match(r"^\d", a) or re.match(r"^\d", b):
            return None
        return a, b
    return None
