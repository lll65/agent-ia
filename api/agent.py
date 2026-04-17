from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import uuid

from agent.core import run_agent
from agent.memory import save_agent, get_agent, list_agents, clear_history, get_history
from agent.tools import AVAILABLE_TOOLS
from config import config

router = APIRouter()

DEFAULT_AGENT = {
    "id": "default",
    "name": "Agent Principal",
    "description": "Agent IA personnel polyvalent — peut créer d'autres agents, coder, chercher sur le web et créer des vidéos.",
    "system_prompt": "Tu es un agent IA personnel puissant. Tu aides à créer des projets, écrire du code, chercher des informations et créer du contenu.",
    "tools": list(AVAILABLE_TOOLS.keys()),
    "model": config.OLLAMA_MODEL,
}


class ChatRequest(BaseModel):
    message: str
    agent_id: Optional[str] = "default"
    show_steps: Optional[bool] = False


class ChatResponse(BaseModel):
    answer: str
    agent_id: str
    iterations: int
    steps: Optional[list] = None


class CreateAgentRequest(BaseModel):
    name: str
    description: str
    system_prompt: str
    tools: Optional[list[str]] = None
    model: Optional[str] = None


class AgentInfo(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    model: str
    created_at: Optional[str] = None


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    agent_id = req.agent_id or "default"

    if agent_id == "default":
        agent_config = DEFAULT_AGENT
    else:
        agent_config = get_agent(agent_id)
        if not agent_config:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' introuvable.")

    result = await run_agent(req.message, agent_config, agent_id)
    return ChatResponse(
        answer=result["answer"],
        agent_id=agent_id,
        iterations=result["iterations"],
        steps=result["steps"] if req.show_steps else None,
    )


@router.post("/create", response_model=AgentInfo)
async def create_agent(req: CreateAgentRequest):
    agent_id = str(uuid.uuid4())[:8]
    tools = req.tools or list(AVAILABLE_TOOLS.keys())
    model = req.model or config.OLLAMA_MODEL

    invalid = [t for t in tools if t not in AVAILABLE_TOOLS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Outils inconnus: {invalid}. Disponibles: {list(AVAILABLE_TOOLS.keys())}")

    save_agent(agent_id, req.name, req.description, req.system_prompt, tools, model)

    from datetime import datetime
    return AgentInfo(id=agent_id, name=req.name, description=req.description, model=model, created_at=datetime.utcnow().isoformat())


@router.get("/list", response_model=list[AgentInfo])
async def list_all_agents():
    agents = list_agents()
    result = [AgentInfo(**a) for a in agents]
    result.insert(0, AgentInfo(
        id="default",
        name=DEFAULT_AGENT["name"],
        description=DEFAULT_AGENT["description"],
        model=DEFAULT_AGENT["model"],
    ))
    return result


@router.get("/{agent_id}")
async def get_agent_info(agent_id: str):
    if agent_id == "default":
        return DEFAULT_AGENT
    agent = get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' introuvable.")
    return agent


@router.get("/{agent_id}/history")
async def agent_history(agent_id: str, limit: int = 20):
    return {"agent_id": agent_id, "history": get_history(agent_id, limit)}


@router.delete("/{agent_id}/history")
async def clear_agent_history(agent_id: str):
    clear_history(agent_id)
    return {"message": f"Historique de '{agent_id}' effacé."}


@router.get("/tools/list")
async def available_tools():
    return {"tools": AVAILABLE_TOOLS}
