"""
Générateur de visuels — crée une image (carte, citation, titre de slide) SANS aucune API.

Pourquoi : l'API Canva gratuite crée un design mais ne permet pas d'y écrire du texte
(l'autofill par template exige un plan Enterprise). En revanche elle accepte un ASSET.
On fabrique donc l'image ici, puis on peut l'envoyer dans Canva — ou simplement la
renvoyer à l'utilisateur, ce qui est déjà utile en soi.

100 % local, gratuit, hors-ligne.
"""
import re
import textwrap
import unicodedata
from pathlib import Path

from plugins.base import Plugin

_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

# Palettes (fond dégradé + couleur du texte)
THEMES = {
    "nova":    ((124, 92, 255), (34, 211, 238), (255, 255, 255)),
    "sombre":  ((17, 17, 30), (60, 40, 110), (240, 240, 255)),
    "chaud":   ((249, 115, 22), (236, 72, 153), (255, 255, 255)),
    "nature":  ((16, 185, 129), (34, 211, 238), (255, 255, 255)),
    "clair":   ((245, 245, 250), (215, 220, 245), (30, 30, 50)),
}
SIZES = {"carre": (1080, 1080), "story": (1080, 1920), "slide": (1920, 1080),
         "banniere": (1500, 500), "post": (1200, 630)}


def _font(size: int):
    from PIL import ImageFont
    for p in _FONTS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "visuel").encode("ascii", "ignore").decode()
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower() or "visuel")[:40]


def make_visual(texte: str, sous_titre: str = "", theme: str = "nova",
                format: str = "carre", out_dir: str = "output/visuels") -> str:
    """Fabrique l'image et renvoie son chemin."""
    from PIL import Image, ImageDraw, ImageFilter

    c1, c2, ctxt = THEMES.get(theme, THEMES["nova"])
    W, H = SIZES.get(format, SIZES["carre"])
    img = Image.new("RGB", (W, H), c1)
    d = ImageDraw.Draw(img)

    # Dégradé diagonal doux
    for y in range(H):
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)], fill=(int(c1[0] + (c2[0] - c1[0]) * t),
                                       int(c1[1] + (c2[1] - c1[1]) * t),
                                       int(c1[2] + (c2[2] - c1[2]) * t)))
    # Halo lumineux pour donner du relief
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-W * .2, -H * .3, W * .8, H * .5], fill=(70, 60, 130))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(W // 8)), 0.28)
    d = ImageDraw.Draw(img)

    # Texte principal : taille adaptée à la longueur
    n = max(1, len(texte or ""))
    size = max(34, min(int(W / 8), int(W * 2.6 / (n ** 0.62))))
    f = _font(size)
    wrap = max(10, int(W * 0.86 / (size * 0.56)))
    lines = textwrap.wrap(texte or "", width=wrap)[:6]
    lh = int(size * 1.22)
    total = lh * len(lines) + (int(size * .8) if sous_titre else 0)
    y = (H - total) // 2
    for ln in lines:
        w = d.textlength(ln, font=f)
        d.text(((W - w) / 2 + 3, y + 3), ln, font=f, fill=(0, 0, 0))          # ombre portée
        d.text(((W - w) / 2, y), ln, font=f, fill=ctxt)
        y += lh
    if sous_titre:
        fs = _font(max(20, size // 3))
        w = d.textlength(sous_titre, font=fs)
        d.text(((W - w) / 2, y + 12), sous_titre, font=fs,
               fill=(ctxt[0], ctxt[1], ctxt[2]))

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = str(Path(out_dir) / f"{_slug(texte)}.png")
    img.save(path, "PNG", optimize=True)
    return path


class VisualMakerPlugin(Plugin):
    name = "create_visual"
    description = ("Crée une IMAGE avec du texte (carte, citation, titre de slide, bannière) — "
                   "sans aucune API ni clé. PARAMS : texte (obligatoire), sous_titre, "
                   "theme (nova|sombre|chaud|nature|clair), format (carre|story|slide|banniere|post).")
    parameters = {
        "texte":      {"type": "string", "description": "Texte principal", "required": True},
        "sous_titre": {"type": "string", "description": "Sous-titre (optionnel)", "required": False},
        "theme":      {"type": "string", "description": "nova|sombre|chaud|nature|clair", "required": False},
        "format":     {"type": "string", "description": "carre|story|slide|banniere|post", "required": False},
    }

    def run(self, texte: str = "", sous_titre: str = "", theme: str = "nova",
            format: str = "carre", **kw) -> str:
        texte = (texte or kw.get("text") or kw.get("title") or "").strip()
        if not texte:
            return "⚠️ Précise le texte à écrire sur le visuel."
        try:
            path = make_visual(texte, sous_titre, theme, format)
        except Exception as e:
            return f"❌ Création du visuel impossible : {type(e).__name__}: {str(e)[:150]}"
        return (f"✅ Visuel créé : {path}\n"
                f"Texte : « {texte} »" + (f" · {sous_titre}" if sous_titre else "")
                + f" · thème {theme} · format {format}")
