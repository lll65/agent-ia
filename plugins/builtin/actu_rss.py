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

# Mots de la question → thème de flux à interroger.
# ⚠️ La comparaison se fait sur des MOTS ENTIERS. Chercher « ia » en sous-chaîne classait
# « le procès de la mafia » et « on passe via Lyon » en actualité tech.
_THEMES = (
    ("tech", ("tech", "technologie", "technologique", "numérique", "numerique", "informatique",
              "ia", "intelligence artificielle", "smartphone", "apple", "google",
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
    """Quel thème d'actualité la question vise-t-elle ? (comparaison par mots entiers)"""
    m = (question or "").lower()
    for nom, mots in _THEMES:
        for k in mots:
            if re.search(r"(?<![\wÀ-ÿ])" + re.escape(k) + r"(?![\wÀ-ÿ])", m):
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


def _lien_article(el) -> str:
    """Adresse de l'ARTICLE, pas celle de ses commentaires.

    En Atom un <entry> porte plusieurs <link> : rel="replies" (commentaires),
    rel="self", rel="alternate" (l'article). On prenait le premier venu — les liens
    rendus pointaient donc parfois vers la page de commentaires.
    """
    ATOM = "{http://www.w3.org/2005/Atom}link"
    liens = el.findall(ATOM)
    for attendu in ("alternate", None):
        for l in liens:
            rel = l.attrib.get("rel")
            if rel == attendu or (attendu is None and not rel):
                href = (l.attrib.get("href") or "").strip()
                if href:
                    return href
    # RSS : <link> simple. <guid> ne sert de lien que s'il est un permalien.
    for chemin in ("link", "{http://purl.org/rss/1.0/}link"):
        n = el.find(chemin)
        if n is not None and (n.text or "").strip():
            return n.text.strip()
    g = el.find("guid")
    if g is not None and (g.text or "").strip().startswith("http") \
            and g.attrib.get("isPermaLink", "true").lower() != "false":
        return g.text.strip()
    return liens[0].attrib.get("href", "").strip() if liens else ""


def lire_flux(xml: bytes, source: str) -> list:
    """Articles d'un flux RSS 2.0 ou Atom. Ne lève jamais : un flux cassé en vaut zéro."""
    out = []
    try:
        racine = ET.fromstring(xml)
    except Exception as e:
        logger.warning(f"[actu] flux illisible ({source}) : {str(e)[:80]}")
        return out
    # RSS 2.0 : <item> — RSS 1.0/RDF : <item> dans l'espace purl.org — Atom : <entry>.
    # Sans le cas RDF, ces flux étaient lus comme « zéro article », en silence.
    articles = (racine.findall(".//item")
                or racine.findall(".//{http://purl.org/rss/1.0/}item")
                or racine.findall(".//{http://www.w3.org/2005/Atom}entry"))
    for a in articles:
        titre = _texte(a, "title", "{http://purl.org/rss/1.0/}title",
                       "{http://www.w3.org/2005/Atom}title")
        if not titre:
            continue
        lien = _lien_article(a)
        resume = _nettoie(_texte(a, "description", "{http://purl.org/rss/1.0/}description", "summary",
                                 "{http://www.w3.org/2005/Atom}summary",
                                 "{http://www.w3.org/2005/Atom}content"))
        date = _quand(_texte(a, "pubDate", "published", "updated",
                             "{http://www.w3.org/2005/Atom}published",
                             "{http://www.w3.org/2005/Atom}updated",
                             "{http://purl.org/dc/elements/1.1/}date"))
        out.append({"titre": _nettoie(titre, 180), "lien": lien,
                    "resume": resume, "date": date, "source": source, "theme": ""})
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
    # ⚠️ On n'interroge QUE les médias du thème demandé. Ajouter un flux généraliste
    # « au cas où » paraissait prudent, mais il publie en continu : au tri par date ses
    # articles politiques évinçaient tous les articles tech. Constaté en production —
    # « résume l'actu tech » ramenait les Houthis et des affaires judiciaires.
    sources = FLUX.get(theme) or FLUX["general"]

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

    def lire_tout(liste):
        if not liste:
            return []
        got = []
        # En parallèle : le temps total est celui du flux le plus lent, pas la somme.
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(6, len(liste)))) as ex:
            for lot in ex.map(un, liste):
                got.extend(lot)
        return got

    articles = lire_tout(sources)
    theme_reel = theme
    # Aucun média du thème n'a répondu (panne, blocage) : mieux vaut l'actualité générale
    # que rien du tout. Mais seulement EN DERNIER RECOURS, jamais en mélange.
    if not articles and theme != "general":
        logger.info(f"[actu] aucun média « {theme} » joignable → repli sur l'actualité générale")
        articles = lire_tout(FLUX["general"])
        theme_reel = "general"      # ⚠️ ne JAMAIS étiqueter « tech » de l'actualité générale
    for a in articles:
        a["theme"] = theme_reel

    maintenant = datetime.now(timezone.utc)
    limite = maintenant - timedelta(hours=FRAICHEUR_H)
    # Une date dans le futur (fuseau mal déclaré, coquille du média) plaçait l'article
    # en tête de liste devant la vraie actualité du jour. On la ramène à maintenant.
    for a in articles:
        if a["date"] and a["date"] > maintenant + timedelta(hours=6):
            a["date"] = maintenant
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


_THEME_FR = {"tech": " tech", "science": " scientifique", "economie": " économique",
             "sport": " sportive", "general": ""}


def formater(articles: list, theme: str = "") -> str:
    """Mise en forme identique à celle de la recherche web (titres, source, date, lien).

    L'étiquette dit ce que la liste CONTIENT vraiment : le thème réellement interrogé
    (pas celui demandé, si on a dû se replier) et « récents » seulement si ça l'est.
    """
    if not articles:
        return ""
    # Le thème porté par les articles fait foi ; le paramètre n'est qu'un repli.
    reel = next((a.get("theme") for a in articles if a.get("theme")), None) or theme
    t = _THEME_FR.get(reel, f" {reel}" if reel and reel != "general" else "")
    limite = datetime.now(timezone.utc) - timedelta(hours=FRAICHEUR_H)
    dates = [a["date"] for a in articles if a.get("date")]
    recents = bool(dates) and all(d >= limite for d in dates)
    if recents:
        entete = f"📰 **Actualité{t} — {len(articles)} articles récents**"
    elif dates:
        vieux = min(dates).strftime("%d/%m")
        entete = (f"📰 **Actualité{t} — {len(articles)} articles**\n"
                  f"_Rien de neuf dans les dernières {FRAICHEUR_H} h : voici les plus "
                  f"récents trouvés, le plus ancien datant du {vieux}._")
    else:
        entete = f"📰 **Actualité{t} — {len(articles)} articles (dates non fournies)**"
    lignes = [entete + "\n"]
    for i, a in enumerate(articles, 1):
        quand = a["date"].strftime("%d/%m %Hh%M") if a.get("date") else ""
        meta = " · ".join(x for x in (a.get("source", ""), quand) if x)
        lignes.append(f"**{i}. {a['titre']}**" + (f"\n_{meta}_" if meta else "")
                      + (f"\n{a['resume']}" if a.get("resume") else "")
                      + (f"\n🔗 {a['lien']}" if a.get("lien") else ""))
    return "\n\n".join(lignes)
