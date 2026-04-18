from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from config import config

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    system: Optional[str] = "Tu es un assistant IA personnel utile et précis."
    temperature: Optional[float] = 0.7


class ChatResponse(BaseModel):
    response: str
    model: str
    done: bool


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    import asyncio
    from llm.client import chat as llm_chat
    messages = [
        {"role": "system", "content": req.system},
        {"role": "user", "content": req.message},
    ]
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: llm_chat(messages, req.temperature))
        return ChatResponse(response=response, model=config.LLM_MODEL, done=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Erreur LLM: {e}")


@router.get("/status")
async def status():
    from llm.client import chat as llm_chat
    try:
        llm_chat([{"role": "user", "content": "ping"}], temperature=0.1)
        return {"status": "online", "provider": config.LLM_PROVIDER, "model": config.LLM_MODEL}
    except Exception as e:
        return {"status": "offline", "provider": config.LLM_PROVIDER, "error": str(e)}
