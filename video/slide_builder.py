"""
Slide builder professionnel — vraies photos de fond (picsum.photos, gratuit).
Chaque slide = photo + overlay sombre + texte avec ombre + emoji + progress.
"""
import hashlib
import re
import textwrap
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

BG_CACHE = Path("data/bg_cache")
BG_CACHE.mkdir(parents=True, exist_ok=True)

THEMES = {
    "dark":  {"accent": (99,102,241),  "accent2": (167,139,250), "text": (255,255,255), "sub": (210,210,240)},
    "fire":  {"accent": (255,90,0),    "accent2": (255,200,0),   "text": (255,255,255), "sub": (255,195,110)},
    "ocean": {"accent": (0,185,255),   "accent2": (100,220,255), "text": (255,255,255), "sub": (155,220,255)},
    "gold":  {"accent": (212,175,55),  "accent2": (255,220,100), "text": (255,255,255), "sub": (225,205,125)},
}

FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

EMOJI_FONTS = [
    "C:/Windows/Fonts/seguiemj.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _emoji_font(size: int):
    for p in EMOJI_FONTS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return None


def _kw_seed(keyword: str) -> int:
    return int(hashlib.md5(keyword.encode()).hexdigest()[:8], 16) % 1084


def fetch_bg(keyword: str, w: int, h: int) -> Image.Image | None:
    """Photo réelle sémantique (Wikimedia), fallback loremflickr/picsum."""
    import requests
    seed = _kw_seed(keyword)
    cache = BG_CACHE / f"{seed}_{w}x{h}.jpg"
    if cache.exists():
        try:
            return Image.open(cache).convert("RGB")
        except Exception:
            cache.unlink(missing_ok=True)
    try:
        # 1) Wikimedia Commons (pertinent au mot-clé, libre de droits)
        q = re.sub(r"[^a-zA-Z0-9 ]", " ", keyword).strip()
        if q:
            api = "https://commons.wikimedia.org/w/api.php"
            params = {
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": q,
                "gsrlimit": 5,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": w,
                "iiurlheight": h,
            }
            jr = requests.get(api, params=params, timeout=15).json()
            pages = (jr.get("query", {}).get("pages", {}) or {}).values()
            for pg in pages:
                ii = (pg.get("imageinfo") or [{}])[0]
                u = ii.get("thumburl") or ii.get("url")
                if not u:
                    continue
                ir = requests.get(u, timeout=15)
                if ir.status_code == 200 and ir.content:
                    cache.write_bytes(ir.content)
                    return Image.open(BytesIO(ir.content)).convert("RGB")

        # 2) loremflickr keyword-based
        query = re.sub(r"[^a-zA-Z0-9, ]", " ", keyword).strip().replace(" ", ",")
        if query:
            r = requests.get(f"https://loremflickr.com/{w}/{h}/{query}?lock={seed}",
                             timeout=16, allow_redirects=True)
            if r.status_code == 200 and r.content:
                cache.write_bytes(r.content)
                return Image.open(BytesIO(r.content)).convert("RGB")
        # 3) fallback robuste
        r = requests.get(f"https://picsum.photos/seed/{seed}/{w}/{h}",
                         timeout=15, allow_redirects=True)
        if r.status_code == 200:
            cache.write_bytes(r.content)
            return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        pass
    return None


def _gen_bg(w: int, h: int, accent: tuple) -> Image.Image:
    """Fond géométrique généré si hors-ligne."""
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    dark = tuple(max(0, c - 155) for c in accent)
    darker = tuple(max(0, c - 195) for c in accent)
    for y in range(h):
        t = y / h
        c = tuple(int(dark[i] + (darker[i] - dark[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=c)
    col = tuple(min(255, int(c * 0.3)) for c in accent)
    draw.ellipse([(int(w * 0.55), -int(h * 0.15)), (int(w * 1.3), int(h * 0.55))], outline=col, width=3)
    draw.ellipse([(int(-w * 0.2), int(h * 0.65)), (int(w * 0.4), int(h * 1.2))], outline=col, width=2)
    return img


def _overlay(img: Image.Image, accent: tuple) -> Image.Image:
    """Améliore lisibilité sans noircir l'image."""
    img = ImageEnhance.Brightness(img).enhance(0.90)
    # Légère teinte accent
    tint = Image.new("RGB", img.size, tuple(min(255, int(c * 0.22)) for c in accent))
    img = Image.blend(img, tint, 0.22)
    # Dégradé vertical doux (haut/bas un peu plus sombres, centre lisible)
    w, h = img.size
    veil = Image.new("L", (w, h), 0)
    vd = ImageDraw.Draw(veil)
    for y in range(h):
        t = abs((y / max(1, h - 1)) - 0.5) * 2.0  # 0 centre, 1 bords verticaux
        alpha = int(12 + 70 * (t ** 1.8))          # max ~82 aux bords
        vd.line([(0, y), (w, y)], fill=alpha)
    black = Image.new("RGB", (w, h), (0, 0, 0))
    img = Image.composite(black, img, veil)  # assombrit légèrement haut/bas, garde le centre clair
    return img


def _blend_color(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _progress_bar(draw, w, h, progress, accent):
    bar_h = 5
    muted = tuple(max(0, c - 180) for c in accent)
    draw.rectangle([(0, h - bar_h), (w, h)], fill=muted)
    bw = max(6, int(w * progress))
    for x in range(bw):
        t = x / bw
        c = _blend_color(tuple(int(c * 0.55) for c in accent), accent, t)
        draw.line([(x, h - bar_h), (x, h)], fill=c)


def _shadow(draw, text, font, cx, y, color, spread=3):
    bb = draw.textbbox((0, 0), text, font=font)
    tw = bb[2] - bb[0]
    x = cx - tw // 2
    for dx, dy in [(spread, spread), (spread - 1, spread - 1), (-1, 1)]:
        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=color)
    return bb[3] - bb[1]


def _draw_emoji(draw, img_draw_ctx, emoji: str, cx: int, y: int, size: int):
    ef = _emoji_font(size)
    if ef:
        bb = draw.textbbox((0, 0), emoji, font=ef)
        ew = bb[2] - bb[0]
        draw.text((cx - ew // 2, y), emoji, font=ef)


# ── Renderers par type ────────────────────────────────────────────────────────

def _intro(draw, data, p, w, h):
    cx = w // 2
    accent, accent2 = p["accent"], p["accent2"]

    _draw_emoji(draw, draw, data.get("icon", "🎬"), cx, int(h * 0.18), 70)

    title = data.get("title", "")
    fs = 72 if len(title) <= 18 else 58 if len(title) <= 30 else 46
    font = _font(fs)
    lines = textwrap.fill(title, width=max(8, int(14 * 52 / fs))).split("\n")
    th = len(lines) * int(fs * 1.3)
    y = h // 2 - th // 2 - 15
    for line in lines:
        _shadow(draw, line, font, cx, y, p["text"])
        y += int(fs * 1.3)

    # Trait accent
    draw.rectangle([(cx - 70, y + 12), (cx + 70, y + 18)], fill=accent)

    if data.get("subtitle"):
        sf = _font(26)
        for line in textwrap.fill(data["subtitle"], 24).split("\n"):
            _shadow(draw, line, sf, cx, y + 30, p["sub"])
            y += 34


def _content(draw, data, p, w, h):
    cx = w // 2
    accent = p["accent"]

    # Icône au quart supérieur
    icon_y = int(h * 0.2)
    _draw_emoji(draw, draw, data.get("icon", "▶"), cx, icon_y - 45, 58)

    # Ligne séparatrice
    draw.rectangle([(45, icon_y + 20), (w - 45, icon_y + 24)], fill=accent)

    title = data.get("title", "")
    fs = 52 if len(title) <= 22 else 42 if len(title) <= 40 else 34
    font = _font(fs)
    lines = textwrap.fill(title, width=max(8, int(16 * 38 / fs))).split("\n")
    y = icon_y + 38
    for line in lines:
        _shadow(draw, line, font, cx, y, p["text"])
        y += int(fs * 1.35)

    draw.rectangle([(cx - 45, y + 8), (cx + 45, y + 12)], fill=tuple(int(c * 0.7) for c in accent))

    if data.get("subtitle"):
        sf = _font(30)
        y += 24
        for line in textwrap.fill(data["subtitle"], 24).split("\n"):
            _shadow(draw, line, sf, cx, y, p["sub"])
            y += 32


def _stat(draw, data, p, w, h):
    cx = w // 2
    accent, accent2 = p["accent"], p["accent2"]

    _draw_emoji(draw, draw, data.get("icon", "📊"), cx, int(h * 0.15) - 30, 55)

    number = str(data.get("number", "") or "")
    if number:
        nf = _font(122)
        nb = draw.textbbox((0, 0), number, font=nf)
        nw = nb[2] - nb[0]
        ny = h // 2 - 75
        draw.text((cx - nw // 2 + 4, ny + 5), number, font=nf,
                  fill=tuple(max(0, int(c * 0.25)) for c in accent))
        draw.text((cx - nw // 2, ny), number, font=nf, fill=accent2)
        draw.rectangle([(cx - 55, ny + 112), (cx + 55, ny + 117)], fill=accent)
        y = ny + 130
    else:
        y = h // 2 - 30

    title = data.get("title", "")
    fs = 46 if len(title) <= 30 else 36
    font = _font(fs)
    for line in textwrap.fill(title, width=max(8, int(18 * 32 / fs))).split("\n"):
        _shadow(draw, line, font, cx, y, p["text"])
        y += int(fs * 1.35)

    if data.get("subtitle"):
        y += 4
        sf = _font(22)
        for line in textwrap.fill(data["subtitle"], 26).split("\n"):
            _shadow(draw, line, sf, cx, y, p["sub"])
            y += 28


def _cta(draw, data, p, w, h):
    cx = w // 2
    accent, accent2 = p["accent"], p["accent2"]

    _draw_emoji(draw, draw, data.get("icon", "🚀"), cx, int(h * 0.22) - 40, 65)

    # Bouton coloré
    btn_y = h // 2 - 65
    draw.rectangle([(35, btn_y), (w - 35, btn_y + 52)], fill=accent)
    bf = _font(30)
    btn_text = "PASSE À L'ACTION"
    bb = draw.textbbox((0, 0), btn_text, font=bf)
    bw = bb[2] - bb[0]
    draw.text((cx - bw // 2, btn_y + 14), btn_text, font=bf, fill=(255, 255, 255))

    title = data.get("title", "")
    fs = 50 if len(title) <= 22 else 40 if len(title) <= 40 else 32
    font = _font(fs)
    y = btn_y + 70
    for line in textwrap.fill(title, width=max(8, int(16 * 34 / fs))).split("\n"):
        _shadow(draw, line, font, cx, y, p["text"])
        y += int(fs * 1.38)

    if data.get("subtitle"):
        y += 4
        sf = _font(24)
        for line in textwrap.fill(data["subtitle"], 22).split("\n"):
            _shadow(draw, line, sf, cx, y, p["sub"])
            y += 32

    draw.rectangle([(55, h - 60), (w - 55, h - 55)], fill=accent)
    draw.rectangle([(75, h - 48), (w - 75, h - 44)], fill=tuple(int(c * 0.6) for c in accent2))


# ── API publique ──────────────────────────────────────────────────────────────

def build_slide(slide_data: dict, idx: int, total: int,
                size: tuple = (480, 854), theme: str = "dark",
                out_path: str = None) -> Image.Image:

    p = THEMES.get(theme, THEMES["dark"])
    w, h = size

    # Photo de fond
    bg = fetch_bg(slide_data.get("bg_keyword", "abstract dark background"), w, h)
    if bg is None:
        bg = _gen_bg(w, h, p["accent"])

    bg = _overlay(bg, p["accent"])
    draw = ImageDraw.Draw(bg)
    cx = w // 2

    # Progress bar
    _progress_bar(draw, w, h, (idx + 1) / total, p["accent"])

    # Numéro slide
    nf = _font(17)
    nstr = f"{idx+1} / {total}"
    nb = draw.textbbox((0, 0), nstr, font=nf)
    draw.text((w - (nb[2] - nb[0]) - 14, 14), nstr, font=nf, fill=p["sub"])

    slide_type = slide_data.get("type", "content")
    if slide_type == "intro":
        _intro(draw, slide_data, p, w, h)
    elif slide_type == "stat":
        _stat(draw, slide_data, p, w, h)
    elif slide_type == "cta":
        _cta(draw, slide_data, p, w, h)
    else:
        _content(draw, slide_data, p, w, h)

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        bg.save(out_path, "PNG")
    return bg
