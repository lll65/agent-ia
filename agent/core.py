"""
Moteur ReAct — Reasoning + Acting loop avec mémoire et plugins.
"""
import json
import re
import logging
import asyncio
from config import config

logger = logging.getLogger(__name__)

# Directive compacte (le MASTER complet ~2500 tokens dépassait la limite Groq 12k tok/min)
try:
    from agent.system_prompt import AGENT_COMPACT_DIRECTIVE as _MASTER_SYS
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
1. N'invente JAMAIS une observation — attends toujours l'OBSERVATION réelle de l'outil.
2. QUESTION FACTUELLE (actualité, tendances, marché, prix, événements récents, "en 2026", chiffres réels,
   idées/analyses qui dépendent du contexte actuel) : ta PREMIÈRE action DOIT être `search_web`.
   N'exécute PAS de code Python pour "inventer" des données qui devraient venir du web.
3. ANTI-HALLUCINATION : ne cite JAMAIS une source, une date ou un chiffre précis sans qu'un OUTIL te l'ait
   réellement renvoyé. Sans appel d'outil correspondant → écris "estimation non vérifiée".
4. SUJET RESPECTÉ : ne parle de bourse, actions, ETF, crypto, marchés, investissement ou épargne QUE si
   l'utilisateur le demande explicitement. N'utilise le format financier (entrée/TP/stop-loss) que pour
   l'analyse d'un actif précis réellement demandée. Ne change jamais de sujet de toi-même.
5. LONGUEUR PROPORTIONNELLE : remarque simple ou question courte → réponse courte (1-3 phrases), sans titres
   ni plan d'action. Réserve les réponses structurées aux vraies demandes complexes.
6. FINAL directement exploitable. Jamais "je ne peux pas" sans alternative.
7. CHIFFRES DE MARCHÉ : un cours, un indice (CAC 40, S&P 500), un prix de crypto ou une statistique ne
   peuvent JAMAIS sortir de ta mémoire. Sans OBSERVATION d'outil correspondante, tu n'en cites aucun.
8. DONNÉES PERSONNELLES (agenda, événements, mails, contacts, fichiers, messages) : elles ne peuvent venir
   QUE d'un outil (connected_app). Si aucun OUTIL ne te les a réellement renvoyées, ou si l'outil a échoué,
   tu DOIS le dire clairement ("je n'ai pas pu accéder à…"). Inventer un agenda, un mail ou un rendez-vous
   est une faute GRAVE et strictement interdite — même si le résultat semble plausible.
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
    """Température adaptée au rôle. Factuel/finance = déterministe pour limiter les hallucinations."""
    role = (agent_config.get("role") or "").lower()
    if role in ("finance_analyst", "crypto_analyst") or "finance" in role:
        return 0.2
    if agent_config.get("force_search"):
        return 0.25  # questions factuelles/actu → basse température = moins d'inventions
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

def _is_stub_answer(text: str, tool_calls_made: int, needs_tools: bool = False) -> bool:
    """Détecte une réponse VRAIMENT paresseuse : le LLM se défausse alors qu'un outil était requis.

    ⚠️ Historique : l'ancienne heuristique « réponse < 400 caractères sans chiffre = stub »
    forçait un appel d'outil sur TOUTE réponse courte, ce qui poussait le modèle à produire
    des pavés remplis de chiffres… inventés. Supprimée : une réponse courte est souvent
    la bonne réponse (« salut », « ok », une remarque personnelle).
    """
    if tool_calls_made > 0 or not needs_tools:
        return False
    t = text.lower()
    return any(kw in t for kw in _STUB_KEYWORDS)


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

    # Auto-résumé si historique long (mémoire non fatale)
    try:
        if mem.should_summarize(agent_id):
            recent = mem.recall_recent(agent_id, limit=config.SUMMARY_THRESHOLD)
            summary = await summarize_messages(recent)
            mem.cache_summary(agent_id, summary)
    except Exception as e:
        logger.warning(f"Auto-résumé/mémoire ignoré: {e}")

    # Injecte les leçons apprises si disponibles
    try:
        from agent.self_improve import get_improvement_context
        domain = agent_config.get("role", "general")
        lessons = get_improvement_context(domain=domain, max_lessons=4)
        if lessons:
            system = system + f"\n\n{lessons}"
    except Exception:
        pass

    try:
        context = mem.build_context(agent_id, task, recent_limit=6)
    except Exception as e:
        logger.warning(f"build_context ignoré: {e}")
        context = ""
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "assistant", "content": f"[Contexte mémoriel]\n{context}"})

    # Rappel outil UNIQUEMENT si la question réclame des données réelles (factuel/temps réel).
    # Auparavant ce rappel était injecté sur CHAQUE message → l'agent dégainait un outil même
    # pour « j'ai 17 ans », ce qui produisait des rapports hors-sujet.
    required_tools = agent_config.get("tools") or []
    task_msg = task
    if required_tools and agent_config.get("force_search"):
        task_msg += (
            f"\n\n[INSTRUCTION SYSTÈME: question factuelle → utilise tes outils pour obtenir des "
            f"données réelles avant de répondre. Outil suggéré : {required_tools[0]}]"
        )
    messages.append({"role": "user", "content": task_msg})
    try:
        mem.remember(agent_id, "user", task)
    except Exception as e:
        logger.warning(f"mem.remember ignoré: {e}")

    steps = []
    tool_calls_made = 0
    stub_retries = 0
    # Un outil n'est "requis" que si la question est factuelle/temps réel (force_search).
    needs_tools = bool(agent_config.get("force_search"))

    # Forçage déterministe de search_web sur les questions factuelles (idem run_agent_stream)
    if agent_config.get("force_search") and "search_web" in required_tools:
        try:
            obs = safe_tool_call(loader, "search_web", {"query": task[:200], "mode": "web"})
            steps.append({"type": "action", "tool": "search_web", "params": {"query": task[:120]}})
            steps.append({"type": "observation", "tool": "search_web", "result": obs[:400]})
            messages.append({"role": "assistant",
                             "content": f'ACTION: search_web\nPARAMS: {{"query": "{task[:120]}"}}'})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [search_web]: {obs[:1400]}\n\n"
                "Utilise UNIQUEMENT ces résultats réels pour répondre, en citant leurs sources.")})
            tool_calls_made += 1
        except Exception as e:
            logger.warning(f"[force_search] échec: {e}")
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
            if _is_stub_answer(response_text, tool_calls_made, needs_tools):
                stub_retries += 1
                first_tool = required_tools[0]
                logger.warning(f"[core] Réponse stub iter {iteration+1} — forçage outil '{first_tool}' (retry {stub_retries}/{MAX_STUB_RETRIES})")
                messages.append({"role": "assistant", "content": llm_out})
                messages.append({"role": "user", "content": (
                    f"Tu t'es défaussé alors qu'un outil pouvait répondre.\n"
                    f"Utilise '{first_tool}' avec des paramètres adaptés à la question :\n"
                    f"THOUGHT: je récupère les données réelles avec {first_tool}\n"
                    f"ACTION: {first_tool}\n"
                    f"PARAMS: {{...}}"
                )})
                # Ne pas incrémenter iteration — rejouer sans compter comme une itération normale
                continue

        if final:
            steps.append(step)
            mem.remember(agent_id, "assistant", final[:350])
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
                f"OBSERVATION [{action}]: {observation[:1200]}\n\n"
                f"Continue. Si tu as les informations nécessaires, donne ta réponse FINAL "
                f"— en te basant UNIQUEMENT sur les observations réelles ci-dessus :"
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
    if required_tools and agent_config.get("force_search"):
        task_msg += (
            f"\n\n[INSTRUCTION SYSTÈME: question factuelle → utilise tes outils pour des données "
            f"réelles avant de répondre. Outil suggéré : {required_tools[0]}]"
        )
    messages.append({"role": "user", "content": task_msg})
    mem.remember(agent_id, "user", task)

    tool_calls_made = 0
    stub_retries    = 0
    needs_tools     = bool(agent_config.get("force_search"))

    # ── FORÇAGE DÉTERMINISTE DE search_web pour les questions factuelles ──────
    # On exécute une VRAIE recherche DuckDuckGo AVANT le 1er appel LLM et on injecte
    # l'observation → le modèle répond sur des données réelles (avec vraies sources),
    # il ne peut plus halluciner ni exécuter un script Python à la place.
    if agent_config.get("force_search") and "search_web" in required_tools:
        try:
            obs = safe_tool_call(loader, "search_web", {"query": task[:200], "mode": "web"})
            yield {"type": "action", "tool": "search_web", "params": {"query": task[:120]}, "iteration": 0}
            yield {"type": "observation", "tool": "search_web", "result": obs[:400], "iteration": 0}
            messages.append({"role": "assistant",
                             "content": f'THOUGHT: recherche web pour données réelles\nACTION: search_web\nPARAMS: {{"query": "{task[:120]}"}}'})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [search_web]: {obs[:1400]}\n\n"
                "Utilise UNIQUEMENT ces résultats réels pour répondre, en citant leurs sources. "
                "Toute source, DATE ou chiffre que tu cites DOIT apparaître mot pour mot dans ces "
                "résultats — interdiction absolue d'inventer un nom de média, une date ou un montant. "
                "Si l'info manque, dis clairement « je n'ai pas trouvé ».")})
            tool_calls_made += 1
        except Exception as e:
            logger.warning(f"[force_search] échec: {e}")

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
            if _is_stub_answer(response_text, tool_calls_made, needs_tools):
                stub_retries += 1
                first_tool = required_tools[0]
                messages.append({"role": "assistant", "content": llm_out})
                messages.append({"role": "user", "content": (
                    f"Un outil peut répondre. Utilise '{first_tool}' maintenant.\n"
                    f"THOUGHT: je récupère les données réelles\nACTION: {first_tool}\nPARAMS: {{}}"
                )})
                continue

        if thought:
            yield {"type": "thought", "text": thought, "iteration": iteration + 1}

        if final:
            mem.remember(agent_id, "assistant", final[:350])
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
                f"OBSERVATION [{action}]: {observation[:1200]}\n\n"
                "Continue. Si tu as toutes les données, donne ta réponse FINAL:"
            )})
        else:
            mem.remember(agent_id, "assistant", llm_out)
            yield {"type": "final", "answer": llm_out, "iterations": iteration + 1}
            return

    last = f"⚠️ Limite de {config.MAX_ITERATIONS} itérations atteinte."
    mem.remember(agent_id, "assistant", last)
    yield {"type": "final", "answer": last, "iterations": config.MAX_ITERATIONS}
