"""
Master Agent — cerveau central du système.

Reçoit un objectif complexe et:
1. Le décompose en sous-tâches via LLM
2. Crée les sous-agents spécialisés nécessaires
3. Orchestre leur exécution (séquentielle ou parallèle)
4. Synthétise les résultats
"""
import asyncio
import json
import logging
import httpx
from datetime import datetime

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
  "synthesis_instruction": "Comment combiner les résultats en réponse finale"
}}

Règles:
- Maximum 5 sous-tâches
- parallel=true si la tâche peut tourner en même temps que les autres
- instruction = ce que l'agent doit faire concrètement
"""

SYNTHESIS_PROMPT = """Tu es le coordinateur final. Synthétise les résultats de plusieurs agents.

Objectif original: {goal}
Instruction de synthèse: {synthesis_instruction}

Résultats des agents:
{results}

Génère une réponse finale complète, structurée et utile pour l'utilisateur.
"""


class MasterAgent:
    def __init__(self):
        self.registry = AgentRegistry()
        self._loader = get_loader()
        self._memory = get_memory()

    async def execute(self, goal: str, session_id: str = "master") -> dict:
        """Point d'entrée principal: reçoit un goal, retourne le résultat orchestré."""
        logger.info(f"[Master] Nouveau goal: {goal[:80]}")

        # 1. Décomposer en tâches
        plan = await self._decompose(goal)
        if not plan:
            # Fallback: exécuter directement avec l'agent par défaut
            logger.warning("[Master] Décomposition échouée, exécution directe.")
            result = await run_agent(goal, self._default_agent_config(), session_id)
            return {"goal": goal, "mode": "direct", "answer": result["answer"], "steps": result["steps"]}

        tasks = plan.get("tasks", [])
        synthesis_instruction = plan.get("synthesis_instruction", "Synthétise les résultats.")

        # 2. Créer les sous-agents
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

        # 3. Exécuter les tâches
        results = await self._execute_tasks(agent_configs, session_id)

        # 4. Synthétiser
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
        prompt = DECOMPOSE_PROMPT.format(goal=goal)
        try:
            import ollama
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, lambda: ollama.chat(
                model=config.OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": "Tu es un architecte de systèmes IA. Tu réponds UNIQUEMENT en JSON valide."},
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": 0.3},
            )["message"]["content"])
            json_match = re.search(r"\{[\s\S]+\}", raw)
            if json_match:
                return json.loads(json_match.group())
        except Exception as e:
            logger.warning(f"[Master] Décomposition échouée: {e}")
        return None

    async def _execute_tasks(self, agent_configs: list[dict], session_id: str) -> list[dict]:
        results = []
        parallel_batch = []
        sequential = []

        for cfg in agent_configs:
            if cfg.get("parallel"):
                parallel_batch.append(cfg)
            else:
                sequential.append(cfg)

        # Exécution parallèle
        if parallel_batch:
            tasks = [
                run_agent(cfg["instruction"], cfg, f"{session_id}_{cfg['id']}")
                for cfg in parallel_batch
            ]
            parallel_results = await asyncio.gather(*tasks, return_exceptions=True)
            for cfg, res in zip(parallel_batch, parallel_results):
                if isinstance(res, Exception):
                    answer = f"Erreur: {res}"
                    self.registry.update_status(cfg["id"], "error", answer)
                else:
                    answer = res.get("answer", "")
                    self.registry.update_status(cfg["id"], "done", answer)
                results.append({"agent_id": cfg["id"], "role": cfg["role"], "objective": cfg["objective"], "result": answer})

        # Exécution séquentielle
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

    async def _synthesize(self, goal: str, instruction: str, results: list[dict]) -> str:
        results_text = "\n\n".join(
            f"=== Agent {r['role']} ({r['objective']}) ===\n{r['result']}"
            for r in results
        )
        prompt = SYNTHESIS_PROMPT.format(
            goal=goal,
            synthesis_instruction=instruction,
            results=results_text,
        )
        try:
            import ollama
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: ollama.chat(
                model=config.OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": "Tu es un expert en synthèse. Tu produis des réponses complètes et structurées."},
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": 0.5},
            )["message"]["content"].strip())
        except Exception as e:
            logger.error(f"[Master] Synthèse échouée: {e}")
            return "\n\n".join(r["result"] for r in results)

    def _default_agent_config(self) -> dict:
        from plugins import get_loader
        return {
            "id": "default",
            "name": "Agent Principal",
            "system_prompt": "Tu es un assistant IA polyvalent.",
            "tools": list(get_loader().list_all().keys()),
            "model": config.OLLAMA_MODEL,
        }
