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

logger = logging.getLogger(__name__)


DECOMPOSE_PROMPT = """Tu reçois un objectif complexe. Décompose-le en 2-4 sous-tâches concrètes.

Objectif: {goal}

Réponds UNIQUEMENT avec ce JSON (sans markdown, sans explication):
{{
  "tasks": [
    {{"role": "researcher", "objective": "...", "instruction": "...", "parallel": false}},
    {{"role": "coder", "objective": "...", "instruction": "...", "parallel": true}}
  ],
  "synthesis_instruction": "Combine les résultats en une réponse structurée"
}}

Rôles disponibles: researcher, coder, video_creator, analyst, writer, generic
parallel=true si la tâche est indépendante des autres."""


SYNTHESIS_PROMPT = """Tu es le coordinateur final. Synthétise les résultats de plusieurs agents spécialisés.

Objectif original: {goal}
Instruction de synthèse: {synthesis_instruction}

Résultats des agents:
{results}

Produis une réponse finale complète, bien structurée et directement utile. En français."""


def _extract_json(text: str) -> dict | None:
    """Extrait le JSON d'un texte potentiellement entouré de markdown."""
    # Essaie d'abord les blocs ```json
    for pattern in [r"```json\s*([\s\S]+?)\s*```", r"```\s*([\s\S]+?)\s*```"]:
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    # Sinon cherche le premier objet JSON
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


class MasterAgent:
    def __init__(self):
        self.registry = AgentRegistry()

    async def execute(self, goal: str, session_id: str = "master") -> dict:
        logger.info(f"[Master] Goal: {goal[:80]}")

        plan = await self._decompose(goal)
        if not plan or not plan.get("tasks"):
            logger.warning("[Master] Décomposition échouée, exécution directe.")
            result = await run_agent(goal, self._default_config(), session_id)
            return {"goal": goal, "mode": "direct", "answer": result["answer"], "steps": result.get("steps", [])}

        tasks = plan["tasks"][:5]
        synthesis_instruction = plan.get("synthesis_instruction", "Synthétise les résultats.")
        logger.info(f"[Master] Plan: {len(tasks)} tâches")

        agent_configs = []
        for task in tasks:
            try:
                cfg = await generate_agent_config(
                    role=task.get("role", "generic"),
                    objective=task.get("objective", goal),
                )
                cfg["instruction"] = task.get("instruction") or task.get("objective", goal)
                cfg["parallel"] = task.get("parallel", False)
                agent_configs.append(cfg)
                self.registry.register(cfg)
            except Exception as e:
                logger.warning(f"[Master] Config agent échouée: {e}")

        if not agent_configs:
            result = await run_agent(goal, self._default_config(), session_id)
            return {"goal": goal, "mode": "direct", "answer": result["answer"], "steps": []}

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
        try:
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, lambda: chat(
                [
                    {"role": "system", "content": "Tu es un architecte de systèmes IA. Tu réponds UNIQUEMENT en JSON valide, sans markdown, sans texte avant ou après."},
                    {"role": "user", "content": DECOMPOSE_PROMPT.format(goal=goal)},
                ],
                temperature=0.2,
            ))
            result = _extract_json(raw)
            if result:
                logger.info(f"[Master] JSON extrait: {len(result.get('tasks', []))} tâches")
            return result
        except Exception as e:
            logger.warning(f"[Master] Décomposition échouée: {e}")
        return None

    async def _execute_tasks(self, agent_configs: list, session_id: str) -> list:
        results = []
        parallel = [c for c in agent_configs if c.get("parallel")]
        sequential = [c for c in agent_configs if not c.get("parallel")]

        if parallel:
            coros = [run_agent(c["instruction"], c, f"{session_id}_{c['id']}") for c in parallel]
            raw_results = await asyncio.gather(*coros, return_exceptions=True)
            for cfg, res in zip(parallel, raw_results):
                if isinstance(res, Exception):
                    answer = f"Erreur: {res}"
                    self.registry.update_status(cfg["id"], "error", answer)
                else:
                    answer = res.get("answer", "")
                    self.registry.update_status(cfg["id"], "done", answer)
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
            f"=== {r['role'].upper()} ({r['objective'][:50]}) ===\n{r['result']}"
            for r in results
        )
        prompt = SYNTHESIS_PROMPT.format(
            goal=goal, synthesis_instruction=instruction, results=results_text
        )
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: chat(
                [
                    {"role": "system", "content": "Tu es un expert en synthèse. Tu produis des réponses complètes et structurées en français."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
            ))
        except Exception as e:
            logger.error(f"[Master] Synthèse échouée: {e}")
            return "\n\n".join(f"**{r['role']}**: {r['result']}" for r in results)

    def _default_config(self) -> dict:
        return {
            "id": "default",
            "name": "MasterAgent-Gros",
            "system_prompt": "Tu es un assistant IA polyvalent et puissant. Tu réponds en français.",
            "tools": list(get_loader().list_all().keys()),
            "model": config.LLM_MODEL,
        }
