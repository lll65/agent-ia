"""
Recherche web — robuste et sans clé.

La librairie `duckduckgo_search` renvoie souvent des résultats hors-sujet
(backend cassé / rate-limit). On interroge donc en PREMIER l'endpoint HTML
DuckDuckGo directement (fiable, région FR), avec la librairie en repli.
"""
import html as _html
import os
import re
import time
from urllib.parse import unquote

from plugins.base import Plugin

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _strip(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _extrait(s: str, maxi: int = 300) -> str:
    """Résumé d'un résultat, débarrassé de ses répétitions.

    Certaines pages (podcasts, agrégateurs) répètent le même paragraphe 3 fois : la
    même description occupait 2 000 caractères du contexte du modèle, chassant les
    résultats suivants et le poussant à répondre à côté.
    """
    t = re.sub(r"\s+", " ", _strip(s))
    if not t:
        return ""
    vues, garde = set(), []
    for phrase in re.split(r"(?<=[.!?])\s+|\s*\[\.\.\.\]\s*", t):
        p = phrase.strip()
        if len(p) < 3:
            continue
        cle = re.sub(r"[^\wÀ-ÿ]", "", p.lower())[:70]
        if cle in vues:
            continue
        vues.add(cle)
        garde.append(p)
        if sum(len(x) for x in garde) >= maxi:
            break
    return " ".join(garde)[:maxi].strip()


# ⏱️ Budget d'UNE recherche. Les délais s'empilaient (Tavily 20 s + deux hôtes
# DuckDuckGo à 15 s + la librairie), et l'agent rejouait le tout : une seule question
# pouvait engloutir tout le temps imparti et finir sur « la recherche a pris trop de
# temps » alors que des résultats étaient déjà là.
BUDGET_RECHERCHE = float(os.getenv("SEARCH_BUDGET", "22"))
TIMEOUT_HOTE = 8


# ── Ancrage géographique ──────────────────────────────────────────────────────
# « quelles sont les taxes ? » a rendu les tranches fiscales AMÉRICAINES : « 10 %
# à 37 % », « 545 500 $ », « Net Investment Income Tax ». Une réponse impeccable —
# pour quelqu'un qui vit aux États-Unis. Deux causes, toutes les deux réparées ici.
#
# 1. DuckDuckGo était réglé sur fr-fr ; Tavily, interrogé EN PREMIER, sur rien du
#    tout. Encore une correction posée d'un côté et pas sur son miroir.
# 2. Régler le moteur ne suffit pas : « taxes trading 2026 » remonte des pages
#    américaines même en fr-fr, simplement parce que ce sont elles qui existent.
#    Sur ces sujets-là, la réponse CHANGE avec le pays — impôts, droit, âge légal,
#    aides, salaire. Il faut donc le dire dans la requête, pas seulement l'espérer.
#
# Une réponse juste pour un autre pays est plus dangereuse qu'une absence de
# réponse : elle a l'air complète, elle est sourcée, et elle est fausse.
PAYS = os.getenv("NOVA_PAYS", "France")
PAYS_TAVILY = os.getenv("NOVA_PAYS_TAVILY", "france")     # Tavily attend un nom en minuscules

_SUJETS_NATIONAUX = re.compile(
    r"\b(imp[oô]ts?|imposition|fiscal\w*|taxes?|tax|tva|pr[ée]l[eè]vements?|flat\s*tax|"
    r"plus-values?|cotisations?|urssaf|auto-?entrepreneur|micro-?entreprise|"
    r"loi|l[ée]gal\w*|l[ée]gislation|r[ée]glementation|juridique|interdit\w*|autoris[ée]\w*|"
    r"majorit[ée]|mineur\w*|[ée]mancip\w*|"
    r"allocations?|aides?|caf|apl|rsa|crous|retraite|ch[oô]mage|"
    r"salaires?|smic|remboursements?|s[ée]curit[ée]\s+sociale|mutuelle|"
    r"permis|passeport|visa|amendes?)\b", re.I)

# Si le pays est déjà nommé, on ne le contredit pas : « fiscalité en Belgique »
# doit rester une question belge.
_PAYS_DEJA = re.compile(
    r"\b(france|fran[cç]ais\w*|belgi\w+|suisse|canada|qu[ée]b[eé]\w+|luxembourg|"
    r"usa|u\.s\.|[ée]tats-unis|am[ée]ricain\w*|uk|royaume-uni|angleterre|"
    r"allemagne|espagne|italie|portugal|maroc|europe|europ[ée]en\w*)\b", re.I)


def _ancre_pays(query: str, pays: str = "") -> str:
    """Ajoute le pays quand la réponse en dépend et qu'aucun pays n'est nommé."""
    q = (query or "").strip()
    if not q or not _SUJETS_NATIONAUX.search(q) or _PAYS_DEJA.search(q):
        return q
    return f"{q} {pays or PAYS}"


def _ddg_html(query: str, max_results: int, region: str = "fr-fr",
              fin: float = 0.0) -> list[dict]:
    """Interroge l'endpoint HTML DuckDuckGo (pas d'API key, résultats fiables)."""
    import requests
    out: list[dict] = []
    for host in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        if fin and time.monotonic() >= fin:
            break
        try:
            r = requests.post(host, data={"q": query, "kl": region},
                              headers={"User-Agent": _UA, "Referer": "https://duckduckgo.com/"},
                              timeout=TIMEOUT_HOTE)
            if r.status_code != 200:
                continue
            txt = r.text
            # Titres + liens (html/ endpoint)
            for m in re.finditer(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', txt, re.DOTALL):
                href, title = m.group(1), _strip(m.group(2))
                ud = re.search(r'uddg=([^&"]+)', href)
                url = unquote(ud.group(1)) if ud else href
                if title and url.startswith("http"):
                    out.append({"title": title, "href": url, "body": ""})
                if len(out) >= max_results:
                    break
            # Snippets
            snips = re.findall(r'result__snippet[^>]*>(.*?)</a>', txt, re.DOTALL)
            for i, s in enumerate(snips[:len(out)]):
                out[i]["body"] = _extrait(s)
            if out:
                return out
        except Exception:
            continue
    return out


def _tavily(query: str, max_results: int, mode: str = "web") -> list[dict]:
    """Recherche via Tavily (fiable depuis un serveur, ~1000/mois gratuit). Clé requise."""
    try:
        from config import config
        key = getattr(config, "TAVILY_API_KEY", "")
    except Exception:
        key = ""
    if not key:
        return []
    try:
        import requests
        charge = {"api_key": key, "query": query, "max_results": max_results,
                  "search_depth": "basic"}
        if mode == "news":                       # Tavily sait cibler l'actualité récente
            charge.update(topic="news", days=3)
        else:
            # ⚠️ C'est ICI que les tranches fiscales américaines entraient. DuckDuckGo
            # était localisé, Tavily non — et Tavily passe en premier.
            charge.update(topic="general", country=PAYS_TAVILY)
        r = requests.post("https://api.tavily.com/search", json=charge, timeout=12)
        if r.status_code != 200 and "country" in charge:
            # Un paramètre que l'API refuserait ne doit pas faire disparaître la
            # recherche entière : on retente sans, plutôt que de rendre zéro résultat.
            charge.pop("country", None)
            charge.pop("topic", None)
            r = requests.post("https://api.tavily.com/search", json=charge, timeout=12)
        if r.status_code != 200:
            return []
        data = r.json().get("results", [])
        return [{"title": x.get("title", ""), "href": x.get("url", ""),
                 "body": x.get("content", "")} for x in data]
    except Exception:
        return []


def _ddg_lib(query: str, max_results: int, mode: str) -> list[dict]:
    """Repli via la librairie (nouveau paquet `ddgs`, sinon `duckduckgo_search`)."""
    try:
        try:
            from ddgs import DDGS            # paquet successeur (recommandé)
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as d:
            if mode == "news":
                return list(d.news(query, max_results=max_results, region="fr-fr"))
            return list(d.text(query, max_results=max_results, region="fr-fr"))
    except Exception:
        return []


class WebSearchPlugin(Plugin):
    name = "search_web"
    description = "Recherche web (DuckDuckGo, région FR, sans clé). Résultats numérotés avec source, résumé et lien."
    parameters = {
        "query": {"type": "string", "description": "Requête de recherche", "required": True},
        "mode": {"type": "string", "description": "web (défaut) | news (actualités)", "required": False},
    }

    def run(self, query: str, max_results: int = 6, mode: str = "web") -> str:
        query = (query or "").strip()
        if not query:
            return "Aucune requête fournie."

        # 1) Tavily si clé (fiable depuis un serveur). 2) HTML DuckDuckGo. 3) librairie.
        # Chaque repli n'est tenté que s'il reste du temps : mieux vaut rendre les
        # résultats d'un moteur que d'épuiser le budget à en interroger trois.
        fin = time.monotonic() + BUDGET_RECHERCHE

        # ── Actualité : les flux RSS des médias AVANT tout moteur ──────────────
        # Un moteur interroge le web par mots-clés ; il remonte donc les pages qui
        # contiennent « actualité technologie <date> » — souvent des communiqués. Les
        # flux des médias sont datés, triés par fraîcheur et forcément sur le sujet.
        if mode == "news":
            try:
                from plugins.builtin.actu_rss import recuperer, formater, theme_de
                arts = recuperer(query, max_results, fin)
                # ⚠️ « Valneva cours euro 1er septembre 2026 » rendait « Deux
                # ex-directeurs du groupe Orpea bientôt jugés à Paris pour délit
                # d'initié ». Les flux sont interrogés par THÈME (bourse), jamais par
                # société : sans ce filtre, on sert les titres du jour comme s'ils
                # répondaient à la question. Des articles hors sujet présentés comme la
                # réponse, c'est pire que pas de réponse — il croit que c'est ça,
                # l'actualité de sa valeur.
                from agent.entites import filtre_sur_sujet, entites_citees
                noms = entites_citees(query)
                if arts and noms:
                    sur_sujet = filtre_sur_sujet(arts, noms)
                    if sur_sujet:
                        return formater(sur_sujet, theme_de(query))
                    # Rien qui parle d'elle : on laisse la main aux moteurs, qui
                    # cherchent par mots-clés. Ne surtout PAS rendre le reste.
                    arts = []
                if arts:
                    return formater(arts, theme_de(query))
            except Exception as e:
                pass    # média injoignable : on retombe sur les moteurs, jamais d'échec

        # Le pays n'est ajouté que pour les MOTEURS : les flux RSS ci-dessus sont
        # filtrés par entité citée, et un « France » ajouté deviendrait une entité
        # à chercher dans les titres.
        q = _ancre_pays(query)

        results = _tavily(q, max_results, mode)
        if not results and time.monotonic() < fin:
            # Même en mode actualité : l'endpoint HTML reste le repli le plus fiable
            # depuis un serveur, et la requête est déjà datée.
            results = _ddg_html(q, max_results, fin=fin)
        if not results and time.monotonic() < fin:
            results = _ddg_lib(q, max_results, mode)

        if not results:
            return (f"⚠️ Aucun résultat exploitable pour « {q} » "
                    "(moteur de recherche momentanément indisponible). "
                    "Réponds honnêtement que la donnée n'a pas pu être vérifiée.")

        # La requête affichée est celle qui a VRAIMENT été envoyée : s'il voit
        # « … France » il sait pourquoi les sources sont françaises.
        head = f"🔎 **Résultats web : {q}** ({len(results)})\n"
        lines = [head]
        for i, r in enumerate(results, 1):
            title = _strip(r.get("title", ""))
            body = _extrait(r.get("body") or r.get("excerpt") or "")
            url = r.get("href") or r.get("url") or ""
            src = _domain(url) or r.get("source", "")
            date = (r.get("date", "") or "")[:10]
            meta = " · ".join(x for x in (src, date) if x)
            lines.append(f"**{i}. {title}**" + (f"\n_{meta}_" if meta else "")
                         + (f"\n{body}" if body else "") + (f"\n🔗 {url}" if url else ""))
        return "\n\n".join(lines)
