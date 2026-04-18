"""
Pipeline vidéo — FFmpeg direct via subprocess pour qualité maximale.
Assemble: images Pillow → MP4 avec voix gTTS + transitions.
"""
import subprocess
import shutil
import logging
from pathlib import Path
from config import config

logger = logging.getLogger(__name__)


def _ffmpeg(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Appel FFmpeg direct."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def create_video(
    slides: list[str],
    output_name: str,
    duration_per_slide: float = 4.0,
    lang: str = "fr",
    add_audio: bool = True,
    theme: str = "dark",
    fps: int = 24,
) -> str:
    from video.image_gen import save_slide
    from video.tts import text_to_speech

    if not _has_ffmpeg():
        logger.warning("FFmpeg non trouvé, fallback MoviePy.")
        return _create_video_moviepy(slides, output_name, duration_per_slide, lang, add_audio, theme)

    out_dir = Path(config.OUTPUT_DIR) / "videos"
    tmp_dir = out_dir / f"tmp_{output_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    video_segments = []

    for i, slide_text in enumerate(slides):
        img_path = str(tmp_dir / f"slide_{i:03d}.png")
        save_slide(slide_text, img_path, index=i, total=len(slides), theme=theme)

        audio_path = str(tmp_dir / f"audio_{i:03d}.mp3")
        seg_path = str(tmp_dir / f"seg_{i:03d}.mp4")

        if add_audio:
            try:
                text_to_speech(slide_text, audio_path, lang=lang)
                # Durée réelle de l'audio
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                    capture_output=True, text=True,
                )
                audio_dur = float(probe.stdout.strip() or duration_per_slide) + 0.5
                duration = max(audio_dur, duration_per_slide)

                _ffmpeg(
                    "-loop", "1", "-i", img_path,
                    "-i", audio_path,
                    "-c:v", "libx264", "-tune", "stillimage",
                    "-c:a", "aac", "-b:a", "128k",
                    "-pix_fmt", "yuv420p",
                    "-vf", f"scale={config.VIDEO_RESOLUTION[0]}:{config.VIDEO_RESOLUTION[1]}:force_original_aspect_ratio=decrease,pad={config.VIDEO_RESOLUTION[0]}:{config.VIDEO_RESOLUTION[1]}:(ow-iw)/2:(oh-ih)/2",
                    "-t", str(duration),
                    "-r", str(fps),
                    seg_path,
                )
            except Exception as e:
                logger.warning(f"Audio slide {i} échoué: {e}, segment muet.")
                add_audio_for_seg = False
                _ffmpeg(
                    "-loop", "1", "-i", img_path,
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p",
                    "-vf", f"scale={config.VIDEO_RESOLUTION[0]}:{config.VIDEO_RESOLUTION[1]}",
                    "-t", str(duration_per_slide),
                    "-r", str(fps), "-shortest",
                    seg_path,
                )
        else:
            _ffmpeg(
                "-loop", "1", "-i", img_path,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "libx264", "-c:a", "aac",
                "-pix_fmt", "yuv420p",
                "-vf", f"scale={config.VIDEO_RESOLUTION[0]}:{config.VIDEO_RESOLUTION[1]}",
                "-t", str(duration_per_slide),
                "-r", str(fps), "-shortest",
                seg_path,
            )

        video_segments.append(seg_path)

    # Concaténation des segments
    output_path = str(out_dir / f"{output_name}.mp4")
    if len(video_segments) == 1:
        shutil.copy(video_segments[0], output_path)
    else:
        concat_list = str(tmp_dir / "concat.txt")
        with open(concat_list, "w") as f:
            for seg in video_segments:
                f.write(f"file '{seg}'\n")
        _ffmpeg(
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy",
            output_path,
        )

    shutil.rmtree(str(tmp_dir), ignore_errors=True)
    logger.info(f"Vidéo créée: {output_path}")
    return output_path


def _create_video_moviepy(slides, output_name, duration_per_slide, lang, add_audio, theme) -> str:
    """Fallback MoviePy si FFmpeg n'est pas disponible."""
    from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
    from video.image_gen import save_slide
    from video.tts import text_to_speech
    import tempfile, os

    out_dir = Path(config.OUTPUT_DIR) / "videos"
    out_dir.mkdir(parents=True, exist_ok=True)

    clips = []
    tmp_files = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, text in enumerate(slides):
            img_path = f"{tmp}/slide_{i}.png"
            save_slide(text, img_path, i, len(slides), theme)
            tmp_files.append(img_path)

            if add_audio:
                audio_path = f"{tmp}/audio_{i}.mp3"
                try:
                    text_to_speech(text, audio_path, lang=lang)
                    audio = AudioFileClip(audio_path)
                    duration = max(audio.duration + 0.5, duration_per_slide)
                    clip = ImageClip(img_path, duration=duration).set_audio(audio)
                except Exception:
                    clip = ImageClip(img_path, duration=duration_per_slide)
            else:
                clip = ImageClip(img_path, duration=duration_per_slide)
            clips.append(clip)

        output_path = str(out_dir / f"{output_name}.mp4")
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac", logger=None)

    return output_path
