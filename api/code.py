from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx

from agent.tools import exec_python, write_file
from config import config

router = APIRouter()


class GenerateRequest(BaseModel):
    description: str
    language: Optional[str] = "python"
    save_as: Optional[str] = None


class ExecuteRequest(BaseModel):
    code: str


class GenerateResponse(BaseModel):
    code: str
    language: str
    saved_to: Optional[str] = None


class ExecuteResponse(BaseModel):
    output: str
    success: bool


async def ask_llm_for_code(description: str, language: str) -> str:
    prompt = (
        f"Écris du code {language} pour: {description}\n"
        f"Réponds UNIQUEMENT avec le code, sans explication ni markdown. "
        f"Le code doit être complet et fonctionnel."
    )
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "system": f"Tu es un expert en {language}. Tu génères uniquement du code propre et fonctionnel.",
        "stream": False,
        "options": {"temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(f"{config.OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            code = resp.json().get("response", "")
            # Nettoie les balises markdown si présentes
            if "```" in code:
                lines = code.split("\n")
                code_lines = []
                inside = False
                for line in lines:
                    if line.startswith("```"):
                        inside = not inside
                        continue
                    if inside or not line.startswith("```"):
                        code_lines.append(line)
                code = "\n".join(code_lines).strip()
            return code
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="Ollama non démarré.")


@router.post("/generate", response_model=GenerateResponse)
async def generate_code(req: GenerateRequest):
    code = await ask_llm_for_code(req.description, req.language)
    saved_to = None
    if req.save_as:
        result = write_file(req.save_as, code)
        saved_to = result
    return GenerateResponse(code=code, language=req.language, saved_to=saved_to)


@router.post("/execute", response_model=ExecuteResponse)
async def execute_code(req: ExecuteRequest):
    output = exec_python(req.code)
    success = "Erreur" not in output and "Error" not in output
    return ExecuteResponse(output=output, success=success)


@router.post("/generate-and-run")
async def generate_and_run(req: GenerateRequest):
    if req.language != "python":
        raise HTTPException(status_code=400, detail="Seul Python est exécutable directement.")
    code = await ask_llm_for_code(req.description, "python")
    output = exec_python(code)
    saved_to = None
    if req.save_as:
        write_file(req.save_as, code)
        saved_to = req.save_as
    return {"code": code, "output": output, "saved_to": saved_to}
