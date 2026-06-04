"""
Pipeline Image → Vidéo réaliste (720p min, 5s min).
Sources 100% gratuites à vie:
  1. HuggingFace Spaces publics (SVD communautaire, aucun crédit)
  2. Replicate (crédits gratuits optionnels)
  3. Fallback FFmpeg Ken Burns (toujours dispo)
Boucle d'auto-amélioration: score le prompt, l'affine, régénère jusqu'au seuil.
"""
import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HF_MODELS = [
    "stabilityai/stable-video-diffusion-img2vid-xt-1-1",
    "stabilityai/stable-video-diffusion-img2vid-xt",
]
_HF_API_BASE = "https://api-inference.huggingface.co/models/{}"
_QUALITY_THRESHOLD = 6
_DEFAULT_DUR = 5.0
_DEFAULT_FPS = 24
_DEFAULT_W = 720     # portrait par défaut (9:16)
_DEFAULT_H = 1280


# ── HuggingFace Spaces (gratuit à vie) ────────────────────────────────────────

# Spaces HF publics gratuits à vie — testés dans l'ordre
_HF_SPACES = [
    "waloneai/SDXT-Image-To-Video",
    "stabilityai/stable-video-diffusion",
    "multimodalart/stable-video-diffusion",
    "wangfuyun/AnimateLCM-SVD",
]


def _space_predict(space_id: str, image_path: str,
                   hf_token: Optional[str] = None,
                   status_cb=None) -> Optional[str]:
    """
    Appelle un HuggingFace Space public.
    1er essai : gradio_client (API discovery automatique)
    2e essai  : HTTP direct Gradio 4.x queue+SSE (si pas d'API exposée)
    Gratuit à vie — aucun crédit, aucun compte requis.
    """
    if status_cb:
        status_cb(f"🔌 Connexion à {space_id}...")
    logger.info(f"[HF Space] Connexion à {space_id}")

    try_http = False

    # ── Méthode 1 : gradio_client ────────────────────────────────────────────
    try:
        from gradio_client import Client, handle_file

        client = Client(space_id, hf_token=hf_token or None, verbose=False)

        endpoints = []
        try:
            api = client.view_api(print_info=False, return_format="dict")
            endpoints = list(api.get("named_endpoints", {}).keys())
            logger.info(f"[HF Space] Endpoints: {endpoints}")
        except Exception:
            pass

        for ep in (endpoints or ["/predict", "/video", "/generate", "/run", "/infer"]):
            try:
                if status_cb:
                    status_cb(f"🎬 {space_id} → {ep}...")
                result = client.predict(
                    handle_file(str(Path(image_path).resolve())),
                    api_name=ep,
                )
                if result:
                    path = result[0] if isinstance(result, (list, tuple)) else result
                    path = str(path)
                    if Path(path).exists() and Path(path).stat().st_size > 10_000:
                        logger.info(f"[HF Space] ✅ {space_id} ({ep})")
                        if status_cb:
                            status_cb(f"✅ {space_id} — succès!")
                        return path
            except Exception as ep_err:
                logger.debug(f"[HF Space] {space_id} {ep}: {ep_err}")
                continue

        try_http = True  # connecté mais aucun endpoint fonctionnel

    except Exception as e:
        err = str(e)
        if any(k in err for k in ("getaddrinfo", "Name or service", "nodename", "No address")):
            logger.warning(f"[HF Space] {space_id}: DNS introuvable")
            return None
        if any(k in err for k in ("Could not fetch api info", "No API found", "Expecting value")):
            logger.info(f"[HF Space] {space_id} sans API Gradio → HTTP direct")
            try_http = True
        else:
            logger.warning(f"[HF Space] {space_id}: {err[:100]}")
            if status_cb:
                status_cb(f"⚠️ {space_id}: {err[:60]}")
            return None

    if not try_http:
        return None

    # ── Méthode 2 : HTTP direct (Gradio 3/4/5 multi-versions) ───────────────
    import base64, uuid, json as _json
    try:
        import httpx
    except ImportError:
        return None

    owner, name = space_id.split("/", 1)
    space_url = f"https://{owner}-{name}.hf.space".replace("_", "-").lower()

    with open(image_path, "rb") as f:
        img_b64 = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()

    hdrs = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    session_hash = uuid.uuid4().hex[:8]
    data_input = [{"name": "image.jpg", "data": img_b64}]

    def _unwrap(v):
        for _ in range(4):
            if not isinstance(v, dict):
                break
            v = (v.get("video") or v.get("url") or v.get("name") or
                 v.get("path") or next(iter(v.values()), None))
        return v if isinstance(v, str) and v else None

    def _save(http_client, v):
        if not v:
            return None
        if v.startswith("http"):
            try:
                r = http_client.get(v, timeout=120.0)
                if r.status_code == 200 and len(r.content) > 10_000:
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                        tmp.write(r.content)
                        return tmp.name
            except Exception:
                pass
        elif Path(v).exists() and Path(v).stat().st_size > 10_000:
            return v
        return None

    if status_cb:
        status_cb(f"🌐 {space_id} — connexion directe...")

    try:
        with httpx.Client(headers=hdrs, follow_redirects=True) as http:

            # Lire le bon chemin API depuis /config (peut différer du root)
            api_base = space_url
            try:
                cfg = (http.get(f"{space_url}/config", timeout=10.0).json() or {})
                root = cfg.get("root", "") or ""
                if root.startswith("http"):
                    api_base = root.rstrip("/")
                elif root and root not in ("/", ""):
                    api_base = f"{space_url}{root.rstrip('/')}"
            except Exception:
                pass

            # ── Gradio 5.x: POST /call/{fn} → SSE /call/{fn}/{event_id} ─────
            for fn in ("predict", "video", "generate", "infer", "run"):
                try:
                    r5 = http.post(f"{api_base}/call/{fn}",
                                  json={"data": data_input}, timeout=30.0)
                    if r5.status_code != 200:
                        continue
                    event_id = (r5.json() or {}).get("event_id", "")
                    if not event_id:
                        continue
                    logger.info(f"[HF Space] Gradio5 {space_id} /call/{fn} → {event_id}")
                    with http.stream("GET", f"{api_base}/call/{fn}/{event_id}",
                                    timeout=180.0) as resp:
                        for line in resp.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                ev = _json.loads(line[5:])
                            except Exception:
                                continue
                            ev_type = ""
                            if isinstance(ev, dict):
                                ev_type = ev.get("type", ev.get("msg", ""))
                            elif isinstance(ev, list):
                                v = _unwrap(ev[0] if ev else None)
                                if v:
                                    p = _save(http, v)
                                    if p:
                                        logger.info(f"[HF Space HTTP] ✅ {space_id} G5/{fn}")
                                        if status_cb:
                                            status_cb(f"✅ {space_id}!")
                                        return p
                                continue
                            if "complete" in ev_type or "succeed" in ev_type:
                                out = ev.get("output") or ev.get("data") or []
                                if isinstance(out, dict):
                                    out = out.get("data", [])
                                v = _unwrap(out[0] if out else None)
                                p = _save(http, v) if v else None
                                if p:
                                    logger.info(f"[HF Space HTTP] ✅ {space_id} G5/{fn}")
                                    if status_cb:
                                        status_cb(f"✅ {space_id}!")
                                    return p
                                break
                            elif "error" in ev_type:
                                logger.debug(f"[HF Space] {space_id} G5 error: {ev}")
                                break
                    break
                except Exception as e5:
                    logger.debug(f"[HF Space] {space_id} /call/{fn}: {e5}")
                    continue

            # ── Gradio 4.x: POST /queue/join → SSE /queue/data ───────────────
            try:
                r4 = http.post(f"{api_base}/queue/join", json={
                    "data": data_input, "fn_index": 0, "session_hash": session_hash,
                }, timeout=30.0)
                if r4.status_code == 200:
                    with http.stream("GET",
                            f"{api_base}/queue/data?session_hash={session_hash}",
                            timeout=180.0) as resp:
                        for line in resp.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                ev = _json.loads(line[5:])
                            except Exception:
                                continue
                            if ev.get("msg") == "process_completed":
                                out = ev.get("output", {}).get("data", [])
                                v = _unwrap(out[0] if out else None)
                                p = _save(http, v) if v else None
                                if p:
                                    logger.info(f"[HF Space HTTP] ✅ {space_id} G4")
                                    if status_cb:
                                        status_cb(f"✅ {space_id}!")
                                    return p
                                break
                            elif ev.get("msg") in ("process_errored", "queue_full"):
                                break
            except Exception as e4:
                logger.debug(f"[HF Space] {space_id} G4 queue: {e4}")

            # ── Gradio 3.x: POST /run/predict (synchrone) ────────────────────
            try:
                r3 = http.post(f"{api_base}/run/predict",
                              json={"data": data_input, "fn_index": 0},
                              timeout=180.0)
                if r3.status_code == 200:
                    out = r3.json().get("data", [])
                    v = _unwrap(out[0] if out else None)
                    p = _save(http, v) if v else None
                    if p:
                        logger.info(f"[HF Space HTTP] ✅ {space_id} G3")
                        if status_cb:
                            status_cb(f"✅ {space_id}!")
                        return p
            except Exception as e3:
                logger.debug(f"[HF Space] {space_id} /run/predict: {e3}")

    except Exception as e:
        logger.warning(f"[HF Space HTTP] {space_id}: {e}")
        if status_cb:
            status_cb(f"⚠️ {space_id}: {str(e)[:60]}")

    return None


async def _call_hf_spaces(image_path: str, hf_token: str = "",
                           on_progress=None) -> Optional[str]:
    """Essaie tous les spaces HF en séquence."""
    loop = asyncio.get_event_loop()

    def _status(msg):
        logger.info(f"[HF Spaces] {msg}")
        if on_progress:
            on_progress(None, msg)

    for space_id in _HF_SPACES:
        result = await loop.run_in_executor(
            None, _space_predict, space_id, image_path, hf_token or None, _status
        )
        if result:
            return result

    return None


# ── Prompt ─────────────────────────────────────────────────────────────────────

async def _build_motion_prompt(description: str, domain: str = "img2video") -> str:
    """LLM génère le meilleur motion prompt possible pour l'image."""
    from llm.client import chat
    from agent.self_improve import get_improvement_context

    ctx = get_improvement_context(domain=domain, max_lessons=3)
    ctx_block = f"\n\nLeçons passées:\n{ctx}" if ctx else ""

    user_msg = (
        f"Image: {description or 'scène non décrite'}{ctx_block}\n\n"
        "Génère un motion prompt EN ANGLAIS pour Stable Video Diffusion. "
        "Décris le mouvement caméra et l'animation naturelle de la scène. "
        "Exemple: 'slow cinematic dolly forward, gentle parallax motion, natural lighting'\n"
        "Réponds UNIQUEMENT avec le prompt (1 ligne max, pas de guillemets):"
    )
    loop = asyncio.get_event_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: chat(
            [{"role": "system", "content": "Tu es expert vidéo IA. Réponds UNIQUEMENT avec le prompt demandé."},
             {"role": "user", "content": user_msg}],
            temperature=0.65,
        ))
        lines = [l for l in raw.strip().splitlines() if l.strip()]
        return lines[0][:200] if lines else "slow cinematic push forward, subtle parallax, photorealistic motion"
    except Exception:
        return "slow cinematic push forward, subtle parallax, photorealistic motion"


# ── HuggingFace SVD ────────────────────────────────────────────────────────────

_hf_last_error: str = ""   # visible dans les logs UI

# ── Replicate API (SVD) ────────────────────────────────────────────────────────

async def _call_replicate_svd(image_path: str, replicate_token: str,
                               motion_prompt: str = "", duration: float = 5.0) -> Optional[str]:
    """
    Appelle Replicate stability-ai/stable-video-diffusion.
    Retourne l'URL de la vidéo générée, ou None si échec.
    """
    import httpx, base64

    with open(image_path, "rb") as f:
        img_b64 = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()

    headers = {
        "Authorization": f"Token {replicate_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "version": "3f0457e4619daac51203dedb472816fd4af51f3149fa7a9e0b5ffcf1b8172438",
        "input": {
            "image": img_b64,
            "sizing_strategy": "maintain_aspect_ratio",
            "frames_per_second": 6,
            "motion_bucket_id": 127,
            "cond_aug": 0.02,
            "decoding_t": 7,
            "video_length": "25_frames_with_svd_xt",
        }
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            # Crée la prédiction
            r = await client.post(
                "https://api.replicate.com/v1/predictions",
                headers=headers, json=payload
            )
            if r.status_code not in (200, 201):
                logger.error(f"[Replicate] Création échouée {r.status_code}: {r.text[:200]}")
                return None

            pred = r.json()
            pred_url = pred.get("urls", {}).get("get", "")
            if not pred_url:
                return None

            # Polling jusqu'au résultat (max 5 min)
            for _ in range(60):
                await asyncio.sleep(5)
                poll = await client.get(pred_url, headers=headers)
                data = poll.json()
                status = data.get("status", "")

                if status == "succeeded":
                    output = data.get("output")
                    if isinstance(output, list) and output:
                        return output[0]
                    if isinstance(output, str):
                        return output
                    return None

                elif status in ("failed", "canceled"):
                    logger.error(f"[Replicate] Prédiction échouée: {data.get('error', '')}")
                    return None

            logger.error("[Replicate] Timeout polling")
            return None

        except Exception as e:
            logger.error(f"[Replicate] Erreur: {e}")
            return None


async def _download_video(url: str, output_path: str,
                           min_dur: float, fps: int, w: int, h: int) -> bool:
    """Télécharge la vidéo depuis une URL et la convertit en 720p."""
    import httpx, shutil

    if not shutil.which("ffmpeg"):
        return False

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                return False
            return _hf_bytes_to_720p(r.content, output_path, min_dur, fps, w, h)
        except Exception as e:
            logger.error(f"[download_video] {e}")
            return False


async def _call_hf_svd(image_path: str, hf_token: str) -> Optional[bytes]:
    """
    Envoie l'image à HuggingFace SVD, reçoit la vidéo en bytes.
    Stocke le dernier message d'erreur dans _hf_last_error pour l'UI.
    """
    global _hf_last_error
    import httpx

    with open(image_path, "rb") as f:
        img_data = f.read()

    headers = {
        "Authorization": f"Bearer {hf_token}",
        "Content-Type": "image/jpeg",
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        for model in _HF_MODELS:
            url = _HF_API_BASE.format(model)
            logger.info(f"[HF-SVD] Essai: {model}")

            for attempt in range(3):
                try:
                    r = await client.post(url, headers=headers, content=img_data)

                    if r.status_code == 200:
                        if len(r.content) > 50_000:
                            logger.info(f"[HF-SVD] OK {model} ({len(r.content)//1024} KB)")
                            _hf_last_error = ""
                            return r.content
                        logger.warning(f"[HF-SVD] Réponse trop petite ({len(r.content)} B): {r.text[:100]}")
                        _hf_last_error = f"Réponse invalide du modèle ({len(r.content)} B)"
                        break

                    elif r.status_code == 401:
                        _hf_last_error = "Token HuggingFace invalide ou expiré"
                        logger.error(f"[HF-SVD] 401 Token invalide")
                        return None

                    elif r.status_code == 403:
                        _hf_last_error = "Licence SVD non acceptée — va sur huggingface.co/stabilityai/stable-video-diffusion-img2vid-xt et clique 'Agree and access repository'"
                        logger.error(f"[HF-SVD] 403 Accès refusé (licence non acceptée)")
                        return None

                    elif r.status_code == 503:
                        wait = min(int(r.headers.get("X-Wait-For-Model", "25")), 45)
                        _hf_last_error = f"Modèle en chargement ({model})..."
                        logger.info(f"[HF-SVD] Modèle en chargement, attente {wait}s")
                        await asyncio.sleep(wait)

                    elif r.status_code == 429:
                        _hf_last_error = "Rate limit HuggingFace — réessaie dans quelques minutes"
                        logger.warning("[HF-SVD] Rate limit, attente 30s")
                        await asyncio.sleep(30)

                    else:
                        _hf_last_error = f"Erreur HTTP {r.status_code}: {r.text[:120]}"
                        logger.warning(f"[HF-SVD] {model} HTTP {r.status_code}: {r.text[:150]}")
                        break

                except httpx.TimeoutException:
                    _hf_last_error = f"Timeout sur {model} (modèle trop lent)"
                    logger.warning(f"[HF-SVD] Timeout (tentative {attempt + 1}/3)")
                    if attempt < 2:
                        await asyncio.sleep(5)
                except Exception as e:
                    _hf_last_error = str(e)
                    logger.warning(f"[HF-SVD] Erreur: {e}")
                    break

    return None


# ── FFmpeg Ken Burns (fallback local) ──────────────────────────────────────────

def _ken_burns(image_path: str, output_path: str,
               duration: float = _DEFAULT_DUR, fps: int = _DEFAULT_FPS,
               w: int = _DEFAULT_W, h: int = _DEFAULT_H) -> bool:
    """
    Ken Burns cinématique: zoom progressif + pan diagonal + vignette.
    Input mis à 3x pour donner de la matière au zoom/pan.
    """
    import shutil
    if not shutil.which("ffmpeg"):
        logger.error("[KenBurns] ffmpeg introuvable")
        return False

    frames = int(duration * fps)
    # Pan en pixels dans l'espace input (= 3x output)
    pan_x = int(w * 0.25)
    pan_y = int(h * 0.15)

    vf = (
        # Mise à l'échelle 3x pour laisser de la marge au zoom + pan
        f"scale={w * 3}:{h * 3}:force_original_aspect_ratio=increase,"
        f"crop={w * 3}:{h * 3},"
        # Zoom linéaire 1.0→1.35 + pan diagonal progressif
        f"zoompan="
        f"z='1.0+0.35*on/{frames}':"
        f"x='iw/2-(iw/zoom/2)+{pan_x}*on/{frames}':"
        f"y='ih/2-(ih/zoom/2)+{pan_y}*on/{frames}':"
        f"d={frames}:s={w}x{h}:fps={fps},"
        # Vignette douce (assombrit les bords → effet cinéma)
        f"vignette=PI/4"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-i", image_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-t", str(duration), "-r", str(fps),
        output_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if res.returncode != 0:
            logger.error(f"[KenBurns] {res.stderr[:300]}")
        return res.returncode == 0 and Path(output_path).exists()
    except subprocess.TimeoutExpired:
        logger.error("[KenBurns] Timeout")
        return False


# ── Conversion bytes HF → fichier 720p ────────────────────────────────────────

def _hf_bytes_to_720p(raw_bytes: bytes, output_path: str,
                       min_dur: float, fps: int, w: int, h: int) -> bool:
    """Convertit les bytes vidéo HF en fichier 720p ≥ min_dur secondes."""
    import shutil
    if not shutil.which("ffmpeg"):
        return False

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = tmp.name

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", tmp_path],
            capture_output=True, text=True, timeout=10,
        )
        actual_dur = float(probe.stdout.strip() or "0")
    except Exception:
        actual_dur = 0.0

    scale_vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    )

    if actual_dur >= min_dur:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_path,
               "-vf", scale_vf, "-c:v", "libx264", "-preset", "medium",
               "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps), output_path]
    else:
        loops = max(1, int(min_dur / max(actual_dur, 0.1)) + 1)
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-stream_loop", str(loops), "-i", tmp_path,
               "-vf", scale_vf, "-c:v", "libx264", "-preset", "medium",
               "-crf", "18", "-pix_fmt", "yuv420p",
               "-t", str(min_dur), "-r", str(fps), output_path]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        logger.error("[hf_bytes_to_720p] Timeout FFmpeg")
        Path(tmp_path).unlink(missing_ok=True)
        return False
    Path(tmp_path).unlink(missing_ok=True)
    return res.returncode == 0 and Path(output_path).exists()


# ── Évaluation + amélioration ──────────────────────────────────────────────────

async def _evaluate_and_improve(
    motion_prompt: str, success: bool, video_path: str, domain: str = "img2video"
) -> tuple[int, str]:
    """
    Score la génération via le moteur self_improve.
    Si score < seuil, le LLM améliore le prompt pour la prochaine tentative.
    Retourne (score, prompt_affiné).
    """
    from agent.self_improve import evaluate_and_learn
    from llm.client import chat

    size_kb = Path(video_path).stat().st_size // 1024 if success and Path(video_path).exists() else 0
    result_desc = (
        f"Vidéo {'générée' if success else 'ÉCHEC'}, {size_kb} KB, "
        f"motion prompt: '{motion_prompt}'"
    )
    ev = await evaluate_and_learn(
        task=f"Générer une vidéo réaliste avec le motion prompt: '{motion_prompt}'",
        result=result_desc,
        domain=domain,
    )
    score = ev.get("score", 5)
    weaknesses = ev.get("weaknesses", "")

    refined = motion_prompt
    if score < _QUALITY_THRESHOLD and weaknesses:
        loop = asyncio.get_event_loop()
        try:
            raw = await loop.run_in_executor(None, lambda: chat(
                [{"role": "user", "content": (
                    f"Améliore ce motion prompt pour Stable Video Diffusion.\n"
                    f"Actuel: {motion_prompt}\n"
                    f"Problèmes: {weaknesses}\n"
                    f"Donne UNIQUEMENT le prompt amélioré (1 ligne, anglais):"
                )}],
                temperature=0.5,
            ))
            refined = raw.strip().splitlines()[0][:200]
        except Exception:
            pass

    return score, refined


# ── Point d'entrée public ──────────────────────────────────────────────────────

async def generate_realistic_video(
    image_path: str,
    output_path: str,
    description: str = "",
    motion_prompt: str = "",
    duration: float = _DEFAULT_DUR,
    fps: int = _DEFAULT_FPS,
    width: int = _DEFAULT_W,
    height: int = _DEFAULT_H,
    on_progress=None,
    max_loops: int = 3,
) -> dict:
    """
    Génère une vidéo réaliste à partir d'une image.

    Essaie d'abord HuggingFace SVD (si HF_API_TOKEN configuré),
    puis bascule automatiquement sur FFmpeg Ken Burns.
    Se ré-essaie jusqu'à max_loops fois en affinant le prompt.

    Returns:
        {"video_path", "method", "score", "attempts", "history"}
        ou {"error", "history"} en cas d'échec total.
    """
    from config import config

    hf_token = getattr(config, "HF_API_TOKEN", "")
    replicate_token = getattr(config, "REPLICATE_API_TOKEN", "")
    out_dir = Path(output_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if not motion_prompt:
        if on_progress:
            on_progress(0.05, "🤖 Génération du motion prompt...")
        motion_prompt = await _build_motion_prompt(description or Path(image_path).stem)
        logger.info(f"[img2video] Motion prompt: {motion_prompt}")

    best_path: Optional[str] = None
    best_score = 0
    history = []

    for loop_i in range(max_loops):
        pct = 0.10 + 0.75 * loop_i / max_loops
        if on_progress:
            on_progress(pct, f"🎬 Génération vidéo — tentative {loop_i + 1}/{max_loops}...")

        attempt_out = str(out_dir / f"_tmp_{loop_i}.mp4")
        success = False
        method = "none"

        # ── Priorité 1 : HuggingFace Spaces (gratuit à vie) ──────────
        if not success:
            if on_progress:
                on_progress(pct + 0.03, "🤗 HuggingFace Spaces — génération IA gratuite...")
            try:
                space_video = await _call_hf_spaces(image_path, hf_token, on_progress=on_progress)
                if space_video:
                    dest = attempt_out.replace(".mp4", "_space.mp4")
                    success = _hf_bytes_to_720p(
                        Path(space_video).read_bytes(), attempt_out, duration, fps, width, height
                    )
                    if success:
                        method = "hf_space_svd"
                    else:
                        # Fichier déjà une vidéo valide — juste copier + redimensionner
                        shutil.copy(space_video, attempt_out)
                        success = Path(attempt_out).exists()
                        if success:
                            method = "hf_space_svd"
                if not success and on_progress:
                    on_progress(None, "⚠️ HF Spaces indisponibles — essai Replicate...")
            except Exception as e:
                logger.warning(f"[img2video] HF Spaces erreur: {e}")

        # ── Priorité 2 : Replicate SVD (crédits gratuits) ─────────────
        if replicate_token and not success:
            if on_progress:
                on_progress(pct + 0.03, "🎥 Replicate SVD — génération IA réaliste...")
            try:
                video_url = await _call_replicate_svd(
                    image_path, replicate_token, motion_prompt, duration
                )
                if video_url:
                    success = await _download_video(video_url, attempt_out, duration, fps, width, height)
                    if success:
                        method = "replicate_svd"
                else:
                    if on_progress:
                        on_progress(None, "⚠️ Replicate SVD échoué — essai HuggingFace...")
            except Exception as e:
                logger.warning(f"[img2video] Replicate erreur: {e}")

        # ── Essai HuggingFace SVD (priorité 2) ────────────────────────
        if hf_token and not success:
            if on_progress:
                on_progress(pct + 0.05, "🤗 HuggingFace SVD — génération IA en cours...")
            try:
                video_bytes = await _call_hf_svd(image_path, hf_token)
                if _hf_last_error and on_progress:
                    on_progress(None, f"⚠️ HF SVD: {_hf_last_error}")
                if video_bytes:
                    success = _hf_bytes_to_720p(
                        video_bytes, attempt_out, duration, fps, width, height
                    )
                    if success:
                        method = "huggingface_svd"
            except Exception as e:
                logger.warning(f"[img2video] HF erreur: {e}")

        # ── Fallback FFmpeg Ken Burns ──────────────────────────────────
        if not success:
            if on_progress:
                on_progress(pct + 0.10, "🎞️ Fallback Ken Burns (FFmpeg)...")
            success = _ken_burns(image_path, attempt_out, duration, fps, width, height)
            if success:
                method = "ffmpeg_ken_burns"

        # ── Évaluation + amélioration du prompt ───────────────────────
        score, motion_prompt = await _evaluate_and_improve(
            motion_prompt, success, attempt_out
        )
        history.append({"loop": loop_i + 1, "method": method, "score": score, "success": success})

        if success and score > best_score:
            best_score = score
            best_path = attempt_out

        if success and score >= _QUALITY_THRESHOLD:
            logger.info(f"[img2video] Seuil qualité {score}/10 atteint (loop {loop_i + 1})")
            break
        elif success:
            logger.info(f"[img2video] Score {score}/10 < {_QUALITY_THRESHOLD}, prompt affiné")

    # ── Finalisation ───────────────────────────────────────────────────
    if best_path and Path(best_path).exists():
        import shutil
        shutil.move(best_path, output_path)
        for i in range(max_loops):
            Path(out_dir / f"_tmp_{i}.mp4").unlink(missing_ok=True)

        final_method = next(
            (h["method"] for h in reversed(history) if h["success"]), method
        )
        if on_progress:
            on_progress(1.0, f"✅ Vidéo prête — méthode: {final_method}, score: {best_score}/10")

        return {
            "video_path": output_path,
            "method": final_method,
            "score": best_score,
            "attempts": len(history),
            "history": history,
        }

    return {"error": "Génération échouée après tous les essais", "history": history}
