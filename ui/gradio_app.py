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
import gradio as gr
from config import config

# ── compatibilité Gradio (détecte les paramètres réels plutôt que la version) ─
_V = int(gr.__version__.split(".")[0])
_CHATBOT_SUPPORTS_TYPE = "type" in inspect.signature(gr.Chatbot.__init__).parameters

# ── sessions ──────────────────────────────────────────────────────────────────
SESSIONS_DIR = Path("data/sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

AGENT_TRIGGERS = ("agent:", "mode complet:", "plein mode:", "utilise tes outils:", "passe en mode agent")

_FINANCE_HINTS = ("bourse", "action ", "crypto", "bitcoin", "ethereum", "ticker", "rsi", "macd", "analyser aapl",
                  "analyser tsla", "analyser nvda", "nasdaq", "cac40", "cac 40", "dow jones", "s&p", "trading",
                  "investir", "portefeuille", "dividende", "cotation", "achat vente action")
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

_FAST_SYS = (
    "Tu es MasterAgent-Gros v3, un super-assistant IA personnel ultra-intelligent et polyvalent. "
    "Tu réponds en français de façon directe, précise et immédiatement utile. "
    "Tu es expert en : code full-stack, finance quantitative, marketing digital, "
    "data science, stratégie business, création de contenu et bien plus. "
    "Tu donnes des réponses structurées avec exemples concrets quand c'est utile. "
    "Pour des tâches nécessitant des outils (données réelles, génération de fichiers, web), "
    "active le Mode Agent 🔥 ou préfixe par 'Agent:'. "
    "Tu ne dis jamais 'je ne sais pas' sans proposer une alternative."
)

def fast_chat(message: str, history: list) -> str:
    from llm.client import chat
    msgs = [{"role": "system", "content": _FAST_SYS}]
    msgs.extend(_to_ollama(history))
    msgs.append({"role": "user", "content": message})
    try:
        return chat(msgs, temperature=0.7)
    except Exception as e:
        return f"❌ LLM indisponible: {e}"


# ═════════════════════════════════════════════════════════════════════════════
# 🔥 FULL AGENT — ReAct + outils + mémoire
# ═════════════════════════════════════════════════════════════════════════════

def full_agent(message: str, history: list, sid: str) -> str:
    from agent.core import run_agent
    from plugins import get_loader
    cfg = {
        "id": sid, "name": "MasterAgent-Gros v3",
        "system_prompt": (
            "Tu es MasterAgent-Gros v3, un agent IA surpuissant et auto-évolutif. "
            "Tu maîtrises tous les domaines : code full-stack, finance quantitative, "
            "marketing digital, création de contenu, data science, stratégie business. "
            "Tu utilises TOUS tes outils sans hésitation pour donner des réponses complètes et actionnables. "
            "Tu génères du VRAI code fonctionnel, de vraies analyses avec données réelles, "
            "de vrais projets téléchargeables en ZIP. "
            "Tu t'améliores à chaque interaction grâce à ta mémoire et tes leçons apprises. "
            "Tu réponds en français avec des réponses structurées, détaillées et immédiatement utiles. "
            "Tu ne dis jamais 'je ne peux pas' — tu trouves toujours une solution créative. "
            "Si tu génères du code, il est COMPLET et FONCTIONNEL, pas un squelette."
        ),
        "tools": list(get_loader().list_all().keys()),
        "model": config.LLM_MODEL,
    }
    try:
        result = _run(run_agent(message, cfg, sid))
        answer = result.get("answer", "Pas de réponse.")
        used = {s["action"] for s in result.get("steps", []) if s.get("action")}
        if used:
            answer += f"\n\n*🔧 Outils : {', '.join(sorted(used))}*"
        # Auto-amélioration en arrière-plan (thread indépendant)
        def _bg(t, a):
            try:
                from agent.self_improve import evaluate_and_learn
                asyncio.run(evaluate_and_learn(t, a, domain="chat"))
            except Exception:
                pass
        threading.Thread(target=_bg, args=(message, answer), daemon=True).start()
        return answer
    except Exception as e:
        import traceback
        return f"❌ Erreur agent: {e}\n\n```\n{traceback.format_exc()[:800]}\n```"


# ═════════════════════════════════════════════════════════════════════════════
# ROUTING
# ═════════════════════════════════════════════════════════════════════════════

def _route(message: str, mode: str):
    """Retourne (use_agent, message_nettoyé)."""
    if mode == "agent": return True, message
    low = message.lower().strip()
    for t in AGENT_TRIGGERS:
        if low.startswith(t):
            return True, message[len(t):].strip() or message
    return False, message

def send(message: str, history: list, mode: str, sid: str):
    if not message.strip():
        return history, history, ""
    use_agent, clean = _route(message, mode)
    if use_agent:
        answer = full_agent(clean, history, sid)
    else:
        answer = fast_chat(clean, history)
        # Smart intent hints — guide l'utilisateur vers le bon onglet
        low = clean.lower()
        if any(k in low for k in _FINANCE_HINTS):
            answer += "\n\n💡 *Pour une analyse complète avec RSI/MACD/Bollinger → onglet **💹 Finance***"
        elif any(k in low for k in _VIDEO_HINTS):
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

def make_img2video(image_file, description, motion_prompt, duration, progress=gr.Progress()):
    """Transforme une image en vidéo réaliste 720p via img2video pipeline."""
    import re
    from pathlib import Path

    if image_file is None:
        return None, "⚠️ Charge une image d'abord."

    progress(0.05, desc="🖼️ Chargement de l'image...")

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
            duration=max(float(duration), 5.0),
            on_progress=_on_prog,
            max_loops=2,
        )

    result = _run(_run_gen())

    if "error" in result:
        return None, f"❌ Erreur: {result['error']}\n\n" + "\n".join(steps)

    hf_note = ""
    if result.get("method") == "ffmpeg_ken_burns":
        hf_note = "\n\n💡 **Astuce**: Ajoute un `HF_API_TOKEN` dans `.env` pour activer Stable Video Diffusion (vidéos encore plus réalistes)."

    status = (
        f"✅ **Vidéo générée!**\n\n"
        f"- Méthode: `{result.get('method')}`\n"
        f"- Score qualité: **{result.get('score')}/10**\n"
        f"- Tentatives: {result.get('attempts')}\n"
        f"{hf_note}"
    )
    return result["video_path"], status


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


# ═════════════════════════════════════════════════════════════════════════════
# FINANCE
# ═════════════════════════════════════════════════════════════════════════════

def analyze_finance(ticker: str, period: str) -> str:
    if not ticker.strip():
        return "⚠️ Saisis un ticker (ex: AAPL, BTC-USD, MC.PA)"
    try:
        from plugins.builtin.finance import StockAnalysisPlugin
        return StockAnalysisPlugin().run(ticker=ticker.strip(), period=period)
    except Exception as e:
        return f"❌ Erreur: {e}"

def compare_finance(tickers: str, period: str) -> str:
    if not tickers.strip():
        return "⚠️ Saisis des tickers séparés par virgule (ex: AAPL,MSFT,GOOGL)"
    try:
        from plugins.builtin.finance import MultiStockComparePlugin
        return MultiStockComparePlugin().run(tickers=tickers, period=period)
    except Exception as e:
        return f"❌ Erreur: {e}"

def finance_news(ticker: str) -> str:
    if not ticker.strip():
        return "⚠️ Saisis un ticker"
    try:
        from plugins.builtin.finance import MarketNewsPlugin
        return MarketNewsPlugin().run(ticker=ticker.strip())
    except Exception as e:
        return f"❌ Erreur: {e}"

def finance_agent_analysis(question: str) -> str:
    """Lance l'agent finance_analyst complet avec ReAct + outils."""
    if not question.strip():
        return "⚠️ Pose une question financière."
    from agent.core import run_agent
    from plugins import get_loader
    cfg = {
        "id": "finance_ui",
        "name": "FinanceAgent Pro",
        "system_prompt": (
            "Tu es un analyste financier senior expert. "
            "Tu utilises analyze_stock, compare_stocks et get_market_news pour obtenir des données réelles. "
            "Tu fournis une analyse complète avec recommandation achat/vente/hold motivée. "
            "Tu rappelles que tes analyses ne sont pas des conseils financiers officiels."
        ),
        "tools": ["analyze_stock", "compare_stocks", "get_market_news", "search_web"],
        "model": config.LLM_MODEL,
    }
    try:
        result = _run(run_agent(question, cfg, "finance_ui"))
        return result.get("answer", "Pas de réponse.")
    except Exception as e:
        return f"❌ Erreur agent: {e}"


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

def run_master(goal, progress=gr.Progress()):
    from orchestrator import get_master
    if not goal.strip():
        return "⚠️ Saisis un objectif complexe."
    try:
        progress(0.05, desc="🔍 Analyse de la requête...")
        result = _run(get_master().execute(goal))
        progress(0.95, desc="✅ Synthèse terminée")

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
        return out

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return f"❌ **Erreur orchestrateur**\n\n```\n{tb[:1500]}\n```\n\n*Vérifie le terminal pour plus de détails.*"


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

                mode_btn.click(toggle_mode, [mode_state], [mode_state, mode_btn, mode_ind])
                send_btn.click(send, [msg_in, chat_st, mode_state, sid_state], [chatbot, chat_st, msg_in])
                msg_in.submit(send, [msg_in, chat_st, mode_state, sid_state], [chatbot, chat_st, msg_in])
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
                    "### Image → Vidéo réaliste 720p · 5s minimum · 100% gratuit\n"
                    "**Méthode 1** (si `HF_API_TOKEN` configuré): Stable Video Diffusion via HuggingFace API\n\n"
                    "**Méthode 2** (fallback automatique): Effet Ken Burns haute qualité via FFmpeg\n\n"
                    "L'agent optimise automatiquement le mouvement et se ré-essaie si nécessaire."
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        iv_image = gr.Image(
                            label="🖼️ Image source", type="filepath",
                            sources=["upload", "clipboard"],
                            height=280,
                        )
                        iv_desc = gr.Textbox(
                            label="📝 Description de l'image (optionnel)",
                            placeholder="Ex: coucher de soleil sur la mer, plage déserte...",
                            lines=2,
                        )
                        iv_prompt = gr.Textbox(
                            label="🎥 Indication de mouvement (optionnel)",
                            placeholder="Ex: slow camera pan right, gentle waves motion",
                            lines=1,
                        )
                        iv_duration = gr.Slider(
                            minimum=5, maximum=15, value=5, step=1,
                            label="⏱️ Durée (secondes)",
                        )
                        iv_btn = gr.Button("🎬 Générer la vidéo", variant="primary", size="lg")
                        iv_status = gr.Markdown("")

                    with gr.Column(scale=2):
                        iv_video = gr.Video(label="🎥 Vidéo générée", height=450)

                iv_btn.click(
                    make_img2video,
                    [iv_image, iv_desc, iv_prompt, iv_duration],
                    [iv_video, iv_status],
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

            # ══ FINANCE ═══════════════════════════════════════════════════════
            with gr.TabItem("💹 Finance"):
                gr.Markdown(
                    "### Analyse boursière & crypto en temps réel\n"
                    "Données réelles · RSI · MACD · Bollinger · Recommandation achat/vente"
                )
                with gr.Tabs():

                    with gr.TabItem("📊 Analyser une action"):
                        with gr.Row():
                            with gr.Column(scale=1):
                                f_ticker  = gr.Textbox(label="Ticker", placeholder="AAPL · BTC-USD · MC.PA · ETH-USD · NVDA")
                                f_period  = gr.Dropdown(["1mo","3mo","6mo","1y","2y"], value="6mo", label="Période")
                                f_btn     = gr.Button("📊 Analyser", variant="primary", size="lg")
                            with gr.Column(scale=2):
                                f_out = gr.Markdown()
                        f_btn.click(analyze_finance, [f_ticker, f_period], [f_out])

                    with gr.TabItem("⚖️ Comparer"):
                        with gr.Row():
                            with gr.Column(scale=1):
                                fc_tickers = gr.Textbox(label="Tickers (séparés par virgule)",
                                                        placeholder="AAPL,MSFT,GOOGL,NVDA")
                                fc_period  = gr.Dropdown(["1mo","3mo","6mo","1y"], value="3mo", label="Période")
                                fc_btn     = gr.Button("⚖️ Comparer", variant="primary")
                            with gr.Column(scale=2):
                                fc_out = gr.Markdown()
                        fc_btn.click(compare_finance, [fc_tickers, fc_period], [fc_out])

                    with gr.TabItem("📰 Actualités"):
                        with gr.Row():
                            with gr.Column(scale=1):
                                fn_ticker = gr.Textbox(label="Ticker", placeholder="AAPL · TSLA · BTC-USD")
                                fn_btn    = gr.Button("📰 Voir les news", variant="primary")
                            with gr.Column(scale=2):
                                fn_out = gr.Markdown()
                        fn_btn.click(finance_news, [fn_ticker], [fn_out])

                    with gr.TabItem("🤖 Agent Financier"):
                        gr.Markdown(
                            "Pose n'importe quelle question financière — l'agent utilise les vrais outils "
                            "(analyze_stock, compare_stocks, news) et synthétise une réponse complète."
                        )
                        fa_in  = gr.Textbox(label="Question", lines=3,
                                            placeholder="Ex: Faut-il acheter Apple en ce moment? Compare NVDA vs AMD. Analyse le Bitcoin sur 6 mois.")
                        fa_btn = gr.Button("🤖 Analyser avec l'agent", variant="primary", size="lg")
                        fa_out = gr.Markdown()
                        fa_btn.click(finance_agent_analysis, [fa_in], [fa_out])

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
                    from agent.self_improve import get_stats, get_recent_lessons
                    stats = get_stats()
                    lessons = get_recent_lessons(15)
                    stats_md = (
                        f"**Runs évalués :** {stats.get('runs', 0)}\n\n"
                        f"**Score moyen :** {stats.get('avg_score', 0):.1f} / 10"
                    )
                    rows = []
                    for l in reversed(lessons):
                        ts = l.get("timestamp", "")[:16].replace("T", " ")
                        rows.append([ts, l.get("domain","?"), f"{l.get('score','?')}/10", l.get("lesson","")[:80]])
                    return stats_md, rows

                def reset_si():
                    from pathlib import Path
                    Path("data/self_improve.json").unlink(missing_ok=True)
                    return "✅ Réinitialisé.", []

                si_btn.click(show_si, [], [si_stats, si_lessons])
                si_rst.click(reset_si, [], [si_stats, si_lessons])

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
    demo.launch(**launch_kwargs)


if __name__ == "__main__":
    launch()
