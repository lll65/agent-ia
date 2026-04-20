"""
Génération de slides vidéo professionnelles — Pillow RGB pur, 100% local.
Layouts différenciés: intro / contenu / stat / CTA.
"""
import textwrap
import re
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

THEMES = {
    "dark": {
        "bg_top": (10, 10, 22), "bg_bottom": (28, 28, 55),
        "accent": (99, 102, 241), "accent2": (167, 139, 250),
        "text": (255, 255, 255), "subtext": (180, 180, 215),
        "muted": (40, 40, 70),
    },
    "fire": {
        "bg_top": (18, 5, 0), "bg_bottom": (55, 12, 0),
        "accent": (255, 90, 0), "accent2": (255, 200, 0),
        "text": (255, 255, 255), "subtext": (255, 185, 100),
        "muted": (50, 15, 0),
    },
    "ocean": {
        "bg_top": (0, 12, 35), "bg_bottom": (0, 45, 90),
        "accent": (0, 185, 255), "accent2": (100, 220, 255),
        "text": (255, 255, 255), "subtext": (150, 215, 255),
        "muted": (0, 25, 55),
    },
    "gold": {
        "bg_top": (10, 8, 2), "bg_bottom": (28, 22, 4),
        "accent": (212, 175, 55), "accent2": (255, 220, 100),
        "text": (255, 255, 255), "subtext": (220, 200, 120),
        "muted": (22, 17, 3),
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
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _blend(c1: tuple, c2: tuple, t: float) -> tuple:
    """Mélange linéaire entre deux couleurs RGB."""
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _gradient(img: Image.Image, top: tuple, bottom: tuple):
    w, h = img.size
    pixels = img.load()
    for y in range(h):
        t = y / h
        c = _blend(top, bottom, t)
        for x in range(w):
            pixels[x, y] = c


def _dim(color: tuple, factor: float) -> tuple:
    """Assombrit une couleur (factor < 1)."""
    return tuple(int(c * factor) for c in color)


def _draw_progress(draw: ImageDraw.Draw, w: int, h: int, progress: float, accent: tuple, muted: tuple):
    bar_h = 6
    draw.rectangle([(0, h - bar_h), (w, h)], fill=muted)
    bar_w = max(8, int(w * progress))
    for x in range(bar_w):
        t = x / max(bar_w, 1)
        c = _blend(_dim(accent, 0.6), accent, t)
        draw.line([(x, h - bar_h), (x, h)], fill=c)


def _draw_corners(draw: ImageDraw.Draw, w: int, h: int, accent: tuple):
    """Coins décoratifs discrets."""
    size = 25
    col = _dim(accent, 0.5)
    lw = 2
    # coin haut-gauche
    draw.line([(20, 20), (20 + size, 20)], fill=col, width=lw)
    draw.line([(20, 20), (20, 20 + size)], fill=col, width=lw)
    # coin haut-droite
    draw.line([(w - 20, 20), (w - 20 - size, 20)], fill=col, width=lw)
    draw.line([(w - 20, 20), (w - 20, 20 + size)], fill=col, width=lw)
    # coin bas-gauche
    draw.line([(20, h - 20), (20 + size, h - 20)], fill=col, width=lw)
    draw.line([(20, h - 20), (20, h - 20 - size)], fill=col, width=lw)
    # coin bas-droite
    draw.line([(w - 20, h - 20), (w - 20 - size, h - 20)], fill=col, width=lw)
    draw.line([(w - 20, h - 20), (w - 20, h - 20 - size)], fill=col, width=lw)


def _text_centered(draw: ImageDraw.Draw, lines: list, font, cx: int, y: int,
                   color: tuple, shadow: bool = True, spacing: float = 1.35) -> int:
    size = font.size if hasattr(font, "size") else 28
    step = int(size * spacing)
    for line in lines:
        bb = draw.textbbox((0, 0), line, font=font)
        lw = bb[2] - bb[0]
        x = cx - lw // 2
        if shadow:
            draw.text((x + 2, y + 2), line, font=font, fill=_dim(color, 0.15))
        draw.text((x, y), line, font=font, fill=color)
        y += step
    return y


# ── Layouts ──────────────────────────────────────────────────────────────────

def _layout_intro(draw, w, h, text, p, index, total):
    accent, accent2 = p["accent"], p["accent2"]

    _draw_corners(draw, w, h, accent)

    # Barre horizontale décorative en haut
    for x in range(40, w - 40):
        t = (x - 40) / (w - 80)
        c = _blend(accent, accent2, t)
        draw.point((x, 55), fill=c)
    draw.line([(40, 55), (w - 40, 55)], fill=accent, width=2)

    # Compteur de slide
    fn = _load_font(20)
    label = f"{index+1} / {total}"
    lb = draw.textbbox((0, 0), label, font=fn)
    draw.text((w // 2 - (lb[2] - lb[0]) // 2, 28), label, font=fn, fill=p["subtext"])

    # Titre principal
    words = len(text.split())
    fs = 62 if words <= 4 else 50 if words <= 7 else 40
    font = _load_font(fs)
    wrapped = textwrap.fill(text, width=max(10, int(16 * 50 / fs)))
    lines = wrapped.split("\n")
    th = len(lines) * int(fs * 1.35)
    y = (h - th) // 2 - 30
    _text_centered(draw, lines, font, w // 2, y, p["text"])

    # Ligne accent sous le titre
    lend_y = y + th + 18
    bar_len = min(220, w - 80)
    bx = (w - bar_len) // 2
    draw.rectangle([(bx, lend_y), (bx + bar_len, lend_y + 5)], fill=accent)

    _draw_progress(draw, w, h, (index + 1) / total, accent, p["muted"])


def _layout_content(draw, w, h, text, p, index, total):
    accent = p["accent"]

    # Numéro en filigrane (couleur sombre)
    num_font = _load_font(120)
    num_str = str(index)
    nb = draw.textbbox((0, 0), num_str, font=num_font)
    nw = nb[2] - nb[0]
    # Couleur très atténuée pour le filigrane
    fili_col = _blend(p["bg_bottom"], accent, 0.25)
    draw.text((w // 2 - nw // 2, 10), num_str, font=num_font, fill=fili_col)

    # Ligne séparatrice
    draw.line([(50, 148), (w - 50, 148)], fill=accent, width=3)

    # Texte
    fs = 40 if len(text) < 60 else 32 if len(text) < 120 else 26
    font = _load_font(fs)
    wrapped = textwrap.fill(text, width=int(22 * 32 / fs))
    lines = wrapped.split("\n")
    th = len(lines) * int(fs * 1.4)
    y = max(170, (h - th) // 2 + 20)
    _text_centered(draw, lines, font, w // 2, y, p["text"], spacing=1.4)

    _draw_progress(draw, w, h, (index + 1) / total, accent, p["muted"])


def _layout_stat(draw, w, h, text, p, index, total):
    accent, accent2 = p["accent"], p["accent2"]
    _draw_corners(draw, w, h, accent)

    numbers = re.findall(r'\d+[\s]?[%xk€$M]?', text)
    if numbers:
        big = numbers[0].strip()
        rest = text[text.find(big) + len(big):].strip(" -–:,.")

        # Grand chiffre coloré
        big_font = _load_font(105)
        bb = draw.textbbox((0, 0), big, font=big_font)
        bw = bb[2] - bb[0]
        by = h // 2 - 100
        draw.text((w // 2 - bw // 2 + 2, by + 3), big, font=big_font, fill=_dim(p["text"], 0.1))
        draw.text((w // 2 - bw // 2, by), big, font=big_font, fill=accent)

        # Ligne sous le chiffre
        draw.rectangle([(w // 2 - 60, by + 115), (w // 2 + 60, by + 119)], fill=accent2)

        # Texte contextuel
        if rest:
            ctx_font = _load_font(28)
            wrapped = textwrap.fill(rest, width=22)
            _text_centered(draw, wrapped.split("\n"), ctx_font, w // 2, by + 135, p["subtext"])
    else:
        _layout_content(draw, w, h, text, p, index, total)
        return

    _draw_progress(draw, w, h, (index + 1) / total, accent, p["muted"])


def _layout_cta(draw, w, h, text, p, index, total):
    accent, accent2 = p["accent"], p["accent2"]

    # Fond légèrement plus chaud (superpose accent en haut)
    for y in range(h // 2):
        t = y / (h // 2)
        bg = _blend(_dim(accent, 0.25), p["bg_bottom"], t)
        draw.line([(0, y), (w, y)], fill=bg)

    _draw_corners(draw, w, h, accent2)

    # Badge "ACTION"
    badge_font = _load_font(22)
    badge = "→  ACTION  ←"
    bb = draw.textbbox((0, 0), badge, font=badge_font)
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    pad = 14
    bx = (w - bw - pad * 2) // 2
    badge_y = h // 2 - 165
    # Rectangle arrondi manuel
    rx, ry = bx, badge_y
    rw, rh_box = bw + pad * 2, bh + pad
    draw.rectangle([(rx + 6, ry), (rx + rw - 6, ry + rh_box)], fill=accent)
    draw.rectangle([(rx, ry + 6), (rx + rw, ry + rh_box - 6)], fill=accent)
    draw.text((rx + pad, ry + pad // 2), badge, font=badge_font, fill=(255, 255, 255))

    # Texte principal
    fs = 38 if len(text) < 60 else 30
    font = _load_font(fs)
    wrapped = textwrap.fill(text, width=int(20 * 36 / fs))
    lines = wrapped.split("\n")
    th = len(lines) * int(fs * 1.4)
    y = (h - th) // 2 + 30
    _text_centered(draw, lines, font, w // 2, y, p["text"], spacing=1.4)

    # Ligne de fin
    draw.line([(60, h - 70), (w - 60, h - 70)], fill=accent, width=2)
    fin_font = _load_font(18)
    fin = "Agis maintenant ✓"
    fb = draw.textbbox((0, 0), fin, font=fin_font)
    draw.text((w // 2 - (fb[2] - fb[0]) // 2, h - 58), fin, font=fin_font, fill=p["subtext"])

    _draw_progress(draw, w, h, 1.0, accent, p["muted"])


# ── API publique ──────────────────────────────────────────────────────────────

def create_slide(text: str, slide_index: int, total_slides: int,
                 size: Tuple[int, int] = (480, 854), theme: str = "dark",
                 layout: str = "auto") -> Image.Image:

    palette = THEMES.get(theme, THEMES["dark"])
    w, h = size

    img = Image.new("RGB", size)
    _gradient(img, palette["bg_top"], palette["bg_bottom"])
    draw = ImageDraw.Draw(img)

    if layout == "auto":
        if slide_index == 0:
            actual = "intro"
        elif slide_index == total_slides - 1:
            actual = "cta"
        else:
            actual = "stat" if re.search(r'\d+', text) and len(text) < 80 else "content"
    else:
        actual = layout

    if actual == "intro":
        _layout_intro(draw, w, h, text, palette, slide_index, total_slides)
    elif actual == "stat":
        _layout_stat(draw, w, h, text, palette, slide_index, total_slides)
    elif actual == "cta":
        _layout_cta(draw, w, h, text, palette, slide_index, total_slides)
    else:
        _layout_content(draw, w, h, text, palette, slide_index, total_slides)

    return img


def save_slide(text: str, path: str, index: int = 0, total: int = 1,
               theme: str = "dark", layout: str = "auto") -> str:
    img = create_slide(text, index, total, theme=theme, layout=layout)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path
