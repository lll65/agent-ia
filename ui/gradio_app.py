"""
MasterAgent-Gros — Interface Web
⚡ Mode Rapide : Groq direct, < 2s, aucun outil
🔥 Mode Agent  : ReAct + outils + mémoire + sous-agents
"""
import asyncio
import concurrent.futures
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import inspect

# Patch de compatibilité : HfFolder supprimé dans huggingface_hub ≥ 0.24
import huggingface_hub as _hfh
if not hasattr(_hfh, "HfFolder"):
    class _HfFolderCompat:
        @staticmethod
        def get_token():
            try:
                return _hfh.get_token()
            except Exception:
                return None
        @staticmethod
        def save_token(token, new_format=True):
            pass
    _hfh.HfFolder = _HfFolderCompat

# Patch de compatibilité : bug gradio_client "TypeError: argument of type 'bool'
# is not iterable" quand un schéma JSON contient un booléen (additionalProperties:
# true/false). Rend la conversion de schéma tolérante → la page ne plante plus.
try:
    import gradio_client.utils as _gcu

    _orig_json_to_pytype = _gcu._json_schema_to_python_type

    def _safe_json_to_pytype(schema, defs=None):
        if isinstance(schema, bool):
            return "Any"
        try:
            return _orig_json_to_pytype(schema, defs)
        except Exception:
            return "Any"

    _gcu._json_schema_to_python_type = _safe_json_to_pytype

    _orig_get_type = _gcu.get_type

    def _safe_get_type(schema):
        if not isinstance(schema, dict):
            return "Any"
        return _orig_get_type(schema)

    _gcu.get_type = _safe_get_type
except Exception:
    pass

import gradio as gr
from config import config

# ── compatibilité Gradio (détecte les paramètres réels plutôt que la version) ─
_V = int(gr.__version__.split(".")[0])
_CHATBOT_SUPPORTS_TYPE = "type" in inspect.signature(gr.Chatbot.__init__).parameters

# ── sessions ──────────────────────────────────────────────────────────────────
SESSIONS_DIR = Path("data/sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

AGENT_TRIGGERS = ("agent:", "mode complet:", "plein mode:", "utilise tes outils:", "passe en mode agent")

_FINANCE_HINTS = (
    "bourse", "action ", "crypto", "bitcoin", "ethereum", "ticker", "rsi", "macd",
    "analyser aapl", "analyser tsla", "analyser nvda", "nasdaq", "cac40", "cac 40",
    "dow jones", "s&p", "trading", "investir", "portefeuille", "dividende", "cotation",
    "achat vente action", "faut il acheter", "vaut-il acheter", "vaut il acheter",
    "analyse technique", "analyse fondamentale", "support résistance", "tendance",
    "bullish", "bearish", "haussier", "baissier", "cours de", "prix de l'action",
    "bitcoin", "ethereum", "solana", "soitec", "valneva", "renault", "orange bourse",
    "apple stock", "nvidia stock", "tesla stock", "amazon stock",
    "bonne action", "meilleure action", "action à acheter",
    "crypto à acheter", "altcoin", "defi", "nft", "matières premières", "or ", "pétrole",
    "forex", "euro dollar", "dollar", "taux d'intérêt", "banque centrale",
    "sanofi", "lvmh", "airbus", "bnp", "total bourse", "kering", "hermes bourse",
)
_VIDEO_HINTS   = ("fais une vidéo", "crée une vidéo", "génère une vidéo", "créer une vidéo", "slides sur",
                  "diaporama", "présentation vidéo")
_CODE_HINTS    = ("crée un projet", "génère un projet", "génère une application", "crée une application",
                  "full-stack", "fullstack", "projet complet", "application web complète", "zip téléchargeable")


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _run(coro):
    """Lance une coroutine depuis un contexte synchrone, compatible Gradio/FastAPI."""
    try:
        asyncio.get_running_loop()
        # Il y a déjà une boucle active (FastAPI/Gradio) → thread isolé avec sa propre boucle
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=300)
    except RuntimeError:
        # Pas de boucle active → asyncio.run() direct
        return asyncio.run(coro)


def _to_ollama(history: list) -> list:
    out = []
    for m in history[-10:]:
        if isinstance(m, dict) and m.get("role"):
            out.append({"role": m["role"], "content": m["content"]})
        elif isinstance(m, (list, tuple)) and len(m) == 2:
            if m[0]: out.append({"role": "user",      "content": str(m[0])})
            if m[1]: out.append({"role": "assistant", "content": str(m[1])})
    return out


def _add(history, user_msg, bot_msg):
    if _CHATBOT_SUPPORTS_TYPE:
        return history + [{"role": "user", "content": user_msg},
                          {"role": "assistant", "content": bot_msg}]
    return history + [(user_msg, bot_msg)]


def _normalize_history(h: list) -> list:
    """Convertit l'historique sauvegardé au format attendu par le Chatbot courant."""
    if not h:
        return []
    if _CHATBOT_SUPPORTS_TYPE:
        out = []
        for m in h:
            if isinstance(m, dict) and "role" in m:
                out.append({"role": m["role"], "content": str(m.get("content", ""))})
            elif isinstance(m, (list, tuple)) and len(m) == 2:
                if m[0]: out.append({"role": "user",      "content": str(m[0])})
                if m[1]: out.append({"role": "assistant",  "content": str(m[1])})
        return out
    else:
        out, buf = [], {}
        for m in h:
            if isinstance(m, (list, tuple)) and len(m) == 2:
                out.append([m[0], m[1]])
            elif isinstance(m, dict) and m.get("role") == "user":
                buf = {"u": m.get("content", "")}
            elif isinstance(m, dict) and m.get("role") == "assistant":
                out.append([buf.get("u", ""), m.get("content", "")])
                buf = {}
        return out


# ═════════════════════════════════════════════════════════════════════════════
# SESSIONS
# ═════════════════════════════════════════════════════════════════════════════

def _sid() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def _spath(sid): return SESSIONS_DIR / f"{sid}.json"

def _save(sid, name, history):
    _spath(sid).write_text(
        json.dumps({"id": sid, "name": name[:60],
                    "updated_at": datetime.now().isoformat(), "history": history},
                   ensure_ascii=False), encoding="utf-8")

def _load(sid):
    p = _spath(sid)
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except: pass
    return {"id": sid, "name": "Nouvelle", "history": []}

def _list_sessions():
    out = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:20]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append(f"{d.get('name','?')} | {d['id']}")
        except: pass
    return out


# ═════════════════════════════════════════════════════════════════════════════
# ⚡ FAST CHAT — Groq direct, aucun outil
# ═════════════════════════════════════════════════════════════════════════════

try:
    from agent.system_prompt import SHORT_SYSTEM_PROMPT as _FAST_SYS
except ImportError:
    _FAST_SYS = (
        "Tu es MasterAgent-Gros, expert IA d'élite. "
        "Réponses directes, chiffrées, actionnables. Français. "
        "Finance: toujours zone d'entrée + TP + SL. Zéro généralité vide."
    )

def fast_chat(message: str, history: list) -> str:
    from llm.client import chat
    msgs = [{"role": "system", "content": _FAST_SYS}]
    msgs.extend(_to_ollama(history))
    msgs.append({"role": "user", "content": message})
    try:
        answer = chat(msgs, temperature=0.7)
    except Exception as e:
        return f"❌ LLM indisponible: {e}"
    # Auto-amélioration pour les questions substantielles (> 5 mots)
    if answer and len(message.split()) > 5:
        def _bg(t, a):
            try:
                from agent.self_improve import evaluate_and_learn
                asyncio.run(evaluate_and_learn(t, a, domain="chat"))
            except Exception:
                pass
        threading.Thread(target=_bg, args=(message, answer), daemon=True).start()
    return answer


# ═════════════════════════════════════════════════════════════════════════════
# 🔥 FULL AGENT — ReAct + outils + mémoire
# ═════════════════════════════════════════════════════════════════════════════

def full_agent(message: str, history: list, sid: str) -> str:
    from agent.core import run_agent_stream
    from plugins import get_loader
    cfg = {
        "id": sid, "name": "MasterAgent-Gros v4",
        "system_prompt": (
            "Tu es MasterAgent-Gros v4, un agent IA surpuissant et auto-évolutif. "
            "Tu maîtrises tous les domaines : code full-stack, finance quantitative, "
            "création de contenu, data science, stratégie business. "
            "Tu utilises TOUS tes outils sans hésitation pour des réponses chiffrées et actionnables. "
            "Tu t'améliores à chaque interaction. Tu réponds en français. "
            "Tu ne dis jamais 'je ne peux pas' — tu trouves toujours une solution."
        ),
        "tools": list(get_loader().list_all().keys()),
        "model": config.LLM_MODEL,
    }
    trace_lines: list[str] = []
    used_tools: list[str] = []
    final_answer = ""
    iters = 0
    try:
        async def _collect():
            nonlocal final_answer, iters
            async for step in run_agent_stream(message, cfg, sid):
                if step["type"] == "thought":
                    trace_lines.append(f"💭 **Réflexion :** {step['text']}")
                elif step["type"] == "action":
                    tool   = step.get("tool", "")
                    params = step.get("params", {}) or {}
                    used_tools.append(tool)
                    if "search" in tool.lower() or "web" in tool.lower():
                        q = params.get("query") or params.get("q") or json.dumps(params, ensure_ascii=False)
                        trace_lines.append(f"🔍 **Recherche :** `{str(q)[:120]}`")
                    else:
                        p = json.dumps(params, ensure_ascii=False)
                        trace_lines.append(f"🔧 **Outil `{tool}`** — `{p[:120]}`")
                elif step["type"] == "observation":
                    trace_lines.append(f"👁️ *Résultat :* {step['result'][:220]}")
                    trace_lines.append("")
                elif step["type"] == "final":
                    final_answer = step["answer"]
                    iters = step.get("iterations", 0)
        _run(_collect())
    except Exception as e:
        import traceback
        return f"❌ Erreur agent: {e}\n\n```\n{traceback.format_exc()[:600]}\n```"

    # Bloc de raisonnement affiché EN HAUT et OUVERT (comme Claude) : on voit ce que
    # l'agent pense et où il cherche, avant sa réponse finale.
    if trace_lines:
        trace_md = "\n\n".join(trace_lines)
        reasoning = (
            f"<details open><summary>🧠 <b>Ce que l'agent a fait</b> "
            f"({iters} étapes) — clique pour replier</summary>\n\n"
            f"{trace_md}\n\n</details>\n\n---\n\n"
        )
        final_answer = reasoning + final_answer

    if used_tools:
        final_answer += f"\n\n*🔧 Outils utilisés : {', '.join(dict.fromkeys(used_tools))}*"

    def _bg(t, a):
        try:
            from agent.self_improve import evaluate_and_learn
            asyncio.run(evaluate_and_learn(t, a, domain="chat"))
        except Exception:
            pass
    threading.Thread(target=_bg, args=(message, final_answer), daemon=True).start()
    return final_answer


# ═════════════════════════════════════════════════════════════════════════════
# ROUTING
# ═════════════════════════════════════════════════════════════════════════════

def _is_finance_question(message: str) -> bool:
    """Détecte si le message est une question financière nécessitant des données réelles."""
    low = message.lower()
    return any(hint in low for hint in _FINANCE_HINTS)


def _route(message: str, mode: str):
    """Retourne (use_agent, use_finance, message_nettoyé)."""
    if mode == "agent":
        return True, False, message
    low = message.lower().strip()
    for t in AGENT_TRIGGERS:
        if low.startswith(t):
            return True, False, message[len(t):].strip() or message
    return False, False, message

def send(message: str, history: list, mode: str, sid: str):
    if not message.strip():
        return history, history, ""
    use_agent, _use_finance, clean = _route(message, mode)
    if use_agent:
        answer = full_agent(clean, history, sid)
    else:
        if not use_agent and _is_finance_question(clean):
            answer = finance_agent_analysis(clean)
        else:
            answer = fast_chat(clean, history)
            # Smart intent hints — guide l'utilisateur vers le bon onglet
            low = clean.lower()
            if any(k in low for k in _VIDEO_HINTS):
                answer += "\n\n💡 *Pour générer une vraie vidéo avec photos et voix → onglet **🎬 Vidéo***"
            elif any(k in low for k in _CODE_HINTS):
                answer += "\n\n💡 *Pour un projet complet téléchargeable en ZIP → onglet **💻 Code & Projets → Projet Complet***"
    new_h = _add(history, message, answer)
    existing = _load(sid)
    name = existing.get("name", "Nouvelle") if existing.get("history") else message[:50]
    _save(sid, name, new_h)
    return new_h, new_h, ""

def toggle_mode(mode: str):
    new = "agent" if mode == "fast" else "fast"
    if new == "fast":
        return new, gr.update(value="⚡ Mode Rapide — ACTIF"), "🟢 **Mode Rapide ⚡** — Réponses directes, sans outils"
    return new, gr.update(value="🔥 Mode Agent — ACTIF"), "🔴 **Mode Agent Complet 🔥** — ReAct + outils + mémoire"

def new_conv():
    return [], [], _sid()

def load_sess(choice: str):
    if choice and " | " in choice:
        sid = choice.split(" | ")[-1].strip()
        d = _load(sid)
        h = _normalize_history(d.get("history", []))
        return h, h, sid
    return [], [], _sid()

def refresh_sess():
    return gr.update(choices=_list_sessions(), value=None)


# ═════════════════════════════════════════════════════════════════════════════
# VIDÉO
# ═════════════════════════════════════════════════════════════════════════════

def make_video(topic, style, lang, n_slides, theme, add_audio, quality,
               progress=gr.Progress()):
    """
    quality="rapide" → slides simples (PIL seul, immédiat)
    quality="pro"    → pipeline multi-agents (photos réelles, voix, FFmpeg)
    """
    from pathlib import Path

    if not topic.strip():
        return [], None, None, "⚠️ Saisis un sujet."

    if quality == "pro":
        return _make_video_pro(topic, style, lang, int(n_slides), theme, add_audio, progress)
    else:
        return _make_video_fast(topic, style, lang, int(n_slides), theme, add_audio, progress)


def _make_video_fast(topic, style, lang, n_slides, theme, add_audio, progress):
    """Mode rapide — slides PIL sans téléchargement."""
    from llm.client import chat
    from video.image_gen import save_slide
    from pathlib import Path

    progress(0.05, desc="✍️ Génération du script...")
    try:
        raw = chat([
            {"role": "system", "content": "Tu crées des scripts vidéo. Réponds UNIQUEMENT en JSON."},
            {"role": "user", "content": (
                f'Crée {n_slides} slides pour une vidéo "{style}" sur "{topic}" en {lang}. '
                f'JSON: {{"slides": ["texte slide 1", "texte slide 2", ...]}}'
            )},
        ], temperature=0.8)
        m = re.search(r'\{[\s\S]+\}', raw)
        slides = json.loads(m.group()).get("slides", [topic]) if m else [topic]
    except Exception:
        slides = [topic] + [f"Point {i+1}" for i in range(n_slides - 1)]

    safe = re.sub(r"[^\w-]", "_", topic.strip()[:35])
    tmp = Path("output/tmp_preview") / safe
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, txt in enumerate(slides[:n_slides]):
        p = str(tmp / f"slide_{i:03d}.png")
        save_slide(txt, p, index=i, total=len(slides), theme=theme)
        paths.append(p)
        progress(0.1 + 0.6 * (i + 1) / len(slides), desc=f"🖼️ Slide {i+1}/{len(slides)}")

    gif_path = _make_gif(paths, safe)
    progress(1.0, desc="✅ Terminé!")
    status = f"✅ **{len(paths)} slides** — mode Rapide · sujet : _{topic}_\n\nPasse en mode **Pro** pour des photos réelles et la voix."
    return paths, gif_path, None, status


def _make_video_pro(topic, style, lang, n_slides, theme, add_audio, progress):
    """Mode Pro — pipeline multi-agents complet."""
    import re

    def _on_prog(val, desc):
        progress(val, desc=desc)

    try:
        from video.pipeline import create_pro_video
        result = _run(create_pro_video(
            topic=topic, style=style, lang=lang,
            n_slides=n_slides, theme=theme,
            add_audio=add_audio,
            on_progress=_on_prog,
        ))
    except Exception as e:
        return [], None, None, f"❌ Erreur pipeline Pro: {e}"

    slide_paths = result.get("slide_paths", [])
    video_path = result.get("video_path")
    script = result.get("script", {})
    title = result.get("title", topic)

    # GIF prévisualisation
    safe = re.sub(r"[^\w-]", "_", topic.strip()[:35])
    gif = _make_gif(slide_paths, safe + "_pro")

    # Résumé du script
    script_txt = f"# {title}\n\n"
    for i, s in enumerate(script.get("slides", []), 1):
        script_txt += f"**Slide {i} ({s.get('type','')}):** {s.get('title','')}\n"
        if s.get("subtitle"):
            script_txt += f"  ↳ {s['subtitle']}\n"
        script_txt += f"  🎤 _{s.get('narration','')}_\n\n"

    video_out = video_path if video_path and Path(video_path).exists() else None

    status = f"🏆 **Vidéo Pro terminée!**\n\n**{title}**\n\n{len(slide_paths)} slides · "
    if video_out:
        ext = Path(video_out).suffix.upper().lstrip(".")
        status += f"Fichier {ext} prêt au téléchargement."
    else:
        status += "GIF disponible (FFmpeg absent)."

    return slide_paths, gif, video_out, status


def _make_gif(paths: list, name: str) -> str | None:
    from pathlib import Path
    if not paths:
        return None
    try:
        from PIL import Image as PILImage
        frames = []
        for p in paths:
            try:
                frames.append(PILImage.open(p).convert("RGB"))
            except Exception:
                pass
        if not frames:
            return None
        gif_path = f"output/videos/{name}.gif"
        Path("output/videos").mkdir(parents=True, exist_ok=True)
        frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                       duration=2800, loop=0, optimize=True)
        return gif_path
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# IMAGE → VIDÉO RÉALISTE
# ═════════════════════════════════════════════════════════════════════════════

_FORMATS = {
    "Portrait 9:16 (TikTok/Reels)": (720, 1280),
    "Paysage 16:9 (YouTube)":       (1280, 720),
    "Carré 1:1 (Instagram)":        (720, 720),
}


def _video_html(video_path: str) -> str:
    """Encode la vidéo en base64 pour l'afficher directement sans passer par Gradio."""
    import base64
    try:
        data = Path(video_path).read_bytes()
        b64  = base64.b64encode(data).decode()
        return (
            '<video controls autoplay loop muted playsinline '
            'style="width:100%;max-height:520px;border-radius:10px;background:#000">'
            f'<source src="data:video/mp4;base64,{b64}" type="video/mp4">'
            'Ton navigateur ne supporte pas la lecture vidéo.</video>'
        )
    except Exception as e:
        return f"<div style='color:#f85149'>Erreur affichage vidéo: {e}</div>"


def make_img2video(image_file, description, motion_prompt, duration, fmt, progress=gr.Progress()):
    """Transforme une image en vidéo réaliste via img2video pipeline."""
    import re

    if image_file is None:
        return "<div style='color:#8b949e;padding:1rem'>⚠️ Charge une image d'abord.</div>", None, "⚠️ Charge une image d'abord."

    progress(0.05, desc="🖼️ Chargement de l'image...")

    w, h = _FORMATS.get(fmt, (720, 1280))
    img_path = str(image_file)
    safe = re.sub(r"[^\w-]", "_", Path(img_path).stem)[:40]
    out_dir = Path("output/videos")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / f"{safe}_realistic.mp4")

    steps = []

    def _on_prog(val, desc):
        if desc:
            steps.append(desc)
        if val is not None:
            progress(val, desc=desc or "...")

    async def _run_gen():
        from video.img2video import generate_realistic_video
        return await generate_realistic_video(
            image_path=img_path,
            output_path=out_path,
            description=description or "",
            motion_prompt=motion_prompt or "",
            width=w,
            height=h,
            duration=max(float(duration), 5.0),
            on_progress=_on_prog,
            max_loops=2,
        )

    result = _run(_run_gen())

    if "error" in result:
        return (
            "<div style='color:#f85149;padding:1rem'>❌ Génération échouée.</div>",
            None,
            f"❌ Erreur: {result['error']}",
        )

    video_path = result["video_path"]
    method     = result.get("method", "?")
    score      = result.get("score", 0)
    attempts   = result.get("attempts", 1)

    svd_note = ""
    if method == "ffmpeg_ken_burns":
        svd_note = "\n\n💡 Ken Burns cinématique · Lance le serveur SVD sur ton PC GPU pour la vraie IA."
    elif method == "local_svd":
        svd_note = "\n\n🖥️ Générée par ton PC GPU via SVD — qualité maximale!"

    status = (
        f"✅ **Vidéo générée!**\n\n"
        f"- Méthode : `{method}`\n"
        f"- Score qualité : **{score}/10**\n"
        f"- Tentatives : {attempts}"
        f"{svd_note}"
    )

    html  = _video_html(video_path)
    fpath = video_path if Path(video_path).exists() else None
    return html, fpath, status


# ═════════════════════════════════════════════════════════════════════════════
# CODE
# ═════════════════════════════════════════════════════════════════════════════

def gen_code(desc, lang, run_it):
    import httpx
    async def _g():
        async with httpx.AsyncClient(timeout=120) as c:
            ep = "/code/generate-and-run" if run_it and lang == "python" else "/code/generate"
            r = await c.post(f"http://localhost:{config.PORT}{ep}",
                             json={"description": desc, "language": lang})
            return r.json()
    d = _run(_g())
    code = d.get("code", str(d))
    if run_it and lang == "python":
        output = d.get("output", "⚠️ Pas de sortie.")
    else:
        output = "💡 Coche 'Exécuter' pour lancer le code Python." if lang == "python" else ""
    return code, output


def gen_project(desc, proj_type, progress=gr.Progress()):
    """Génère un projet complet via le plugin FullProjectPlugin (code réel + preview + ZIP)."""
    if not desc.strip():
        return None, "", "⚠️ Décris le projet."

    progress(0.1, desc="🧠 Génération du code par l'IA...")
    try:
        from plugins.builtin.project_builder import FullProjectPlugin
        result = FullProjectPlugin().run(description=desc, project_type=proj_type)
    except Exception as e:
        return None, "", f"❌ Erreur: {e}"

    progress(0.9, desc="📦 Finalisation...")

    # Parse le résultat pour extraire ZIP et preview
    zip_path = None
    preview_path = None
    for line in result.split("\n"):
        if line.startswith("ZIP:"):
            zip_path = line.split("ZIP:", 1)[1].strip()
        elif line.startswith("Preview:"):
            preview_path = line.split("Preview:", 1)[1].strip()

    # Lit le HTML de preview pour l'afficher inline
    preview_html = ""
    if preview_path and Path(preview_path).exists():
        try:
            preview_html = Path(preview_path).read_text(encoding="utf-8")
        except Exception:
            pass

    progress(1.0)
    summary = result.replace("\n", "\n\n")
    return zip_path, preview_html, f"✅ {summary}"


def debug_project_fn(path: str, do_fix: bool, progress=gr.Progress()):
    """Analyse un projet entier, liste les bugs et (option) les corrige."""
    if not path or not path.strip():
        return "⚠️ Indique le chemin d'un dossier de projet (ex: C:\\Users\\lohan\\mon-projet)."
    progress(0.1, desc="🔍 Lecture des fichiers...")
    try:
        from plugins.builtin.project_debugger import ProjectDebuggerPlugin
        progress(0.3, desc="🧠 Analyse des bugs par l'IA (peut prendre 1-3 min)...")
        result = ProjectDebuggerPlugin().run(path=path.strip(), fix=bool(do_fix))
        progress(1.0, desc="✅ Terminé")
        return result
    except Exception as e:
        return f"❌ Erreur: {e}"


def analyze_document_fn(path: str, question: str, progress=gr.Progress()):
    """Lit un PDF/document/dossier et répond à une question ou en fait un résumé."""
    if not path or not path.strip():
        return "⚠️ Indique le chemin d'un fichier (PDF, Word, texte) ou d'un dossier."
    progress(0.2, desc="📄 Lecture du document...")
    try:
        from plugins.builtin.document_analyzer import DocumentAnalyzerPlugin
        progress(0.5, desc="🧠 Analyse par l'IA...")
        result = DocumentAnalyzerPlugin().run(path=path.strip(), question=(question or "").strip())
        progress(1.0, desc="✅ Terminé")
        return result
    except Exception as e:
        return f"❌ Erreur: {e}"


# ═════════════════════════════════════════════════════════════════════════════
# FINANCE
# ═════════════════════════════════════════════════════════════════════════════

def analyze_finance(ticker: str, period: str):
    if not ticker.strip():
        return "⚠️ Saisis un ticker (ex: AAPL, BTC-USD, MC.PA)", None
    try:
        from plugins.builtin.finance import StockAnalysisPlugin, generate_stock_chart
        analysis = StockAnalysisPlugin().run(ticker=ticker.strip(), period=period)
        # Si yfinance manque, le plugin renvoie une erreur ❌ → on tente la cascade
        if analysis.startswith("❌") and "yfinance" in analysis:
            direct = _fetch_yahoo_direct(ticker.strip(), period)
            if direct:
                analysis = f"*⚠️ yfinance absent — données Yahoo Finance HTTP*\n\n{direct}"
        chart = generate_stock_chart(ticker.strip(), period)
        return analysis, chart
    except Exception as e:
        return f"❌ Erreur: {e}", None

def compare_finance(tickers: str, period: str):
    if not tickers.strip():
        return "⚠️ Saisis des tickers séparés par virgule (ex: AAPL,MSFT,GOOGL)", None
    try:
        from plugins.builtin.finance import MultiStockComparePlugin, generate_compare_chart
        analysis = MultiStockComparePlugin().run(tickers=tickers, period=period)
        symbols  = [t.strip().upper() for t in tickers.split(",") if t.strip()][:8]
        chart    = generate_compare_chart(symbols, period)
        return analysis, chart
    except Exception as e:
        return f"❌ Erreur: {e}", None

def finance_news(ticker: str) -> str:
    if not ticker.strip():
        return "⚠️ Saisis un ticker"
    try:
        from plugins.builtin.finance import MarketNewsPlugin
        return MarketNewsPlugin().run(ticker=ticker.strip())
    except Exception as e:
        return f"❌ Erreur: {e}"

def market_dashboard_fn() -> str:
    try:
        from plugins.builtin.finance import MarketDashboardPlugin
        return MarketDashboardPlugin().run()
    except Exception as e:
        return f"❌ Erreur dashboard: {e}"


def crypto_market_fn(coins: str) -> str:
    """Marché crypto (CoinGecko) + indice Fear & Greed."""
    try:
        from plugins.builtin.finance_extra import CryptoMarketPlugin, FearGreedPlugin
        market = CryptoMarketPlugin().run(coins=coins or "")
        senti  = FearGreedPlugin().run()
        return market + "\n\n---\n\n" + senti
    except Exception as e:
        return f"❌ Erreur: {e}"


def currency_rates_fn(to: str) -> str:
    try:
        from plugins.builtin.finance_extra import CurrencyRatesPlugin
        return CurrencyRatesPlugin().run(base="EUR", to=to or "USD,GBP,JPY,CHF")
    except Exception as e:
        return f"❌ Erreur: {e}"

# ═════════════════════════════════════════════════════════════════════════════
# AUTO-MODIFICATION DE CODE
# ═════════════════════════════════════════════════════════════════════════════

def self_modify_fn(request: str) -> str:
    """
    Pipeline d'auto-modification: l'agent lit le code, génère un patch, l'applique
    de façon sécurisée (backup + validation + rollback auto).
    """
    if not request.strip():
        return "⚠️ Décris l'amélioration à apporter."

    from llm.client import chat as llm_chat
    from agent.self_modify import read_source, apply_modification, ALLOWED_FILES
    import re as _re

    out = ["## 🔧 Auto-modification\n"]

    # 1. Le LLM choisit le fichier à modifier
    file_list = "\n".join(f"- {f}" for f in sorted(ALLOWED_FILES))
    pick_prompt = (
        f"Demande d'amélioration: \"{request}\"\n\n"
        f"Fichiers modifiables:\n{file_list}\n\n"
        "Quel SEUL fichier faut-il modifier ? Réponds UNIQUEMENT avec le chemin exact."
    )
    try:
        target_file = llm_chat([{"role": "user", "content": pick_prompt}], temperature=0.0).strip()
        target_file = target_file.strip("`'\" \n")
        # Match contre la whitelist
        target_file = next((f for f in ALLOWED_FILES if f in target_file), target_file)
    except Exception as e:
        return f"❌ Erreur sélection fichier: {e}"

    if target_file not in ALLOWED_FILES:
        return (
            f"❌ Fichier ciblé non autorisé: `{target_file}`\n\n"
            f"Fichiers modifiables:\n{file_list}"
        )

    out.append(f"📄 Fichier ciblé: `{target_file}`")

    # 2. Lecture du code actuel
    current_code = read_source(target_file)
    if current_code.startswith("❌"):
        return current_code

    # 3. Le LLM génère le nouveau code COMPLET
    gen_prompt = (
        f"Tu es un ingénieur Python senior. Voici le code actuel de `{target_file}`:\n\n"
        f"```python\n{current_code}\n```\n\n"
        f"DEMANDE: {request}\n\n"
        "Génère le fichier COMPLET modifié. Règles:\n"
        "- Garde tout le code existant qui fonctionne\n"
        "- Applique UNIQUEMENT le changement demandé\n"
        "- Code propre, gestion d'erreurs, compatible avec l'existant\n"
        "- INTERDIT: os.system, subprocess, eval, exec, accès .env/tokens\n"
        "- Réponds UNIQUEMENT avec le code Python complet dans un bloc ```python"
    )
    try:
        raw = llm_chat(
            [
                {"role": "system", "content": "Tu génères du code Python complet et fonctionnel. Bloc de code uniquement."},
                {"role": "user", "content": gen_prompt},
            ],
            temperature=0.1,
        )
    except Exception as e:
        return f"❌ Erreur génération: {e}"

    # Extraction du bloc de code
    m = _re.search(r"```(?:python)?\s*\n(.*?)```", raw, _re.DOTALL)
    new_code = m.group(1) if m else raw
    new_code = new_code.strip() + "\n"

    if len(new_code) < 50:
        return "❌ Le code généré est trop court — modification annulée par sécurité."

    # 4. Application sécurisée
    result = apply_modification(target_file, new_code, reason=request[:200])
    out.append("\n" + result["message"])

    if result["ok"]:
        out.append(
            "\n\n*Le module a été rechargé. Recharge la page si l'effet n'apparaît "
            "pas immédiatement. Utilise ↩️ pour annuler si besoin.*"
        )
    return "\n".join(out)


def self_modify_rollback_fn() -> str:
    from agent.self_modify import rollback_last
    return "## ↩️ Rollback\n\n" + rollback_last()["message"]


def self_modify_status_fn() -> str:
    from plugins.builtin.self_modify_plugin import SelfModStatusPlugin
    return SelfModStatusPlugin().run()


_PORTFOLIOS_FILE = Path("data/portfolios.json")


def _pf_load_all() -> dict:
    if _PORTFOLIOS_FILE.exists():
        try:
            return json.loads(_PORTFOLIOS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _pf_save(name: str, text: str):
    all_pf = _pf_load_all()
    all_pf[name.strip()] = {"positions": text, "updated": datetime.now().isoformat()}
    _PORTFOLIOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PORTFOLIOS_FILE.write_text(json.dumps(all_pf, ensure_ascii=False, indent=2), encoding="utf-8")


def _pf_list() -> list[str]:
    return list(_pf_load_all().keys())


def portfolio_analyze_fn(positions_text: str):
    if not positions_text.strip():
        return (
            "⚠️ Saisis tes positions (une par ligne) :\n\n"
            "```\nAAPL 10 150.00\nvalneva 25 4.10\nBTC-USD 0.5 42000\n```\n\n"
            "Tu peux utiliser les noms (apple, tesla, valneva…) ou les tickers Yahoo Finance.",
            None,
        )
    try:
        from plugins.builtin.finance import analyze_portfolio
        return analyze_portfolio(positions_text)
    except Exception as e:
        return f"❌ Erreur: {e}", None


def portfolio_save_fn(name: str, text: str):
    if not name.strip():
        return gr.update(), gr.update(choices=_pf_list()), "⚠️ Donne un nom au portefeuille."
    if not text.strip():
        return gr.update(), gr.update(choices=_pf_list()), "⚠️ Saisis d'abord tes positions."
    _pf_save(name.strip(), text)
    return gr.update(choices=_pf_list(), value=name.strip()), gr.update(choices=_pf_list()), f"✅ '{name}' sauvegardé."


def portfolio_load_fn(name: str):
    if not name:
        return gr.update(), ""
    all_pf = _pf_load_all()
    if name in all_pf:
        return all_pf[name]["positions"], f"✅ '{name}' chargé."
    return gr.update(), f"⚠️ Portefeuille '{name}' introuvable."


def portfolio_delete_fn(name: str):
    if not name:
        return gr.update(choices=_pf_list()), "", "⚠️ Sélectionne un portefeuille."
    all_pf = _pf_load_all()
    all_pf.pop(name, None)
    _PORTFOLIOS_FILE.write_text(json.dumps(all_pf, ensure_ascii=False, indent=2), encoding="utf-8")
    return gr.update(choices=_pf_list(), value=None), "", f"🗑️ '{name}' supprimé."

def _fetch_yahoo_direct(ticker: str, period: str = "3mo") -> str:
    """Requête HTTP directe vers l'API Yahoo Finance — contourne les bugs yfinance."""
    try:
        import requests, json
        from datetime import datetime, timedelta
        interval = "1d"
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval={interval}&range={period}"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return ""
        r = result[0]
        meta  = r.get("meta", {})
        closes = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes if c is not None]
        if not closes:
            return ""
        price      = closes[-1]
        prev_price = closes[-2] if len(closes) > 1 else price
        chg_pct    = (price - prev_price) / prev_price * 100 if prev_price else 0
        h_period   = max(closes)
        l_period   = min(closes)
        perf       = (price / closes[0] - 1) * 100 if closes[0] else 0
        currency   = meta.get("currency", "")
        name       = meta.get("longName") or meta.get("shortName") or ticker
        # Sanity check : performance irréaliste → anomalie de données
        perf_display = f"{perf:+.2f}%" if perf is not None and abs(perf) < 200 else "N/A (données aberrantes)"
        lines = [
            f"Source: Yahoo Finance direct",
            f"Nom: {name}",
            f"Prix actuel: {price:.2f} {currency}",
            f"Variation séance: {chg_pct:+.2f}%",
            f"Haut période ({period}): {h_period:.2f}",
            f"Bas période ({period}): {l_period:.2f}",
            f"Performance période: {perf_display}",
            f"Nb séances: {len(closes)}",
        ]
        return "\n".join(lines)
    except Exception:
        return ""


def _search_financial_web(ticker: str) -> str:
    """Fallback: cherche des données financières via DuckDuckGo si yfinance échoue."""
    try:
        from duckduckgo_search import DDGS
        queries = [
            f"{ticker} cours bourse prix aujourd'hui analyse technique",
            f"{ticker} stock price RSI buy sell signal",
        ]
        lines = []
        with DDGS() as ddgs:
            for q in queries:
                try:
                    for r in ddgs.text(q, max_results=3, region="fr-fr"):
                        title = str(r.get("title", ""))
                        body  = str(r.get("body",  ""))[:300]
                        if title:
                            lines.append(f"• {title}: {body}")
                except Exception:
                    pass
                if len(lines) >= 6:
                    break
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""



def deep_finance_fn(question: str):
    """
    Wrapper Gradio pour l'agent financier professionnel.
    Yields accumulated markdown (streaming).
    """
    if not question.strip():
        yield "⚠️ Pose ta question (ex: 'J'ai 600€ à investir en PEA ETF, que faire ?')"
        return
    try:
        from agent.finance_deep import deep_finance_research
        for chunk in deep_finance_research(question):
            yield chunk
    except Exception as e:
        yield f"❌ Erreur: {e}"


def finance_agent_analysis(question: str) -> str:
    """
    Pipeline direct garanti : extraction ticker → données réelles → synthèse LLM.
    Fallback web search si yfinance indisponible.
    """
    if not question.strip():
        return "⚠️ Pose une question financière."

    from llm.client import chat as llm_chat
    import re as _re

    # ── 1. Extraire le(s) ticker(s) via LLM ───────────────────────────────────
    ticker_prompt = (
        f"Question: \"{question}\"\n\n"
        "Extrais les symboles boursiers exacts mentionnés ou sous-entendus.\n"
        "Conversions OBLIGATOIRES (utilise EXACTEMENT ces tickers Yahoo Finance):\n"
        "--- Actions US ---\n"
        "Apple→AAPL | Tesla→TSLA | Nvidia→NVDA | Amazon→AMZN | Microsoft→MSFT\n"
        "Google/Alphabet→GOOGL | Meta→META | Netflix→NFLX | AMD→AMD | Intel→INTC\n"
        "Palantir→PLTR | Arm→ARM | Broadcom→AVGO | ASML→ASML | Qualcomm→QCOM\n"
        "--- Actions françaises (Euronext Paris) ---\n"
        "Soitec→SOI.PA | LVMH→MC.PA | Airbus→AIR.PA | BNP→BNP.PA | Sanofi→SAN.PA\n"
        "TotalEnergies/Total→TTE.PA | Kering→KER.PA | Hermès→RMS.PA | L'Oréal→OR.PA\n"
        "Valneva→VLA.PA | Renault→RNO.PA | Orange→ORA.PA | Carrefour→CA.PA\n"
        "Danone→BN.PA | Schneider→SU.PA | Thales→HO.PA | Michelin→ML.PA\n"
        "Capgemini→CAP.PA | Dassault Systèmes→DSY.PA | Safran→SAF.PA | Vivendi→VIV.PA\n"
        "Alstom→ALO.PA | Worldline→WLN.PA | Teleperformance→TEP.PA | Pernod→RI.PA\n"
        "Veolia→VIE.PA | EssilorLuxottica→EL.PA | STMicroelectronics→STMPA.PA\n"
        "Stellantis→STLAM.MI | Société Générale→GLE.PA | Crédit Agricole→ACA.PA\n"
        "--- Crypto ---\n"
        "Bitcoin→BTC-USD | Ethereum→ETH-USD | Solana→SOL-USD | BNB→BNB-USD\n"
        "XRP→XRP-USD | Cardano→ADA-USD | Avalanche→AVAX-USD | Polkadot→DOT-USD\n"
        "--- Indices / Forex / Matières ---\n"
        "Or→GC=F | Pétrole→CL=F | CAC40→^FCHI | S&P500→^GSPC | NASDAQ→^IXIC\n"
        "EUR/USD→EURUSD=X | Dollar→DX-Y.NYB\n"
        "Si question générale sans actif précis → GENERAL\n"
        "Réponds UNIQUEMENT avec les tickers séparés par virgule, ou GENERAL."
    )
    try:
        ticker_raw = llm_chat(
            [{"role": "user", "content": ticker_prompt}],
            temperature=0.0,
        ).strip().upper()
    except Exception:
        ticker_raw = "GENERAL"

    # Extraire uniquement les tokens format ticker
    tokens = _re.split(r"[,\s]+", ticker_raw)
    tickers = [t for t in tokens if _re.match(r"^\^?[A-Z]{1,5}[\-\.]?[A-Z0-9]{0,4}(=F|=X)?$", t) and t != "GENERAL"][:3]
    is_general = not tickers or "GENERAL" in ticker_raw

    # Variantes alternatives à essayer si le ticker principal échoue
    _TICKER_ALTS: dict[str, list[str]] = {
        "VALN": ["VLA.PA", "VALN"],        # Valneva : Paris = VLA.PA, NASDAQ = VALN
        "VLA.PA": ["VLA.PA", "VALN"],
        "STLAM.MI": ["STLAM.MI", "STLA"],  # Stellantis
        "STMPA.PA": ["STMPA.PA", "STM"],   # STMicro
        "EL.PA": ["EL.PA", "ESLOY"],       # EssilorLuxottica
    }

    # ── 2. Récupérer les données réelles ──────────────────────────────────────
    data_blocks = []

    if not is_general and tickers:
        from plugins.builtin.finance import StockAnalysisPlugin, MarketNewsPlugin, MultiStockComparePlugin
        stock_plugin = StockAnalysisPlugin()
        news_plugin  = MarketNewsPlugin()

        for tk in tickers:
            # Essai 1: plugin StockAnalysis avec le ticker principal + ses variantes
            analysis = None
            tk_used = tk
            candidates = _TICKER_ALTS.get(tk, [tk])
            if tk not in candidates:
                candidates = [tk] + candidates

            for candidate in candidates:
                if analysis:
                    break
                for period in ("3mo", "6mo", "1mo", "1y"):
                    try:
                        result = stock_plugin.run(ticker=candidate, period=period)
                        if result and "❌" not in result[:10] and "Erreur" not in result[:20]:
                            analysis = result
                            tk_used = candidate
                            break
                    except Exception:
                        pass

            if analysis:
                data_blocks.append(f"## Analyse technique {tk_used}\n{analysis}")
            else:
                # Essai 2: requête HTTP directe vers Yahoo Finance pour chaque variante
                direct_data = ""
                for candidate in candidates:
                    direct_data = _fetch_yahoo_direct(candidate)
                    if direct_data:
                        tk_used = candidate
                        break
                if direct_data:
                    data_blocks.append(
                        f"## {tk_used} — Données Yahoo Finance (HTTP direct):\n{direct_data}"
                    )
                else:
                    # Essai 3: recherche web DuckDuckGo
                    web_data = _search_financial_web(tk)
                    if web_data:
                        data_blocks.append(
                            f"## {tk} — données web (yfinance indisponible):\n{web_data}"
                        )
                    else:
                        # Aucune source ne répond — analyse qualitative uniquement
                        data_blocks.append(
                            f"## {tk} — données temps réel non accessibles\n"
                            f"Ticker: {tk}\n"
                            f"Tickers essayés: {', '.join(candidates)}\n"
                            f"INSTRUCTION STRICTE: Les données temps réel sont introuvables.\n"
                            f"Tu DOIS:\n"
                            f"- Donner une analyse QUALITATIVE: secteur, positionnement, "
                            f"risques principaux, catalyseurs potentiels (basé sur tes connaissances)\n"
                            f"- NE PAS inventer de chiffres précis (P/E, capitalisation, cours, CA, "
                            f"résultats financiers) — ces données seraient fausses et dangereuses\n"
                            f"- Donner une orientation générale (PLUTÔT HAUSSIER / BAISSIER / NEUTRE) "
                            f"justifiée par des éléments qualitatifs uniquement\n"
                            f"- Indiquer clairement que le prix actuel est inconnu et qu'il faut "
                            f"vérifier sur Yahoo Finance ou Boursorama avant tout achat"
                        )

            # Actualités
            try:
                news = news_plugin.run(ticker=tk)
                if news and "Aucune" not in news and "❌" not in news:
                    data_blocks.append(f"## Actualités {tk}\n{news[:700]}")
            except Exception:
                pass

        if len(tickers) > 1:
            try:
                cmp = MultiStockComparePlugin().run(",".join(tickers), "3mo")
                data_blocks.append(f"## Comparaison\n{cmp}")
            except Exception:
                pass
    else:
        from plugins.builtin.finance import MarketDashboardPlugin, MultiStockComparePlugin
        try:
            data_blocks.append(f"## Dashboard marchés\n{MarketDashboardPlugin().run()}")
            top = MultiStockComparePlugin().run("AAPL,NVDA,MSFT,AMZN,TSLA", "1mo")
            data_blocks.append(f"## Top 5 US (1 mois)\n{top}")
        except Exception as e:
            web = _search_financial_web("market overview stocks today")
            data_blocks.append(f"## Marché (données web)\n{web}" if web else f"Erreur dashboard: {e}")

    data_context = "\n\n".join(data_blocks)
    if not data_context.strip():
        data_context = "Données financières non disponibles pour cette requête."

    # ── 3. LLM synthétise avec les vraies données ─────────────────────────────
    try:
        from agent.system_prompt import FINANCE_SYSTEM_PROMPT
        system_prompt = FINANCE_SYSTEM_PROMPT
    except ImportError:
        system_prompt = (
            "Tu es un gérant de portefeuille senior (CFA). "
            "TOUJOURS fournir: zone d'entrée + TP1 + TP2 + stop-loss + ratio R/R pour chaque actif. "
            "TOUJOURS un verdict: ACHETER/DCA/ATTENDRE/ÉVITER + conviction /10. "
            "JAMAIS inventer des fondamentaux non sourcés — écrire N/D. "
            "JAMAIS 'consulter un professionnel'. "
            "Si données absentes: [estimation] mais toujours fournir les niveaux."
        )

    user_msg = (
        f"DONNÉES RÉELLES (récupérées maintenant):\n\n{data_context}\n\n"
        f"---\nQUESTION: {question}\n\n"
        "Réponds directement en français. Structure OBLIGATOIRE:\n"
        "1. Contexte marché (2-3 points chiffrés)\n"
        "2. Verdict (ACHETER MAINTENANT / DCA / ATTENDRE / ÉVITER)\n"
        "3. Analyse technique: RSI, tendance, Bollinger\n"
        "4. Niveaux de trading: zone entrée | TP1 | TP2 | Stop-loss | R/R\n"
        "5. Risques (3 min.)\n"
        "6. Plan d'action numéroté"
    )

    try:
        answer = llm_chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.5,
        ) or data_context
    except Exception as e:
        return f"⚠️ Erreur LLM ({e})\n\nDonnées brutes:\n\n{data_context[:4000]}"

    # Auto-amélioration en arrière-plan
    def _bg_finance(q, a):
        try:
            from agent.self_improve import evaluate_and_learn
            import asyncio
            asyncio.run(evaluate_and_learn(q, a, domain="finance"))
        except Exception:
            pass
    threading.Thread(target=_bg_finance, args=(question, answer), daemon=True).start()
    return answer


# ═════════════════════════════════════════════════════════════════════════════
# AGENTS & PLUGINS
# ═════════════════════════════════════════════════════════════════════════════

def mk_agent(role, obj, model):
    from orchestrator.factory import generate_agent_config
    from orchestrator import get_registry
    cfg = _run(generate_agent_config(role, obj, model or config.LLM_MODEL))
    get_registry().register(cfg)
    return json.dumps(cfg, indent=2, ensure_ascii=False)

def ls_agents():
    from orchestrator import get_registry
    return [[a["id"], a.get("name",""), a.get("role",""), a.get("status",""), a.get("last_active","")]
            for a in get_registry().list_all()]

def ls_plugins():
    from plugins import get_loader
    return [[n, d] for n, d in get_loader().list_all().items()]

def add_plugin(code):
    from plugins import get_loader
    Path(config.PLUGINS_DIR).mkdir(exist_ok=True)
    (Path(config.PLUGINS_DIR) / "user_plugin.py").write_text(code, encoding="utf-8")
    get_loader().reload_user_plugins()
    return f"✅ Plugin chargé : {list(get_loader().list_all().keys())}"

def run_master(goal):
    from orchestrator import get_master
    if not goal.strip():
        yield "⚠️ Saisis un objectif complexe."
        return
    yield "## 🧠 Orchestrateur — Analyse en cours...\n\n⏳ Décomposition de l'objectif en sous-tâches..."
    try:
        result = _run(get_master().execute(goal))

        mode = result.get("mode", "?")
        out = f"## ✅ Résultat — mode **{mode}**\n\n"

        created = result.get("auto_created", [])
        if created:
            out += "**🆕 Créés automatiquement :** " + ", ".join(f"`{c}`" for c in created) + "\n\n"

        agents = result.get("agents", [])
        if agents:
            out += "**Agents mobilisés :**\n"
            for a in agents:
                out += f"  - `{a['role']}` → _{a['objective']}_\n"
            out += "\n---\n\n"

        out += result.get("answer", "_Pas de réponse._")
        yield out

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        yield f"❌ **Erreur orchestrateur**\n\n```\n{tb[:1500]}\n```"


# ═════════════════════════════════════════════════════════════════════════════
# UI
# ═════════════════════════════════════════════════════════════════════════════

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="MasterAgent-Gros") as demo:

        mode_state = gr.State("fast")
        sid_state  = gr.State(_sid())

        # ── En-tête ──────────────────────────────────────────────────────────
        with gr.Row():
            gr.Markdown("# 🤖 MasterAgent-Gros")
            with gr.Column(scale=0, min_width=260):
                mode_btn = gr.Button("⚡ Mode Rapide — ACTIF", variant="secondary", size="sm")
        mode_ind = gr.Markdown("🟢 **Mode Rapide ⚡** — Réponses directes, sans outils")

        with gr.Tabs():

            # ══ CHAT ══════════════════════════════════════════════════════════
            with gr.TabItem("💬 Chat"):
                with gr.Row():
                    with gr.Column(scale=1, min_width=200):
                        gr.Markdown("### 📋 Historique")
                        new_btn   = gr.Button("✨ Nouvelle conv.", variant="primary", size="sm")
                        sess_dd   = gr.Dropdown(choices=_list_sessions(), label="Sessions", value=None)
                        ref_btn   = gr.Button("🔄 Rafraîchir", size="sm")

                    with gr.Column(scale=4):
                        if _CHATBOT_SUPPORTS_TYPE:
                            chatbot = gr.Chatbot(height=480, label="", type="messages")
                        else:
                            chatbot = gr.Chatbot(height=480, label="")
                        with gr.Row():
                            msg_in  = gr.Textbox(placeholder='Message… ou "Agent: fais-moi un script Python"',
                                                 scale=5, label="", lines=1)
                            send_btn = gr.Button("Envoyer ▶", variant="primary", scale=1)

                chat_st = gr.State([])

                # queue=False → le changement de mode est instantané, il ne fait plus
                # la file d'attente derrière une réponse Agent en cours (qui peut durer plusieurs minutes).
                mode_btn.click(toggle_mode, [mode_state], [mode_state, mode_btn, mode_ind], queue=False)
                # queue=False → le chat utilise une simple requête POST directe au lieu de la
                # file d'attente SSE de Gradio (souvent bloquée par les antivirus / proxys / VPN
                # qui inspectent les connexions streaming locales). Rend le chat fiable partout.
                send_btn.click(send, [msg_in, chat_st, mode_state, sid_state], [chatbot, chat_st, msg_in], queue=False)
                msg_in.submit(send, [msg_in, chat_st, mode_state, sid_state], [chatbot, chat_st, msg_in], queue=False)
                new_btn.click(new_conv, [], [chatbot, chat_st, sid_state]).then(refresh_sess, [], [sess_dd])
                ref_btn.click(refresh_sess, [], [sess_dd])
                sess_dd.change(load_sess, [sess_dd], [chatbot, chat_st, sid_state])

            # ══ ORCHESTRATEUR ═════════════════════════════════════════════════
            with gr.TabItem("🧠 Orchestrateur"):
                gr.Markdown("Donne un objectif complexe — le Master Agent le décompose en sous-agents spécialisés.")
                goal_in  = gr.Textbox(label="Objectif", lines=3,
                                      placeholder="Ex: Crée une vidéo virale sur le Bitcoin ET génère le code pour tracker son prix")
                orch_btn = gr.Button("🚀 Lancer", variant="primary")
                orch_out = gr.Markdown()
                orch_btn.click(run_master, [goal_in], [orch_out])

            # ══ VIDÉO ═════════════════════════════════════════════════════════
            with gr.TabItem("🎬 Vidéo"):
                gr.Markdown(
                    "**⚡ Rapide** — slides PIL immédiates  |  "
                    "**🏆 Pro** — photos réelles + voix + montage FFmpeg (5-10 min)"
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        v_topic   = gr.Textbox(label="💡 Sujet ou lien produit", lines=2,
                                               placeholder="Ex: https://monsite.com/produit-x  ou  5 erreurs qui font rater tes ventes Vinted")
                        v_quality = gr.Radio(["rapide", "pro"], value="rapide",
                                             label="🎚️ Qualité",
                                             info="Pro = agents spécialisés + vraies photos")
                        v_style   = gr.Dropdown(["éducatif","motivation","humour","tutoriel","choc","viral"],
                                                value="éducatif", label="🎭 Style")
                        with gr.Row():
                            v_lang  = gr.Dropdown(["fr","en","es"], value="fr", label="🌍 Langue")
                            v_theme = gr.Dropdown(["dark","fire","ocean","gold"], value="dark", label="🎨 Thème")
                        v_slides = gr.Slider(3, 10, value=6, step=1, label="📊 Slides")
                        v_audio  = gr.Checkbox(value=False, label="🔊 Voix TTS")
                        v_btn    = gr.Button("🎬 Générer", variant="primary", size="lg")
                        v_status = gr.Markdown("")

                    with gr.Column(scale=2):
                        with gr.Tabs():
                            with gr.TabItem("🖼️ Slides"):
                                v_gallery = gr.Gallery(label="", columns=3, height=420)
                            with gr.TabItem("🎞️ GIF (aperçu)"):
                                v_gif = gr.Image(label="Aperçu animé", height=420)
                            with gr.TabItem("🎥 Télécharger MP4"):
                                v_video = gr.File(label="Vidéo finale (MP4 ou GIF)")
                            with gr.TabItem("📝 Script"):
                                v_script = gr.Markdown("")

                v_btn.click(make_video,
                            [v_topic, v_style, v_lang, v_slides, v_theme, v_audio, v_quality],
                            [v_gallery, v_gif, v_video, v_status])

            # ══ IMAGE → VIDÉO RÉALISTE ════════════════════════════════════════
            with gr.TabItem("🖼️→🎬 Réaliste"):
                gr.Markdown(
                    "### Image → Vidéo réaliste · 5s minimum · 100% gratuit\n"
                    "**Méthode 1** (si `HF_API_TOKEN` configuré): Stable Video Diffusion via HuggingFace API\n\n"
                    "**Méthode 2** (fallback automatique): Ken Burns cinématique — zoom + pan diagonal + vignette\n\n"
                    "L'agent optimise automatiquement le mouvement et se ré-essaie si nécessaire."
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        iv_image = gr.Image(
                            label="🖼️ Image source", type="filepath",
                            sources=["upload", "clipboard"],
                            height=260,
                        )
                        iv_format = gr.Radio(
                            choices=list(_FORMATS.keys()),
                            value="Portrait 9:16 (TikTok/Reels)",
                            label="📐 Format",
                        )
                        iv_desc = gr.Textbox(
                            label="📝 Description de l'image (optionnel)",
                            placeholder="Ex: château dans la forêt brumeuse...",
                            lines=2,
                        )
                        iv_prompt = gr.Textbox(
                            label="🎥 Indication de mouvement (optionnel)",
                            placeholder="Ex: slow camera push forward, gentle fog motion",
                            lines=1,
                        )
                        iv_duration = gr.Slider(
                            minimum=5, maximum=15, value=5, step=1,
                            label="⏱️ Durée (secondes)",
                        )
                        iv_btn    = gr.Button("🎬 Générer la vidéo", variant="primary", size="lg")
                        iv_status = gr.Markdown("")
                        iv_file   = gr.File(label="⬇️ Télécharger le MP4")

                    with gr.Column(scale=2):
                        iv_video = gr.HTML(
                            value="<div style='height:400px;display:flex;align-items:center;"
                                  "justify-content:center;color:#8b949e;font-size:1.1rem;"
                                  "border:1px dashed #30363d;border-radius:10px'>"
                                  "🎬 La vidéo apparaîtra ici</div>"
                        )

                iv_btn.click(
                    make_img2video,
                    [iv_image, iv_desc, iv_prompt, iv_duration, iv_format],
                    [iv_video, iv_file, iv_status],
                )

            # ══ CODE & PROJETS ════════════════════════════════════════════════
            with gr.TabItem("💻 Code & Projets"):
                with gr.Tabs():

                    with gr.TabItem("⚙️ Générer du code"):
                        with gr.Row():
                            with gr.Column():
                                c_desc = gr.Textbox(label="Description", lines=3,
                                                    placeholder="Ex: Script Python pour télécharger des vidéos YouTube")
                                c_lang = gr.Dropdown(["python","javascript","bash","sql"], value="python", label="Langage")
                                c_run  = gr.Checkbox(value=False, label="Exécuter (Python uniquement)")
                                c_btn  = gr.Button("⚙️ Générer", variant="primary")
                            with gr.Column():
                                c_out  = gr.Code(label="Code généré", language="python")
                                c_exec = gr.Textbox(label="Sortie", lines=5)
                        c_btn.click(gen_code, [c_desc, c_lang, c_run], [c_out, c_exec])

                    with gr.TabItem("🗂️ Projet Complet"):
                        gr.Markdown(
                            "### Génère une application entière — vrai code fonctionnel + preview + ZIP\n"
                            "L'IA génère **tous** les fichiers (HTML, CSS, JS, Python, Docker, README…)"
                        )
                        with gr.Row():
                            with gr.Column(scale=1):
                                p_desc = gr.Textbox(
                                    label="Description du projet", lines=4,
                                    placeholder="Ex: Application web de gestion de tâches avec drag & drop, localStorage, design moderne dark")
                                p_type = gr.Dropdown(
                                    ["web", "python-api", "game", "dashboard", "cli", "react"],
                                    value="web", label="Type de projet")
                                p_btn  = gr.Button("🗂️ Générer le projet", variant="primary", size="lg")
                                p_zip  = gr.File(label="📦 Télécharger ZIP")
                                p_stat = gr.Markdown()
                            with gr.Column(scale=2):
                                gr.Markdown("#### 👁️ Preview")
                                p_prev = gr.HTML(label="", value="<div style='padding:2rem;color:#666;text-align:center'>La preview apparaîtra ici après génération</div>")
                        p_btn.click(gen_project, [p_desc, p_type], [p_zip, p_prev, p_stat])

                    with gr.TabItem("🐞 Débugger un projet"):
                        gr.Markdown(
                            "### Analyse un projet entier → trouve les bugs → les corrige\n"
                            "Donne le **chemin d'un dossier** de code. L'IA lit les fichiers, "
                            "liste les vrais bugs, et (si coché) les corrige en gardant une "
                            "sauvegarde `.bak` de chaque original.\n\n"
                            "⏱️ Peut prendre 1-3 min selon la taille (max 30 fichiers)."
                        )
                        with gr.Row():
                            with gr.Column(scale=1):
                                dbg_path = gr.Textbox(
                                    label="Chemin du dossier du projet",
                                    placeholder="C:\\Users\\lohan\\Documents\\mon-projet",
                                )
                                dbg_fix = gr.Checkbox(
                                    value=False,
                                    label="🔧 Corriger automatiquement (crée des .bak)",
                                )
                                dbg_btn = gr.Button("🐞 Analyser le projet", variant="primary", size="lg")
                            with gr.Column(scale=2):
                                dbg_out = gr.Markdown("*Le rapport de bugs apparaîtra ici.*")
                        # queue=False → revient malgré l'antivirus qui bloque le streaming
                        dbg_btn.click(debug_project_fn, [dbg_path, dbg_fix], [dbg_out], queue=False)

                    with gr.TabItem("📄 Analyser un document"):
                        gr.Markdown(
                            "### Lis et analyse un PDF, un document Word/texte, ou un dossier entier\n"
                            "Donne le **chemin d'un fichier ou dossier** + une **question** "
                            "(ou laisse vide pour un résumé). Marche avec les PDF, .docx, .txt, .md, code…"
                        )
                        with gr.Row():
                            with gr.Column(scale=1):
                                doc_path = gr.Textbox(
                                    label="Chemin du fichier ou dossier",
                                    placeholder="C:\\Users\\lohan\\Documents\\rapport.pdf",
                                )
                                doc_q = gr.Textbox(
                                    label="Ta question (optionnel)",
                                    placeholder="Ex: résume les points clés · quels sont les risques ? · explique la partie 3",
                                    lines=2,
                                )
                                doc_btn = gr.Button("📄 Analyser le document", variant="primary", size="lg")
                            with gr.Column(scale=2):
                                doc_out = gr.Markdown("*L'analyse apparaîtra ici.*")
                        doc_btn.click(analyze_document_fn, [doc_path, doc_q], [doc_out], queue=False)

            # ══ FINANCE ═══════════════════════════════════════════════════════
            with gr.TabItem("💹 Finance"):
                gr.Markdown(
                    "### Analyse boursière & crypto en temps réel\n"
                    "Données réelles · RSI · MACD · Bollinger · Graphiques · Portefeuille · Conseiller Pro"
                )
                with gr.Tabs():

                    # ── Dashboard ─────────────────────────────────────────────
                    with gr.TabItem("🌐 Dashboard"):
                        gr.Markdown("Vue d'ensemble instantanée — indices, crypto, forex, matières premières")
                        db_btn = gr.Button("🔄 Actualiser le dashboard", variant="primary", size="lg")
                        db_out = gr.Markdown()
                        db_btn.click(market_dashboard_fn, [], [db_out])

                    # ── Analyser ──────────────────────────────────────────────
                    with gr.TabItem("📊 Analyser"):
                        with gr.Row():
                            with gr.Column(scale=1):
                                f_ticker = gr.Textbox(
                                    label="Ticker",
                                    placeholder="AAPL · BTC-USD · MC.PA · ETH-USD · NVDA",
                                )
                                f_period = gr.Dropdown(
                                    ["1mo", "3mo", "6mo", "1y", "2y"], value="6mo", label="Période"
                                )
                                f_btn = gr.Button("📊 Analyser", variant="primary", size="lg")
                            with gr.Column(scale=2):
                                f_out = gr.Markdown()
                        f_chart = gr.Image(label="📈 Graphique technique", show_label=True)
                        f_btn.click(analyze_finance, [f_ticker, f_period], [f_out, f_chart])

                    # ── Crypto & Sentiment (sources gratuites CoinGecko + Fear&Greed) ──
                    with gr.TabItem("🪙 Crypto & Sentiment"):
                        gr.Markdown(
                            "Marché crypto en direct (CoinGecko) + **indice Fear & Greed** "
                            "+ taux de change (BCE). 100% gratuit, sans clé."
                        )
                        with gr.Row():
                            with gr.Column(scale=1):
                                cr_coins = gr.Textbox(
                                    label="Cryptos (optionnel, vide = top 8)",
                                    placeholder="BTC,ETH,SOL",
                                )
                                cr_btn = gr.Button("🪙 Marché crypto + sentiment", variant="primary")
                                gr.Markdown("—")
                                cur_to = gr.Textbox(label="Devises (depuis EUR)", value="USD,GBP,JPY,CHF")
                                cur_btn = gr.Button("💱 Taux de change", size="sm")
                            with gr.Column(scale=2):
                                cr_out = gr.Markdown("*Clique pour charger le marché crypto.*")
                        cr_btn.click(crypto_market_fn, [cr_coins], [cr_out])
                        cur_btn.click(currency_rates_fn, [cur_to], [cr_out])

                    # ── Portefeuille ──────────────────────────────────────────
                    with gr.TabItem("💼 Portefeuille"):
                        gr.Markdown(
                            "**TICKER/NOM QUANTITE PRIX_ACHAT** — une ligne par position\n\n"
                            "- Noms simples : `valneva 25 4.10` · `apple 10 150` · `bitcoin 0.1 60000`\n"
                            "- Tickers directs : `VLA.PA 25 4.10` · `AAPL 10 150` · `BTC-USD 0.1 60000`\n"
                            "- ETFs multi-mots : `Amundi Nasdaq PEA 100 5.29` · `PANX.PA 100 5.29`\n"
                            "- Commentaires : `AAPL 10 150  # achat jan 2024` (tout après `#` est ignoré)\n\n"
                            "**Allocation** = poids de chaque ligne dans la valeur totale du portefeuille."
                        )
                        with gr.Row():
                            with gr.Column(scale=1):
                                # Sauvegarde / chargement
                                with gr.Row():
                                    pf_name = gr.Textbox(
                                        label="Nom du portefeuille",
                                        placeholder="Mon portefeuille principal",
                                        scale=3,
                                    )
                                    pf_save_btn = gr.Button("💾 Sauver", scale=1, size="sm")
                                with gr.Row():
                                    pf_dd = gr.Dropdown(
                                        choices=_pf_list(),
                                        label="Charger un portefeuille sauvegardé",
                                        value=None, scale=3,
                                    )
                                    pf_load_btn   = gr.Button("📂 Charger", scale=1, size="sm")
                                    pf_delete_btn = gr.Button("🗑️", scale=0, size="sm", variant="secondary")
                                pf_status = gr.Markdown("")

                                pf_in = gr.Textbox(
                                    label="Positions",
                                    lines=12,
                                    placeholder=(
                                        "valneva 25 4.10\n"
                                        "AAPL 10 150.00\n"
                                        "BTC-USD 0.1 60000\n"
                                        "MC.PA 3 650.00\n"
                                        "NVDA 2 800.00\n"
                                        "PANX.PA 100 25.00  # Amundi Nasdaq PEA\n"
                                        "Amundi World PEA 50 35.50"
                                    ),
                                )
                                pf_btn = gr.Button("💼 Analyser", variant="primary", size="lg")

                            with gr.Column(scale=2):
                                pf_out = gr.Markdown()

                        pf_chart = gr.Image(label="📊 Allocation & Performance")

                        pf_btn.click(portfolio_analyze_fn, [pf_in], [pf_out, pf_chart])
                        pf_save_btn.click(
                            portfolio_save_fn, [pf_name, pf_in],
                            [pf_dd, pf_dd, pf_status],
                        )
                        pf_load_btn.click(
                            portfolio_load_fn, [pf_dd],
                            [pf_in, pf_status],
                        )
                        pf_delete_btn.click(
                            portfolio_delete_fn, [pf_dd],
                            [pf_dd, pf_in, pf_status],
                        )

                    # ── Actualités ────────────────────────────────────────────
                    with gr.TabItem("📰 Actualités"):
                        with gr.Row():
                            with gr.Column(scale=1):
                                fn_ticker = gr.Textbox(label="Ticker", placeholder="AAPL · TSLA · BTC-USD")
                                fn_btn    = gr.Button("📰 Voir les news", variant="primary")
                            with gr.Column(scale=2):
                                fn_out = gr.Markdown()
                        fn_btn.click(finance_news, [fn_ticker], [fn_out])

                    # ── Agent Financier ───────────────────────────────────────
                    with gr.TabItem("🤖 Agent Financier"):
                        gr.Markdown(
                            "Pose n'importe quelle question — l'agent récupère les données réelles "
                            "(analyze_stock, compare_stocks, market_dashboard, news) et synthétise "
                            "une analyse complète avec recommandation motivée."
                        )
                        fa_in  = gr.Textbox(
                            label="Question",
                            lines=3,
                            placeholder="Ex: Faut-il acheter Apple en ce moment ? Compare NVDA vs AMD. Analyse le BTC sur 6 mois.",
                        )
                        fa_btn = gr.Button("🤖 Analyser avec l'agent", variant="primary", size="lg")
                        fa_out = gr.Markdown()
                        fa_btn.click(finance_agent_analysis, [fa_in], [fa_out])

                    # ── Conseiller Pro (deep research streaming) ──────────────
                    with gr.TabItem("🎯 Conseiller Pro"):
                        gr.Markdown(
                            "### Agent financier professionnel — analyse multi-sources\n\n"
                            "Scan de l'univers ETF PEA • Données marché temps réel • "
                            "Scoring technique RSI/SMA/momentum • Recherche web • "
                            "Rapport structuré avec allocation exacte et plan d'action.\n\n"
                            "**Durée typique : 2-4 minutes.** L'analyse tourne en streaming — "
                            "tu vois la progression en direct."
                        )
                        pro_in = gr.Textbox(
                            label="Ta question",
                            lines=3,
                            placeholder=(
                                "J'ai 600€ à investir sur PEA en ETF, que recommandes-tu ?\n"
                                "Faut-il acheter des ETF Nasdaq PEA maintenant ou attendre ?\n"
                                "Construis-moi un portefeuille PEA diversifié avec 2000€."
                            ),
                        )
                        pro_btn = gr.Button(
                            "🚀 Lancer l'analyse complète",
                            variant="primary",
                            size="lg",
                        )
                        pro_out = gr.Markdown(
                            value="*L'analyse apparaît ici en temps réel...*"
                        )
                        pro_btn.click(
                            deep_finance_fn,
                            inputs=[pro_in],
                            outputs=[pro_out],
                        )

            # ══ AUTO-AMÉLIORATION ═════════════════════════════════════════════
            with gr.TabItem("🧬 Auto-Amélioration"):
                gr.Markdown(
                    "### L'agent apprend de chaque exécution\n"
                    "Après chaque réponse de l'Orchestrateur, il évalue sa propre qualité "
                    "et mémorise des leçons pour s'améliorer automatiquement."
                )
                with gr.Row():
                    si_btn = gr.Button("🔄 Voir les statistiques", variant="primary")
                    si_rst = gr.Button("🗑️ Réinitialiser", variant="secondary")
                with gr.Row():
                    with gr.Column():
                        si_stats = gr.Markdown("*Clique sur Voir les statistiques*")
                    with gr.Column():
                        si_lessons = gr.Dataframe(
                            headers=["Date", "Domaine", "Score", "Leçon apprise"],
                            label="Leçons récentes")

                def show_si():
                    try:
                        import importlib, sys
                        # Force reload si le module a été auto-modifié
                        mod_name = "agent.self_improve"
                        if mod_name in sys.modules:
                            importlib.reload(sys.modules[mod_name])
                        from agent.self_improve import get_stats, get_recent_lessons
                        stats = get_stats()
                        lessons = get_recent_lessons(15)
                        runs = stats.get("runs", 0)
                        avg = stats.get("avg_score", 0.0)
                        try:
                            avg_str = f"{float(avg):.1f}"
                        except (TypeError, ValueError):
                            avg_str = str(avg)
                        stats_md = (
                            f"**Runs évalués :** {runs}\n\n"
                            f"**Score moyen :** {avg_str} / 10"
                        )
                        rows = []
                        for l in reversed(list(lessons)):
                            ts = str(l.get("timestamp", ""))[:16].replace("T", " ")
                            score_val = l.get("score", "?")
                            rows.append([
                                ts,
                                str(l.get("domain", "?")),
                                f"{score_val}/10",
                                str(l.get("lesson", ""))[:80],
                            ])
                        return stats_md, rows
                    except Exception as e:
                        return f"❌ Erreur chargement statistiques: {e}", []

                def reset_si():
                    from pathlib import Path
                    Path("data/self_improve.json").unlink(missing_ok=True)
                    return "✅ Réinitialisé.", []

                si_btn.click(show_si, [], [si_stats, si_lessons])
                si_rst.click(reset_si, [], [si_stats, si_lessons])

                # ── Auto-modification de code ────────────────────────────────
                gr.Markdown("---\n### 🔧 Auto-modification de code")
                gr.Markdown(
                    "L'agent peut réécrire son propre code (system_prompt, finance_deep, "
                    "self_improve, finance plugin). Chaque modification est **sécurisée** : "
                    "backup automatique, validation syntaxe + import, rollback si échec.\n\n"
                    "Décris l'amélioration souhaitée — l'agent lit le code concerné, "
                    "génère le patch, le valide et l'applique."
                )
                with gr.Row():
                    with gr.Column(scale=2):
                        sm_request = gr.Textbox(
                            label="Amélioration à apporter",
                            lines=3,
                            placeholder=(
                                "Ex: Ajoute le calcul du MACD dans finance_deep.py\n"
                                "Ex: Ajoute l'ETF Amundi MSCI India à l'univers PEA\n"
                                "Ex: Renforce la règle anti-hallucination dans le system prompt"
                            ),
                        )
                        with gr.Row():
                            sm_btn      = gr.Button("🔧 Générer & appliquer", variant="primary")
                            sm_rollback = gr.Button("↩️ Annuler dernière modif", variant="secondary")
                            sm_status_btn = gr.Button("📋 État / journal", variant="secondary")
                    with gr.Column(scale=3):
                        sm_out = gr.Markdown("*Décris une amélioration et clique sur Générer.*")

                sm_btn.click(self_modify_fn, [sm_request], [sm_out])
                sm_rollback.click(self_modify_rollback_fn, [], [sm_out])
                sm_status_btn.click(self_modify_status_fn, [], [sm_out])

            # ══ AGENTS ════════════════════════════════════════════════════════
            with gr.TabItem("🤖 Agents"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Créer un sous-agent")
                        a_role  = gr.Dropdown(
                            ["researcher","coder","fullstack_dev","finance_analyst","crypto_analyst",
                             "marketing_expert","copywriter","seo_expert","video_creator","youtube_creator",
                             "writer","data_scientist","analyst","ecommerce_expert","game_developer","generic"],
                            value="coder", label="Rôle")
                        a_obj   = gr.Textbox(label="Objectif")
                        a_model = gr.Textbox(label="Modèle (vide = défaut)")
                        a_btn   = gr.Button("➕ Créer", variant="primary")
                        a_cfg   = gr.Code(label="Config JSON", language="json")
                        a_btn.click(mk_agent, [a_role, a_obj, a_model], [a_cfg])
                    with gr.Column():
                        gr.Markdown("### Agents existants")
                        a_rbtn  = gr.Button("🔄 Rafraîchir")
                        a_tbl   = gr.Dataframe(headers=["ID","Nom","Rôle","Statut","Dernière activité"])
                        a_rbtn.click(ls_agents, [], [a_tbl])

            # ══ PLUGINS ═══════════════════════════════════════════════════════
            with gr.TabItem("🔌 Plugins"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Plugins installés")
                        p_tbl  = gr.Dataframe(headers=["Nom","Description"])
                        p_rbtn = gr.Button("🔄 Rafraîchir")
                        p_rbtn.click(ls_plugins, [], [p_tbl])
                    with gr.Column():
                        gr.Markdown("### Ajouter un plugin")
                        p_code = gr.Code(language="python", label="Code",
                                         value='from plugins.base import Plugin\n\nclass MonPlugin(Plugin):\n    name = "mon_outil"\n    description = "Description"\n    parameters = {"input": {"type": "string", "required": True}}\n\n    def run(self, input: str) -> str:\n        return f"Résultat: {input}"\n')
                        p_abtn = gr.Button("⬆️ Charger", variant="primary")
                        p_stat = gr.Textbox(label="Statut")
                        p_abtn.click(add_plugin, [p_code], [p_stat])

    return demo


def launch(share: bool = False, port: int = None):
    demo = build_ui()
    # allowed_paths: autorise Gradio à servir les fichiers de output/ et data/
    # SANS ça → 403 Forbidden sur toutes les images/vidéos générées
    allowed = []
    for d in ("output", "data"):
        p = Path(d).resolve()
        p.mkdir(parents=True, exist_ok=True)
        allowed.append(str(p))

    launch_kwargs = {
        "server_port": port or config.GRADIO_PORT,
        "share": share or config.GRADIO_SHARE,
        "server_name": "0.0.0.0",
        "show_error": True,
        "allowed_paths": allowed,
    }
    demo.queue()
    demo.launch(**launch_kwargs)


if __name__ == "__main__":
    launch()
