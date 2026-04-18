"""
Résume automatiquement les conversations longues pour garder le contexte
sans exploser la fenêtre du LLM.
"""
import httpx
import logging
from config import config

logger = logging.getLogger(__name__)


async def summarize_messages(messages: list[dict], model: str | None = None) -> str:
    """Génère un résumé concis d'une liste de messages."""
    if not messages:
        return ""

    model = model or config.OLLAMA_MODEL
    conv = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
    prompt = (
        f"Voici une conversation entre un utilisateur et un agent IA:\n\n{conv}\n\n"
        f"Fais un résumé dense en 3-5 phrases qui capture: les sujets abordés, "
        f"les décisions prises, les fichiers créés, et les tâches accomplies. "
        f"Commence par 'RÉSUMÉ: '"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Tu es un expert en synthèse de conversations. Sois concis et précis."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.3},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.warning(f"Résumé impossible: {e}")
            # Fallback: concaténation simple
            recent = messages[-5:]
            return "RÉSUMÉ (auto): " + " | ".join(f"{m['role']}: {m['content'][:100]}" for m in recent)
