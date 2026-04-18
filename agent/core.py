"""
Moteur de l'agent — boucle ReAct (Raisonnement + Action) avec:
 - Mémoire longue durée (ChromaDB)
 - Auto-résumé au seuil configuré
 - Self-healing sur les outils
 - Système de plugins extensible
"""
import json
import re
import logging
import httpx
from config import config

logger = logging.getLogger(__name__)

SYSTEM_TEMPLATE = """Tu es {name}.
{description}

Tu as accès aux outils suivants (plugins):
{tools_list}

PROTOCOLE D'ACTION — réponds TOUJOURS dans ce format exact:
┌─ Pour utiliser un outil:
│  THOUGHT: <ta réflexion>
│  ACTION: <nom_outil>
│  PARAMS: {{"param1": "valeur1"}}
│
└─ Pour donner la réponse finale:
   THOUGHT: <synthèse finale>
   FINAL: <ta réponse complète à l'utilisateur>

Règles:
- Utilise les outils pour tout ce qui nécessite des informations externes
- Sois précis et concis dans FINAL
- Langue: français sauf demande contraire
- Si un outil échoue, essaie une approche alternative
"""


def build_system(agent_config: dict, plugins: dict) -> str:
    available = agent_config.get("tools") or list(plugins.keys())
    tools_list = "\n".join(
        f"  • {name}: {desc}"
        for name, desc in plugins.items()
        if name in available
    )
    return SYSTEM_TEMPLATE.format(
        name=agent_config.get("name", "Agent IA"),
        description=agent_config.get("system_prompt", "Tu es un assistant polyvalent."),
        tools_list=tools_list or "  (aucun outil disponible)",
    )


async def llm_call(prompt: str, system: str, model: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.7, "num_ctx": 4096},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")


def parse_response(text: str) -> tuple[str | None, dict | None, str | None]:
    """
    Parse la réponse LLM.
    Returns: (action_name, params, final_answer) — un seul non-None à la fois.
    """
    # FINAL
    final_m = re.search(r"FINAL:\s*(.+)", text, re.DOTALL | re.IGNORECASE)
    if final_m:
        return None, None, final_m.group(1).strip()

    # ACTION + PARAMS
    action_m = re.search(r"ACTION:\s*(\w+)", text, re.IGNORECASE)
    params_m = re.search(r"PARAMS:\s*(\{.+?\})", text, re.DOTALL | re.IGNORECASE)
    if action_m:
        action = action_m.group(1).strip()
        params = {}
        if params_m:
            try:
                params = json.loads(params_m.group(1))
            except json.JSONDecodeError:
                # Essaie de réparer le JSON basique
                raw = params_m.group(1).replace("'", '"')
                try:
                    params = json.loads(raw)
                except Exception:
                    pass
        return action, params, None

    # Pas de format reconnu → traiter comme réponse finale
    return None, None, text.strip()


async def run_agent(
    task: str,
    agent_config: dict,
    agent_id: str = "default",
    plugin_loader=None,
    memory_manager=None,
) -> dict:
    from plugins import get_loader
    from memory import get_memory
    from agent.self_heal import safe_tool_call, health_monitor
    from memory.summarizer import summarize_messages

    loader = plugin_loader or get_loader()
    mem = memory_manager or get_memory()
    model = agent_config.get("model", config.OLLAMA_MODEL)
    system = build_system(agent_config, loader.list_all())

    # Auto-résumé si la mémoire est trop longue
    if mem.should_summarize(agent_id):
        recent = mem.recall_recent(agent_id, limit=config.SUMMARY_THRESHOLD)
        try:
            summary = await summarize_messages(recent, model)
            mem.cache_summary(agent_id, summary)
            logger.info(f"[{agent_id}] Mémoire résumée automatiquement.")
        except Exception as e:
            logger.warning(f"Auto-résumé échoué: {e}")

    # Construction du prompt avec contexte mémoriel
    context = mem.build_context(agent_id, task, recent_limit=8)
    prompt = f"{context}\n\nUSER: {task}" if context else f"USER: {task}"

    mem.remember(agent_id, "user", task)

    steps = []
    for iteration in range(config.MAX_ITERATIONS):
        try:
            llm_out = await llm_call(prompt, system, model)
        except httpx.ConnectError:
            return {
                "answer": "Ollama n'est pas démarré. Lance 'ollama serve' dans un terminal.",
                "steps": steps,
                "iterations": iteration,
                "error": "ollama_offline",
            }
        except Exception as e:
            return {"answer": f"Erreur LLM: {e}", "steps": steps, "iterations": iteration, "error": str(e)}

        step = {"iteration": iteration + 1, "llm_output": llm_out}
        action, params, final = parse_response(llm_out)

        if final:
            steps.append(step)
            mem.remember(agent_id, "assistant", final)
            return {"answer": final, "steps": steps, "iterations": iteration + 1}

        if action:
            observation = safe_tool_call(loader, action, params or {})
            success = not observation.startswith("[Self-heal]") and "Erreur" not in observation
            health_monitor.record(action, success)

            step["action"] = action
            step["params"] = params
            step["observation"] = observation
            steps.append(step)

            prompt = (
                f"{prompt}\n\n"
                f"ASSISTANT:\n{llm_out}\n\n"
                f"OBSERVATION [{action}]:\n{observation}\n\n"
                f"Continue (utilise un autre outil ou donne FINAL):"
            )
        else:
            steps.append(step)
            mem.remember(agent_id, "assistant", llm_out)
            return {"answer": llm_out, "steps": steps, "iterations": iteration + 1}

    last = steps[-1].get("llm_output", "Limite d'itérations atteinte.")
    mem.remember(agent_id, "assistant", last)
    return {"answer": last, "steps": steps, "iterations": config.MAX_ITERATIONS}
