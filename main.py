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
import sys
from contextlib import asynccontextmanager
from pathlib import Path

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

    # PEA Watcher — surveillance autonome + alertes Telegram
    if config.WATCHER_ENABLED:
        if config.TELEGRAM_TOKEN:
            from agent.pea_watcher import watch_loop
            bot_tasks.append(asyncio.create_task(watch_loop()))
            logger.info("PEA Watcher démarré (alertes Telegram).")
        else:
            logger.warning("WATCHER_ENABLED=true mais TELEGRAM_TOKEN absent — watcher non démarré.")

    logger.info(f"Agent IA démarré sur http://{config.HOST}:{config.PORT}")
    logger.info(f"Documentation: http://localhost:{config.PORT}/docs")

    yield

    supervisor.stop()
    for t in bot_tasks:
        t.cancel()


# ── Application FastAPI ────────────────────────────────────────────────────────

app = FastAPI(
    title="Agent IA Personnel",
    description="Ton propre serveur IA — 100% local, 0€/mois.\n\nDocs: /docs | UI: /ui",
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
            "ui": "/ui",
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


# ── Montage de l'UI Gradio SUR l'app FastAPI ────────────────────────────────────
# UN SEUL process, UN SEUL port (config.PORT). Indispensable pour Render/Railway/etc.
# qui n'exposent qu'un seul port : l'ancien montage sur un port séparé (GRADIO_PORT,
# via un thread) rendait l'UI injoignable publiquement. Ici Gradio partage l'event
# loop d'uvicorn → plus besoin du hack de thread + event loop maison, et les bots
# Telegram/Discord + le PEA Watcher continuent de tourner dans le lifespan de l'app.
_HEADLESS = "--no-ui" in sys.argv or os.getenv("DISABLE_UI", "").lower() == "true"
if not _HEADLESS:
    try:
        # Ordre important : build_ui applique le patch de compat huggingface_hub
        # (HfFolder) AVANT que gradio ne soit importé.
        from ui.gradio_app import build_ui
        import gradio as gr

        _demo = build_ui()
        _demo.queue()  # nécessaire pour les événements streaming (générateurs)
        _allowed = [str(Path(d).resolve()) for d in ("output", "data")]
        for _d in _allowed:
            Path(_d).mkdir(parents=True, exist_ok=True)
        app = gr.mount_gradio_app(app, _demo, path="/ui", allowed_paths=_allowed)
        logger.info("Interface Gradio montée sur /ui (même port que l'API).")
    except Exception as e:
        logger.error(f"Échec du montage de l'UI Gradio: {e}", exc_info=True)


# ── Lancement ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"UI : http://localhost:{config.PORT}/ui   ·   Docs : http://localhost:{config.PORT}/docs")
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload="--reload" in sys.argv,
        log_level="info",
    )
