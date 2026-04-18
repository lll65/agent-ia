"""
Fabrique de sous-agents: génère dynamiquement la config complète
(nom, prompt système, outils) en fonction du rôle demandé.
"""
import uuid
import httpx
import logging
from config import config

logger = logging.getLogger(__name__)

# Templates de prompts par type d'agent
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
        "tools": None,  # tous les outils disponibles
    },
}


async def generate_agent_config(role: str, objective: str, model: str | None = None) -> dict:
    """
    Génère la configuration complète d'un agent à partir de son rôle et objectif.
    Utilise le LLM pour personnaliser le prompt système.
    """
    model = model or config.OLLAMA_MODEL
    template = AGENT_TEMPLATES.get(role, AGENT_TEMPLATES["generic"])

    # Génère un prompt système personnalisé via LLM
    prompt = (
        f"Crée le prompt système d'un agent IA spécialisé.\n"
        f"Rôle: {role}\n"
        f"Objectif: {objective}\n"
        f"Base de départ: {template['system_prompt']}\n\n"
        f"Génère un prompt système en 3-5 phrases qui définit précisément "
        f"le comportement, les capacités et le style de cet agent. "
        f"Réponds UNIQUEMENT avec le prompt, sans introduction."
    )

    payload = {
        "model": model,
        "prompt": prompt,
        "system": "Tu génères des prompts système précis pour des agents IA.",
        "stream": False,
        "options": {"temperature": 0.6},
    }

    system_prompt = template["system_prompt"]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{config.OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            generated = resp.json().get("response", "").strip()
            if generated and len(generated) > 20:
                system_prompt = generated
    except Exception as e:
        logger.warning(f"Génération LLM du prompt échouée, utilisation du template: {e}")

    from plugins import get_loader
    all_tools = list(get_loader().list_all().keys())

    return {
        "id": str(uuid.uuid4())[:8],
        "name": f"Agent {role.capitalize()} — {objective[:30]}",
        "role": role,
        "objective": objective,
        "system_prompt": system_prompt,
        "tools": template["tools"] or all_tools,
        "model": model,
        "status": "idle",
    }
