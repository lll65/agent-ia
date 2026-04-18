"""
Interface Web Gradio — accès visuel à tout ton agent IA depuis le navigateur.
Lance avec: python main.py (intégré)
"""
import asyncio
import json
import gradio as gr

from config import config


# ── helpers async ──────────────────────────────────────────────────────────────

def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── onglet Chat ────────────────────────────────────────────────────────────────

def chat_with_agent(message: str, history: list, agent_id: str, show_steps: bool):
    from agent.core import run_agent
    from plugins import get_loader
    from orchestrator import get_registry

    registry = get_registry()
    agent = registry.get(agent_id) if agent_id != "default" else None
    if agent is None:
        agent = {
            "id": "default",
            "name": "Agent Principal",
            "system_prompt": "Tu es un assistant IA polyvalent.",
            "tools": list(get_loader().list_all().keys()),
            "model": config.OLLAMA_MODEL,
        }

    result = _run(run_agent(message, agent, agent_id))
    answer = result.get("answer", "Pas de réponse.")

    if show_steps and result.get("steps"):
        steps_text = "\n\n".join(
            f"**Étape {s['iteration']}**\n"
            + (f"→ Action: `{s.get('action', '')}` | Params: `{s.get('params', {})}`\n" if s.get("action") else "")
            + (f"→ Observation: {s.get('observation', '')[:300]}\n" if s.get("observation") else "")
            for s in result["steps"] if s.get("action")
        )
        if steps_text:
            answer = f"{answer}\n\n---\n**Détail des actions ({result['iterations']} itérations):**\n{steps_text}"

    history.append((message, answer))
    return history, history, ""


# ── onglet Orchestrateur ───────────────────────────────────────────────────────

def run_master(goal: str, progress=gr.Progress()):
    from orchestrator import get_master
    if not goal.strip():
        return "Saisis un objectif."
    progress(0.1, desc="Décomposition du goal...")
    result = _run(get_master().execute(goal))
    progress(0.9, desc="Synthèse...")
    output = f"## Résultat pour: {goal}\n\n"
    output += f"**Mode:** {result.get('mode', 'unknown')}\n"
    if result.get("agents"):
        output += f"**Agents créés:** {len(result['agents'])}\n"
        for a in result["agents"]:
            output += f"  - `{a['role']}`: {a['objective']}\n"
    output += f"\n---\n\n{result.get('answer', '')}"
    return output


# ── onglet Vidéo ───────────────────────────────────────────────────────────────

def create_video_ui(topic: str, style: str, lang: str, slides_n: int, theme: str, add_audio: bool, progress=gr.Progress()):
    import httpx

    async def _gen():
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://localhost:{config.PORT}/video/script",
                json={"topic": topic, "style": style, "lang": lang, "slides_count": int(slides_n)},
            )
            return resp.json()

    progress(0.2, desc="Génération du script...")
    data = _run(_gen())
    slides = data.get("slides", [topic])

    progress(0.4, desc="Création des images...")
    from video.creator import create_video
    try:
        path = create_video(slides, topic[:30].replace(" ", "_"), lang=lang, add_audio=add_audio, theme=theme)
        progress(1.0, desc="Terminé!")
        script = "\n\n---\n\n".join(slides)
        return path, script
    except Exception as e:
        return None, f"Erreur: {e}"


# ── onglet Code ────────────────────────────────────────────────────────────────

def generate_code_ui(description: str, language: str, run_code: bool):
    import httpx

    async def _gen():
        async with httpx.AsyncClient(timeout=90.0) as c:
            if run_code and language == "python":
                r = await c.post(
                    f"http://localhost:{config.PORT}/code/generate-and-run",
                    json={"description": description, "language": language},
                )
            else:
                r = await c.post(
                    f"http://localhost:{config.PORT}/code/generate",
                    json={"description": description, "language": language},
                )
            return r.json()

    data = _run(_gen())
    code = data.get("code", "Erreur: " + str(data))
    output = data.get("output", "")
    return code, output or "(code non exécuté)"


# ── onglet Plugins ─────────────────────────────────────────────────────────────

def list_plugins_ui():
    from plugins import get_loader
    plugins = get_loader().list_all()
    return [[name, desc] for name, desc in plugins.items()]


def add_plugin_ui(code: str):
    from pathlib import Path
    path = Path(config.PLUGINS_DIR) / "user_plugin.py"
    path.write_text(code, encoding="utf-8")
    from plugins import get_loader
    get_loader().reload_user_plugins()
    return f"Plugin chargé. Disponibles: {list(get_loader().list_all().keys())}"


# ── onglet Agents ──────────────────────────────────────────────────────────────

def list_agents_ui():
    from orchestrator import get_registry
    agents = get_registry().list_all()
    return [[a["id"], a.get("name", ""), a.get("role", ""), a.get("status", ""), a.get("last_active", "")] for a in agents]


def create_agent_ui(role: str, objective: str, model: str):
    from orchestrator.factory import generate_agent_config
    from orchestrator import get_registry
    cfg = _run(generate_agent_config(role, objective, model or config.OLLAMA_MODEL))
    get_registry().register(cfg)
    return json.dumps(cfg, indent=2, ensure_ascii=False)


# ── Construction de l'UI ───────────────────────────────────────────────────────

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Agent IA Personnel") as demo:
        gr.Markdown("# Agent IA Personnel\n*100% local — 0€/mois*")

        with gr.Tabs():

            # ── CHAT ──────────────────────────────────────────────────────────
            with gr.TabItem("Chat"):
                # type="messages" = format Gradio 6+
                chatbot = gr.Chatbot(height=500, label="Conversation")
                with gr.Row():
                    msg_input = gr.Textbox(placeholder="Ton message...", scale=5, label="")
                    send_btn = gr.Button("Envoyer", variant="primary", scale=1)
                with gr.Row():
                    agent_dropdown = gr.Dropdown(choices=["default"], value="default", label="Agent", scale=3)
                    show_steps = gr.Checkbox(label="Voir les étapes", value=False, scale=1)
                    clear_btn = gr.Button("Effacer", scale=1)

                state = gr.State([])
                send_btn.click(chat_with_agent, [msg_input, state, agent_dropdown, show_steps], [chatbot, state, msg_input])
                msg_input.submit(chat_with_agent, [msg_input, state, agent_dropdown, show_steps], [chatbot, state, msg_input])
                clear_btn.click(lambda: ([], []), None, [chatbot, state])

            # ── ORCHESTRATEUR ─────────────────────────────────────────────────
            with gr.TabItem("Orchestrateur"):
                gr.Markdown("Donne un objectif complexe — le Master Agent le décompose et crée les sous-agents.")
                goal_input = gr.Textbox(
                    placeholder='Ex: Crée une vidéo virale sur le Bitcoin ET génère le code Python pour tracker son prix',
                    label="Objectif", lines=3
                )
                orchestrate_btn = gr.Button("Lancer", variant="primary")
                orch_output = gr.Markdown()
                orchestrate_btn.click(run_master, [goal_input], [orch_output])

            # ── VIDÉO ─────────────────────────────────────────────────────────
            with gr.TabItem("Créateur Vidéo"):
                with gr.Row():
                    with gr.Column():
                        topic_in = gr.Textbox(label="Sujet", placeholder="Ex: Les erreurs des débutants en crypto")
                        style_in = gr.Dropdown(["éducatif", "motivation", "humour", "tutoriel", "choc"], value="éducatif", label="Style")
                        with gr.Row():
                            lang_in = gr.Dropdown(["fr", "en", "es"], value="fr", label="Langue")
                            theme_in = gr.Dropdown(["dark", "fire", "ocean", "gold"], value="dark", label="Thème")
                        slides_in = gr.Slider(3, 10, value=5, step=1, label="Nombre de slides")
                        audio_in = gr.Checkbox(value=True, label="Voix (TTS)")
                        video_btn = gr.Button("Créer la vidéo", variant="primary")
                    with gr.Column():
                        video_out = gr.Video(label="Vidéo générée")
                        script_out = gr.Textbox(label="Script", lines=10)
                video_btn.click(create_video_ui, [topic_in, style_in, lang_in, slides_in, theme_in, audio_in], [video_out, script_out])

            # ── CODE ──────────────────────────────────────────────────────────
            with gr.TabItem("Générateur de Code"):
                with gr.Row():
                    with gr.Column():
                        code_desc = gr.Textbox(label="Description", placeholder="Ex: Script Python pour télécharger des vidéos YouTube", lines=3)
                        code_lang = gr.Dropdown(["python", "javascript", "bash", "sql"], value="python", label="Langage")
                        run_checkbox = gr.Checkbox(value=False, label="Exécuter (Python uniquement)")
                        code_btn = gr.Button("Générer", variant="primary")
                    with gr.Column():
                        code_out = gr.Code(label="Code généré", language="python")
                        exec_out = gr.Textbox(label="Sortie", lines=5)
                code_btn.click(generate_code_ui, [code_desc, code_lang, run_checkbox], [code_out, exec_out])

            # ── AGENTS ────────────────────────────────────────────────────────
            with gr.TabItem("Mes Agents"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Créer un sous-agent")
                        role_in = gr.Dropdown(["researcher", "coder", "video_creator", "analyst", "writer", "generic"], value="coder", label="Rôle")
                        obj_in = gr.Textbox(label="Objectif", placeholder="Ex: Spécialiste APIs REST en Python")
                        model_in = gr.Textbox(label="Modèle (vide = défaut)", placeholder=config.OLLAMA_MODEL)
                        create_agent_btn = gr.Button("Créer", variant="primary")
                        agent_cfg_out = gr.Code(label="Config générée", language="json")
                        create_agent_btn.click(create_agent_ui, [role_in, obj_in, model_in], [agent_cfg_out])
                    with gr.Column():
                        gr.Markdown("### Agents existants")
                        refresh_btn = gr.Button("Rafraîchir")
                        agents_table = gr.Dataframe(headers=["ID", "Nom", "Rôle", "Statut", "Dernière activité"], label="")
                        refresh_btn.click(list_agents_ui, None, [agents_table])

            # ── PLUGINS ───────────────────────────────────────────────────────
            with gr.TabItem("Plugins"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Plugins installés")
                        plugins_table = gr.Dataframe(headers=["Nom", "Description"], label="")
                        refresh_plugins_btn = gr.Button("Rafraîchir")
                        refresh_plugins_btn.click(list_plugins_ui, None, [plugins_table])
                    with gr.Column():
                        gr.Markdown("### Ajouter un plugin\nColle ton code Python ici:")
                        plugin_code = gr.Code(
                            label="Code du plugin",
                            language="python",
                            value='from plugins.base import Plugin\n\nclass MonPlugin(Plugin):\n    name = "mon_outil"\n    description = "Ce que fait l\'outil"\n    parameters = {"input": {"type": "string", "required": True}}\n\n    def run(self, input: str) -> str:\n        return f"Résultat: {input}"\n',
                        )
                        add_plugin_btn = gr.Button("Charger le plugin", variant="primary")
                        plugin_status = gr.Textbox(label="Statut")
                        add_plugin_btn.click(add_plugin_ui, [plugin_code], [plugin_status])

    return demo


def launch(share: bool = False, port: int = None):
    demo = build_ui()
    demo.launch(
        server_port=port or config.GRADIO_PORT,
        share=share or config.GRADIO_SHARE,
        server_name="0.0.0.0",
        show_error=True,
    )


if __name__ == "__main__":
    launch()
