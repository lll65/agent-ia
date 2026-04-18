from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from orchestrator import get_master, get_registry
from orchestrator.factory import generate_agent_config, AGENT_TEMPLATES
from orchestrator.supervisor import Supervisor
from agent.self_heal import health_monitor

router = APIRouter()
_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = Supervisor(get_registry())
    return _supervisor


class ExecuteRequest(BaseModel):
    goal: str
    session_id: Optional[str] = "master"


class CreateAgentRequest(BaseModel):
    role: str
    objective: str
    model: Optional[str] = None


class AgentResult(BaseModel):
    goal: str
    mode: str
    answer: str
    tasks_count: Optional[int] = None
    agents: Optional[list] = None


@router.post("/execute", response_model=AgentResult)
async def execute_goal(req: ExecuteRequest):
    """Point d'entrée principal: donne un objectif complexe au Master Agent."""
    result = await get_master().execute(req.goal, req.session_id)
    return AgentResult(
        goal=result["goal"],
        mode=result["mode"],
        answer=result["answer"],
        tasks_count=result.get("tasks_count"),
        agents=result.get("agents"),
    )


@router.post("/agents/create")
async def create_sub_agent(req: CreateAgentRequest):
    """Crée un sous-agent spécialisé et l'enregistre dans le registre."""
    valid_roles = list(AGENT_TEMPLATES.keys())
    if req.role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Rôles valides: {valid_roles}")
    cfg = await generate_agent_config(req.role, req.objective, req.model)
    get_registry().register(cfg)
    return cfg


@router.get("/agents")
async def list_agents():
    return {"agents": get_registry().list_all()}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = get_registry().get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' introuvable.")
    return agent


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    if not get_registry().delete(agent_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' introuvable.")
    return {"deleted": agent_id}


@router.get("/health")
async def health():
    sup = get_supervisor()
    return sup.health_report()


@router.get("/tools/stats")
async def tool_stats():
    return {"tool_health": health_monitor.report()}


@router.post("/supervisor/start")
async def start_supervisor(interval: int = 60):
    get_supervisor().start(interval)
    return {"status": "superviseur démarré", "interval_seconds": interval}
