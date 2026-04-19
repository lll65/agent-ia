"""
Interface Web — MasterAgent-Gros
Architecture deux couches :
  ⚡ Fast Chat  — appel direct Ollama, sans ReAct ni outils, < 5s
  🔥 Full Agent — boucle ReAct complète avec mémoire, outils, sous-agents
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

import gradio as gr

from config import config

# ── compat Gradio 4 / 5 ───────────────────────────────────────────────────────
_GRADIO_MAJOR = int(gr.__version__.split(".")[0])
_USE_DICT_FORMAT = _GRADIO_MAJOR >= 5

# ── sessions persistantes ─────────────────────────────────────────────────────
SESSIONS_DIR = Path("data/sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Préfixes qui activent le mode Agent depuis le mode Rapide
AGENT_TRIGGERS = (
    "agent:",
    "mode complet:",
    "plein mode:",
    "utilise tes outils:",
    "passe en mode agent",
)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _run(coro):
    """Exécute une coroutine depuis un contexte synchrone."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _history_to_ollama(history: list) -> list:
    """Convertit l'historique Gradio en messages Ollama (10 derniers max)."""
    msgs = []
    for m in history[-10:]:
        if isinstance(m, dict):
            msgs.append({"role": m["role"], "content": m["content"]})
        elif isinstance(m, (list, tuple)) and len(m) == 2:
            if m[0]:
                msgs.append({"role": "user", "content": str(m[0])})
            if m[1]:
                msgs.append({"role": "assistant", "content": str(m[1])})
    return msgs


def _append_history(history: list, user_msg: str, assistant_msg: str) -> list:
    """Ajoute un échange à l'historique dans le bon format selon la version Gradio."""
    if _USE_DICT_FORMAT:
        return history + [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ]
    return history + [(user_msg, assistant_msg)]


# ══════════════════════════════════════════════════════════════════════════════
# GESTION DES SESSIONS
# ══════════════════════════════════════════════════════════════════════════════

def _session_path(sid: str) -> Path:
    return SESSIONS_DIR / f"{sid}.json"


def save_session(sid: str, name: str, history: list):
    _session_path(sid).write_text(
        json.dumps(
            {"id": sid, "name": name[:60], "updated_at": datetime.now().isoformat(), "history": history},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def load_session(sid: str) -> dict:
    p = _session_path(sid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"id": sid, "name": "Nouvelle", "history": []}


def list_sessions() -> list:
    """Liste les 20 sessions les plus récentes (format 'nom | id')."""
    sessions = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            sessions.append(f"{d.get('name', 'Sans titre')} | {d['id']}")
        except Exception:
            pass
    return sessions[:20]


def new_sid() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ══════════════════════════════════════════════════════════════════════════════
# ⚡ FAST CHAT LAYER  —  appel direct Ollama, aucun outil, aucun ReAct
# ══════════════════════════════════════════════════════════════════════════════

_FAST_SYSTEM = (
    "Tu es MasterAgent, un assistant IA personnel chaleureux, direct et efficace. "
    "Tu réponds en français, naturellement et de façon concise. "
    "Pour des tâches complexes (créer du code, gérer des fichiers, rechercher sur "
    "internet, orchestrer des agents), invite l'utilisateur à activer le "
    "Mode Agent Complet 🔥 ou à préfixer son message par 'Agent:'."
)


def fast_chat(message: str, history: list) -> str:
    """Appel direct LLM — sans pipeline ReAct, réponse rapide."""
    from llm.client import chat
    messages = [{"role": "system", "content": _FAST_SYSTEM}]
    messages.extend(_history_to_ollama(history))
    messages.append({"role": "user", "content": message})
    try:
        return chat(messages, temperature=0.7)
    except Exception as e:
        return f"❌ LLM indisponible: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# 🔥 FULL AGENT LAYER  —  ReAct complet, outils, mémoire, sous-agents
# ══════════════════════════════════════════════════════════════════════════════

def full_agent_chat(message: str, history: list, session_id: str) -> str:
    """Pipeline ReAct complète avec tous les outils et la mémoire ChromaDB."""
    from agent.core import run_agent
    from plugins import get_loader

    agent_config = {
        "id": session_id,
        "name": "MasterAgent-Gros",
        "system_prompt": (
            "Tu es MasterAgent-Gros, un agent IA maître ultra-puissant. "
            "Tu utilises tous les outils disponibles pour accomplir les tâches: "
            "créer des sous-agents, écrire du code, gérer des fichiers, "
            "orchestrer des workflows complexes. Tu réponds en français."
        ),
        "tools": list(get_loader().list_all().keys()),
        "model": config.OLLAMA_MODEL,
    }
    try:
        result = _run(run_agent(message, agent_config, session_id))
        answer = result.get("answer", "Pas de réponse.")
        used = {s["action"] for s in result.get("steps", []) if s.get("action")}
        if used:
            answer += f"\n\n*🔧 Outils utilisés : {', '.join(sorted(used))}*"
        return answer
    except Exception as e:
        return f"❌ Erreur agent: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# LOGIQUE CENTRALE
# ══════════════════════════════════════════════════════════════════════════════

def _should_use_agent(message: str, mode: str) -> tuple:
    """Retourne (use_agent: bool, message_nettoyé: str)."""
    if mode == "agent":
        return True, message
    low = message.lower().strip()
    for trigger in AGENT_TRIGGERS:
        if low.startswith(trigger):
            cleaned = message[len(trigger):].strip()
            return True, cleaned or message
    return False, message


def send_message(message: str, history: list, mode: str, sid: str):
    """Point d'entrée unique — route vers Fast ou Full Agent selon le mode."""
    if not message.strip():
        return history, history, ""

    use_agent, clean = _should_use_agent(message, mode)

    if use_agent:
        answer = full_agent_chat(clean, history, sid)
    else:
        answer = fast_chat(clean, history)

    new_history = _append_history(history, message, answer)

    # Sauvegarde : nom de session = premier message utilisateur
    existing = load_session(sid)
    name = existing.get("name", "Nouvelle") if existing.get("history") else message[:50]
    save_session(sid, name, new_history)

    return new_history, new_history, ""


def do_toggle_mode(mode: str):
    """Bascule Fast ↔ Agent et met à jour le bouton + indicateur."""
    new_mode = "agent" if mode == "fast" else "fast"
    if new_mode == "fast":
        label = "⚡ Mode Rapide — ACTIF"
        indicator = "🟢 **Mode Rapide ⚡** — Réponses directes en quelques secondes, sans outils"
    else:
        label = "🔥 Mode Agent Complet — ACTIF"
        indicator = "🔴 **Mode Agent Complet 🔥** — ReAct + outils + mémoire (30-60s par réponse)"
    return new_mode, gr.update(value=label), indicator


def do_new_conversation():
    return [], [], new_sid()


def do_load_session(choice: str):
    if choice and " | " in choice:
        sid = choice.split(" | ")[-1].strip()
        data = load_session(sid)
        h = data.get("history", [])
        return h, h, sid
    return [], [], new_sid()


def do_refresh_sessions():
    return gr.update(choices=list_sessions(), value=None)


# ══════════════════════════════════════════════════════════════════════════════
# CONSTRUCTION DE L'INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="MasterAgent-Gros") as demo:

        # ── États globaux ────────────────────────────────────────────────────
        mode_state = gr.State("fast")
        sid_state = gr.State(new_sid())

        # ── En-tête ──────────────────────────────────────────────────────────
        with gr.Row():
            gr.Markdown("# 🤖 MasterAgent-Gros")
            with gr.Column(scale=0, min_width=280):
                mode_btn = gr.Button("⚡ Mode Rapide — ACTIF", variant="secondary", size="sm")

        mode_indicator = gr.Markdown(
            "🟢 **Mode Rapide ⚡** — Réponses directes en quelques secondes, sans outils"
        )

        with gr.Tabs():

            # ══ CHAT ══════════════════════════════════════════════════════════
            with gr.TabItem("💬 Chat"):
                with gr.Row():

                    # Panneau gauche — historique des sessions
                    with gr.Column(scale=1, min_width=210):
                        gr.Markdown("### 📋 Historique")
                        new_btn = gr.Button("✨ Nouvelle conv.", variant="primary", size="sm")
                        sessions_dd = gr.Dropdown(
                            choices=list_sessions(),
                            label="Sessions passées",
                            value=None,
                            interactive=True,
                        )
                        refresh_sessions_btn = gr.Button("🔄 Rafraîchir", size="sm")

                    # Panneau droit — chat
                    with gr.Column(scale=4):
                        chatbot = gr.Chatbot(height=500, label="")
                        with gr.Row():
                            msg_in = gr.Textbox(
                                placeholder='Message… ou "Agent: fais-moi un script Python" pour le mode complet',
                                scale=5,
                                label="",
                                lines=1,
                            )
                            send_btn = gr.Button("Envoyer ▶", variant="primary", scale=1)

                chat_state = gr.State([])

                # Événements chat
                mode_btn.click(
                    do_toggle_mode,
                    inputs=[mode_state],
                    outputs=[mode_state, mode_btn, mode_indicator],
                )
                send_btn.click(
                    send_message,
                    inputs=[msg_in, chat_state, mode_state, sid_state],
                    outputs=[chatbot, chat_state, msg_in],
                )
                msg_in.submit(
                    send_message,
                    inputs=[msg_in, chat_state, mode_state, sid_state],
                    outputs=[chatbot, chat_state, msg_in],
                )
                new_btn.click(
                    do_new_conversation,
                    outputs=[chatbot, chat_state, sid_state],
                ).then(do_refresh_sessions, outputs=[sessions_dd])

                refresh_sessions_btn.click(do_refresh_sessions, outputs=[sessions_dd])
                sessions_dd.change(
                    do_load_session,
                    inputs=[sessions_dd],
                    outputs=[chatbot, chat_state, sid_state],
                )

            # ══ ORCHESTRATEUR ═════════════════════════════════════════════════
            with gr.TabItem("🧠 Orchestrateur"):
                gr.Markdown(
                    "Donne un objectif complexe — le Master Agent le décompose "
                    "et crée les sous-agents spécialisés nécessaires."
                )
                goal_in = gr.Textbox(
                    placeholder="Ex: Crée une vidéo virale sur le Bitcoin ET génère du code Python pour tracker son prix",
                    label="Objectif",
                    lines=3,
                )
                orch_btn = gr.Button("🚀 Lancer", variant="primary")
                orch_out = gr.Markdown()

                def run_master_ui(goal: str, progress=gr.Progress()):
                    from orchestrator import get_master
                    if not goal.strip():
                        return "Saisis un objectif."
                    progress(0.1, desc="Décomposition...")
                    result = _run(get_master().execute(goal))
                    progress(0.9, desc="Synthèse...")
                    out = f"## Résultat\n\n**Mode:** {result.get('mode', '?')}\n"
                    for a in result.get("agents", []):
                        out += f"  - `{a['role']}`: {a['objective']}\n"
                    return out + f"\n---\n\n{result.get('answer', '')}"

                orch_btn.click(run_master_ui, [goal_in], [orch_out])

            # ══ VIDÉO ═════════════════════════════════════════════════════════
            with gr.TabItem("🎬 Vidéo"):
                with gr.Row():
                    # ── Panneau gauche : contrôles ───────────────────────────
                    with gr.Column(scale=1):
                        vtopic = gr.Textbox(
                            label="💡 Sujet de la vidéo",
                            placeholder="Ex: Les 5 erreurs des débutants en crypto",
                            lines=2,
                        )
                        vstyle = gr.Dropdown(
                            ["éducatif", "motivation", "humour", "tutoriel", "choc"],
                            value="éducatif", label="🎭 Style",
                        )
                        with gr.Row():
                            vlang = gr.Dropdown(["fr", "en", "es"], value="fr", label="🌍 Langue")
                            vtheme = gr.Dropdown(["dark", "fire", "ocean", "gold"], value="dark", label="🎨 Thème")
                        vslides = gr.Slider(3, 10, value=5, step=1, label="📊 Nombre de slides")
                        vaudio = gr.Checkbox(value=False, label="🔊 Voix (TTS) — désactiver si lent")
                        vbtn = gr.Button("🎬 Générer la vidéo", variant="primary", size="lg")
                        vstatus = gr.Markdown("")

                    # ── Panneau droit : résultats ────────────────────────────
                    with gr.Column(scale=2):
                        with gr.Tabs():
                            with gr.TabItem("🖼️ Slides (aperçu)"):
                                vgallery = gr.Gallery(
                                    label="",
                                    columns=3,
                                    height=420,
                                    object_fit="contain",
                                )
                            with gr.TabItem("🎞️ Vidéo / GIF"):
                                vpreview = gr.Image(
                                    label="Aperçu animé (GIF) — clic droit pour télécharger",
                                    height=420,
                                )
                            with gr.TabItem("📝 Script"):
                                sout = gr.Textbox(label="", lines=15, show_copy_button=True)

                def make_video_ui(topic, style, lang, slides_n, theme, add_audio, progress=gr.Progress()):
                    import httpx, tempfile
                    from pathlib import Path

                    if not topic.strip():
                        return [], None, "⚠️ Saisis un sujet.", "Écris un sujet d'abord."

                    # 1. Génère le script via l'API
                    progress(0.15, desc="✍️ Génération du script...")
                    async def _get_script():
                        async with httpx.AsyncClient(timeout=60.0) as c:
                            r = await c.post(
                                f"http://localhost:{config.PORT}/video/script",
                                json={"topic": topic, "style": style, "lang": lang, "slides_count": int(slides_n)},
                            )
                            return r.json()
                    try:
                        data = _run(_get_script())
                        slides = data.get("slides", [topic])
                    except Exception as e:
                        return [], None, f"❌ Erreur script: {e}", ""

                    script_text = "\n\n---\n\n".join(slides)

                    # 2. Génère les images des slides
                    progress(0.4, desc="🖼️ Création des slides...")
                    from video.image_gen import create_slide, save_slide
                    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in topic)[:35]
                    tmp_dir = Path("output/tmp_preview") / safe_name
                    tmp_dir.mkdir(parents=True, exist_ok=True)

                    slide_paths = []
                    for i, text in enumerate(slides):
                        path = str(tmp_dir / f"slide_{i:03d}.png")
                        save_slide(text, path, index=i, total=len(slides), theme=theme)
                        slide_paths.append(path)
                        progress(0.4 + 0.3 * (i + 1) / len(slides), desc=f"🖼️ Slide {i+1}/{len(slides)}...")

                    # 3. Crée le GIF animé (toujours, indépendant de FFmpeg)
                    progress(0.75, desc="🎞️ Assemblage GIF...")
                    gif_path = None
                    try:
                        from PIL import Image as PILImage
                        frames = [PILImage.open(p) for p in slide_paths]
                        gif_path = str(Path("output/videos") / f"{safe_name}.gif")
                        Path("output/videos").mkdir(parents=True, exist_ok=True)
                        frames[0].save(
                            gif_path, save_all=True, append_images=frames[1:],
                            duration=2500, loop=0, optimize=True,
                        )
                    except Exception as e:
                        pass

                    # 4. Essaie aussi MP4 en arrière-plan (sans bloquer)
                    progress(0.9, desc="🎬 Finalisation...")
                    if add_audio:
                        try:
                            from video.creator import create_video
                            create_video(slides, safe_name, lang=lang, add_audio=True, theme=theme)
                        except Exception:
                            pass

                    progress(1.0, desc="✅ Terminé!")
                    status = f"✅ **{len(slides)} slides générées** pour: _{topic}_"
                    if gif_path:
                        status += f"\n\n📥 GIF prêt — clique sur l'onglet **Vidéo / GIF** pour le voir et le télécharger."

                    return slide_paths, gif_path, status, script_text

                vbtn.click(
                    make_video_ui,
                    [vtopic, vstyle, vlang, vslides, vtheme, vaudio],
                    [vgallery, vpreview, vstatus, sout],
                )

            # ══ CODE ══════════════════════════════════════════════════════════
            with gr.TabItem("💻 Code"):
                with gr.Row():
                    with gr.Column():
                        cdesc = gr.Textbox(label="Description", lines=3, placeholder="Ex: Script Python pour télécharger des vidéos YouTube")
                        clang = gr.Dropdown(["python", "javascript", "bash", "sql"], value="python", label="Langage")
                        crun = gr.Checkbox(value=False, label="Exécuter (Python uniquement)")
                        cbtn = gr.Button("⚙️ Générer", variant="primary")
                    with gr.Column():
                        cout = gr.Code(label="Code généré", language="python")
                        eout = gr.Textbox(label="Sortie d'exécution", lines=5)

                def gen_code_ui(desc, lang, run_it):
                    import httpx
                    async def _g():
                        async with httpx.AsyncClient(timeout=120.0) as c:
                            ep = "/code/generate-and-run" if run_it and lang == "python" else "/code/generate"
                            r = await c.post(f"http://localhost:{config.PORT}{ep}", json={"description": desc, "language": lang})
                            return r.json()
                    d = _run(_g())
                    return d.get("code", str(d)), d.get("output", "(non exécuté)")

                cbtn.click(gen_code_ui, [cdesc, clang, crun], [cout, eout])

            # ══ AGENTS ════════════════════════════════════════════════════════
            with gr.TabItem("🤖 Agents"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Créer un sous-agent")
                        arole = gr.Dropdown(
                            ["researcher", "coder", "video_creator", "analyst", "writer", "generic"],
                            value="coder", label="Rôle",
                        )
                        aobj = gr.Textbox(label="Objectif", placeholder="Ex: Spécialiste APIs REST en Python")
                        amodel = gr.Textbox(label="Modèle (vide = défaut)", placeholder=config.OLLAMA_MODEL)
                        abtn = gr.Button("➕ Créer", variant="primary")
                        acfg = gr.Code(label="Config JSON", language="json")

                        def mk_agent(role, obj, model):
                            from orchestrator.factory import generate_agent_config
                            from orchestrator import get_registry
                            cfg = _run(generate_agent_config(role, obj, model or config.OLLAMA_MODEL))
                            get_registry().register(cfg)
                            return json.dumps(cfg, indent=2, ensure_ascii=False)

                        abtn.click(mk_agent, [arole, aobj, amodel], [acfg])

                    with gr.Column():
                        gr.Markdown("### Agents existants")
                        arbtn = gr.Button("🔄 Rafraîchir")
                        atable = gr.Dataframe(headers=["ID", "Nom", "Rôle", "Statut", "Dernière activité"])

                        def ls_agents():
                            from orchestrator import get_registry
                            return [
                                [a["id"], a.get("name", ""), a.get("role", ""), a.get("status", ""), a.get("last_active", "")]
                                for a in get_registry().list_all()
                            ]

                        arbtn.click(ls_agents, [], [atable])

            # ══ PLUGINS ═══════════════════════════════════════════════════════
            with gr.TabItem("🔌 Plugins"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Plugins installés")
                        ptbl = gr.Dataframe(headers=["Nom", "Description"])
                        prbtn = gr.Button("🔄 Rafraîchir")

                        def ls_plugins():
                            from plugins import get_loader
                            return [[n, d] for n, d in get_loader().list_all().items()]

                        prbtn.click(ls_plugins, [], [ptbl])

                    with gr.Column():
                        gr.Markdown("### Ajouter un plugin")
                        pcode = gr.Code(
                            label="Code Python",
                            language="python",
                            value=(
                                "from plugins.base import Plugin\n\n"
                                "class MonPlugin(Plugin):\n"
                                "    name = \"mon_outil\"\n"
                                "    description = \"Ce que fait l'outil\"\n"
                                "    parameters = {\"input\": {\"type\": \"string\", \"required\": True}}\n\n"
                                "    def run(self, input: str) -> str:\n"
                                "        return f\"Résultat: {input}\"\n"
                            ),
                        )
                        pabtn = gr.Button("⬆️ Charger", variant="primary")
                        pstatus = gr.Textbox(label="Statut")

                        def add_plugin(code):
                            from plugins import get_loader
                            Path(config.PLUGINS_DIR).mkdir(exist_ok=True)
                            (Path(config.PLUGINS_DIR) / "user_plugin.py").write_text(code, encoding="utf-8")
                            get_loader().reload_user_plugins()
                            return f"✅ Chargé. Outils disponibles : {list(get_loader().list_all().keys())}"

                        pabtn.click(add_plugin, [pcode], [pstatus])

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
