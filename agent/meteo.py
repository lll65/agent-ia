"""
La météo de SA ville, mesurée — et non cherchée sur le web.

⚠️ VU EN VRAI, le 8 septembre 2026. « dis-moi la météo à Pau demain » →

    « ⚠️ Je n'ai pas trouvé de prévision météo précise pour demain à Pau. »
    Sources consultées : AccuWeather « Monthly Weather », WeatherSpark « Climat par
    mois », PredictWind « Historical Weather », AQI.in, Timeanddate…

Nova est partie faire une RECHERCHE WEB. Un moteur ne rend pas une prévision : il rend
des pages qui parlent de météo — des moyennes mensuelles, des historiques, du climat.
Aucune ne dit le temps qu'il fera demain.

⚠️ ET ELLE AVAIT LA RÉPONSE SOUS LA MAIN. open-meteo alimente déjà son briefing du matin
et son écran d'accueil : gratuit, sans clé, joignable depuis Render — c'est prouvé tous
les jours. Personne n'avait branché cette source sur une QUESTION. Elle servait quand
Nova parlait toute seule, pas quand il demandait.
Et lui : « c'est pas normal que tu trouves pas la météo de demain de Pau ». Il a raison.

⚠️ « à chaque fois tu me donnes la météo de Paris » : la ville vient de son profil, et
« Paris » n'est qu'un dernier recours. Si la ville demandée est écrite dans la phrase,
c'est ELLE qui gagne — sur le profil comme sur le réglage.
"""
import logging
import re

logger = logging.getLogger(__name__)

_GEO = "https://geocoding-api.open-meteo.com/v1/search"
_PREV = "https://api.open-meteo.com/v1/forecast"

_WCODE = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine",
    55: "bruine forte", 61: "pluie faible", 63: "pluie", 65: "pluie forte",
    66: "pluie verglaçante", 67: "pluie verglaçante forte", 71: "neige faible",
    73: "neige", 75: "neige forte", 77: "grains de neige", 80: "averses",
    81: "averses", 82: "fortes averses", 85: "averses de neige", 86: "averses de neige",
    95: "orage", 96: "orage avec grêle", 99: "orage violent",
}

_JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


# ── Reconnaître la demande ───────────────────────────────────────────────────
_MOTS_METEO = re.compile(
    r"\b(m[ée]t[ée]o|(?:quel )?temps (?:qu'?)?il (?:fait|fera|va faire)|quel temps|"
    r"va-t-il pleuvoir|il va pleuvoir|"
    r"temp[ée]rature|combien de degr[ée]s|fait-il chaud|fait-il froid|ensoleill|pluie pr[ée]vue)\b",
    re.I)
# Ce qui n'est PAS une question de météo malgré le mot « temps ».
_FAUX = re.compile(r"\b(combien de temps|temps de trajet|temps libre|en même temps|"
                   r"temps de r[ée]ponse|perdre du temps)\b", re.I)


def veut_la_meteo(message: str) -> bool:
    m = str(message or "")
    if _FAUX.search(m):
        return False
    return bool(_MOTS_METEO.search(m))


# ⚠️ « la météo de Pau demain » : la ville est dans la phrase. Elle prime sur le profil
# — c'est justement ce qu'il reprochait (« tu me donnes toujours la météo de Paris »).
# ⚠️ Première version : « la météo à Pau demain » rendait la ville « Pau demain ».
# La suite du nom acceptait un mot en minuscule (« [A-ZÀ-Ý]? » optionnel), donc elle
# avalait l'adverbe qui suit. Un nom composé français garde ses particules en minuscule
# (« Lons-le-Saunier », « Saint-Jean-de-Luz ») mais chaque VRAI mot commence par une
# majuscule : c'est ça qu'on exige.
_APRES = re.compile(r"\b(?:[àa]|de|d'|sur|pour|vers)\s+"
                    r"([A-ZÀ-Ý][\wÀ-ÿ'’-]+"
                    r"(?:[- ](?:de|du|des|le|la|les|sur|l[èe]s|en|d')?[- ]?[A-ZÀ-Ý][\wÀ-ÿ'’-]+){0,3})")
_PAS_UNE_VILLE = re.compile(
    r"^(demain|aujourd|hier|ce|cette|la|le|les|mon|ma|mes|midi|minuit|matin|soir|"
    r"semaine|week|nuit|apr[èe]s|quelle|quel|combien|pied|voiture|toi|moi)",
    re.I)


def ville_demandee(message: str) -> str:
    """La ville écrite dans la phrase, ou "" si elle n'y est pas."""
    for m in _APRES.finditer(str(message or "")):
        nom = " ".join(m.group(1).split()).strip(" ,;.")
        if nom and not _PAS_UNE_VILLE.match(nom):
            return nom
    return ""


def quand_demande(message: str) -> int:
    """0 = aujourd'hui, 1 = demain, 2 = après-demain. Par défaut aujourd'hui."""
    m = str(message or "").lower()
    if "après-demain" in m or "apres-demain" in m or "après demain" in m:
        return 2
    if "demain" in m:
        return 1
    return 0


# ── La prévision elle-même ───────────────────────────────────────────────────
def _coordonnees(ville: str, session=None):
    import requests
    s = session or requests
    r = s.get(_GEO, params={"name": ville, "count": 1, "language": "fr", "format": "json"},
              timeout=12)
    res = ((r.json() or {}).get("results") or [])
    if not res:
        return None
    p = res[0]
    nom = p.get("name") or ville
    region = p.get("admin1") or ""
    return p["latitude"], p["longitude"], (f"{nom} ({region})" if region else nom)


def previsions(ville: str, jour: int = 0, session=None) -> str:
    """La météo en français. Jamais une prévision inventée : si ça rate, ça se dit."""
    import requests
    s = session or requests
    try:
        coord = _coordonnees(ville, s)
    except Exception as e:
        logger.info(f"[météo] géocodage impossible ({type(e).__name__})")
        coord = None
    if not coord:
        return (f"🌤️ Je n'ai pas trouvé « {ville} » sur la carte, donc je ne te donne "
                "AUCUNE prévision — précise la commune et le département.")
    lat, lon, nom = coord
    jour = max(0, min(6, int(jour or 0)))
    try:
        r = s.get(_PREV, params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "forecast_days": jour + 1,
            "daily": ("temperature_2m_max,temperature_2m_min,"
                      "precipitation_probability_max,weather_code,wind_speed_10m_max"),
        }, timeout=15)
        d = (r.json() or {}).get("daily") or {}
        tmax = d["temperature_2m_max"][jour]
        tmin = d["temperature_2m_min"][jour]
        pluie = (d.get("precipitation_probability_max") or [None] * (jour + 1))[jour]
        code = (d.get("weather_code") or [0] * (jour + 1))[jour]
        vent = (d.get("wind_speed_10m_max") or [None] * (jour + 1))[jour]
        date = (d.get("time") or [""] * (jour + 1))[jour]
    except Exception as e:
        logger.info(f"[météo] open-meteo indisponible ({type(e).__name__})")
        # ⚠️ Surtout pas de repli « il fera doux » : c'est exactement la phrase qu'elle
        # a sortie quand il a insisté, et elle ne reposait sur rien.
        return (f"🌤️ Je n'ai pas pu récupérer la météo de {nom} à l'instant. Je préfère "
                "te le dire plutôt que t'annoncer un temps que je n'ai pas mesuré.")

    quand = ("Aujourd'hui", "Demain", "Après-demain")[jour] if jour < 3 else date
    desc = _WCODE.get(int(code or 0), "")
    bouts = [f"**{quand} à {nom}** — {desc}" if desc else f"**{quand} à {nom}**"]
    if tmin is not None and tmax is not None:
        bouts.append(f"de {round(tmin)} à {round(tmax)} °C")
    if pluie is not None:
        bouts.append(f"risque de pluie {int(pluie)} %")
    if vent is not None:
        bouts.append(f"vent jusqu'à {round(vent)} km/h")
    return "🌤️ " + ", ".join(bouts) + ".\n\n_Source : open-meteo, relevé à l'instant._"


def repond(message: str, ville_profil: str = "", session=None) -> str:
    """La réponse complète à une question de météo."""
    ville = ville_demandee(message) or ville_profil or "Paris"
    return previsions(ville, quand_demande(message), session)
