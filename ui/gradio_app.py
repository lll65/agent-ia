"""
MasterAgent-Gros — Interface Web
⚡ Mode Rapide : Groq direct, < 2s, aucun outil
🔥 Mode Agent  : ReAct + outils + mémoire + sous-agents
"""
import asyncio
import json
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


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _run(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


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
    "Tu es MasterAgent, un assistant IA personnel intelligent, chaleureux et direct. "
    "Tu réponds en français de façon concise et naturelle. "
    "Pour des tâches complexes (code, fichiers, vidéo, recherche web), "
    "dis à l'utilisateur d'activer le Mode Agent 🔥 ou de préfixer par 'Agent:'."
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
        "id": sid, "name": "MasterAgent-Gros",
        "system_prompt": (
            "Tu es MasterAgent-Gros, un agent IA ultra-puissant. "
            "Tu utilises tous les outils disponibles sans hésitation. "
            "Tu crées du code complet, des fichiers, des projets entiers. "
            "Tu réponds en français avec des réponses détaillées et utiles."
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
        return answer
    except Exception as e:
        return f"❌ Erreur agent: {e}"


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
    answer = full_agent(clean, history, sid) if use_agent else fast_chat(clean, history)
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
        h = d.get("history", [])
        return h, h, sid
    return [], [], _sid()

def refresh_sess():
    return gr.update(choices=_list_sessions(), value=None)


# ═════════════════════════════════════════════════════════════════════════════
# VIDÉO
# ═════════════════════════════════════════════════════════════════════════════

def make_video(topic, style, lang, n_slides, theme, add_audio, progress=gr.Progress()):
    import httpx
    from pathlib import Path
    from video.image_gen import save_slide

    if not topic.strip():
        return [], None, "⚠️ Saisis un sujet."

    # 1. Génère le script
    progress(0.1, desc="✍️ Génération du script...")
    try:
        async def _script():
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"http://localhost:{config.PORT}/video/script",
                                 json={"topic": topic, "style": style, "lang": lang,
                                       "slides_count": int(n_slides)})
                return r.json()
        data = _run(_script())
        slides = data.get("slides", [topic])
    except Exception as e:
        return [], None, f"❌ Erreur script: {e}"

    # 2. Génère les images
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in topic)[:35]
    tmp = Path("output/tmp_preview") / safe
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, txt in enumerate(slides):
        p = str(tmp / f"slide_{i:03d}.png")
        save_slide(txt, p, index=i, total=len(slides), theme=theme)
        paths.append(p)
        progress(0.1 + 0.5 * (i+1)/len(slides), desc=f"🖼️ Slide {i+1}/{len(slides)}")

    # 3. GIF animé
    progress(0.7, desc="🎞️ Création du GIF...")
    gif_path = None
    try:
        from PIL import Image as PILImage
        frames = [PILImage.open(p) for p in paths]
        gif_path = f"output/videos/{safe}.gif"
        Path("output/videos").mkdir(parents=True, exist_ok=True)
        frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                       duration=2500, loop=0, optimize=True)
    except Exception as e:
        pass

    # 4. MP4 en tâche de fond si FFmpeg dispo
    if add_audio:
        try:
            import threading
            def _mp4():
                from video.creator import create_video
                create_video(slides, safe, lang=lang, add_audio=True, theme=theme)
            threading.Thread(target=_mp4, daemon=True).start()
        except: pass

    progress(1.0, desc="✅ Terminé!")
    status = f"✅ **{len(slides)} slides** générées — sujet : _{topic}_"
    if gif_path:
        status += "\n\n👆 Onglet **GIF** pour l'aperçu animé · clic droit → Enregistrer"
    return paths, gif_path, status


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
    return d.get("code", str(d)), d.get("output", "(non exécuté)")


def gen_project(desc, progress=gr.Progress()):
    """Génère un projet complet (tous les fichiers) depuis une description."""
    from llm.client import chat
    import zipfile, tempfile

    if not desc.strip():
        return None, "⚠️ Décris le projet."

    progress(0.1, desc="🧠 Planification du projet...")

    plan_prompt = f"""Tu es un architecte logiciel expert. Génère un projet complet pour: {desc}

Réponds UNIQUEMENT en JSON:
{{
  "name": "nom-du-projet",
  "description": "description courte",
  "files": {{
    "main.py": "contenu complet du fichier",
    "requirements.txt": "contenu",
    "README.md": "contenu",
    "Dockerfile": "contenu"
  }}
}}

Génère du vrai code fonctionnel et complet dans chaque fichier. Maximum 8 fichiers."""

    try:
        raw = chat([
            {"role": "system", "content": "Tu es un expert en développement. Tu génères du code complet et fonctionnel. Réponds UNIQUEMENT en JSON valide."},
            {"role": "user", "content": plan_prompt}
        ], temperature=0.3)

        import re
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            return None, "❌ Impossible de parser le plan."
        plan = json.loads(m.group())
    except Exception as e:
        return None, f"❌ Erreur LLM: {e}"

    progress(0.4, desc="📁 Génération des fichiers...")

    proj_name = plan.get("name", "projet")
    files = plan.get("files", {})
    out_dir = Path("output") / "projects" / proj_name
    out_dir.mkdir(parents=True, exist_ok=True)

    file_list = []
    for i, (fname, content) in enumerate(files.items()):
        fpath = out_dir / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(str(content), encoding="utf-8")
        file_list.append(str(fpath))
        progress(0.4 + 0.4 * (i+1)/len(files), desc=f"📄 {fname}")

    # ZIP
    progress(0.9, desc="📦 Création du ZIP...")
    zip_path = str(Path("output/projects") / f"{proj_name}.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in file_list:
            zf.write(f, Path(f).relative_to(out_dir.parent.parent))

    progress(1.0)
    summary = f"## ✅ Projet `{proj_name}` généré\n\n"
    summary += f"**{len(files)} fichiers créés :**\n"
    for fname in files:
        summary += f"  - `{fname}`\n"
    summary += f"\n📁 Dossier : `output/projects/{proj_name}/`\n"
    summary += f"📦 ZIP : `{zip_path}`"

    return zip_path, summary


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
    if not goal.strip(): return "Saisis un objectif."
    progress(0.1, desc="🧩 Décomposition...")
    result = _run(get_master().execute(goal))
    progress(0.9, desc="🔀 Synthèse...")
    out = f"## Résultat\n**Mode:** {result.get('mode','?')}\n"
    for a in result.get("agents", []):
        out += f"  - `{a['role']}` : {a['objective']}\n"
    return out + f"\n---\n\n{result.get('answer','')}"


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
                with gr.Row():
                    with gr.Column(scale=1):
                        v_topic  = gr.Textbox(label="💡 Sujet", lines=2,
                                              placeholder="Ex: 5 erreurs qui font rater tes ventes Vinted")
                        v_style  = gr.Dropdown(["éducatif","motivation","humour","tutoriel","choc"],
                                               value="éducatif", label="🎭 Style")
                        with gr.Row():
                            v_lang  = gr.Dropdown(["fr","en","es"], value="fr", label="🌍 Langue")
                            v_theme = gr.Dropdown(["dark","fire","ocean","gold"], value="dark", label="🎨 Thème")
                        v_slides = gr.Slider(3, 10, value=5, step=1, label="📊 Slides")
                        v_audio  = gr.Checkbox(value=False, label="🔊 Voix TTS (plus lent)")
                        v_btn    = gr.Button("🎬 Générer", variant="primary", size="lg")
                        v_status = gr.Markdown("")

                    with gr.Column(scale=2):
                        with gr.Tabs():
                            with gr.TabItem("🖼️ Slides"):
                                v_gallery = gr.Gallery(label="", columns=3, height=400)
                            with gr.TabItem("🎞️ GIF (aperçu)"):
                                v_gif = gr.Image(label="Aperçu animé — clic droit pour télécharger", height=400)
                            with gr.TabItem("📝 Script"):
                                v_script = gr.Textbox(label="", lines=12)

                v_btn.click(make_video,
                            [v_topic, v_style, v_lang, v_slides, v_theme, v_audio],
                            [v_gallery, v_gif, v_status])

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
                        gr.Markdown("### Génère une application entière en un seul clic\n"
                                    "Frontend + Backend + Base de données + Docker + README")
                        p_desc   = gr.Textbox(label="Description du projet", lines=3,
                                              placeholder="Ex: Application web de todo list avec FastAPI + HTML + SQLite")
                        p_btn    = gr.Button("🗂️ Générer le projet complet", variant="primary", size="lg")
                        p_zip    = gr.File(label="📦 Télécharger le projet (ZIP)")
                        p_out    = gr.Markdown()
                        p_btn.click(gen_project, [p_desc], [p_zip, p_out])

            # ══ AGENTS ════════════════════════════════════════════════════════
            with gr.TabItem("🤖 Agents"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Créer un sous-agent")
                        a_role  = gr.Dropdown(["researcher","coder","video_creator","analyst","writer","generic"],
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
    launch_kwargs = {
        "server_port": port or config.GRADIO_PORT,
        "share": share or config.GRADIO_SHARE,
        "server_name": "0.0.0.0",
        "show_error": True,
    }
    try:
        launch_kwargs["theme"] = gr.themes.Soft()
    except Exception:
        pass
    demo.launch(**launch_kwargs)


if __name__ == "__main__":
    launch()
