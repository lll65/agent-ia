"""
Fabrique de sous-agents: génère dynamiquement la config complète
(nom, prompt système, outils) en fonction du rôle demandé.
"""
import uuid
import logging
from config import config

logger = logging.getLogger(__name__)

AGENT_TEMPLATES = {
    "researcher": {
        "system_prompt": "Tu es un agent de recherche expert. Tu collectes, synthétises et vérifies des informations sur internet. Tu cites tes sources et présentes des faits vérifiés.",
        "tools": ["search_web", "write_file", "read_file"],
    },
    "coder": {
        "system_prompt": "Tu es un développeur expert. Tu écris du code propre, testé et documenté. Tu génères des projets complets et fonctionnels.",
        "tools": ["exec_python", "write_file", "read_file", "list_files", "create_project"],
    },
    "video_creator": {
        "system_prompt": "Tu es un créateur de contenu viral expert. Tu crées des scripts percutants, des vidéos engageantes et du contenu optimisé pour les réseaux sociaux.",
        "tools": ["create_video_script", "write_file", "search_web"],
    },
    "analyst": {
        "system_prompt": "Tu es un analyste de données expert. Tu analyses des données, produis des insights et génères des rapports clairs et visuels.",
        "tools": ["exec_python", "write_file", "read_file", "search_web"],
    },
    "writer": {
        "system_prompt": "Tu es un rédacteur expert. Tu crées des textes engageants, SEO-optimisés et adaptés à l'audience cible.",
        "tools": ["search_web", "write_file", "read_file"],
    },
    "generic": {
        "system_prompt": "Tu es un assistant IA polyvalent. Tu aides avec tout type de tâche.",
        "tools": None,
    },
}


async def generate_agent_config(role: str, objective: str, model: str | None = None) -> dict:
    import asyncio
    from llm.client import chat

    template = AGENT_TEMPLATES.get(role, AGENT_TEMPLATES["generic"])
    prompt = (
        f"Crée le prompt système d'un agent IA spécialisé.\n"
        f"Rôle: {role}\nObjectif: {objective}\n"
        f"Base: {template['system_prompt']}\n\n"
        f"Génère un prompt système en 3-5 phrases précises. "
        f"Réponds UNIQUEMENT avec le prompt, sans introduction."
    )

    system_prompt = template["system_prompt"]
    try:
        loop = asyncio.get_event_loop()
        generated = await loop.run_in_executor(None, lambda: chat(
            [
                {"role": "system", "content": "Tu génères des prompts système précis pour des agents IA."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
        ))
        if generated and len(generated) > 20:
            system_prompt = generated
    except Exception as e:
        logger.warning(f"Génération prompt échouée, utilisation du template: {e}")

    from plugins import get_loader
    all_tools = list(get_loader().list_all().keys())

    return {
        "id": str(uuid.uuid4())[:8],
        "name": f"Agent {role.capitalize()} — {objective[:30]}",
        "role": role,
        "objective": objective,
        "system_prompt": system_prompt,
        "tools": template["tools"] or all_tools,
        "model": config.LLM_MODEL,
        "status": "idle",
    }
