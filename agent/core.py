"""
Moteur de l'agent — boucle ReAct avec mémoire ChromaDB et plugins.
"""
import json
import re
import logging
import asyncio
from config import config

logger = logging.getLogger(__name__)

SYSTEM_TEMPLATE = """Tu es {name}.
{description}

Outils disponibles:
{tools_list}

PROTOCOLE — réponds TOUJOURS dans ce format:
Pour utiliser un outil:
  THOUGHT: ta réflexion
  ACTION: nom_outil
  PARAMS: {{"param1": "valeur1"}}

Pour donner la réponse finale:
  THOUGHT: synthèse
  FINAL: ta réponse complète

Règles: utilise les outils pour chercher/créer, réponds en français.
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
        tools_list=tools_list or "  (aucun outil)",
    )


def _ollama_call(messages: list, model: str) -> str:
    import ollama
    client = ollama.Client(timeout=120)
    response = client.chat(
        model=model,
        messages=messages,
        options={"temperature": 0.7, "num_ctx": 2048},
    )
    return response["message"]["content"]


async def llm_call(messages: list, model: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _ollama_call(messages, model))


def parse_response(text: str) -> tuple[str | None, dict | None, str | None]:
    final_m = re.search(r"FINAL:\s*(.+)", text, re.DOTALL | re.IGNORECASE)
    if final_m:
        return None, None, final_m.group(1).strip()

    action_m = re.search(r"ACTION:\s*(\w+)", text, re.IGNORECASE)
    params_m = re.search(r"PARAMS:\s*(\{.+?\})", text, re.DOTALL | re.IGNORECASE)
    if action_m:
        action = action_m.group(1).strip()
        params = {}
        if params_m:
            try:
                params = json.loads(params_m.group(1))
            except json.JSONDecodeError:
                try:
                    params = json.loads(params_m.group(1).replace("'", '"'))
                except Exception:
                    pass
        return action, params, None

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

    if mem.should_summarize(agent_id):
        recent = mem.recall_recent(agent_id, limit=config.SUMMARY_THRESHOLD)
        try:
            summary = await summarize_messages(recent, model)
            mem.cache_summary(agent_id, summary)
        except Exception as e:
            logger.warning(f"Auto-résumé échoué: {e}")

    context = mem.build_context(agent_id, task, recent_limit=8)

    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "assistant", "content": f"[Contexte mémoriel]\n{context}"})
    messages.append({"role": "user", "content": task})

    mem.remember(agent_id, "user", task)

    steps = []
    for iteration in range(config.MAX_ITERATIONS):
        try:
            llm_out = await llm_call(messages, model)
        except Exception as e:
            return {"answer": f"Ollama non disponible: {e}\n\nAssure-toi qu'Ollama est démarré et que le modèle est installé avec: ollama pull llama3.2", "steps": steps, "iterations": iteration, "error": str(e)}

        step = {"iteration": iteration + 1, "llm_output": llm_out}
        action, params, final = parse_response(llm_out)

        if final:
            steps.append(step)
            mem.remember(agent_id, "assistant", final)
            return {"answer": final, "steps": steps, "iterations": iteration + 1}

        if action:
            observation = safe_tool_call(loader, action, params or {})
            health_monitor.record(action, "Erreur" not in observation)
            step["action"] = action
            step["params"] = params
            step["observation"] = observation
            steps.append(step)
            messages.append({"role": "assistant", "content": llm_out})
            messages.append({"role": "user", "content": f"OBSERVATION [{action}]: {observation}\n\nContinue ou donne FINAL:"})
        else:
            steps.append(step)
            mem.remember(agent_id, "assistant", llm_out)
            return {"answer": llm_out, "steps": steps, "iterations": iteration + 1}

    last = steps[-1].get("llm_output", "Limite atteinte.")
    mem.remember(agent_id, "assistant", last)
    return {"answer": last, "steps": steps, "iterations": config.MAX_ITERATIONS}
