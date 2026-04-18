"""
Génération d'images pour slides vidéo — Pillow, 100% local.
Résolution réduite (480x854) pour compatibilité mémoire Windows.
"""
import textwrap
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

THEMES = {
    "dark":  {"bg_top": (15,15,25),  "bg_bottom": (30,30,50),   "accent": (99,102,241), "text": (255,255,255), "subtext": (180,180,210)},
    "fire":  {"bg_top": (20,5,5),    "bg_bottom": (60,15,5),    "accent": (255,80,0),   "text": (255,255,255), "subtext": (255,180,100)},
    "ocean": {"bg_top": (5,15,40),   "bg_bottom": (10,40,80),   "accent": (0,180,255),  "text": (255,255,255), "subtext": (150,210,255)},
    "gold":  {"bg_top": (10,8,3),    "bg_bottom": (30,25,5),    "accent": (212,175,55), "text": (255,255,255), "subtext": (220,200,120)},
}

_FONT_CANDIDATES = [
    # Windows
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _gradient(draw: ImageDraw, size: Tuple[int, int], top: tuple, bottom: tuple):
    w, h = size
    for y in range(h):
        r = int(top[0] + (bottom[0] - top[0]) * y / h)
        g = int(top[1] + (bottom[1] - top[1]) * y / h)
        b = int(top[2] + (bottom[2] - top[2]) * y / h)
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def create_slide(text: str, slide_index: int, total_slides: int,
                 size: Tuple[int, int] = (480, 854), theme: str = "dark") -> Image.Image:
    palette = THEMES.get(theme, THEMES["dark"])
    w, h = size

    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    _gradient(draw, size, palette["bg_top"], palette["bg_bottom"])

    # Progress bar
    bar_h = 5
    progress = (slide_index + 1) / total_slides
    draw.rectangle([(0, h - bar_h), (w, h)], fill=(40, 40, 60))
    draw.rectangle([(0, h - bar_h), (int(w * progress), h)], fill=palette["accent"])

    # Counter
    counter_font = _load_font(20)
    draw.text((w - 70, 25), f"{slide_index+1}/{total_slides}", font=counter_font, fill=palette["subtext"])

    # Accent line
    draw.line([(40, 45), (40 + int((w - 80) * progress), 45)], fill=palette["accent"], width=2)

    # Main text
    words = len(text.split())
    font_size = 52 if words <= 5 else 40 if words <= 10 else 32 if words <= 20 else 26
    font = _load_font(font_size)
    chars_per_line = {52: 14, 40: 18, 32: 22, 26: 26}.get(font_size, 26)
    wrapped = textwrap.fill(text, width=chars_per_line)
    lines = wrapped.split("\n")
    total_text_h = len(lines) * (font_size + 12)
    y = (h - total_text_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = (w - lw) // 2
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))  # shadow
        draw.text((x, y), line, font=font, fill=palette["text"])
        y += font_size + 12

    return img


def save_slide(text: str, path: str, index: int = 0, total: int = 1,
               theme: str = "dark", subtitle: str = "") -> str:
    img = create_slide(text, index, total, theme=theme)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path
