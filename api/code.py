from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from agent.tools import exec_python, write_file

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
    import asyncio
    from llm.client import chat
    prompt = (
        f"Écris du code {language} pour: {description}\n"
        f"Réponds UNIQUEMENT avec le code, sans explication ni markdown. "
        f"Le code doit être complet et fonctionnel."
    )
    try:
        loop = asyncio.get_event_loop()
        code = await loop.run_in_executor(None, lambda: chat(
            [
                {"role": "system", "content": f"Tu es un expert en {language}. Tu génères uniquement du code propre et fonctionnel."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        ))
        if "```" in code:
            lines = code.split("\n")
            code_lines, inside = [], False
            for line in lines:
                if line.startswith("```"):
                    inside = not inside
                    continue
                if inside:
                    code_lines.append(line)
            extracted = "\n".join(code_lines).strip()
            if extracted:
                code = extracted
        return code
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Erreur LLM: {e}")


@router.post("/generate", response_model=GenerateResponse)
async def generate_code(req: GenerateRequest):
    code = await ask_llm_for_code(req.description, req.language)
    saved_to = None
    if req.save_as:
        saved_to = write_file(req.save_as, code)
    return GenerateResponse(code=code, language=req.language, saved_to=saved_to)


@router.post("/execute", response_model=ExecuteResponse)
async def execute_code(req: ExecuteRequest):
    output = exec_python(req.code)
    error_patterns = ("Traceback (most recent call last)", "SyntaxError:", "NameError:", "TypeError:", "ValueError:", "ImportError:", "IndentationError:")
    success = not any(p in output for p in error_patterns)
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
