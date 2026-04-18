"""
Point d'entrée principal — démarre:
 - Serveur FastAPI (APIs REST)
 - Interface Gradio (Web UI)
 - Bots Telegram/Discord (si tokens configurés)
 - Superviseur d'agents
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import config
from agent.memory import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialisation
    config.ensure_dirs()
    init_db()
    logger.info("Base de données initialisée.")

    # Démarrage superviseur
    from orchestrator import get_registry
    from orchestrator.supervisor import Supervisor
    supervisor = Supervisor(get_registry())
    supervisor.start(interval_seconds=60)

    # Bots en tâches de fond
    bot_tasks = []
    if config.TELEGRAM_TOKEN:
        from bots.telegram_bot import run_telegram_bot
        bot_tasks.append(asyncio.create_task(run_telegram_bot()))
        logger.info("Bot Telegram démarré.")
    if config.DISCORD_TOKEN:
        from bots.discord_bot import run_discord_bot
        bot_tasks.append(asyncio.create_task(run_discord_bot()))
        logger.info("Bot Discord démarré.")

    logger.info(f"Agent IA démarré sur http://{config.HOST}:{config.PORT}")
    logger.info(f"Documentation: http://localhost:{config.PORT}/docs")

    yield

    supervisor.stop()
    for t in bot_tasks:
        t.cancel()


# ── Application FastAPI ────────────────────────────────────────────────────────

app = FastAPI(
    title="Agent IA Personnel",
    description="Ton propre serveur IA — 100% local, 0€/mois.\n\nDocs: /docs | UI: http://localhost:7860",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
from api.llm import router as llm_router
from api.agent import router as agent_router
from api.video import router as video_router
from api.code import router as code_router
from api.project import router as project_router
from api.orchestrator import router as orch_router

app.include_router(llm_router, prefix="/llm", tags=["LLM"])
app.include_router(agent_router, prefix="/agent", tags=["Agent"])
app.include_router(orch_router, prefix="/orchestrator", tags=["Orchestrateur"])
app.include_router(video_router, prefix="/video", tags=["Vidéo"])
app.include_router(code_router, prefix="/code", tags=["Code"])
app.include_router(project_router, prefix="/project", tags=["Projet"])


@app.get("/", tags=["Info"])
def root():
    from plugins import get_loader
    from orchestrator import get_registry
    return {
        "status": "online",
        "version": "2.0.0",
        "agents": len(get_registry().list_all()) + 1,
        "plugins": len(get_loader().list_all()),
        "endpoints": {
            "chat": "POST /agent/chat",
            "orchestrate": "POST /orchestrator/execute",
            "video": "POST /video/create",
            "code": "POST /code/generate",
            "project": "POST /project/create",
            "docs": "/docs",
            "ui": f"http://localhost:{config.GRADIO_PORT}",
        },
    }


@app.get("/status", tags=["Info"])
async def full_status():
    from api.llm import status as llm_status
    from agent.self_heal import health_monitor
    llm = await llm_status()
    return {
        "llm": llm,
        "tools": health_monitor.report(),
    }


# ── Lancement ─────────────────────────────────────────────────────────────────

def start_gradio():
    """Lance Gradio dans un sous-processus séparé (plus stable qu'un thread)."""
    import subprocess, sys
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); from ui.gradio_app import launch; launch()"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


if __name__ == "__main__":
    import sys

    if "--no-ui" not in sys.argv:
        logger.info(f"Interface Gradio: http://localhost:{config.GRADIO_PORT}")
        start_gradio()

    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload="--reload" in sys.argv,
        log_level="info",
    )
