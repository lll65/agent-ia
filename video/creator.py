"""
Pipeline vidéo — FFmpeg avec paramètres ultra-légers pour Windows.
Fallback PIL+imageio si FFmpeg manque de mémoire.
"""
import subprocess
import shutil
import logging
from pathlib import Path
from config import config

logger = logging.getLogger(__name__)

# FFmpeg léger: ultrafast, 1 ref frame, pas de B-frames → ~60% moins de RAM
_FFMPEG_LOW_MEM = [
    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
    "-refs", "1", "-bf", "0", "-pix_fmt", "yuv420p",
]


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def create_video(
    slides: list,
    output_name: str,
    duration_per_slide: float = 4.0,
    lang: str = "fr",
    add_audio: bool = True,
    theme: str = "dark",
    fps: int = 24,
) -> str:
    from video.image_gen import save_slide

    out_dir = Path(config.OUTPUT_DIR) / "videos"
    tmp_dir = out_dir / f"tmp_{output_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    w, h = config.VIDEO_RESOLUTION
    output_path = str(out_dir / f"{output_name}.mp4")

    if not _has_ffmpeg():
        return _create_gif_fallback(slides, output_name, theme, out_dir)

    segments = []
    for i, text in enumerate(slides):
        img_path = str(tmp_dir / f"slide_{i:03d}.png")
        seg_path = str(tmp_dir / f"seg_{i:03d}.mp4")
        save_slide(text, img_path, index=i, total=len(slides), theme=theme)

        duration = duration_per_slide
        audio_path = None

        if add_audio:
            try:
                from video.tts import text_to_speech
                audio_path = str(tmp_dir / f"audio_{i:03d}.mp3")
                text_to_speech(text, audio_path, lang=lang)
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                    capture_output=True, text=True,
                )
                duration = max(float(probe.stdout.strip() or duration_per_slide) + 0.5, duration_per_slide)
            except Exception as e:
                logger.warning(f"TTS slide {i} échoué: {e}")
                audio_path = None

        if audio_path and Path(audio_path).exists():
            result = _ffmpeg(
                "-loop", "1", "-i", img_path,
                "-i", audio_path,
                *_FFMPEG_LOW_MEM,
                "-c:a", "aac", "-b:a", "96k",
                "-vf", f"scale={w}:{h}",
                "-t", str(duration), "-r", str(fps),
                seg_path,
            )
        else:
            result = _ffmpeg(
                "-loop", "1", "-i", img_path,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                *_FFMPEG_LOW_MEM,
                "-c:a", "aac", "-b:a", "64k",
                "-vf", f"scale={w}:{h}",
                "-t", str(duration), "-r", str(fps), "-shortest",
                seg_path,
            )

        if result.returncode != 0 or not Path(seg_path).exists():
            logger.error(f"Segment {i} échoué: {result.stderr[:300]}")
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
            return _create_gif_fallback(slides, output_name, theme, out_dir)

        segments.append(seg_path)

    if not segments:
        return _create_gif_fallback(slides, output_name, theme, out_dir)

    if len(segments) == 1:
        shutil.copy(segments[0], output_path)
    else:
        concat_file = str(tmp_dir / "concat.txt")
        with open(concat_file, "w") as f:
            for seg in segments:
                f.write(f"file '{seg}'\n")
        _ffmpeg("-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", output_path)

    shutil.rmtree(str(tmp_dir), ignore_errors=True)
    logger.info(f"Vidéo créée: {output_path}")
    return output_path


def _create_gif_fallback(slides: list, output_name: str, theme: str, out_dir: Path) -> str:
    """Fallback: GIF animé si FFmpeg échoue — aucune RAM supplémentaire."""
    from video.image_gen import create_slide
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    gif_path = str(out_dir / f"{output_name}.gif")

    frames = []
    for i, text in enumerate(slides):
        img = create_slide(text, i, len(slides), theme=theme)
        # Resize pour GIF plus léger
        img = img.resize((320, 568), Image.LANCZOS)
        frames.append(img)

    if frames:
        frames[0].save(
            gif_path, save_all=True, append_images=frames[1:],
            duration=3000, loop=0, optimize=True,
        )
        logger.info(f"GIF créé (fallback): {gif_path}")
        return gif_path

    return ""
