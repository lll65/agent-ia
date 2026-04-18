from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx

from agent.tools import create_project, list_files
from config import config

router = APIRouter()


class ProjectRequest(BaseModel):
    name: str
    type: Optional[str] = "python"
    description: Optional[str] = None


class ProjectResponse(BaseModel):
    name: str
    type: str
    path: str
    files: str
    readme: Optional[str] = None


async def generate_readme(name: str, description: str, project_type: str) -> str:
    prompt = (
        f"Génère un README.md complet et professionnel pour un projet nommé '{name}' "
        f"de type {project_type}. Description: {description}. "
        f"Inclus: présentation, installation, utilisation, structure. En français."
    )
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "system": "Tu génères des README.md professionnels en Markdown.",
        "stream": False,
        "options": {"temperature": 0.5},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")
        except httpx.ConnectError:
            return f"# {name}\n\n{description}\n"


@router.post("/create", response_model=ProjectResponse)
async def create_new_project(req: ProjectRequest):
    valid_types = ["python", "web", "api"]
    if req.type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Type invalide. Valides: {valid_types}")

    result = create_project(req.name, req.type)
    files = list_files(req.name)

    readme = None
    if req.description:
        readme = await generate_readme(req.name, req.description, req.type)
        from agent.tools import write_file
        write_file(f"{req.name}/README.md", readme)

    from pathlib import Path
    path = str(Path(config.OUTPUT_DIR) / req.name)
    return ProjectResponse(name=req.name, type=req.type, path=path, files=files, readme=readme)


@router.get("/list")
async def list_projects():
    from pathlib import Path
    base = Path(config.OUTPUT_DIR)
    if not base.exists():
        return {"projects": []}
    projects = [d.name for d in base.iterdir() if d.is_dir()]
    return {"projects": projects}
