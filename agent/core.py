"""
Moteur ReAct — Reasoning + Acting loop avec mémoire et plugins.
"""
import json
import re
import logging
import asyncio
from config import config

logger = logging.getLogger(__name__)

# Import du système prompt maître
try:
    from agent.system_prompt import MASTER_SYSTEM_PROMPT as _MASTER_SYS
except ImportError:
    _MASTER_SYS = ""

SYSTEM_TEMPLATE = """{master_directives}

---

## AGENT ACTIF : {name}
{description}

## OUTILS DISPONIBLES :
{tools_list}

## PROTOCOLE D'ACTION STRICT

Pour utiliser un outil, réponds EXACTEMENT dans ce format :
THOUGHT: [analyse en une phrase — pourquoi cet outil, quelles données tu attends]
ACTION: [nom_exact_de_l_outil]
PARAMS: {{"param": "valeur"}}

Pour donner la réponse finale (quand tu as toutes les informations nécessaires) :
THOUGHT: [synthèse — que vas-tu livrer]
FINAL: [réponse complète, structurée, actionnelle]

## RÈGLES D'EXÉCUTION
1. N'invente JAMAIS une observation — attends toujours l'OBSERVATION réelle de l'outil
2. Utilise tes outils dès qu'ils peuvent fournir des données concrètes — n'improvise pas ce que tu peux mesurer
3. Finance : inclus TOUJOURS zone d'entrée + TP1/TP2 + stop-loss + ratio R/R dans les analyses
4. FINAL doit être une réponse directement exploitable — chiffrée, structurée, plan d'action inclus
5. Jamais "je ne peux pas" — si bloqué, explique ET propose une alternative
"""


def build_system(agent_config: dict, plugins: dict) -> str:
    available  = agent_config.get("tools") or list(plugins.keys())
    tools_list = "\n".join(
        f"  • {name}: {desc}"
        for name, desc in plugins.items()
        if name in available
    )
    return SYSTEM_TEMPLATE.format(
        master_directives=_MASTER_SYS,
        name=agent_config.get("name", "Agent IA"),
        description=agent_config.get("system_prompt", "Tu es un assistant polyvalent."),
        tools_list=tools_list or "  (aucun outil disponible)",
    )


async def llm_call(messages: list, model: str = None, temperature: float = 0.7) -> str:
    from llm.client import chat
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: chat(messages, temperature=temperature))


def _temperature_for_role(agent_config: dict) -> float:
    """Température adaptée au rôle. Finance = déterministe (0.2) pour des chiffres fiables."""
    role = (agent_config.get("role") or "").lower()
    if role in ("finance_analyst", "crypto_analyst") or "finance" in role:
        return 0.2
    return 0.7


def parse_response(text: str) -> tuple:
    """Extrait (action, params, final) depuis la réponse LLM."""
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
            except Exception:
                try:
                    params = json.loads(params_m.group(1).replace("'", '"'))
                except Exception:
                    pass
        return action, params, None

    # Si aucun format reconnu → traiter comme réponse finale
    return None, None, text.strip()


_STUB_KEYWORDS = (
    "attendre", "en cours", "analyse en cours", "à analyser", "dépend",
    "consulter un professionnel", "je ne peux pas", "indisponible",
    "je n'ai pas accès", "données manquantes", "impossible de",
    "attentes les résultats", "attends les résultats",
)

def _is_stub_answer(text: str, tool_calls_made: int) -> bool:
    """Détecte une réponse paresseuse : le LLM répond sans avoir utilisé ses outils."""
    if tool_calls_made > 0:
        return False
    t = text.lower()
    if any(kw in t for kw in _STUB_KEYWORDS):
        return True
    # Réponse trop courte sans aucun chiffre = probablement générique
    if len(text.strip()) < 400 and not any(c.isdigit() for c in text):
        return True
    return False


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
    system = build_system(agent_config, loader.list_all())
    temperature = _temperature_for_role(agent_config)

    # Auto-résumé si historique long
    if mem.should_summarize(agent_id):
        recent = mem.recall_recent(agent_id, limit=config.SUMMARY_THRESHOLD)
        try:
            summary = await summarize_messages(recent)
            mem.cache_summary(agent_id, summary)
        except Exception as e:
            logger.warning(f"Auto-résumé échoué: {e}")

    # Injecte les leçons apprises si disponibles
    try:
        from agent.self_improve import get_improvement_context
        domain = agent_config.get("role", "general")
        lessons = get_improvement_context(domain=domain, max_lessons=4)
        if lessons:
            system = system + f"\n\n{lessons}"
    except Exception:
        pass

    context = mem.build_context(agent_id, task, recent_limit=6)
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "assistant", "content": f"[Contexte mémoriel]\n{context}"})

    # Injecter le rappel outil dans le message utilisateur si l'agent a des outils requis
    required_tools = agent_config.get("tools") or []
    task_msg = task
    if required_tools:
        task_msg += (
            f"\n\n[INSTRUCTION SYSTÈME: Tu as accès à {len(required_tools)} outil(s). "
            f"Commence TOUJOURS par utiliser tes outils pour obtenir des données réelles "
            f"avant de répondre. Premier outil disponible: {required_tools[0]}]"
        )
    messages.append({"role": "user", "content": task_msg})
    mem.remember(agent_id, "user", task)

    steps = []
    tool_calls_made = 0
    stub_retries = 0
    MAX_STUB_RETRIES = 2

    iteration = 0
    while iteration < config.MAX_ITERATIONS:
        try:
            llm_out = await llm_call(messages, temperature=temperature)
        except Exception as e:
            err = f"LLM indisponible: {e}"
            logger.error(err)
            return {"answer": err, "steps": steps, "iterations": iteration, "error": str(e)}

        step = {"iteration": iteration + 1, "llm_output": llm_out}
        action, params, final = parse_response(llm_out)

        # ── Détection réponse paresseuse (aucun outil appelé, réponse vague) ──
        response_text = final or llm_out
        if (final or (not action)) and required_tools and stub_retries < MAX_STUB_RETRIES:
            if _is_stub_answer(response_text, tool_calls_made):
                stub_retries += 1
                first_tool = required_tools[0]
                logger.warning(f"[core] Réponse stub iter {iteration+1} — forçage outil '{first_tool}' (retry {stub_retries}/{MAX_STUB_RETRIES})")
                messages.append({"role": "assistant", "content": llm_out})
                messages.append({"role": "user", "content": (
                    f"⛔ ERREUR : Tu as répondu sans utiliser tes outils. C'est interdit.\n"
                    f"Tu DOIS d'abord appeler '{first_tool}' pour avoir des données RÉELLES.\n"
                    f"Réponds MAINTENANT en utilisant ce format exact:\n"
                    f"THOUGHT: Je vais récupérer les données réelles avec {first_tool}\n"
                    f"ACTION: {first_tool}\n"
                    f"PARAMS: {{\"ticker\": \"...\"}}"
                )})
                # Ne pas incrémenter iteration — rejouer sans compter comme une itération normale
                continue

        if final:
            steps.append(step)
            mem.remember(agent_id, "assistant", final)
            return {"answer": final, "steps": steps, "iterations": iteration + 1}

        if action:
            observation = safe_tool_call(loader, action, params or {})
            health_monitor.record(action, "Erreur" not in observation)
            tool_calls_made += 1
            step["action"] = action
            step["params"] = params
            step["observation"] = observation[:500]
            steps.append(step)
            messages.append({"role": "assistant", "content": llm_out})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [{action}]: {observation}\n\n"
                f"Continue ton analyse. Si tu as toutes les données nécessaires, "
                f"donne ta réponse FINAL complète et chiffrée:"
            )})
        else:
            steps.append(step)
            mem.remember(agent_id, "assistant", llm_out)
            return {"answer": llm_out, "steps": steps, "iterations": iteration + 1}

        iteration += 1

    last = steps[-1].get("llm_output", "Limite d'itérations atteinte.") if steps else "Limite d'itérations atteinte."
    mem.remember(agent_id, "assistant", last)
    return {"answer": last, "steps": steps, "iterations": config.MAX_ITERATIONS}


def _extract_thought(llm_out: str) -> str:
    """Extrait la section THOUGHT: d'une sortie LLM."""
    m = re.search(r"THOUGHT:\s*(.+?)(?=\n(?:ACTION|FINAL|PARAMS):|$)", llm_out, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip()[:200] if m else ""


async def run_agent_stream(
    task: str,
    agent_config: dict,
    agent_id: str = "default",
    plugin_loader=None,
    memory_manager=None,
):
    """
    Async generator — même logique que run_agent mais yield chaque étape ReAct.
    Permet d'afficher le raisonnement en temps réel dans l'UI.

    Yields dicts:
      {"type": "thought",      "text": str, "iteration": int}
      {"type": "action",       "tool": str, "params": dict, "iteration": int}
      {"type": "observation",  "tool": str, "result": str,  "iteration": int}
      {"type": "final",        "answer": str, "iterations": int}
    """
    from plugins import get_loader
    from memory import get_memory
    from agent.self_heal import safe_tool_call, health_monitor
    from memory.summarizer import summarize_messages

    loader = plugin_loader or get_loader()
    mem    = memory_manager or get_memory()
    system = build_system(agent_config, loader.list_all())
    temperature = _temperature_for_role(agent_config)

    if mem.should_summarize(agent_id):
        recent = mem.recall_recent(agent_id, limit=config.SUMMARY_THRESHOLD)
        try:
            summary = await summarize_messages(recent)
            mem.cache_summary(agent_id, summary)
        except Exception:
            pass

    try:
        from agent.self_improve import get_improvement_context
        domain  = agent_config.get("role", "general")
        lessons = get_improvement_context(domain=domain, max_lessons=4)
        if lessons:
            system = system + f"\n\n{lessons}"
    except Exception:
        pass

    context  = mem.build_context(agent_id, task, recent_limit=6)
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "assistant", "content": f"[Contexte mémoriel]\n{context}"})

    required_tools = agent_config.get("tools") or []
    task_msg = task
    if required_tools:
        task_msg += (
            f"\n\n[INSTRUCTION SYSTÈME: Tu as accès à {len(required_tools)} outil(s). "
            f"Commence TOUJOURS par utiliser tes outils. Premier outil: {required_tools[0]}]"
        )
    messages.append({"role": "user", "content": task_msg})
    mem.remember(agent_id, "user", task)

    tool_calls_made = 0
    stub_retries    = 0

    for iteration in range(config.MAX_ITERATIONS):
        try:
            llm_out = await llm_call(messages, temperature=temperature)
        except Exception as e:
            yield {"type": "final", "answer": f"❌ LLM indisponible: {e}", "iterations": iteration}
            return

        thought = _extract_thought(llm_out)
        action, params, final = parse_response(llm_out)

        # Stub detection
        response_text = final or llm_out
        if (final or not action) and required_tools and stub_retries < 2:
            if _is_stub_answer(response_text, tool_calls_made):
                stub_retries += 1
                first_tool = required_tools[0]
                messages.append({"role": "assistant", "content": llm_out})
                messages.append({"role": "user", "content": (
                    f"⛔ Tu as répondu sans outil. Utilise '{first_tool}' maintenant.\n"
                    f"THOUGHT: Je vais récupérer les données réelles\nACTION: {first_tool}\nPARAMS: {{}}"
                )})
                continue

        if thought:
            yield {"type": "thought", "text": thought, "iteration": iteration + 1}

        if final:
            mem.remember(agent_id, "assistant", final)
            yield {"type": "final", "answer": final, "iterations": iteration + 1}
            return

        if action:
            yield {"type": "action", "tool": action, "params": params or {}, "iteration": iteration + 1}
            observation = safe_tool_call(loader, action, params or {})
            health_monitor.record(action, "Erreur" not in observation)
            tool_calls_made += 1
            yield {"type": "observation", "tool": action, "result": observation[:400], "iteration": iteration + 1}
            messages.append({"role": "assistant", "content": llm_out})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [{action}]: {observation}\n\n"
                "Continue. Si tu as toutes les données, donne ta réponse FINAL:"
            )})
        else:
            mem.remember(agent_id, "assistant", llm_out)
            yield {"type": "final", "answer": llm_out, "iterations": iteration + 1}
            return

    last = f"⚠️ Limite de {config.MAX_ITERATIONS} itérations atteinte."
    mem.remember(agent_id, "assistant", last)
    yield {"type": "final", "answer": last, "iterations": config.MAX_ITERATIONS}
