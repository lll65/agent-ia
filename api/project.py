from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from pathlib import Path

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
    import asyncio
    from llm.client import chat
    prompt = (
        f"Génère un README.md complet pour un projet '{name}' de type {project_type}. "
        f"Description: {description}. "
        f"Inclus: présentation, installation, utilisation, structure. En français."
    )
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: chat(
            [
                {"role": "system", "content": "Tu génères des README.md professionnels en Markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
        ))
    except Exception:
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

    path = str(Path(config.OUTPUT_DIR) / req.name)
    return ProjectResponse(name=req.name, type=req.type, path=path, files=files, readme=readme)


@router.get("/list")
async def list_projects():
    base = Path(config.OUTPUT_DIR)
    if not base.exists():
        return {"projects": []}
    return {"projects": [d.name for d in base.iterdir() if d.is_dir()]}
