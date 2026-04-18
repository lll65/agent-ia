"""
Génération d'images de haute qualité pour les slides vidéo.
Utilise Pillow uniquement — 100% local, aucune API.
Produit des slides avec dégradé, ombre portée, mise en forme pro.
"""
import textwrap
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Palettes de couleurs par style
THEMES = {
    "dark": {
        "bg_top": (15, 15, 25),
        "bg_bottom": (30, 30, 50),
        "accent": (99, 102, 241),  # indigo
        "text": (255, 255, 255),
        "subtext": (180, 180, 210),
    },
    "fire": {
        "bg_top": (20, 5, 5),
        "bg_bottom": (60, 15, 5),
        "accent": (255, 80, 0),
        "text": (255, 255, 255),
        "subtext": (255, 180, 100),
    },
    "ocean": {
        "bg_top": (5, 15, 40),
        "bg_bottom": (10, 40, 80),
        "accent": (0, 180, 255),
        "text": (255, 255, 255),
        "subtext": (150, 210, 255),
    },
    "gold": {
        "bg_top": (10, 8, 3),
        "bg_bottom": (30, 25, 5),
        "accent": (212, 175, 55),
        "text": (255, 255, 255),
        "subtext": (220, 200, 120),
    },
}


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _vertical_gradient(draw: ImageDraw, size: Tuple[int, int], top: tuple, bottom: tuple):
    w, h = size
    for y in range(h):
        ratio = y / h
        r = int(top[0] + (bottom[0] - top[0]) * ratio)
        g = int(top[1] + (bottom[1] - top[1]) * ratio)
        b = int(top[2] + (bottom[2] - top[2]) * ratio)
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def create_slide(
    text: str,
    slide_index: int,
    total_slides: int,
    size: Tuple[int, int] = (1080, 1920),
    theme: str = "dark",
    subtitle: str = "",
) -> Image.Image:
    palette = THEMES.get(theme, THEMES["dark"])
    w, h = size

    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)

    # Dégradé de fond
    _vertical_gradient(draw, size, palette["bg_top"], palette["bg_bottom"])

    # Barre d'accent en bas
    bar_h = 8
    draw.rectangle([(0, h - bar_h), (w, h)], fill=palette["accent"])

    # Ligne de progression
    progress = (slide_index + 1) / total_slides
    draw.rectangle([(0, h - bar_h), (int(w * progress), h)], fill=palette["accent"])

    # Numéro de slide (top right)
    counter_font = _load_font(36)
    counter_text = f"{slide_index + 1}/{total_slides}"
    draw.text((w - 120, 50), counter_text, font=counter_font, fill=palette["subtext"])

    # Ligne décorative top
    draw.line([(80, 80), (80 + int((w - 160) * progress), 80)], fill=palette["accent"], width=3)

    # Texte principal — wrap et centrage
    font_size = _auto_font_size(text, w - 160)
    font = _load_font(font_size, bold=True)
    wrapped = textwrap.fill(text, width=_chars_per_line(font_size))
    lines = wrapped.split("\n")

    total_text_h = len(lines) * (font_size + 20)
    y_start = (h - total_text_h) // 2 - (80 if subtitle else 0)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (w - line_w) // 2

        # Ombre
        for dx, dy in [(3, 3), (4, 4)]:
            draw.text((x + dx, y_start + dy), line, font=font, fill=(0, 0, 0, 180))

        # Texte principal
        draw.text((x, y_start), line, font=font, fill=palette["text"])
        y_start += font_size + 20

    # Sous-titre
    if subtitle:
        sub_font = _load_font(40)
        sub_wrapped = textwrap.fill(subtitle, width=28)
        sub_lines = sub_wrapped.split("\n")
        sub_y = y_start + 40
        for line in sub_lines:
            bbox = draw.textbbox((0, 0), line, font=sub_font)
            line_w = bbox[2] - bbox[0]
            x = (w - line_w) // 2
            draw.text((x, sub_y), line, font=sub_font, fill=palette["subtext"])
            sub_y += 55

    # Légère vignette sur les bords
    vignette = Image.new("RGB", size, (0, 0, 0))
    vignette_draw = ImageDraw.Draw(vignette)
    margin = 120
    vignette_draw.rectangle([(margin, margin), (w - margin, h - margin)], fill=(255, 255, 255))
    vignette = vignette.filter(ImageFilter.GaussianBlur(radius=80))
    img = Image.composite(img, Image.new("RGB", size, (0, 0, 0)), vignette.convert("L"))

    return img


def _auto_font_size(text: str, max_width: int) -> int:
    words = len(text.split())
    if words <= 5:
        return 90
    if words <= 10:
        return 72
    if words <= 20:
        return 58
    return 46


def _chars_per_line(font_size: int) -> int:
    sizes = {90: 14, 72: 18, 58: 22, 46: 26}
    for fs, cpl in sorted(sizes.items(), reverse=True):
        if font_size >= fs:
            return cpl
    return 28


def save_slide(text: str, path: str, index: int = 0, total: int = 1, theme: str = "dark", subtitle: str = "") -> str:
    img = create_slide(text, index, total, theme=theme, subtitle=subtitle)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", quality=95)
    return path
