from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from agent.core import run_agent
from agent.memory import get_history, clear_history
from memory import get_memory
from plugins import get_loader
from config import config

router = APIRouter()

DEFAULT_AGENT = {
    "id": "default",
    "name": "Agent Principal",
    "description": "Agent IA personnel polyvalent — peut créer d'autres agents, coder, chercher et créer des vidéos.",
    "system_prompt": "Tu es un agent IA personnel puissant. Tu aides à créer des projets, écrire du code, chercher des informations et créer du contenu.",
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
    memory_count: Optional[int] = None


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    agent_id = req.agent_id or "default"

    if agent_id == "default":
        agent_config = {**DEFAULT_AGENT, "tools": list(get_loader().list_all().keys())}
    else:
        from orchestrator import get_registry
        agent_config = get_registry().get(agent_id)
        if not agent_config:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' introuvable.")

    result = await run_agent(req.message, agent_config, agent_id)
    mem = get_memory()

    return ChatResponse(
        answer=result["answer"],
        agent_id=agent_id,
        iterations=result["iterations"],
        steps=result["steps"] if req.show_steps else None,
        memory_count=mem.chroma.count(agent_id) if mem.chroma.available else None,
    )


@router.get("/{agent_id}/history")
async def agent_history(agent_id: str, limit: int = 20):
    return {"agent_id": agent_id, "history": get_history(agent_id, limit)}


@router.delete("/{agent_id}/memory")
async def clear_memory(agent_id: str):
    get_memory().clear(agent_id)
    return {"message": f"Mémoire de '{agent_id}' effacée (SQLite + ChromaDB)."}


@router.get("/{agent_id}/summary")
async def get_summary(agent_id: str):
    summary = get_memory().get_summary(agent_id)
    return {"agent_id": agent_id, "summary": summary or "Aucun résumé disponible."}


@router.get("/tools/list")
async def available_tools():
    return {"tools": get_loader().list_all()}
