"""
Pipeline vidéo — FFmpeg ultraléger + transitions xfade.
Fallback: GIF animé PIL si FFmpeg indisponible ou OOM.
"""
import subprocess
import shutil
import logging
from pathlib import Path
from config import config

logger = logging.getLogger(__name__)

_FFMPEG_BASE = [
    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
    "-refs", "1", "-bf", "0", "-pix_fmt", "yuv420p",
]


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-y", "-loglevel", "warning"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _ffprobe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _make_segment(img_path: str, audio_path: str | None, seg_path: str,
                  duration: float, w: int, h: int, fps: int) -> bool:
    if audio_path and Path(audio_path).exists():
        r = _ffmpeg(
            "-loop", "1", "-i", img_path,
            "-i", audio_path,
            *_FFMPEG_BASE,
            "-c:a", "aac", "-b:a", "96k",
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            "-t", str(duration), "-r", str(fps),
            seg_path,
        )
    else:
        r = _ffmpeg(
            "-loop", "1", "-i", img_path,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            *_FFMPEG_BASE,
            "-c:a", "aac", "-b:a", "64k",
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            "-t", str(duration), "-r", str(fps), "-shortest",
            seg_path,
        )
    if r.returncode != 0:
        logger.error(f"Segment échoué: {r.stderr[:300]}")
    return r.returncode == 0 and Path(seg_path).exists()


def _concat_with_transitions(segments: list, output_path: str, transition: str = "fade") -> bool:
    """Concat via filter_complex xfade (2 segments minimum)."""
    if len(segments) == 1:
        shutil.copy(segments[0], output_path)
        return True

    if len(segments) == 2:
        seg_a, seg_b = segments
        dur_a = _ffprobe_duration(seg_a)
        if dur_a < 0.5:
            dur_a = 4.0
        offset = max(0.1, dur_a - 0.5)
        r = _ffmpeg(
            "-i", seg_a, "-i", seg_b,
            "-filter_complex",
            f"[0:v][1:v]xfade=transition={transition}:duration=0.5:offset={offset:.2f}[v];"
            "[0:a][1:a]acrossfade=d=0.5[a]",
            "-map", "[v]", "-map", "[a]",
            *_FFMPEG_BASE, "-c:a", "aac", "-b:a", "96k",
            output_path,
        )
        return r.returncode == 0

    # 3+ segments: concat simple (xfade sur >2 segments = très lourd en RAM)
    concat_file = str(Path(output_path).parent / "concat.txt")
    with open(concat_file, "w") as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")
    r = _ffmpeg("-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", output_path)
    return r.returncode == 0


def create_video(
    slides: list,
    output_name: str,
    duration_per_slide: float = 4.0,
    lang: str = "fr",
    add_audio: bool = True,
    theme: str = "dark",
    fps: int = 24,
    transition: str = "fade",
) -> str:
    from video.image_gen import save_slide

    out_dir = Path(config.OUTPUT_DIR) / "videos"
    tmp_dir = out_dir / f"tmp_{output_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    w, h = config.VIDEO_RESOLUTION
    output_path = str(out_dir / f"{output_name}.mp4")

    if not _has_ffmpeg():
        logger.info("FFmpeg absent — fallback GIF.")
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
                dur = _ffprobe_duration(audio_path)
                if dur > 0:
                    duration = max(dur + 0.5, duration_per_slide)
            except Exception as e:
                logger.warning(f"TTS slide {i}: {e}")
                audio_path = None

        ok = _make_segment(img_path, audio_path, seg_path, duration, w, h, fps)
        if not ok:
            logger.warning(f"Slide {i} échouée — fallback GIF.")
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
            return _create_gif_fallback(slides, output_name, theme, out_dir)
        segments.append(seg_path)

    if not segments:
        return _create_gif_fallback(slides, output_name, theme, out_dir)

    ok = _concat_with_transitions(segments, output_path, transition)
    shutil.rmtree(str(tmp_dir), ignore_errors=True)

    if ok and Path(output_path).exists():
        logger.info(f"Vidéo créée: {output_path}")
        return output_path

    return _create_gif_fallback(slides, output_name, theme, out_dir)


def _create_gif_fallback(slides: list, output_name: str, theme: str, out_dir: Path) -> str:
    from video.image_gen import create_slide
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    gif_path = str(out_dir / f"{output_name}.gif")

    frames = []
    for i, text in enumerate(slides):
        img = create_slide(text, i, len(slides), theme=theme)
        frames.append(img.resize((320, 568), Image.LANCZOS))

    if not frames:
        return ""

    frames[0].save(
        gif_path, save_all=True, append_images=frames[1:],
        duration=3000, loop=0, optimize=True,
    )
    logger.info(f"GIF créé: {gif_path}")
    return gif_path
