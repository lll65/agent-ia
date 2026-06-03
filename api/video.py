from fastapi import APIRouter, HTTPException, BackgroundTasks, File, UploadFile, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
import re

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


# ── Image → Vidéo réaliste ─────────────────────────────────────────────────────

class Img2VideoResponse(BaseModel):
    status: str
    video_path: Optional[str] = None
    method: Optional[str] = None
    score: Optional[int] = None
    attempts: Optional[int] = None
    message: str


@router.post("/img2video", response_model=Img2VideoResponse)
async def image_to_video(
    image: UploadFile = File(..., description="Image source (JPG/PNG)"),
    description: str = Form("", description="Description de l'image pour guider le mouvement"),
    motion_prompt: str = Form("", description="Prompt de mouvement manuel (optionnel)"),
    duration: float = Form(5.0, description="Durée min en secondes (≥ 5)"),
    fps: int = Form(24, description="Images par seconde"),
    width: int = Form(1280, description="Largeur (défaut 1280)"),
    height: int = Form(720, description="Hauteur (défaut 720)"),
    max_loops: int = Form(2, description="Nombre max d'améliorations automatiques"),
):
    """
    Transforme une image en vidéo réaliste 720p minimum, durée ≥ 5s.
    Utilise HuggingFace SVD (si HF_API_TOKEN configuré) + fallback FFmpeg Ken Burns.
    """
    if duration < 5.0:
        duration = 5.0

    img_data = await image.read()
    safe_name = re.sub(r"[^\w-]", "_", Path(image.filename or "video").stem)[:40]

    out_dir = Path(config.OUTPUT_DIR) / "videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_path = str(out_dir / f"_src_{safe_name}.jpg")
    out_path = str(out_dir / f"{safe_name}_realistic.mp4")

    # Sauvegarde l'image source
    Path(img_path).write_bytes(img_data)

    # Conversion JPEG si besoin (PNG → JPEG pour SVD)
    try:
        from PIL import Image as PILImage
        img = PILImage.open(img_path).convert("RGB")
        img.save(img_path, "JPEG", quality=95)
    except Exception:
        pass

    from video.img2video import generate_realistic_video
    result = await generate_realistic_video(
        image_path=img_path,
        output_path=out_path,
        description=description,
        motion_prompt=motion_prompt,
        duration=duration,
        fps=fps,
        width=width,
        height=height,
        max_loops=max_loops,
    )

    Path(img_path).unlink(missing_ok=True)

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return Img2VideoResponse(
        status="done",
        video_path=result["video_path"],
        method=result.get("method"),
        score=result.get("score"),
        attempts=result.get("attempts"),
        message=(
            f"Vidéo générée en {result.get('attempts', 1)} tentative(s) — "
            f"méthode: {result.get('method')} — score: {result.get('score')}/10. "
            f"GET /video/download/{safe_name}_realistic"
        ),
    )
