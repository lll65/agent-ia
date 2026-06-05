"""
Serveur SVD local — à lancer sur le PC avec GPU (pas ce PC).

Installation sur le PC GPU (1 seule fois):
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
    pip install diffusers transformers accelerate fastapi uvicorn pillow

Lancement:
    python video/svd_server.py

Puis mettre dans le .env du PC principal:
    LOCAL_SVD_URL=http://192.168.1.XXX:9876   (remplace XXX par l'IP du PC GPU)
"""
import io
import os
import tempfile
from pathlib import Path

import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image

app = FastAPI(title="SVD Local Server")
_pipe = None


def _load_pipe():
    global _pipe
    if _pipe is not None:
        return _pipe

    from diffusers import StableVideoDiffusionPipeline

    print("[SVD] Chargement du modèle (première fois ~5 min, ensuite quelques sec)...")
    _pipe = StableVideoDiffusionPipeline.from_pretrained(
        "stabilityai/stable-video-diffusion-img2vid",
        torch_dtype=torch.float16,
        variant="fp16",
    )
    _pipe.enable_model_cpu_offload()
    try:
        _pipe.unet.enable_xformers_memory_efficient_attention()
    except Exception:
        pass
    _pipe.vae.enable_slicing()
    print("[SVD] Modèle prêt!")
    return _pipe


@app.get("/health")
def health():
    return {
        "status": "ok",
        "cuda": torch.cuda.is_available(),
        "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
    }


@app.post("/generate")
async def generate(
    image: UploadFile = File(...),
    num_frames: int = 25,
    fps: int = 7,
    seed: int = 42,
):
    """Génère une vidéo SVD depuis une image. Retourne les bytes MP4."""
    try:
        img_data = await image.read()
        img = Image.open(io.BytesIO(img_data)).convert("RGB").resize((1024, 576))

        pipe = _load_pipe()
        generator = torch.manual_seed(seed)

        frames = pipe(
            img,
            num_frames=num_frames,
            num_inference_steps=25,
            decode_chunk_size=8,
            generator=generator,
        ).frames[0]

        out_path = tempfile.mktemp(suffix=".mp4")
        from diffusers.utils import export_to_video
        export_to_video(frames, out_path, fps=fps)

        with open(out_path, "rb") as f:
            data = f.read()
        Path(out_path).unlink(missing_ok=True)

        return Response(content=data, media_type="video/mp4")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SVD_PORT", "9876"))
    print(f"[SVD] Serveur sur http://0.0.0.0:{port}")
    print("[SVD] Modèle chargé au 1er appel (pas au démarrage)")
    uvicorn.run(app, host="0.0.0.0", port=port)
