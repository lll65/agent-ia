"""Résume automatiquement les conversations longues."""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def summarize_messages(messages: list[dict], model: str | None = None) -> str:
    if not messages:
        return ""

    from llm.client import chat
    conv = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
    prompt = (
        f"Voici une conversation:\n\n{conv}\n\n"
        f"Fais un résumé dense en 3-5 phrases: sujets, décisions, fichiers créés, tâches. "
        f"Commence par 'RÉSUMÉ: '"
    )
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: chat(
            [
                {"role": "system", "content": "Tu es expert en synthèse. Sois concis et précis."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        ))
    except Exception as e:
        logger.warning(f"Résumé impossible: {e}")
        recent = messages[-5:]
        return "RÉSUMÉ (auto): " + " | ".join(f"{m['role']}: {m['content'][:100]}" for m in recent)
