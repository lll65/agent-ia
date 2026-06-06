from plugins.base import Plugin


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _esc(s) -> str:
    return str(s).replace("<", "&lt;").replace(">", "&gt;")


class WebSearchPlugin(Plugin):
    name = "search_web"
    description = "Recherche web via DuckDuckGo (texte ou actualités). Résultats numérotés avec source et résumé."
    parameters = {
        "query": {"type": "string", "description": "Requête de recherche", "required": True},
        "mode": {"type": "string", "description": "Mode: web (défaut) | news (actualités récentes)", "required": False},
    }

    def run(self, query: str, max_results: int = 6, mode: str = "web") -> str:
        from duckduckgo_search import DDGS

        # Retry léger : DDGS est parfois capricieux (rate-limit / backend)
        results, last_err = [], None
        for attempt in range(3):
            try:
                with DDGS() as ddgs:
                    if mode == "news":
                        results = list(ddgs.news(query, max_results=max_results, region="fr-fr"))
                    else:
                        results = list(ddgs.text(query, max_results=max_results, region="fr-fr"))
                if results:
                    break
            except Exception as e:
                last_err = e
                import time
                time.sleep(1.5 * (attempt + 1))

        if not results:
            return f"Recherche indisponible: {last_err}" if last_err else "Aucun résultat trouvé."

        header = f"🔎 **{'Actualités' if mode == 'news' else 'Recherche'} : {_esc(query)}** — {len(results)} résultats\n"
        lines = [header]
        for i, r in enumerate(results, 1):
            title = _esc(r.get("title", ""))
            body = _esc(r.get("body") or r.get("excerpt") or "")
            url = r.get("href") or r.get("url") or ""
            src = _domain(url) or _esc(r.get("source", ""))
            date = r.get("date", "")
            meta = f" · {src}" if src else ""
            meta += f" · {date[:10]}" if date else ""
            lines.append(f"**{i}. {title}**{meta}\n{body}\n🔗 {url}")
        return "\n\n".join(lines)
