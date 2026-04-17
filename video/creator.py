"""
Créateur de vidéo — assemble des slides texte + voix en MP4 via MoviePy.
100% gratuit, 100% local, aucune API externe.
"""
import os
import textwrap
from pathlib import Path
from typing import Optional
from config import config


def create_text_image(text: str, size: tuple = (1080, 1920), bg_color=(15, 15, 15), text_color=(255, 255, 255)) -> "Image":
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, color=bg_color)
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
        small_font = font

    # Wrap text
    wrapped = textwrap.fill(text, width=20)
    lines = wrapped.split("\n")

    total_height = len(lines) * 80
    y_start = (size[1] - total_height) // 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (size[0] - w) // 2
        y = y_start + i * 80
        # Ombre
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=text_color)

    return img


def create_video(
    slides: list[str],
    output_name: str,
    duration_per_slide: float = 4.0,
    lang: str = "fr",
    add_audio: bool = True,
) -> str:
    from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
    from video.tts import text_to_speech

    out_dir = Path(config.OUTPUT_DIR) / "videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    clips = []
    for i, slide_text in enumerate(slides):
        # Image
        img = create_text_image(slide_text)
        img_path = str(tmp_dir / f"slide_{i}.png")
        img.save(img_path)

        if add_audio:
            audio_path = str(tmp_dir / f"audio_{i}.mp3")
            try:
                text_to_speech(slide_text, audio_path, lang=lang)
                audio = AudioFileClip(audio_path)
                duration = max(audio.duration + 0.5, duration_per_slide)
                clip = ImageClip(img_path, duration=duration).set_audio(audio)
            except Exception:
                clip = ImageClip(img_path, duration=duration_per_slide)
        else:
            clip = ImageClip(img_path, duration=duration_per_slide)

        clips.append(clip)

    final = concatenate_videoclips(clips, method="compose")
    output_path = str(out_dir / f"{output_name}.mp4")
    final.write_videofile(
        output_path,
        fps=24,
        codec="libx264",
        audio_codec="aac",
        logger=None,
    )

    # Nettoyage tmp
    import shutil
    shutil.rmtree(str(tmp_dir), ignore_errors=True)

    return output_path
