"""
Agent Creator — le Master Agent s'auto-complète.
Si une demande nécessite des agents ou plugins inexistants, il les crée.
"""
import json
import logging
import re
import uuid
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """Tu es un architecte de systèmes IA.

Requête utilisateur: "{request}"

Agents actuels: {agents}
Plugins actuels: {plugins}

Détermine si des agents spécialisés ou des plugins manquent.

Réponds UNIQUEMENT en JSON:
{{
  "analysis": "explication courte de ce qui manque",
  "needs_new_agents": true,
  "new_agents": [
    {{
      "role": "finance_analyst",
      "name": "FinanceAgent Pro",
      "system_prompt": "Tu es un analyste financier expert. Tu analyses les marchés boursiers avec des données réelles. Tu fournis des recommandations achat/vente basées sur l'analyse technique et fondamentale. Tu expliques clairement tes raisonnements.",
      "tools": ["analyze_stock", "compare_stocks", "get_market_news", "search_web"]
    }}
  ],
  "needs_new_plugins": false,
  "new_plugins": []
}}

Domaines couverts à créer si manquants:
- finance_analyst: analyse boursière, crypto, investissement, trading, recommandations achat/vente
- crypto_analyst: cryptomonnaies spécifiques, DeFi, NFT, on-chain analytics
- fullstack_dev: projets web complets, React, Vue, FastAPI, Node, Docker
- data_scientist: analyse de données, ML, pandas, scikit-learn, visualisation
- marketing_expert: copywriting, SEO, campagnes, growth hacking, réseaux sociaux
- legal_assistant: contrats, RGPD, analyse juridique simple
- health_coach: nutrition, sport, bien-être, régimes
- game_developer: jeux web HTML5/JS, logique de jeu, canvas, physics
- youtube_creator: scripts YouTube SEO, miniatures, watch time, monétisation
- ecommerce_expert: Shopify, dropshipping, fiches produit, publicité Facebook/TikTok
- automation_expert: scripts d'automatisation, Selenium, APIs REST, cron jobs
- writer: articles SEO, ebooks, études de cas, newsletters, ghostwriting
- copywriter: landing pages, emails marketing, scripts de vente, AIDA/PAS
- video_creator: scripts TikTok/Reels/Shorts, hooks 3s, rétention, CTAs
- seo_expert: audit technique SEO, mots-clés, clusters de contenu, netlinking
- analyst: KPIs business, tableaux de bord, rapports stratégiques, benchmarks

Génère uniquement ce qui est VRAIMENT manquant pour traiter la requête.
Si plugin inexistant mais difficile (ex: scraping temps réel), utilise search_web à la place."""


PLUGIN_PROMPT = """Crée le code Python d'un plugin pour l'agent IA.

Plugin: {name}
Description: {description}
Utilisation: {usage}

Structure OBLIGATOIRE:
```python
from plugins.base import Plugin

class {class_name}(Plugin):
    name = "{plugin_name}"
    description = "{description}"
    parameters = {{
        "param1": {{"type": "string", "description": "...", "required": True}},
    }}

    def run(self, param1: str) -> str:
        # Implémentation complète
        ...
        return result_str
```

Génère du code Python complet, fonctionnel, sans dépendances exotiques.
Utilise requests, json, pathlib, datetime si besoin.
Réponds UNIQUEMENT avec le code Python (pas de markdown)."""


async def ensure_agents_for_request(request: str, auto_create: bool = True) -> dict:
    """
    Analyse la requête, crée les agents/plugins manquants si nécessaire.
    Retourne {"created": [...], "analysis": str, "plan": dict}
    """
    from llm.client import chat
    from plugins import get_loader
    from orchestrator import get_registry
    import asyncio

    loader = get_loader()
    registry = get_registry()

    existing_plugins = list(loader.list_all().keys())
    existing_agents = [f"{a.get('role','?')}({a.get('name','?')})" for a in registry.list_all()]

    loop = asyncio.get_event_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: chat(
            [
                {"role": "system", "content": "Tu es un architecte IA. Réponds UNIQUEMENT en JSON valide."},
                {"role": "user", "content": ANALYSIS_PROMPT.format(
                    request=request,
                    agents=existing_agents or ["(aucun)"],
                    plugins=existing_plugins,
                )},
            ],
            temperature=0.2,
        ))
    except Exception as e:
        logger.error(f"[AgentCreator] analyse échouée: {e}")
        return {"created": [], "analysis": str(e)}

    plan = _extract_json(raw)
    if not plan:
        return {"created": [], "analysis": "parse JSON échoué"}

    analysis = plan.get("analysis", "")
    created = []

    if not auto_create:
        return {"created": [], "analysis": analysis, "plan": plan}

    # Crée les nouveaux plugins demandés
    for plugin_def in plan.get("new_plugins", []):
        name = plugin_def.get("name", "")
        desc = plugin_def.get("description", "")
        if not name or not desc:
            continue

        class_name = "".join(p.capitalize() for p in name.replace("-", "_").split("_")) + "Plugin"
        usage = plugin_def.get("usage", desc)

        logger.info(f"[AgentCreator] Création plugin: {name}")
        try:
            code_raw = await loop.run_in_executor(None, lambda: chat(
                [
                    {"role": "system", "content": "Tu génères du code Python propre et fonctionnel."},
                    {"role": "user", "content": PLUGIN_PROMPT.format(
                        name=name, description=desc, usage=usage,
                        class_name=class_name, plugin_name=name,
                    )},
                ],
                temperature=0.2,
            ))
            code = _extract_code(code_raw)
            if not code:
                continue

            plugin_path = Path(config.PLUGINS_DIR) / f"{name}.py"
            plugin_path.parent.mkdir(parents=True, exist_ok=True)
            plugin_path.write_text(code, encoding="utf-8")

            loader.reload_user_plugins()
            if name in loader.list_all():
                logger.info(f"[AgentCreator] Plugin '{name}' chargé ✓")
                created.append(f"plugin:{name}")
            else:
                logger.warning(f"[AgentCreator] Plugin '{name}' compilé mais pas chargé")
                plugin_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"[AgentCreator] plugin {name}: {e}")

    # Recharge les plugins
    loader.reload_user_plugins()
    all_tools = list(loader.list_all().keys())

    # Crée les nouveaux agents
    for agent_def in plan.get("new_agents", []):
        role = agent_def.get("role", "generic")
        name = agent_def.get("name", f"Agent {role}")
        tools = agent_def.get("tools") or all_tools
        # Filtre les outils qui existent réellement
        tools = [t for t in tools if t in all_tools] or all_tools

        cfg = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "role": role,
            "objective": agent_def.get("description", f"Agent spécialisé {role}"),
            "system_prompt": agent_def.get("system_prompt", f"Tu es un expert {role}."),
            "tools": tools,
            "model": config.LLM_MODEL,
            "status": "idle",
        }
        registry.register(cfg)
        logger.info(f"[AgentCreator] Agent créé: {name} (rôle: {role})")
        created.append(f"agent:{name}")

    return {"created": created, "analysis": analysis, "plan": plan}


def _extract_json(text: str) -> dict | None:
    for pat in [r"```json\s*([\s\S]+?)\s*```", r"```\s*([\s\S]+?)\s*```"]:
        m = re.search(pat, text)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


def _extract_code(text: str) -> str:
    for pat in [r"```python\s*([\s\S]+?)\s*```", r"```\s*([\s\S]+?)\s*```"]:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return text.strip() if "class " in text and "Plugin" in text else ""
