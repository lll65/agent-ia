from orchestrator.master import MasterAgent
from orchestrator.registry import AgentRegistry

_master: MasterAgent | None = None
_registry: AgentRegistry | None = None


def get_master() -> MasterAgent:
    global _master
    if _master is None:
        _master = MasterAgent()
    return _master


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
