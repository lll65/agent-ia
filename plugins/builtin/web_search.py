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
                if arts:
                    return formater(arts, theme_de(query))
            except Exception as e:
                pass    # média injoignable : on retombe sur les moteurs, jamais d'échec

        results = _tavily(query, max_results, mode)
        if not results and time.monotonic() < fin:
            # Même en mode actualité : l'endpoint HTML reste le repli le plus fiable
            # depuis un serveur, et la requête est déjà datée.
            results = _ddg_html(query, max_results, fin=fin)
        if not results and time.monotonic() < fin:
            results = _ddg_lib(query, max_results, mode)

        if not results:
            return (f"⚠️ Aucun résultat exploitable pour « {query} » "
                    "(moteur de recherche momentanément indisponible). "
                    "Réponds honnêtement que la donnée n'a pas pu être vérifiée.")

        head = f"🔎 **Résultats web : {query}** ({len(results)})\n"
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
