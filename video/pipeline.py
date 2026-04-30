"""
Pipeline vidéo multi-agents professionnel.

Agents créés automatiquement:
  1. ScriptAgent    — rédige le script structuré (Groq)
  2. ArtDirector    — choisit visuals/keywords (Groq)
  3. SlideBuilders  — construisent les slides en parallèle (PIL + picsum)
  4. NarratorAgent  — TTS par slide
  5. EditorAgent    — assemblage FFmpeg avec transitions
"""
import asyncio
import json
import logging
import re
import shutil
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4)


def _safe_subtitle(text: str, max_len: int = 72) -> str:
    raw = (text or "").strip().replace("\n", " ")
    if not raw:
        return ""
    clipped = textwrap.shorten(raw, width=max_len, placeholder="…")
    return clipped.replace(":", r"\:").replace("'", r"\'")


def _landing_page_brief(url: str) -> dict:
    """
    Extrait un mini brief marketing depuis une URL produit.
    Retourne: {title, description, bullets}
    """
    import requests
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return {}
        html = r.text[:220000]
    except Exception:
        return {}

    def _meta(prop: str) -> str:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        return m.group(1).strip() if m else ""

    title_m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = (title_m.group(1).strip() if title_m else "")[:120]
    desc = (_meta("og:description") or _meta("description"))[:300]
    h1 = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    h2 = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.I | re.S)

    def _clean(txt: str) -> str:
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt

    bullets = []
    for x in h1[:2] + h2[:4]:
        c = _clean(x)
        if 8 <= len(c) <= 90:
            bullets.append(c)

    return {
        "title": _clean(title),
        "description": _clean(desc),
        "bullets": bullets[:5],
    }

# ── Prompts ───────────────────────────────────────────────────────────────────

SCRIPT_PROMPT = """Tu es un expert en création de contenu viral pour TikTok/Instagram/YouTube Shorts.
Crée un script vidéo "{style}" en langue "{lang}" sur le sujet: "{topic}"

Réponds UNIQUEMENT en JSON valide (sans markdown):
{{
  "title": "Titre accrocheur de la vidéo (max 8 mots)",
  "hook": "Phrase d'accroche ultra-forte (max 12 mots)",
  "slides": [
    {{
      "type": "intro",
      "title": "Texte principal court (3-6 mots)",
      "subtitle": "Texte secondaire (8-14 mots, peut être null)",
      "narration": "Ce que la voix dira (1-2 phrases naturelles et engageantes)",
      "bg_keyword": "3 mots anglais pour l'image de fond (ex: business money success)",
      "icon": "1 emoji parfaitement adapté",
      "number": null
    }}
  ]
}}

Règles strictes:
- Exactement {n_slides} slides
- Types: "intro" (slide 1), "content" (slides 2 à N-1), "stat" (si chiffre fort), "cta" (dernière slide)
- Pour "stat": mettre le chiffre/pourcentage dans "number" (ex: "87%", "3x", "1000€")
- bg_keyword: EN ANGLAIS, description visuelle (pas le sujet, l'ambiance visuelle)
- Chaque slide doit apporter une valeur ou émotion différente
- Style: {style} | Langue: {lang}"""

ART_PROMPT = """Tu es un directeur artistique expert en vidéo virale.
Pour chaque slide du script suivant, améliore le bg_keyword pour qu'il corresponde
à une photo professionnelle percutante.

Script: {script_json}

Réponds UNIQUEMENT en JSON:
{{"enhanced_keywords": ["keyword slide 1", "keyword slide 2", ...]}}

Règles: mots-clés EN ANGLAIS, 3-4 mots, décrivent une scène photographique précise.
Ex: "person holding money", "laptop screen coding", "crowd cheering stadium" """


def _extract_json(text: str) -> dict | None:
    for pat in [r"```json\s*([\s\S]+?)\s*```", r"```\s*([\s\S]+?)\s*```"]:
        m = re.search(pat, text)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


# ── Agents ────────────────────────────────────────────────────────────────────

async def _script_agent(topic: str, style: str, lang: str, n_slides: int,
                        on_progress=None, ad_brief: dict | None = None) -> dict | None:
    from llm.client import chat
    if on_progress:
        on_progress(0.08, "🖊️ ScriptAgent — rédaction du script...")
    loop = asyncio.get_event_loop()
    try:
        raw = await loop.run_in_executor(_EXECUTOR, lambda: chat(
            [
                {"role": "system", "content": "Tu es un expert en contenu viral. Réponds UNIQUEMENT en JSON valide, sans aucun texte avant ou après."},
                {"role": "user", "content": SCRIPT_PROMPT.format(
                    topic=topic, style=style, lang=lang, n_slides=n_slides) + brief_block},
            ],
            temperature=0.8,
        ))
        result = _extract_json(raw)
        if result and result.get("slides"):
            logger.info(f"[ScriptAgent] {len(result['slides'])} slides générées")
            return result
    except Exception as e:
        logger.error(f"[ScriptAgent] {e}")
    return None


async def _art_director_agent(script: dict, on_progress=None) -> list[str]:
    from llm.client import chat
    if on_progress:
        on_progress(0.18, "🎨 ArtDirector — direction visuelle...")
    loop = asyncio.get_event_loop()
    try:
        script_json = json.dumps({"slides": [
            {"type": s.get("type"), "title": s.get("title"), "bg_keyword": s.get("bg_keyword")}
            for s in script.get("slides", [])
        ]}, ensure_ascii=False)
        raw = await loop.run_in_executor(_EXECUTOR, lambda: chat(
            [
                {"role": "system", "content": "Tu es un directeur artistique. Réponds UNIQUEMENT en JSON valide."},
                {"role": "user", "content": ART_PROMPT.format(script_json=script_json)},
            ],
            temperature=0.5,
        ))
        result = _extract_json(raw)
        if result and result.get("enhanced_keywords"):
            kws = result["enhanced_keywords"]
            slides = script.get("slides", [])
            return [kws[i] if i < len(kws) else slides[i].get("bg_keyword", "abstract")
                    for i in range(len(slides))]
    except Exception as e:
        logger.warning(f"[ArtDirector] {e}")
    return [s.get("bg_keyword", "abstract background") for s in script.get("slides", [])]


def _build_one_slide(args) -> str:
    slide_data, idx, total, size, theme, out_path, enhanced_kw = args
    try:
        from video.slide_builder import build_slide
        if enhanced_kw:
            slide_data = {**slide_data, "bg_keyword": enhanced_kw}
        build_slide(slide_data, idx, total, size=size, theme=theme, out_path=out_path)
        return out_path
    except Exception as e:
        logger.error(f"[SlideBuilder {idx}] {e}")
        from video.image_gen import save_slide
        text = slide_data.get("title", f"Slide {idx+1}")
        save_slide(text, out_path, index=idx, total=total, theme=theme)
        return out_path


async def _slide_builders(script: dict, keywords: list, tmp_dir: Path,
                          size: tuple, theme: str, on_progress=None) -> list[str]:
    slides = script.get("slides", [])
    total = len(slides)
    tasks = [
        (slides[i], i, total, size, theme,
         str(tmp_dir / f"slide_{i:03d}.png"),
         keywords[i] if i < len(keywords) else None)
        for i in range(total)
    ]

    if on_progress:
        on_progress(0.28, f"🖼️ SlideBuilders — {total} slides en parallèle...")

    loop = asyncio.get_event_loop()
    paths = []
    done = 0

    def _build_batch():
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(_build_one_slide, tasks))

    results = await loop.run_in_executor(_EXECUTOR, _build_batch)

    for r in results:
        if r and Path(r).exists():
            paths.append(r)
        done += 1
        if on_progress:
            on_progress(0.28 + 0.30 * done / total, f"🖼️ Slide {done}/{total} construite")

    return paths


async def _narrator_agent(script: dict, tmp_dir: Path, lang: str,
                          on_progress=None) -> list[str | None]:
    slides = script.get("slides", [])
    if on_progress:
        on_progress(0.60, f"🎤 NarratorAgent — voix pour {len(slides)} slides...")
    audio_paths = []
    for i, slide in enumerate(slides):
        narr = slide.get("narration") or slide.get("title", "")
        apath = str(tmp_dir / f"audio_{i:03d}.mp3")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(_EXECUTOR, lambda n=narr, p=apath: _tts_safe(n, p, lang))
            audio_paths.append(apath if Path(apath).exists() else None)
        except Exception as e:
            logger.warning(f"[Narrator {i}] {e}")
            audio_paths.append(None)
        if on_progress:
            on_progress(0.60 + 0.12 * (i + 1) / len(slides), f"🎤 Voix {i+1}/{len(slides)}")
    return audio_paths


def _tts_safe(text: str, out: str, lang: str):
    try:
        from video.tts import text_to_speech
        text_to_speech(text, out, lang=lang)
    except Exception:
        pass


async def _editor_agent(slide_paths: list, audio_paths: list, script: dict,
                        tmp_dir: Path, output_path: str, size: tuple,
                        fps: int, on_progress=None) -> str:
    import subprocess
    w, h = size
    if on_progress:
        on_progress(0.74, "🎬 EditorAgent — montage vidéo...")

    def _has_ffmpeg():
        return shutil.which("ffmpeg") is not None

    if not _has_ffmpeg():
        if on_progress:
            on_progress(0.80, "⚠️ FFmpeg absent — création GIF HD...")
        return _gif_export(slide_paths, output_path, audio_paths)

    segments = []
    slides = script.get("slides", [])
    for i, img_path in enumerate(slide_paths):
        seg = str(tmp_dir / f"seg_{i:03d}.mp4")
        aud = audio_paths[i] if i < len(audio_paths) else None
        dur = _audio_duration(aud) if aud and Path(aud).exists() else 0
        dur = max(dur + 0.5, 4.5)
        slide = slides[i] if i < len(slides) else {}
        subtitle = _safe_subtitle(slide.get("subtitle") or slide.get("title", ""))
        motion = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},"
            f"zoompan=z='min(1.08,1+0.0016*on)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={max(1, int(dur * fps))}:s={w}x{h}:fps={fps}"
        )
        if subtitle:
            motion += (
                f",drawbox=x=22:y=h-220:w=w-44:h=150:color=black@0.35:t=fill"
                f",drawtext=text='{subtitle}':fontcolor=white:fontsize=36:"
                f"x=(w-text_w)/2:y=h-165:line_spacing=6:borderw=2:bordercolor=black@0.7"
            )

        if aud and Path(aud).exists():
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-loop", "1", "-i", img_path,
                   "-i", aud,
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                   "-refs", "1", "-bf", "0", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "128k",
                   "-vf", motion,
                   "-t", str(dur), "-r", str(fps), seg]
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-loop", "1", "-i", img_path,
                   "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                   "-refs", "1", "-bf", "0", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "64k",
                   "-vf", motion,
                   "-t", "4.5", "-r", str(fps), "-shortest", seg]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and Path(seg).exists():
            segments.append(seg)
        else:
            logger.error(f"[Editor seg {i}] {r.stderr[:200]}")
        if on_progress:
            on_progress(0.74 + 0.14 * (i + 1) / len(slide_paths),
                        f"🎬 Encodage slide {i+1}/{len(slide_paths)}")

    if not segments:
        return _gif_export(slide_paths, output_path, audio_paths)

    if len(segments) == 1:
        shutil.copy(segments[0], output_path)
    else:
        # Essai 1: concat avec transitions (rendu "vraie vidéo")
        if not _concat_with_xfade(segments, output_path):
            # Essai 2: concat simple ultra-rapide
            concat = str(tmp_dir / "concat.txt")
            with open(concat, "w") as f:
                for s in segments:
                    f.write(f"file '{s}'\n")
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", concat, "-c", "copy", output_path],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                logger.error(f"[Editor concat] {r.stderr[:300]}")
                return _gif_export(slide_paths, output_path, audio_paths)

    if on_progress:
        on_progress(0.92, "✅ Montage terminé")
    return output_path


def _audio_duration(path: str) -> float:
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _gif_export(slide_paths: list, output_path: str, audio_paths=None) -> str:
    from PIL import Image
    out = Path(output_path).with_suffix(".gif")
    frames = []
    for p in slide_paths:
        try:
            img = Image.open(p).convert("RGB").resize((320, 568), Image.LANCZOS)
            frames.append(img)
        except Exception:
            pass
    if frames:
        frames[0].save(str(out), save_all=True, append_images=frames[1:],
                       duration=3500, loop=0, optimize=True)
        return str(out)
    return ""


def _concat_with_xfade(segments: list[str], output_path: str, transition_sec: float = 0.45) -> bool:
    """Concatène avec transitions xfade + acrossfade pour un rendu plus 'vraie vidéo'."""
    import subprocess
    if len(segments) < 2:
        return False
    try:
        inputs = []
        for s in segments:
            inputs.extend(["-i", s])

        durations = [_audio_duration(s) for s in segments]
        offsets = []
        acc = 0.0
        for i in range(len(segments) - 1):
            d = durations[i] if durations[i] > 0 else 4.5
            acc += max(0.8, d - transition_sec)
            offsets.append(acc)

        v_prev = "[0:v]"
        a_prev = "[0:a]"
        filters = []
        for i in range(1, len(segments)):
            v_out = f"[v{i}]"
            a_out = f"[a{i}]"
            filters.append(
                f"{v_prev}[{i}:v]xfade=transition=fade:duration={transition_sec}:offset={offsets[i-1]:.2f}{v_out}"
            )
            filters.append(f"{a_prev}[{i}:a]acrossfade=d={transition_sec}{a_out}")
            v_prev, a_prev = v_out, a_out

        cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
               "-filter_complex", ";".join(filters),
               "-map", v_prev, "-map", a_prev,
               "-c:v", "libx264", "-preset", "medium", "-crf", "23",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
               output_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
        return r.returncode == 0 and Path(output_path).exists()
    except Exception:
        return False


# ── Entrée principale ─────────────────────────────────────────────────────────

async def create_pro_video(
    topic: str,
    style: str = "éducatif",
    lang: str = "fr",
    n_slides: int = 6,
    theme: str = "dark",
    add_audio: bool = True,
    fps: int = 24,
    on_progress=None,
) -> dict:
    """
    Lance tous les agents et retourne:
    {video_path, slide_paths, script, title}
    """
    safe = re.sub(r"[^\w-]", "_", topic.strip()[:35])
    out_dir = Path(config.OUTPUT_DIR) / "videos"
    tmp_dir = out_dir / f"tmp_{safe}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(out_dir / f"{safe}.mp4")

    size = config.VIDEO_RESOLUTION

    # Mode pub: URL -> extraction d'angles depuis landing page
    detected_url = ""
    m_url = re.search(r"(https?://[^\s]+)", topic)
    if m_url:
        detected_url = m_url.group(1).strip()
    ad_brief = _landing_page_brief(detected_url) if detected_url else {}
    prompt_topic = topic
    if ad_brief:
        base = ad_brief.get("title") or topic
        prompt_topic = f"PUB VIRAL pour {base}"
        if on_progress:
            on_progress(0.05, "🔎 Analyse du lien produit pour angle publicitaire...")

    # Agent 1 — Script
    script = await _script_agent(prompt_topic, style, lang, n_slides, on_progress, ad_brief=ad_brief)
    if not script:
        script = {
            "title": topic,
            "slides": [{"type": "intro" if i == 0 else "cta" if i == n_slides - 1 else "content",
                        "title": topic if i == 0 else f"Point {i}",
                        "subtitle": None, "narration": topic,
                        "bg_keyword": "abstract background", "icon": "🎬", "number": None}
                       for i in range(n_slides)],
        }

    # Agent 2 — Art Director
    keywords = await _art_director_agent(script, on_progress)

    # Agent 3 — Slide Builders (parallèle)
    slide_paths = await _slide_builders(script, keywords, tmp_dir, size, theme, on_progress)

    if not slide_paths:
        return {"error": "Aucune slide générée", "slide_paths": [], "script": script}

    # Copie des slides vers un dossier persistant (évite les images cassées dans Gradio)
    gallery_dir = out_dir / f"{safe}_slides"
    gallery_dir.mkdir(parents=True, exist_ok=True)
    persisted_slide_paths = []
    for i, p in enumerate(slide_paths):
        src = Path(p)
        if not src.exists():
            continue
        dst = gallery_dir / f"slide_{i:03d}.png"
        try:
            shutil.copy(str(src), str(dst))
            persisted_slide_paths.append(str(dst))
        except Exception:
            persisted_slide_paths.append(str(src))

    # Agent 4 — Narrator
    audio_paths = []
    if add_audio:
        audio_paths = await _narrator_agent(script, tmp_dir, lang, on_progress)

    # Agent 5 — Editor
    video_path = await _editor_agent(slide_paths, audio_paths, script, tmp_dir,
                                     output_path, size, fps, on_progress)

    try:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    except Exception:
        pass

    if on_progress:
        on_progress(1.0, "🏆 Vidéo pro terminée!")

    return {
        "video_path": video_path,
        "slide_paths": persisted_slide_paths or slide_paths,
        "script": script,
        "title": script.get("title", topic),
    }
    brief_block = ""
    if ad_brief:
        bullets = "\n".join(f"- {b}" for b in ad_brief.get("bullets", []))
        brief_block = (
            "\n\nCONTEXTE LANDING PAGE (PUB):\n"
            f"Titre: {ad_brief.get('title', '')}\n"
            f"Description: {ad_brief.get('description', '')}\n"
            f"Points clés:\n{bullets}\n"
            "Objectif: script orienté conversion (hook fort, douleur, bénéfice, preuve, CTA)."
        )
