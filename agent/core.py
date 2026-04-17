"""
Moteur de l'agent — boucle ReAct (Raisonnement + Action).
L'agent pense, choisit un outil, l'exécute, observe le résultat, répète.
"""
import json
import re
import httpx
from config import config
from agent.tools import run_tool, AVAILABLE_TOOLS
from agent.memory import get_history, save_message

SYSTEM_TEMPLATE = """Tu es {name}.
{description}

Tu as accès aux outils suivants:
{tools_list}

Pour utiliser un outil, réponds EXACTEMENT dans ce format:
TOOL: <nom_outil>
PARAMS: {{"param1": "valeur1", "param2": "valeur2"}}

Quand tu as la réponse finale, réponds:
FINAL: <ta réponse complète>

Règles:
- Utilise les outils pour rechercher, créer des fichiers, exécuter du code
- FINAL doit être une réponse claire et complète
- Tu peux enchaîner plusieurs outils avant de donner FINAL
- Réponds toujours en français sauf si demandé autrement
"""


def build_system_prompt(agent_config: dict) -> str:
    tools_list = "\n".join(
        f"  - {t}: {AVAILABLE_TOOLS.get(t, 'outil personnalisé')}"
        for t in agent_config.get("tools", list(AVAILABLE_TOOLS.keys()))
    )
    return SYSTEM_TEMPLATE.format(
        name=agent_config.get("name", "Agent IA"),
        description=agent_config.get("system_prompt", ""),
        tools_list=tools_list,
    )


async def call_llm(prompt: str, system: str, model: str) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {"temperature": 0.7},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{config.OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "")


def parse_action(text: str) -> tuple[str | None, dict | None, str | None]:
    """Returns (tool_name, params, final_answer)."""
    final_match = re.search(r"FINAL:\s*(.+)", text, re.DOTALL)
    if final_match:
        return None, None, final_match.group(1).strip()

    tool_match = re.search(r"TOOL:\s*(\w+)", text)
    params_match = re.search(r"PARAMS:\s*(\{.+?\})", text, re.DOTALL)

    if tool_match:
        tool_name = tool_match.group(1).strip()
        params = {}
        if params_match:
            try:
                params = json.loads(params_match.group(1))
            except json.JSONDecodeError:
                pass
        return tool_name, params, None

    # Si pas de format reconnu, traiter comme réponse finale
    return None, None, text.strip()


async def run_agent(task: str, agent_config: dict, agent_id: str = "default") -> dict:
    system = build_system_prompt(agent_config)
    model = agent_config.get("model", config.OLLAMA_MODEL)

    history = get_history(agent_id, limit=10)
    context_parts = [f"{m['role'].upper()}: {m['content']}" for m in history]
    context_parts.append(f"USER: {task}")
    prompt = "\n\n".join(context_parts)

    save_message(agent_id, "user", task)

    steps = []
    for i in range(config.MAX_ITERATIONS):
        llm_response = await call_llm(prompt, system, model)
        steps.append({"iteration": i + 1, "llm_output": llm_response})

        tool_name, params, final = parse_action(llm_response)

        if final:
            save_message(agent_id, "assistant", final)
            return {"answer": final, "steps": steps, "iterations": i + 1}

        if tool_name:
            observation = run_tool(tool_name, params or {})
            steps[-1]["tool"] = tool_name
            steps[-1]["params"] = params
            steps[-1]["observation"] = observation
            prompt = f"{prompt}\n\nASSISTANT: {llm_response}\n\nOBSERVATION ({tool_name}): {observation}\n\nContinue avec la prochaine action ou donne FINAL:"
        else:
            save_message(agent_id, "assistant", llm_response)
            return {"answer": llm_response, "steps": steps, "iterations": i + 1}

    last = steps[-1].get("llm_output", "Limite d'itérations atteinte.")
    save_message(agent_id, "assistant", last)
    return {"answer": last, "steps": steps, "iterations": config.MAX_ITERATIONS}
