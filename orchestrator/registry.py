"""
Registre de tous les agents — persistance JSON + cache en mémoire.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)


class AgentRegistry:
    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._dir = Path(config.AGENTS_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def _load_all(self):
        for f in self._dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                self._cache[data["id"]] = data
            except Exception as e:
                logger.warning(f"Agent non chargé ({f.name}): {e}")

    def _save(self, agent: dict):
        path = self._dir / f"{agent['id']}.json"
        path.write_text(json.dumps(agent, indent=2, ensure_ascii=False))

    def register(self, agent: dict) -> dict:
        agent["registered_at"] = datetime.utcnow().isoformat()
        self._cache[agent["id"]] = agent
        self._save(agent)
        return agent

    def get(self, agent_id: str) -> dict | None:
        return self._cache.get(agent_id)

    def list_all(self) -> list[dict]:
        return list(self._cache.values())

    def delete(self, agent_id: str) -> bool:
        if agent_id not in self._cache:
            return False
        del self._cache[agent_id]
        path = self._dir / f"{agent_id}.json"
        path.unlink(missing_ok=True)
        return True

    def update_status(self, agent_id: str, status: str, last_result: str = ""):
        if agent_id in self._cache:
            self._cache[agent_id]["status"] = status
            self._cache[agent_id]["last_active"] = datetime.utcnow().isoformat()
            if last_result:
                self._cache[agent_id]["last_result"] = last_result[:500]
            self._save(self._cache[agent_id])
