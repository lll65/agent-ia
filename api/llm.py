"""
API LLM — wraps Ollama (modèle IA local gratuit).
Utilise /api/chat compatible avec toutes les versions d'Ollama.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx

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


async def call_ollama(prompt: str, system: str, model: str, temperature: float) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="Ollama non démarré. Lance 'ollama serve'.")


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    model = req.model or config.OLLAMA_MODEL
    response = await call_ollama(req.message, req.system, model, req.temperature)
    return ChatResponse(response=response, model=model, done=True)


@router.get("/models", response_model=list[ModelInfo])
async def list_models():
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{config.OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return [
                ModelInfo(
                    name=m["name"],
                    size=f"{m.get('size', 0) // 1_000_000} MB" if m.get("size") else None,
                )
                for m in models
            ]
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="Ollama non démarré.")


@router.get("/status")
async def status():
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            await client.get(f"{config.OLLAMA_URL}/api/tags")
            return {"status": "online", "url": config.OLLAMA_URL, "model": config.OLLAMA_MODEL}
        except httpx.ConnectError:
            return {"status": "offline", "url": config.OLLAMA_URL}
