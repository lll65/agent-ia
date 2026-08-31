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
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import hmac

from config import config
from agent.memory import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class _RedactSecrets(logging.Filter):
    """Masque les secrets dans les logs.

    Les requêtes SSE (EventSource) ne peuvent pas porter d'en-tête : la clé transite
    donc en paramètre d'URL et se retrouvait EN CLAIR dans les logs d'accès Render.
    Ce filtre la remplace par « *** » partout où elle apparaît.
    """
    _PAT = re.compile(r"((?:key|api_key|token|password)=)[^&\s\"']+", re.I)

    def filter(self, record):
        try:
            if record.args:
                record.args = tuple(
                    self._PAT.sub(r"\1***", a) if isinstance(a, str) else a
                    for a in record.args
                )
            if isinstance(record.msg, str):
                record.msg = self._PAT.sub(r"\1***", record.msg)
        except Exception:
            pass
        return True


for _name in ("uvicorn.access", "uvicorn.error", "uvicorn", ""):
    logging.getLogger(_name).addFilter(_RedactSecrets())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialisation
    config.ensure_dirs()
    init_db()
    logger.info("Base de données initialisée.")

    # ⚙️ Pool de threads dédié aux appels BLOQUANTS (LLM, recherche web, Composio, mémoire).
    # Par défaut Python en alloue min(32, CPU+4) → 5 seulement sur une petite instance Render :
    # deux conversations simultanées suffisaient à saturer le pool et Nova restait bloquée sur
    # « réflexion » sans jamais répondre.
    import concurrent.futures
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="nova"))

    # Démarrage superviseur
    from orchestrator import get_registry
    from orchestrator.supervisor import Supervisor
    supervisor = Supervisor(get_registry())
    supervisor.start(interval_seconds=60)

    # Bots en tâches de fond.
    # ⚠️ On écrivait « Bot Telegram démarré » AVANT que la tâche ait rien fait. Si elle
    # mourait dans la seconde — jeton révoqué, ou le « Conflict » que Telegram renvoie
    # quand deux instances interrogent le même bot (l'ancienne installation locale et
    # Render) — l'exception partait dans une tâche que personne n'attend, et Python
    # l'avalait. Les journaux affirmaient « démarré », rien n'arrivait, et rien ne
    # disait pourquoi. `lancer` retient ce qui arrive à chaque tâche (agent/taches.py).
    from agent.taches import lancer
    bot_tasks = []
    if config.TELEGRAM_TOKEN:
        from bots.telegram_bot import run_telegram_bot
        bot_tasks.append(lancer("bot Telegram", run_telegram_bot()))
    else:
        logger.info("Bot Telegram non lancé : TELEGRAM_TOKEN absent.")
    if config.DISCORD_TOKEN:
        from bots.discord_bot import run_discord_bot
        bot_tasks.append(lancer("bot Discord", run_discord_bot()))

    # Automatisations — Nova exécute seule les tâches planifiées (« pendant que tu dors »)
    from agent.automations import scheduler_loop
    bot_tasks.append(lancer("planificateur", scheduler_loop()))

    # Briefing du matin proactif (agenda + mails + météo + actu) via Telegram
    if config.BRIEFING_ENABLED:
        from agent.briefing import morning_loop
        bot_tasks.append(lancer("briefing du matin", morning_loop()))
        logger.info(f"Briefing du matin activé (envoi à {config.BRIEFING_HOUR}h via Telegram).")

    # PEA Watcher — surveillance autonome + alertes Telegram
    if config.WATCHER_ENABLED:
        if config.TELEGRAM_TOKEN:
            from agent.pea_watcher import watch_loop
            bot_tasks.append(lancer("veille PEA", watch_loop()))
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

# ═══ VERROU GLOBAL ═══════════════════════════════════════════════════════════
# ⚠️ Seul /agent/* verifiait la cle. Tout le reste etait OUVERT sur l'URL publique
# Render — dont POST /code/generate-and-run, qui fait EXECUTER du Python arbitraire
# sur le serveur. Un visiteur pouvait donc lire os.environ et repartir avec TOUTES
# les cles (Composio, Groq, l'URL Supabase avec son mot de passe).
# On ferme par defaut et on n'ouvre que ce qui doit l'etre : les pages de l'interface
# (elles demandent la cle cote navigateur) et le point de sante du reveil externe.
_PUBLIC = (
    "/", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico",
    "/nova", "/nova/brain", "/nova/cours", "/nova/manifest.webmanifest",
    "/nova/icon.svg", "/sw.js",
)


@app.middleware("http")
async def _verrou(request, call_next):
    from fastapi.responses import JSONResponse
    chemin = request.url.path.rstrip("/") or "/"
    if request.method == "OPTIONS" or chemin in _PUBLIC:
        return await call_next(request)
    # Les routes /agent/* portent deja leur propre controle, plus precis (message clair,
    # comparaison a temps constant) : on le laisse faire pour ne pas doubler les erreurs.
    if chemin == "/agent" or chemin.startswith("/agent/"):
        return await call_next(request)
    attendue = (getattr(config, "AGENT_API_KEY", "") or "").strip()
    if not attendue:
        return JSONResponse(status_code=503, content={
            "detail": "Serveur non securise : definis AGENT_API_KEY sur Render. "
                      "Sans elle, ces routes resteraient ouvertes a tout le monde."})
    fournie = (request.query_params.get("key")
               or request.headers.get("x-api-key")
               or request.headers.get("authorization", "").replace("Bearer ", "")).strip()
    if not hmac.compare_digest(fournie.encode("utf-8"), attendue.encode("utf-8")):
        return JSONResponse(status_code=401, content={"detail": "Cle requise."})
    return await call_next(request)


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


@app.get("/health", tags=["Info"])
def health():
    """Point de réveil pour le cron externe (cron-job.org) qui empêche Render
    de s'endormir — sinon les automatisations planifiées ne partent jamais.
    Volontairement public ET muet : il ne révèle rien (pas de version, pas de
    liste d'outils), il dit juste que le serveur répond."""
    return {"status": "ok"}


_NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


@app.get("/nova", include_in_schema=False)
def nova_ui():
    """Interface futuriste custom (parle à /agent/ask). Alternative animée à Gradio."""
    from fastapi.responses import FileResponse
    # no-store : sans ça le navigateur garde l'ancienne version après un déploiement.
    return FileResponse(str(Path(__file__).parent / "ui" / "nova.html"), headers=_NOCACHE)


@app.get("/nova/brain", include_in_schema=False)
def nova_brain():
    """Constellation : l'escouade de sous-agents Nova et leur activité en temps réel."""
    from fastapi.responses import FileResponse
    return FileResponse(str(Path(__file__).parent / "ui" / "brain.html"), headers=_NOCACHE)


@app.get("/nova/cours", include_in_schema=False)
def nova_cours():
    """Mode Cours : Nova écoute un cours entier et en tire une synthèse + des fiches."""
    from fastapi.responses import FileResponse
    return FileResponse(str(Path(__file__).parent / "ui" / "cours.html"), headers=_NOCACHE)


@app.get("/nova/manifest.webmanifest", include_in_schema=False)
def nova_manifest():
    """Manifest PWA : rend Nova installable sur le téléphone (icône plein écran)."""
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "Nova", "short_name": "Nova", "start_url": "/nova", "scope": "/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#060610", "theme_color": "#7c5cff",
        "description": "Ton assistant IA personnel — vocal, connecté, du futur.",
        "icons": [{"src": "/nova/icon.svg", "sizes": "any", "type": "image/svg+xml",
                   "purpose": "any maskable"}],
    })


@app.get("/nova/icon.svg", include_in_schema=False)
def nova_icon():
    from fastapi.responses import Response
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">'
           '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#7c5cff"/><stop offset="1" stop-color="#22d3ee"/>'
           '</linearGradient></defs><rect width="512" height="512" rx="116" fill="#0a0a18"/>'
           '<text x="50%" y="55%" font-family="Arial,Helvetica,sans-serif" font-size="300" '
           'font-weight="800" fill="url(#g)" text-anchor="middle" dominant-baseline="middle">N</text></svg>')
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/sw.js", include_in_schema=False)
def nova_sw():
    """Service worker minimal (portée racine) → critère d'installabilité PWA rempli."""
    from fastapi.responses import Response
    js = ("self.addEventListener('install',e=>self.skipWaiting());"
          "self.addEventListener('activate',e=>self.clients.claim());"
          "self.addEventListener('fetch',e=>{});")
    return Response(content=js, media_type="application/javascript")


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
# ⚠️ SÉCURITÉ : l'interface Gradio /ui n'a AUCUNE authentification — n'importe qui
# connaissant l'adresse pourrait faire parler l'agent et agir sur les apps connectées.
# Elle est donc DÉSACTIVÉE par défaut (l'interface Nova /nova, elle, exige la clé).
# Pour la réactiver temporairement (débogage local) : ENABLE_GRADIO_UI=true
_UI_ACTIVEE = os.getenv("ENABLE_GRADIO_UI", "").lower() == "true"
_HEADLESS = (not _UI_ACTIVEE) or "--no-ui" in sys.argv or os.getenv("DISABLE_UI", "").lower() == "true"
if _HEADLESS:
    logger.info("Interface Gradio /ui désactivée (sans authentification). Utilise /nova.")
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
