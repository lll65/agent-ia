from plugins.base import Plugin


class WebSearchPlugin(Plugin):
    name = "search_web"
    description = "Recherche sur internet via DuckDuckGo. Renvoie les 5 premiers résultats."
    parameters = {"query": {"type": "string", "description": "Requête de recherche", "required": True}}

    def run(self, query: str, max_results: int = 5) -> str:
        from duckduckgo_search import DDGS
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
        except Exception as e:
            return f"Recherche indisponible: {e}"
        if not results:
            return "Aucun résultat trouvé."
        lines = []
        for r in results:
            title = str(r.get("title", "")).replace("<", "&lt;").replace(">", "&gt;")
            body = str(r.get("body", "")).replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"**{title}**\n{body}\nURL: {r.get('href', '')}")
        return "\n\n".join(lines)
