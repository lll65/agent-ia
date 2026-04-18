"""
Master Agent — cerveau central.
Reçoit un objectif, le décompose, crée les sous-agents, orchestre, synthétise.
"""
import asyncio
import json
import logging
import re
from config import config
from orchestrator.factory import generate_agent_config
from orchestrator.registry import AgentRegistry
from agent.core import run_agent
from plugins import get_loader
from memory import get_memory

logger = logging.getLogger(__name__)


DECOMPOSE_PROMPT = """Tu reçois un objectif complexe. Décompose-le en sous-tâches.

Objectif: {goal}

Réponds UNIQUEMENT en JSON valide, format exact:
{{
  "tasks": [
    {{"role": "researcher|coder|video_creator|analyst|writer|generic", "objective": "...", "instruction": "...", "parallel": false}},
    ...
  ],
  "synthesis_instruction": "Comment combiner les résultats"
}}

Règles: maximum 5 sous-tâches, parallel=true si la tâche peut tourner en parallèle.
"""

SYNTHESIS_PROMPT = """Tu es le coordinateur final. Synthétise les résultats de plusieurs agents.

Objectif original: {goal}
Instruction: {synthesis_instruction}

Résultats:
{results}

Génère une réponse finale complète et structurée.
"""


class MasterAgent:
    def __init__(self):
        self.registry = AgentRegistry()

    async def execute(self, goal: str, session_id: str = "master") -> dict:
        logger.info(f"[Master] Goal: {goal[:80]}")

        plan = await self._decompose(goal)
        if not plan:
            logger.warning("[Master] Décomposition échouée, exécution directe.")
            result = await run_agent(goal, self._default_config(), session_id)
            return {"goal": goal, "mode": "direct", "answer": result["answer"], "steps": result["steps"]}

        tasks = plan.get("tasks", [])
        synthesis_instruction = plan.get("synthesis_instruction", "Synthétise les résultats.")

        agent_configs = []
        for task in tasks:
            cfg = await generate_agent_config(
                role=task.get("role", "generic"),
                objective=task.get("objective", goal),
            )
            cfg["instruction"] = task.get("instruction", task.get("objective", goal))
            cfg["parallel"] = task.get("parallel", False)
            agent_configs.append(cfg)
            self.registry.register(cfg)

        results = await self._execute_tasks(agent_configs, session_id)
        final = await self._synthesize(goal, synthesis_instruction, results)

        return {
            "goal": goal,
            "mode": "orchestrated",
            "tasks_count": len(tasks),
            "agents": [{"id": c["id"], "role": c["role"], "objective": c["objective"]} for c in agent_configs],
            "results": results,
            "answer": final,
        }

    async def _decompose(self, goal: str) -> dict | None:
        from llm.client import chat
        prompt = DECOMPOSE_PROMPT.format(goal=goal)
        try:
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, lambda: chat(
                [
                    {"role": "system", "content": "Tu es un architecte IA. Tu réponds UNIQUEMENT en JSON valide."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            ))
            match = re.search(r"\{[\s\S]+\}", raw)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.warning(f"[Master] Décomposition échouée: {e}")
        return None

    async def _execute_tasks(self, agent_configs: list, session_id: str) -> list:
        results = []
        parallel = [c for c in agent_configs if c.get("parallel")]
        sequential = [c for c in agent_configs if not c.get("parallel")]

        if parallel:
            tasks = [run_agent(c["instruction"], c, f"{session_id}_{c['id']}") for c in parallel]
            for cfg, res in zip(parallel, await asyncio.gather(*tasks, return_exceptions=True)):
                answer = f"Erreur: {res}" if isinstance(res, Exception) else res.get("answer", "")
                self.registry.update_status(cfg["id"], "error" if isinstance(res, Exception) else "done", answer)
                results.append({"agent_id": cfg["id"], "role": cfg["role"], "objective": cfg["objective"], "result": answer})

        for cfg in sequential:
            self.registry.update_status(cfg["id"], "running")
            try:
                res = await run_agent(cfg["instruction"], cfg, f"{session_id}_{cfg['id']}")
                answer = res.get("answer", "")
                self.registry.update_status(cfg["id"], "done", answer)
            except Exception as e:
                answer = f"Erreur: {e}"
                self.registry.update_status(cfg["id"], "error", answer)
            results.append({"agent_id": cfg["id"], "role": cfg["role"], "objective": cfg["objective"], "result": answer})

        return results

    async def _synthesize(self, goal: str, instruction: str, results: list) -> str:
        from llm.client import chat
        results_text = "\n\n".join(
            f"=== Agent {r['role']} ({r['objective']}) ===\n{r['result']}" for r in results
        )
        prompt = SYNTHESIS_PROMPT.format(goal=goal, synthesis_instruction=instruction, results=results_text)
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: chat(
                [
                    {"role": "system", "content": "Tu produis des réponses complètes et structurées."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
            ))
        except Exception as e:
            logger.error(f"[Master] Synthèse échouée: {e}")
            return "\n\n".join(r["result"] for r in results)

    def _default_config(self) -> dict:
        return {
            "id": "default",
            "name": "MasterAgent-Gros",
            "system_prompt": "Tu es un assistant IA polyvalent et puissant.",
            "tools": list(get_loader().list_all().keys()),
            "model": config.LLM_MODEL,
        }
