"""
Superviseur: surveille la santé des agents et génère des rapports.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from orchestrator.registry import AgentRegistry
from agent.self_heal import health_monitor

logger = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, registry: AgentRegistry):
        self.registry = registry
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self, interval_seconds: int = 60):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._monitor_loop(interval_seconds))
            logger.info("Superviseur démarré.")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _monitor_loop(self, interval: int):
        while self._running:
            await asyncio.sleep(interval)
            self._check_agents()

    def _check_agents(self):
        agents = self.registry.list_all()
        stale_threshold = datetime.utcnow() - timedelta(minutes=30)
        for agent in agents:
            last_active = agent.get("last_active")
            status = agent.get("status", "idle")
            if status == "running" and last_active:
                try:
                    last_dt = datetime.fromisoformat(last_active)
                    if last_dt < stale_threshold:
                        logger.warning(f"Agent '{agent['id']}' bloqué depuis >30min — reset.")
                        self.registry.update_status(agent["id"], "stale")
                except Exception:
                    pass

    def health_report(self) -> dict:
        agents = self.registry.list_all()
        tool_stats = health_monitor.report()

        status_counts = {}
        for a in agents:
            s = a.get("status", "idle")
            status_counts[s] = status_counts.get(s, 0) + 1

        critical_tools = {
            name: stats for name, stats in tool_stats.items()
            if float(stats.get("error_rate", "0%").rstrip("%")) > 50
        }

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "agents": {
                "total": len(agents),
                "by_status": status_counts,
            },
            "tools": tool_stats,
            "alerts": {
                "high_error_rate_tools": critical_tools,
                "stale_agents": [a["id"] for a in agents if a.get("status") == "stale"],
            },
        }
