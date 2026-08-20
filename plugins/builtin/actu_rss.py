"""
Actualité par flux RSS — la source la plus fiable pour « l'actu du jour ».

Pourquoi ne pas se contenter d'un moteur de recherche : interroger DuckDuckGo avec
« actualité technologie 20 août 2026 » fait remonter les pages qui CONTIENNENT cette
chaîne de caractères — donc des communiqués de presse et des « trading updates », pas
l'actualité. Constaté en production : Nova rendait « Wel-Bloom Bio-Tech présentera des
jelly fonctionnels » et « Ashtead Technology a publié une mise à jour de trading » comme
actualité tech du jour.

Les flux RSS des médias, eux, sont datés, triés par fraîcheur et toujours sur le sujet.
Gratuits, sans clé, sans quota.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Médias francophones, groupés par thème. Plusieurs par thème : si l'un est
# injoignable ou en panne, les autres portent quand même l'information.
FLUX = {
    "tech": [
        ("Frandroid", "https://www.frandroid.com/feed"),
        ("Numerama", "https://www.numerama.com/feed/"),
        ("01net", "https://www.01net.com/feed/"),
        ("Clubic", "https://www.clubic.com/feed/news.rss"),
        ("Le Monde Informatique",
         "https://www.lemondeinformatique.fr/flux-rss/thematique/toutes-les-actualites/rss.xml"),
    ],
    "science": [
        ("Futura Sciences", "https://www.futura-sciences.com/rss/actualites.xml"),
        ("Sciences et Avenir", "https://www.sciencesetavenir.fr/rss.xml"),
    ],
    "economie": [
        ("La Tribune", "https://www.latribune.fr/feed.xml"),
        ("Les Échos", "https://services.lesechos.fr/rss/les-echos-economie.xml"),
    ],
    "sport": [
        ("L'Équipe", "https://dwh.lequipe.fr/api/edito/rss?path=/"),
        ("France Info Sport", "https://www.francetvinfo.fr/sports.rss"),
    ],
    "general": [
        ("France Info", "https://www.francetvinfo.fr/titres.rss"),
        ("Le Monde", "https://www.lemonde.fr/rss/une.xml"),
        ("BFMTV", "https://www.bfmtv.com/rss/news-24-7/"),
    ],
}

# Mots de la question → thème de flux à interroger
_THEMES = (
    ("tech", ("tech", "technologie", "technologique", "numérique", "numerique", "informatique",
              "ia ", " ia", "intelligence artificielle", "smartphone", "apple", "google",
              "android", "iphone", "logiciel", "internet", "web", "jeu vidéo", "gaming")),
    ("science", ("science", "scientifique", "espace", "astronomie", "biologie", "physique",
                 "recherche scientifique", "nasa", "climat")),
    ("economie", ("économie", "economie", "économique", "bourse", "marché", "marches",
                  "entreprise", "inflation", "finance")),
    ("sport", ("sport", "foot", "football", "rugby", "tennis", "basket", "jo ", "olympique",
               "ligue 1", "match")),
)

TIMEOUT_FLUX = 6          # par flux : on préfère 3 médias en 6 s qu'un seul en 20 s
FRAICHEUR_H = 48          # au-delà, ce n'est plus « l'actu du jour »


def theme_de(question: str) -> str:
    """Quel thème d'actualité la question vise-t-elle ?"""
    m = " " + (question or "").lower() + " "
    for nom, mots in _THEMES:
        if any(k in m for k in mots):
            return nom
    return "general"


def _texte(el, *chemins) -> str:
    """Premier nœud non vide parmi plusieurs chemins possibles (RSS et Atom diffèrent)."""
    for c in chemins:
        n = el.find(c)
        if n is not None:
            v = (n.text or "").strip() or (n.attrib.get("href") or "").strip()
            if v:
                return v
    return ""


def _quand(brut: str):
    """Date d'un article, quel que soit le format (RFC 822 côté RSS, ISO côté Atom)."""
    b = (brut or "").strip()
    if not b:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(b)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _nettoie(s: str, maxi: int = 220) -> str:
    """Texte lisible : balises retirées, entités décodées, espacement correct."""
    import html as _h
    t = re.sub(r"<[^>]+>", " ", s or "")
    t = re.sub(r"\s+", " ", _h.unescape(t)).strip()
    # Retirer une balise laisse une espace parasite avant la ponctuation
    # (« a Cupertino </b>. » → « a Cupertino . »). En français, seuls ; : ! ? »
    # prennent une espace insécable devant ; le point et la virgule, jamais.
    t = re.sub(r"\s+([.,)\]…])", r"\1", t)
    t = re.sub(r"\(\s+", "(", t)
    return t[:maxi]


def lire_flux(xml: bytes, source: str) -> list:
    """Articles d'un flux RSS 2.0 ou Atom. Ne lève jamais : un flux cassé en vaut zéro."""
    out = []
    try:
        racine = ET.fromstring(xml)
    except Exception as e:
        logger.warning(f"[actu] flux illisible ({source}) : {str(e)[:80]}")
        return out
    # RSS : channel/item — Atom : entry (avec espace de noms)
    articles = racine.findall(".//item") or racine.findall(".//{http://www.w3.org/2005/Atom}entry")
    for a in articles:
        titre = _texte(a, "title", "{http://www.w3.org/2005/Atom}title")
        if not titre:
            continue
        lien = _texte(a, "link", "{http://www.w3.org/2005/Atom}link", "guid")
        resume = _nettoie(_texte(a, "description", "summary",
                                 "{http://www.w3.org/2005/Atom}summary",
                                 "{http://www.w3.org/2005/Atom}content"))
        date = _quand(_texte(a, "pubDate", "published", "updated",
                             "{http://www.w3.org/2005/Atom}published",
                             "{http://www.w3.org/2005/Atom}updated",
                             "{http://purl.org/dc/elements/1.1/}date"))
        out.append({"titre": _nettoie(titre, 180), "lien": lien,
                    "resume": resume, "date": date, "source": source})
    return out


def _cle_titre(t: str) -> str:
    """Deux médias reprennent souvent la même dépêche : on ne la garde qu'une fois.

    Les accents sont RETIRÉS : les médias écrivent « dévoile » ou « devoile », et la
    même dépêche ressortait deux fois dans la liste.
    """
    import unicodedata
    sans = unicodedata.normalize("NFD", (t or "").lower())
    sans = "".join(c for c in sans if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", sans)[:60]


def recuperer(question: str, maxi: int = 8, fin: float = 0.0) -> list:
    """Articles récents sur le thème de la question. Liste vide si rien n'est joignable."""
    import concurrent.futures
    import requests

    theme = theme_de(question)
    sources = FLUX.get(theme, []) + ([] if theme == "general" else FLUX["general"][:1])

    def un(src):
        nom, url = src
        if fin and time.monotonic() >= fin:
            return []
        try:
            r = requests.get(url, timeout=TIMEOUT_FLUX, headers={"User-Agent": _UA})
            if r.status_code != 200:
                return []
            return lire_flux(r.content, nom)
        except Exception as e:
            logger.info(f"[actu] {nom} injoignable : {type(e).__name__}")
            return []

    articles = []
    # En parallèle : le temps total est celui du flux le plus lent, pas la somme.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(sources) or 1)) as ex:
        for lot in ex.map(un, sources):
            articles.extend(lot)

    limite = datetime.now(timezone.utc) - timedelta(hours=FRAICHEUR_H)
    frais = [a for a in articles if a["date"] and a["date"] >= limite]
    # Si aucun média n'a publié dans les 48 h (nuit, week-end, flux sans date),
    # on rend quand même les plus récents plutôt que rien.
    retenus = frais or [a for a in articles if a["date"]] or articles
    retenus.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    vus, propres = set(), []
    for a in retenus:
        c = _cle_titre(a["titre"])
        if c in vus:
            continue
        vus.add(c)
        propres.append(a)
        if len(propres) >= maxi:
            break
    return propres


def formater(articles: list, theme: str = "") -> str:
    """Mise en forme identique à celle de la recherche web (titres, source, date, lien)."""
    if not articles:
        return ""
    t = f" {theme}" if theme and theme != "general" else ""
    lignes = [f"📰 **Actualité{t} — {len(articles)} articles récents**\n"]
    for i, a in enumerate(articles, 1):
        quand = a["date"].strftime("%d/%m %Hh%M") if a.get("date") else ""
        meta = " · ".join(x for x in (a.get("source", ""), quand) if x)
        lignes.append(f"**{i}. {a['titre']}**" + (f"\n_{meta}_" if meta else "")
                      + (f"\n{a['resume']}" if a.get("resume") else "")
                      + (f"\n🔗 {a['lien']}" if a.get("lien") else ""))
    return "\n\n".join(lignes)
