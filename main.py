from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os

from config import config
from api.llm import router as llm_router
from api.agent import router as agent_router
from api.video import router as video_router
from api.code import router as code_router
from api.project import router as project_router
from agent.memory import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    os.makedirs(config.AGENTS_DIR, exist_ok=True)
    os.makedirs("data", exist_ok=True)
    init_db()
    yield


app = FastAPI(
    title="Mon Agent IA Personnel",
    description="Ton propre serveur IA — 100% local, 100% gratuit",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(llm_router, prefix="/llm", tags=["LLM"])
app.include_router(agent_router, prefix="/agent", tags=["Agent"])
app.include_router(video_router, prefix="/video", tags=["Video"])
app.include_router(code_router, prefix="/code", tags=["Code"])
app.include_router(project_router, prefix="/project", tags=["Projet"])


@app.get("/")
def root():
    return {
        "message": "Agent IA Personnel — en ligne",
        "routes": {
            "LLM": "/llm/chat",
            "Agent": "/agent/chat",
            "Créer agent": "/agent/create",
            "Vidéo": "/video/create",
            "Code": "/code/generate",
            "Projet": "/project/create",
            "Docs": "/docs",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
