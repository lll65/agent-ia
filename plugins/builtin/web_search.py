"""
Recherche web — robuste et sans clé.

La librairie `duckduckgo_search` renvoie souvent des résultats hors-sujet
(backend cassé / rate-limit). On interroge donc en PREMIER l'endpoint HTML
DuckDuckGo directement (fiable, région FR), avec la librairie en repli.
"""
import html as _html
import re
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


def _ddg_html(query: str, max_results: int, region: str = "fr-fr") -> list[dict]:
    """Interroge l'endpoint HTML DuckDuckGo (pas d'API key, résultats fiables)."""
    import requests
    out: list[dict] = []
    for host in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            r = requests.post(host, data={"q": query, "kl": region},
                              headers={"User-Agent": _UA, "Referer": "https://duckduckgo.com/"},
                              timeout=15)
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
                out[i]["body"] = _strip(s)[:300]
            if out:
                return out
        except Exception:
            continue
    return out


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

        # 1) Endpoint HTML direct (fiable). 2) repli librairie.
        results = []
        if mode != "news":
            results = _ddg_html(query, max_results)
        if not results:
            results = _ddg_lib(query, max_results, mode)

        if not results:
            return (f"⚠️ Aucun résultat exploitable pour « {query} » "
                    "(moteur de recherche momentanément indisponible). "
                    "Réponds honnêtement que la donnée n'a pas pu être vérifiée.")

        head = f"🔎 **Résultats web : {query}** ({len(results)})\n"
        lines = [head]
        for i, r in enumerate(results, 1):
            title = _strip(r.get("title", ""))
            body = _strip(r.get("body") or r.get("excerpt") or "")
            url = r.get("href") or r.get("url") or ""
            src = _domain(url) or r.get("source", "")
            date = (r.get("date", "") or "")[:10]
            meta = " · ".join(x for x in (src, date) if x)
            lines.append(f"**{i}. {title}**" + (f"\n_{meta}_" if meta else "")
                         + (f"\n{body}" if body else "") + (f"\n🔗 {url}" if url else ""))
        return "\n\n".join(lines)
