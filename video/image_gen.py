"""
Génération de slides vidéo professionnelles — Pillow, 100% local.
Layouts différenciés: intro / contenu / CTA / stat / citation.
"""
import textwrap
import math
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ── Palettes ──────────────────────────────────────────────────────────────────

THEMES = {
    "dark": {
        "bg_top": (10, 10, 20), "bg_bottom": (25, 25, 45),
        "accent": (99, 102, 241), "accent2": (167, 139, 250),
        "text": (255, 255, 255), "subtext": (180, 180, 210),
        "card": (30, 30, 55, 200),
    },
    "fire": {
        "bg_top": (15, 5, 0), "bg_bottom": (50, 10, 0),
        "accent": (255, 90, 0), "accent2": (255, 200, 0),
        "text": (255, 255, 255), "subtext": (255, 180, 100),
        "card": (40, 10, 0, 200),
    },
    "ocean": {
        "bg_top": (0, 10, 30), "bg_bottom": (0, 40, 80),
        "accent": (0, 180, 255), "accent2": (100, 220, 255),
        "text": (255, 255, 255), "subtext": (150, 210, 255),
        "card": (0, 20, 50, 200),
    },
    "gold": {
        "bg_top": (8, 6, 2), "bg_bottom": (25, 20, 3),
        "accent": (212, 175, 55), "accent2": (255, 220, 100),
        "text": (255, 255, 255), "subtext": (220, 200, 120),
        "card": (20, 15, 0, 200),
    },
}

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = _FONT_CANDIDATES if bold else _FONT_CANDIDATES[::-1]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _gradient(draw: ImageDraw.Draw, size: Tuple[int, int], top: tuple, bottom: tuple):
    w, h = size
    for y in range(h):
        t = y / h
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def _draw_progress(draw, w, h, progress, accent):
    bar_h = 6
    y0, y1 = h - bar_h, h
    draw.rectangle([(0, y0), (w, y1)], fill=(40, 40, 60))
    bar_w = max(20, int(w * progress))
    # Gradient progress bar
    for x in range(bar_w):
        t = x / bar_w
        r = int(accent[0] * (0.6 + 0.4 * t))
        g = int(accent[1] * (0.6 + 0.4 * t))
        b = int(accent[2] * (0.6 + 0.4 * t))
        draw.line([(x, y0), (x, y1)], fill=(r, g, b))


def _draw_geometric(draw, w, h, accent, accent2):
    """Décoration géométrique subtile en arrière-plan."""
    a1 = accent + (18,)
    a2 = accent2 + (12,)
    # Cercle en haut à droite
    draw.ellipse([(w - 200, -100), (w + 50, 150)], outline=a1, width=2)
    draw.ellipse([(w - 160, -60), (w + 10, 110)], outline=a2, width=1)
    # Ligne diagonale en bas à gauche
    draw.line([(-20, h - 80), (160, h + 40)], fill=a1, width=2)
    draw.line([(0, h - 40), (120, h + 60)], fill=a2, width=1)


def _text_block(draw, lines, font, x_center, y_start, w, color, shadow=True, line_spacing=1.3):
    font_size = font.size if hasattr(font, "size") else 30
    step = int(font_size * line_spacing)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = x_center - lw // 2
        if shadow:
            draw.text((x + 2, y_start + 2), line, font=font, fill=(0, 0, 0, 120))
        draw.text((x, y_start), line, font=font, fill=color)
        y_start += step
    return y_start


# ── Layouts ──────────────────────────────────────────────────────────────────

def _layout_intro(draw, img, w, h, text, palette, index, total):
    """Slide titre — grand, centré, ligne décorative."""
    accent = palette["accent"]
    accent2 = palette["accent2"]

    _draw_geometric(draw, w, h, accent, accent2)

    # Ligne d'accroche en haut
    draw.line([(40, 60), (w - 40, 60)], fill=accent + (60,), width=1)

    # Numéro de slide
    fn = _load_font(18, bold=False)
    draw.text((w // 2 - 15, 30), f"{index+1} / {total}", font=fn, fill=palette["subtext"])

    # Titre principal
    words = text.split()
    font_size = 64 if len(words) <= 4 else 52 if len(words) <= 7 else 42
    font = _load_font(font_size)
    wrapped = textwrap.fill(text, width=max(10, int(16 * 52 / font_size)))
    lines = wrapped.split("\n")
    total_h = len(lines) * int(font_size * 1.3)
    y = (h - total_h) // 2 - 20
    _text_block(draw, lines, font, w // 2, y, w, palette["text"])

    # Trait accentué sous le titre
    accent_line_y = y + total_h + 20
    line_w = min(240, w - 80)
    lx = (w - line_w) // 2
    draw.rectangle([(lx, accent_line_y), (lx + line_w, accent_line_y + 4)], fill=accent)

    _draw_progress(draw, w, h, (index + 1) / total, accent)


def _layout_content(draw, img, w, h, text, palette, index, total):
    """Slide contenu — numéro en haut, texte centré, carte colorée."""
    accent = palette["accent"]

    # Numéro de point grand et stylisé
    num_font = _load_font(90)
    num_str = str(index)
    nb = draw.textbbox((0, 0), num_str, font=num_font)
    nw = nb[2] - nb[0]
    draw.text((w // 2 - nw // 2, 30), num_str, font=num_font,
              fill=accent + (40,) if len(accent) == 3 else accent)

    # Ligne séparatrice
    draw.line([(60, 140), (w - 60, 140)], fill=accent, width=2)

    # Texte principal
    font_size = 40 if len(text) < 60 else 32 if len(text) < 120 else 26
    font = _load_font(font_size)
    wrapped = textwrap.fill(text, width=int(22 * 32 / font_size))
    lines = wrapped.split("\n")
    total_h = len(lines) * int(font_size * 1.4)
    y = max(160, (h - total_h) // 2)
    _text_block(draw, lines, font, w // 2, y, w, palette["text"], line_spacing=1.4)

    _draw_progress(draw, w, h, (index + 1) / total, accent)


def _layout_stat(draw, img, w, h, text, palette, index, total):
    """Slide statistique — chiffre en énorme, contexte en petit."""
    accent = palette["accent"]
    accent2 = palette["accent2"]

    _draw_geometric(draw, w, h, accent, accent2)

    # Cherche un nombre dans le texte pour le mettre en valeur
    import re
    numbers = re.findall(r'\d+[%x€$k]?', text)
    if numbers:
        big = numbers[0]
        rest = text.replace(big, "").strip()
        # Grand chiffre
        big_font = _load_font(110)
        bb = draw.textbbox((0, 0), big, font=big_font)
        bw = bb[2] - bb[0]
        by = h // 2 - 90
        draw.text((w // 2 - bw // 2 + 2, by + 2), big, font=big_font, fill=(0, 0, 0))
        draw.text((w // 2 - bw // 2, by), big, font=big_font, fill=accent)
        # Contexte
        if rest:
            ctx_font = _load_font(28, bold=False)
            wrapped = textwrap.fill(rest, width=20)
            _text_block(draw, wrapped.split("\n"), ctx_font, w // 2, by + 130, w, palette["subtext"])
    else:
        _layout_content(draw, img, w, h, text, palette, index, total)
        return

    _draw_progress(draw, w, h, (index + 1) / total, accent)


def _layout_cta(draw, img, w, h, text, palette, index, total):
    """Slide call-to-action finale — fond accentué, bouton simulé."""
    accent = palette["accent"]
    accent2 = palette["accent2"]

    # Fond spécial pour la slide finale
    for y in range(h):
        t = y / h
        r = int(accent[0] * 0.3 * (1 - t) + palette["bg_bottom"][0] * t)
        g = int(accent[1] * 0.3 * (1 - t) + palette["bg_bottom"][1] * t)
        b = int(accent[2] * 0.3 * (1 - t) + palette["bg_bottom"][2] * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))

    _draw_geometric(draw, w, h, accent, accent2)

    # Badge "Action"
    badge_font = _load_font(20)
    badge = "→ ACTION ←"
    bb = draw.textbbox((0, 0), badge, font=badge_font)
    bw = bb[2] - bb[0]
    bx = (w - bw - 30) // 2
    draw.rounded_rectangle([(bx, h // 2 - 170), (bx + bw + 30, h // 2 - 135)],
                            radius=12, fill=accent)
    draw.text((bx + 15, h // 2 - 166), badge, font=badge_font, fill=(255, 255, 255))

    # Texte principal
    font_size = 38 if len(text) < 60 else 30
    font = _load_font(font_size)
    wrapped = textwrap.fill(text, width=int(20 * 36 / font_size))
    lines = wrapped.split("\n")
    total_h = len(lines) * int(font_size * 1.4)
    y = (h - total_h) // 2 + 20
    _text_block(draw, lines, font, w // 2, y, w, palette["text"], line_spacing=1.4)

    # Éléments décoratifs bas
    draw.line([(60, h - 80), (w - 60, h - 80)], fill=accent, width=2)
    sub_font = _load_font(18, bold=False)
    draw.text((w // 2 - 40, h - 65), "Slide finale", font=sub_font, fill=palette["subtext"])

    _draw_progress(draw, w, h, 1.0, accent)


# ── Fonction principale ───────────────────────────────────────────────────────

def create_slide(text: str, slide_index: int, total_slides: int,
                 size: Tuple[int, int] = (480, 854), theme: str = "dark",
                 layout: str = "auto") -> Image.Image:

    palette = THEMES.get(theme, THEMES["dark"])
    w, h = size

    img = Image.new("RGBA", size, (0, 0, 0, 255))
    draw = ImageDraw.Draw(img, "RGBA")
    _gradient(draw, size, palette["bg_top"], palette["bg_bottom"])

    # Sélection automatique du layout
    if layout == "auto":
        if slide_index == 0:
            actual_layout = "intro"
        elif slide_index == total_slides - 1:
            actual_layout = "cta"
        else:
            import re
            has_number = bool(re.search(r'\d+[%x€$k]?', text))
            actual_layout = "stat" if has_number else "content"
    else:
        actual_layout = layout

    if actual_layout == "intro":
        _layout_intro(draw, img, w, h, text, palette, slide_index, total_slides)
    elif actual_layout == "stat":
        _layout_stat(draw, img, w, h, text, palette, slide_index, total_slides)
    elif actual_layout == "cta":
        _layout_cta(draw, img, w, h, text, palette, slide_index, total_slides)
    else:
        _layout_content(draw, img, w, h, text, palette, slide_index, total_slides)

    return img.convert("RGB")


def save_slide(text: str, path: str, index: int = 0, total: int = 1,
               theme: str = "dark", layout: str = "auto") -> str:
    img = create_slide(text, index, total, theme=theme, layout=layout)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=False)
    return path
