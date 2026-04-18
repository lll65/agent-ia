from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from config import config

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    system: Optional[str] = "Tu es un assistant IA personnel utile et précis."
    model: Optional[str] = None
    temperature: Optional[float] = 0.7


class ChatResponse(BaseModel):
    response: str
    model: str
    done: bool


class ModelInfo(BaseModel):
    name: str
    size: Optional[str] = None


def _ollama_chat(messages: list, model: str, temperature: float) -> str:
    import ollama
    response = ollama.chat(
        model=model,
        messages=messages,
        options={"temperature": temperature},
    )
    return response["message"]["content"]


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    import asyncio
    model = req.model or config.OLLAMA_MODEL
    messages = [
        {"role": "system", "content": req.system},
        {"role": "user", "content": req.message},
    ]
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: _ollama_chat(messages, model, req.temperature))
        return ChatResponse(response=response, model=model, done=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Erreur Ollama: {e}")


@router.get("/models", response_model=list[ModelInfo])
async def list_models():
    import ollama
    try:
        models = ollama.list()
        return [
            ModelInfo(
                name=m["model"],
                size=f"{m.get('size', 0) // 1_000_000} MB" if m.get("size") else None,
            )
            for m in models.get("models", [])
        ]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama non démarré: {e}")


@router.get("/status")
async def status():
    import ollama
    try:
        ollama.list()
        return {"status": "online", "model": config.OLLAMA_MODEL}
    except Exception as e:
        return {"status": "offline", "error": str(e)}
