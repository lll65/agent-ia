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


# Résultat du dernier ping mis en cache : sans ça, chaque affichage de la jauge
# d'énergie relançait un vrai appel modèle (plusieurs secondes, et du quota brûlé).
_PING = {"t": 0.0, "res": None}
_PING_TTL = 60.0        # secondes


@router.get("/status")
async def status():
    """Disponibilité du LLM. L'appel de test tourne dans un THREAD et est BORNÉ.

    ⚠️ Avant, le ping bloquant s'exécutait sur la boucle asyncio : tant que la chaîne
    de fournisseurs répondait lentement (ou tombait en cascade), TOUT le serveur était
    figé — y compris le flux SSE de Nova, qui semblait « réfléchir » sans fin.
    """
    import asyncio
    import time
    from llm.client import chat as llm_chat

    if _PING["res"] is not None and (time.monotonic() - _PING["t"]) < _PING_TTL:
        return _PING["res"]

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: llm_chat([{"role": "user", "content": "ping"}], temperature=0.1)),
            timeout=12.0)
        res = {"status": "online", "provider": config.LLM_PROVIDER, "model": config.LLM_MODEL}
    except asyncio.TimeoutError:
        res = {"status": "offline", "provider": config.LLM_PROVIDER, "error": "délai dépassé (12 s)"}
    except Exception as e:
        res = {"status": "offline", "provider": config.LLM_PROVIDER, "error": str(e)}
    _PING.update(t=time.monotonic(), res=res)
    return res
