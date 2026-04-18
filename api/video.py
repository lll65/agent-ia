from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from pathlib import Path

from config import config

router = APIRouter()


class VideoRequest(BaseModel):
    topic: str
    style: Optional[str] = "éducatif"
    lang: Optional[str] = "fr"
    slides_count: Optional[int] = 5
    add_audio: Optional[bool] = True


class VideoResponse(BaseModel):
    status: str
    video_path: Optional[str] = None
    script: Optional[str] = None
    message: str


async def generate_video_slides(topic: str, style: str, count: int) -> list[str]:
    import asyncio
    from llm.client import chat
    prompt = (
        f"Crée {count} slides courts pour une vidéo virale {style} sur: '{topic}'.\n"
        f"Format EXACT (une slide par ligne, séparées par ---) :\n"
        f"Texte slide 1\n---\nTexte slide 2\n---\n...\n"
        f"Chaque slide: max 15 mots, percutant, accrocheur. Langue française."
    )
    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: chat(
            [
                {"role": "system", "content": "Tu crées du contenu viral pour les réseaux sociaux. Réponses courtes et percutantes."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.8,
        ))
        slides = [s.strip() for s in raw.split("---") if s.strip()]
        return slides[:count] if slides else [topic]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Erreur LLM: {e}")


@router.post("/create", response_model=VideoResponse)
async def create_video(req: VideoRequest, background_tasks: BackgroundTasks):
    slides = await generate_video_slides(req.topic, req.style, req.slides_count)
    script = "\n---\n".join(slides)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in req.topic)[:40]

    def make_video():
        try:
            from video.creator import create_video as build_video
            build_video(slides=slides, output_name=safe_name, lang=req.lang, add_audio=req.add_audio)
        except Exception as e:
            print(f"Erreur création vidéo: {e}")

    background_tasks.add_task(make_video)
    return VideoResponse(
        status="en_cours",
        video_path=f"output/videos/{safe_name}.mp4",
        script=script,
        message=f"Vidéo '{req.topic}' en cours ({len(slides)} slides). GET /video/download/{safe_name}",
    )


@router.post("/script")
async def generate_script_only(req: VideoRequest):
    slides = await generate_video_slides(req.topic, req.style, req.slides_count)
    return {"topic": req.topic, "slides": slides, "script": "\n\n---\n\n".join(slides)}


@router.get("/download/{name}")
async def download_video(name: str):
    path = Path(config.OUTPUT_DIR) / "videos" / f"{name}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Vidéo '{name}' pas encore prête.")
    return FileResponse(str(path), media_type="video/mp4", filename=f"{name}.mp4")


@router.get("/list")
async def list_videos():
    video_dir = Path(config.OUTPUT_DIR) / "videos"
    if not video_dir.exists():
        return {"videos": []}
    return {"videos": [f.name for f in video_dir.glob("*.mp4")]}
