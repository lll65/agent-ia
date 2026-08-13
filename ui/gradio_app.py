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
_FACTUAL_HINTS = (
    "actualité", "actualités", "news", "2024", "2025", "2026", "tendance", "tendances",
    "aujourd'hui", "en ce moment", "récent", "récente", "dernier", "dernière", "derniers",
    "meilleur", "meilleure", "top ", "idées de", "idée de", "combien", "qui est", "quand",
    "cette année", "en france", "dans le monde", "statistiques", "chiffres", "marché de",
    "business", "startup", "prix de", "cours de", "événement", "sortie de",
)


def _is_factual_question(msg: str) -> bool:
    """Question qui nécessite des données actuelles/web (→ forcer search_web)."""
    low = (msg or "").lower()
    return any(h in low for h in _FACTUAL_HINTS)


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
        "Tu es MasterAgent-Gros, assistant IA polyvalent. Réponses directes en français, "
        "format ADAPTÉ à la question. En Mode Rapide tu n'as aucun outil : n'invente jamais "
        "de prix/source/date ; si une donnée réelle manque, dis « ⚠️ estimation non vérifiée » "
        "et propose le Mode Agent."
    )

def fast_chat(message: str, history: list, sid: str = "default") -> str:
    from llm.client import chat
    msgs = [{"role": "system", "content": _FAST_SYS}]
    # Injection mémoire (ChromaDB/Supabase) → personnalisation selon le profil utilisateur
    try:
        from memory import get_memory
        mem = get_memory()
        ctx = mem.build_context(sid, message, recent_limit=6)
        if ctx:
            msgs.append({"role": "system",
                         "content": f"[MÉMOIRE — profil & échanges passés pertinents]\n{ctx}"})
    except Exception:
        mem = None
    msgs.extend(_to_ollama(history))
    msgs.append({"role": "user", "content": message})
    try:
        answer = chat(msgs, temperature=0.7)
    except Exception as e:
        return f"❌ LLM indisponible: {e}"
    # Mémorise l'échange pour les prochaines conversations
    try:
        if mem:
            mem.remember(sid, "user", message)
            mem.remember(sid, "assistant", answer)
    except Exception:
        pass
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
    tools = list(get_loader().list_all().keys())
    factual = _is_factual_question(message)
    # Question factuelle → search_web en 1er outil + forçage déterministe (core.py
    # exécute une vraie recherche web AVANT le 1er appel LLM si force_search=True).
    if factual and "search_web" in tools:
        tools.remove("search_web")
        tools.insert(0, "search_web")
    cfg = {
        "id": sid, "name": "MasterAgent-Gros v4",
        "force_search": factual,
        "system_prompt": (
            "Tu es MasterAgent-Gros v4, un agent IA surpuissant et auto-évolutif. "
            "Tu maîtrises tous les domaines : code full-stack, finance quantitative, "
            "création de contenu, data science, stratégie business. "
            "Pour toute question factuelle/actuelle, ta PREMIÈRE action est search_web — "
            "jamais de données inventées, jamais de source citée sans appel d'outil réel. "
            "Tu réponds en français, format adapté à la question (pas de format trading hors bourse). "
            "Tu ne dis jamais 'je ne peux pas' — tu trouves toujours une solution."
        ),
        "tools": tools,
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

def _artifact_out(text: str):
    """Retourne (contenu_code, visibilité_du_volet). Le volet Artefact ne s'affiche
    QUE si la réponse contient un bloc de code (sinon masqué → chat plein cadre)."""
    import re
    blocks = re.findall(r"```[a-zA-Z0-9]*\n(.*?)```", text or "", re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip(), gr.update(visible=True)
    return gr.update(), gr.update(visible=False)


def _usage_md() -> str:
    """Barre de consommation Groq du jour."""
    try:
        from llm.usage import get_usage
        used, limit = get_usage()
        pct = min(100, int(used / limit * 100)) if limit else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        color = "🟢" if pct < 70 else "🟠" if pct < 90 else "🔴"
        return f"{color} **Groq aujourd'hui** `{bar}` {used:,}/{limit:,} tokens ({pct}%)".replace(",", " ")
    except Exception:
        return ""


_CODE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h",
              ".go", ".rs", ".rb", ".php", ".html", ".css", ".sql", ".sh", ".vue"}


def send(message: str, history: list, mode: str, sid: str, image=None, file=None):
    # ── Fichier déposé (code ou document) → analyse dans le chat ───────────
    if file:
        from pathlib import Path as _P
        fp = _P(str(file))
        q = (message or "").strip()
        try:
            from plugins.builtin.document_analyzer import DocumentAnalyzerPlugin
            if not q:
                q = ("Explique ce code et repère les éventuels bugs."
                     if fp.suffix.lower() in _CODE_EXTS
                     else "Résume et analyse ce document.")
            answer = DocumentAnalyzerPlugin().run(path=str(fp), question=q)
        except Exception as e:
            answer = f"❌ Analyse du fichier impossible : {str(e)[:200]}"
        new_h = _add(history, f"📎 *({fp.name})* {message}".strip(), answer)
        _save(sid, (_load(sid).get("name") or fp.name), new_h)
        return new_h, new_h, "", None, None, *_artifact_out(answer), _usage_md()

    # ── Image jointe → analyse visuelle ────────────────────────────────────
    if image:
        prompt = (message or "").strip() or "Décris et analyse cette image en détail."
        try:
            from llm.client import chat_vision
            answer = chat_vision(image, prompt)
        except Exception as e:
            answer = f"❌ Analyse d'image impossible : {str(e)[:200]}"
        shown = f"🖼️ *(image)* {message}".strip() if message.strip() else "🖼️ *(image jointe)*"
        new_h = _add(history, shown, answer)
        _save(sid, (_load(sid).get("name") or "Image"), new_h)
        return new_h, new_h, "", None, None, *_artifact_out(answer), _usage_md()

    if not message.strip():
        return history, history, "", None, None, *_artifact_out(""), _usage_md()
    use_agent, _use_finance, clean = _route(message, mode)
    if use_agent:
        answer = full_agent(clean, history, sid)
    elif _is_finance_question(clean):
        answer = finance_agent_analysis(clean)
    elif _is_factual_question(clean):
        # BASCULE AUTOMATIQUE en Mode Agent : la question implique des faits/chiffres/
        # tendances/marché → l'utilisateur ne doit pas deviner qu'il faut changer de mode.
        # full_agent force une vraie recherche web (search_web) avant de répondre.
        answer = full_agent(clean, history, sid)
        answer = ("> 🔎 *Basculé en Mode Agent (recherche web) — question factuelle.*\n\n" + answer)
    else:
        # Mode Rapide réservé au non-factuel (conversationnel, créatif, reformulation…)
        answer = fast_chat(clean, history, sid)
    new_h = _add(history, message, answer)
    existing = _load(sid)
    name = existing.get("name", "Nouvelle") if existing.get("history") else message[:50]
    _save(sid, name, new_h)
    return new_h, new_h, "", None, None, *_artifact_out(answer), _usage_md()

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

    # Nettoie le sujet : retire les URLs collées et coupe à une phrase courte
    clean_topic = re.sub(r"https?://\S+", "", topic).strip()
    clean_topic = re.sub(r"\s+", " ", clean_topic)[:120] or "Ma vidéo"

    progress(0.05, desc="✍️ Génération du script...")
    slides = None
    try:
        raw = chat([
            {"role": "system", "content": (
                "Tu es scénariste vidéo. Tu génères le CONTENU des slides (pas la consigne). "
                "Chaque slide = une phrase courte, percutante, max 12 mots. Réponds UNIQUEMENT en JSON.")},
            {"role": "user", "content": (
                f'Sujet : "{clean_topic}". Style : {style}. Langue : {lang}.\n'
                f'Génère {n_slides} slides (accroche → développement → conclusion/CTA). '
                f'NE recopie PAS le sujet tel quel — crée du vrai contenu.\n'
                f'JSON strict : {{"slides": ["accroche", "point 1", "point 2", "...", "conclusion"]}}'
            )},
        ], temperature=0.8)
        m = re.search(r'\{[\s\S]+\}', raw)
        if m:
            parsed = json.loads(m.group()).get("slides")
            if isinstance(parsed, list) and parsed:
                slides = [str(s)[:140] for s in parsed if str(s).strip()]
    except Exception:
        slides = None

    # Repli propre (jamais l'URL / le texte brut) si l'IA n'a pas répondu
    if not slides:
        title = clean_topic[:60]
        slides = [title] + [f"Point clé {i+1}" for i in range(max(1, n_slides - 2))] + ["Merci d'avoir regardé !"]

    topic = clean_topic  # pour le nom de fichier et le statut

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


def debug_project_fn(path: str, do_fix: bool, uploads=None, progress=gr.Progress()):
    """Analyse un projet : dossier (chemin) OU fichiers uploadés."""
    target = (path or "").strip()
    # Si des fichiers sont uploadés → on les copie dans un dossier temporaire
    if uploads:
        import shutil, tempfile
        from pathlib import Path as _P
        files = uploads if isinstance(uploads, list) else [uploads]
        tmp = _P(tempfile.mkdtemp(prefix="debug_"))
        for f in files:
            try:
                shutil.copy(str(f), tmp / _P(str(f)).name)
            except Exception:
                pass
        target = str(tmp)
    if not target:
        return "⚠️ Charge des fichiers (bouton) ou indique le chemin d'un dossier de projet."
    progress(0.3, desc="🧠 Analyse des bugs par l'IA (peut prendre ~1 min)...")
    try:
        from plugins.builtin.project_debugger import ProjectDebuggerPlugin
        result = ProjectDebuggerPlugin().run(path=target, fix=bool(do_fix))
        progress(1.0, desc="✅ Terminé")
        return result
    except Exception as e:
        return f"❌ Erreur: {e}"


def analyze_document_fn(path: str, question: str, upload=None, progress=gr.Progress()):
    """Lit un PDF/document/dossier et répond à une question ou en fait un résumé."""
    target = upload or (path or "").strip()   # le fichier uploadé a priorité
    if not target:
        return "⚠️ Charge un fichier (bouton) ou indique un chemin/dossier."
    progress(0.2, desc="📄 Lecture du document...")
    try:
        from plugins.builtin.document_analyzer import DocumentAnalyzerPlugin
        progress(0.5, desc="🧠 Analyse par l'IA...")
        result = DocumentAnalyzerPlugin().run(path=str(target), question=(question or "").strip())
        progress(1.0, desc="✅ Terminé")
        return result
    except Exception as e:
        return f"❌ Erreur: {e}"


def analyze_health_fn(path: str, question: str, upload=None, progress=gr.Progress()):
    """Analyse des données santé/sport : fichier uploadé, chemin, OU URL Vita."""
    target = upload or (path or "").strip()   # fichier uploadé prioritaire, sinon chemin/URL
    if not target:
        return "⚠️ Charge un fichier, colle un chemin, ou mets l'URL de ton endpoint Vita."
    progress(0.4, desc="🩺 Analyse de tes données santé...")
    try:
        from plugins.builtin.health_analyzer import HealthAnalyzerPlugin
        return HealthAnalyzerPlugin().run(path=str(target), question=(question or "").strip())
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
        # Si yfinance manque OU RSI non calculé → on recalcule via HTTP (RSI/MACD/SMA)
        if analysis.startswith("❌") or "RSI non calculé" in analysis or "yfinance" in analysis:
            direct = _fetch_yahoo_direct(ticker.strip(), period)
            if direct:
                analysis = f"*⚠️ Données via Yahoo Finance HTTP (yfinance absent)*\n\n{direct}"
        # Ajoute un résumé d'actualité (l'onglet Actualités est fusionné ici)
        try:
            news_block = finance_news(ticker.strip())
            if news_block and not news_block.startswith("⚠️") and not news_block.startswith("❌"):
                analysis += "\n\n---\n\n" + news_block
        except Exception:
            pass
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
        news = MarketNewsPlugin().run(ticker=ticker.strip())
    except Exception as e:
        return f"❌ Erreur: {e}"
    if not news or news.startswith("❌") or "Aucune" in news[:30]:
        return news
    # Résumé IA de l'actualité (le vrai plus)
    try:
        from llm.client import chat
        summary = chat([
            {"role": "system", "content": (
                "Tu es analyste financier. Résume l'actualité d'une valeur en 4-5 puces "
                "claires, puis donne le ton général (🟢 positif / 🔴 négatif / 🟠 mitigé). Français, concis.")},
            {"role": "user", "content": f"Actualités de {ticker.upper()} :\n{news[:3000]}\n\nRésume et donne le sentiment."},
        ], temperature=0.3)
        return f"## 📰 Résumé — {ticker.upper()}\n\n{summary}\n\n---\n\n### 🔗 Sources\n{news}"
    except Exception:
        return news

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
        # Indicateurs techniques calculés localement (RSI, MACD, SMA, Bollinger)
        try:
            from agent.finance_deep import _compute_levels
            lvl = _compute_levels(closes)
            if lvl:
                tendance = "haussière 🟢" if price > lvl.get("sma20", price) else "baissière 🔴"
                lines.append("")
                lines.append("— Indicateurs techniques —")
                lines.append(f"RSI(14): {lvl.get('rsi','N/D')}  ("
                             + ("survendu, achat" if lvl.get('rsi',50) <= 32 else
                                "suracheté, prudence" if lvl.get('rsi',50) >= 70 else "neutre") + ")")
                if lvl.get("macd") is not None:
                    macd_sig = "haussier" if lvl["macd"] > (lvl.get("macd_signal") or 0) else "baissier"
                    lines.append(f"MACD: {lvl['macd']} / signal {lvl.get('macd_signal')} → {macd_sig}")
                lines.append(f"SMA20: {lvl.get('sma20','N/D')} · SMA50: {lvl.get('sma50','N/D')} → tendance {tendance}")
                lines.append(f"Bollinger: position {lvl.get('bb_pct','N/D')}% de la bande")
                if lvl.get("entry_low"):
                    lines.append(f"Zone d'entrée: {lvl['entry_low']}-{lvl['entry_high']} · "
                                 f"TP1 {lvl['tp1']} · SL {lvl['sl']} · R/R {lvl['rr']}:1")
        except Exception:
            pass
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

# ─── Identité visuelle : thème + CSS injecté (une seule source de vérité) ───────

_THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.indigo,
    secondary_hue=gr.themes.colors.violet,
    neutral_hue=gr.themes.colors.slate,
    font=[gr.themes.GoogleFont("Inter"), "system-ui", "-apple-system", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
).set(
    block_radius="14px",
    block_border_width="1px",
    block_shadow="0 1px 2px rgba(15,23,42,0.06)",
    button_large_radius="10px",
    button_small_radius="8px",
    input_radius="10px",
)

_CSS = """
:root { --brand:#4f46e5; --brand2:#7c3aed; }
.gradio-container { max-width: 1360px !important; margin: 0 auto !important; }
footer { display:none !important; }

/* En-tête / identité */
#app-header {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding:14px 22px; margin:2px 0 10px; border-radius:16px; color:#fff;
  background:linear-gradient(135deg,var(--brand),var(--brand2));
  box-shadow:0 6px 20px rgba(79,70,229,.25);
}
#app-header .brand { display:flex; align-items:center; gap:13px; }
#app-header .logo { font-size:30px; line-height:1; }
#app-header .brand-name { font-weight:800; font-size:20px; letter-spacing:-.02em; }
#app-header .brand-tag { font-size:12.5px; opacity:.9; margin-top:1px; }
#app-header .pill { font-size:12px; background:rgba(255,255,255,.18); padding:5px 11px;
  border-radius:999px; white-space:nowrap; }

/* Onglets : lisibles et scrollables horizontalement sur mobile */
.tab-nav { overflow-x:auto !important; flex-wrap:nowrap !important; scrollbar-width:thin; }
.tab-nav button { white-space:nowrap; font-weight:600; }

/* Chat : distinction nette utilisateur / agent */
.message-row.user-row .message, .message.user, .bubble.user {
  background:var(--brand) !important; color:#fff !important; border:none !important;
  border-radius:14px 14px 4px 14px !important;
}
.message-row.bot-row .message, .message.bot, .bubble.bot {
  background:rgba(148,163,184,.14) !important; border:1px solid rgba(148,163,184,.22) !important;
  border-radius:14px 14px 14px 4px !important;
}
/* Blocs de code / résultats d'outils : lisibles + scroll horizontal (mobile) */
.message pre, .prose pre, .md pre, .gr-prose pre {
  background:#0f172a !important; color:#e2e8f0 !important; border-radius:10px;
  padding:12px 14px !important; overflow-x:auto; font-size:12.5px; line-height:1.5;
}
.message code, .prose code { font-family:'JetBrains Mono',ui-monospace,monospace; font-size:12.5px; }

/* Conteneur de chat + zone de saisie */
#main-chat { border:1px solid rgba(148,163,184,.18) !important; border-radius:16px !important; }
.gradio-container textarea, .gradio-container input[type=text] { border-radius:10px !important; }
.gradio-container .block { border-radius:14px; }

/* Boutons secondaires en "puces" douces (raccourcis, historique) */
button.secondary, .gr-button-secondary {
  border-radius:999px !important; font-weight:600; border:1px solid rgba(148,163,184,.28) !important;
}
/* Bouton d'envoi bien visible */
button.primary, .gr-button-primary { font-weight:700; }

/* Suggestions & sidebar */
#suggestions { gap:8px; margin-bottom:2px; }
#suggestions button { border-radius:999px !important; font-size:12.5px !important; opacity:.92;
  background:rgba(79,70,229,.08) !important; border:1px solid rgba(79,70,229,.25) !important; }
#suggestions button:hover { opacity:1; background:rgba(79,70,229,.15) !important; }
#chat-sidebar { background:rgba(148,163,184,.06); border-radius:14px; padding:8px; }
/* Zone "Joindre" repliée = plus discrète */
.accordion { border-radius:12px !important; }

/* Responsive mobile : colonnes empilées, chat plein largeur */
@media (max-width:820px) {
  .gradio-container { padding:6px !important; }
  #app-header { padding:11px 15px; }
  #app-header .brand-tag, #app-header .pill { display:none; }
  #app-header .brand-name { font-size:18px; }
  .gap, .gradio-container .gap { gap:8px !important; }
  #main-chat { height:60vh !important; }
}
"""

_HEADER_HTML = (
    "<div id='app-header'>"
    "  <div class='brand'>"
    "    <span class='logo'>🧠</span>"
    "    <div><div class='brand-name'>MasterAgent</div>"
    "    <div class='brand-tag'>Ton agent IA — finance · code · documents · santé</div></div>"
    "  </div>"
    "  <span class='pill'>100% gratuit · Groq/Cerebras</span>"
    "</div>"
)


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="MasterAgent", theme=_THEME, css=_CSS,
                   analytics_enabled=False) as demo:

        mode_state = gr.State("fast")
        sid_state  = gr.State(_sid())

        # ── En-tête (identité visuelle) ───────────────────────────────────────
        gr.HTML(_HEADER_HTML)
        with gr.Row():
            mode_ind = gr.Markdown("🟢 **Mode Rapide ⚡** — direct, sans outils")
            with gr.Column(scale=0, min_width=240):
                mode_btn = gr.Button("⚡ Mode Rapide — ACTIF", variant="secondary", size="sm")

        with gr.Tabs():

            # ══ CHAT (le hub — fichiers, images, code, artefacts) ═════════════
            with gr.TabItem("💬 Chat"):
                with gr.Row(elem_id="chat-row"):
                    # ── Gauche : actions + historique repliable (C) + tokens ──
                    with gr.Column(scale=1, min_width=168, elem_id="chat-sidebar"):
                        new_btn = gr.Button("✨ Nouvelle conversation", variant="primary", size="sm")
                        with gr.Accordion("📋 Historique", open=False):
                            sess_dd = gr.Dropdown(choices=_list_sessions(), label="Sessions", value=None)
                            ref_btn = gr.Button("🔄 Rafraîchir", size="sm")
                        usage_md = gr.Markdown(_usage_md())

                    # ── Centre : conversation ────────────────────────────────
                    with gr.Column(scale=4):
                        if _CHATBOT_SUPPORTS_TYPE:
                            chatbot = gr.Chatbot(height=520, show_label=False, type="messages",
                                                 show_copy_button=True, elem_id="main-chat",
                                                 placeholder="👋 **Bienvenue !** Pose ta question, ou choisis une suggestion ci-dessous.")
                        else:
                            chatbot = gr.Chatbot(height=520, show_label=False, show_copy_button=True, elem_id="main-chat")
                        # (B) Suggestions cliquables — remplissent la saisie
                        with gr.Row(elem_id="suggestions"):
                            sug1 = gr.Button("💡 3 idées de business 2026", size="sm")
                            sug2 = gr.Button("📈 Analyse l'action Nvidia", size="sm")
                            sug3 = gr.Button("🧑‍💻 Explique les closures JS", size="sm")
                        with gr.Row():
                            msg_in  = gr.Textbox(placeholder='Écris ton message…  ("Agent: …" pour forcer les outils)',
                                                 scale=6, label="", lines=1, autofocus=True)
                            send_btn = gr.Button("Envoyer ▶", variant="primary", scale=1)
                        # Raccourcis (agissent sur le fichier/image joint)
                        with gr.Row():
                            sc_explain = gr.Button("🔍 Expliquer le code", size="sm")
                            sc_bugs    = gr.Button("🐛 Trouver les bugs", size="sm")
                            sc_summary = gr.Button("📊 Résumer le document", size="sm")
                        # Joindre — replié par défaut (déclutter l'interface)
                        with gr.Accordion("📎 Joindre un fichier ou une image", open=False):
                            with gr.Row():
                                chat_file = gr.File(label="Fichier (code, PDF, texte…)", file_count="single", type="filepath")
                                chat_img  = gr.Image(label="Image (upload ou Ctrl+V)", type="filepath",
                                                     sources=["upload", "clipboard"], height=150)

                    # ── Droite : Artefact — MASQUÉ tant qu'il n'y a pas de code (A) ──
                    with gr.Column(scale=2, visible=False) as artifact_col:
                        gr.Markdown("### 🧩 Artefact")
                        artifact_code = gr.Code(label="Dernier code généré", language="python")

                chat_st = gr.State([])

                _CHAT_IN  = [msg_in, chat_st, mode_state, sid_state, chat_img, chat_file]
                _CHAT_OUT = [chatbot, chat_st, msg_in, chat_img, chat_file, artifact_code, artifact_col, usage_md]

                mode_btn.click(toggle_mode, [mode_state], [mode_state, mode_btn, mode_ind], queue=False)
                # queue=False → POST direct, fiable même derrière un antivirus qui bloque le SSE
                send_btn.click(send, _CHAT_IN, _CHAT_OUT, queue=False)
                msg_in.submit(send, _CHAT_IN, _CHAT_OUT, queue=False)
                sug1.click(lambda: "Donne-moi 3 idées de business en 2026 et pourquoi", None, msg_in, queue=False)
                sug2.click(lambda: "Analyse l'action Nvidia (NVDA)", None, msg_in, queue=False)
                sug3.click(lambda: "Explique-moi les closures en JavaScript avec un exemple", None, msg_in, queue=False)
                sc_explain.click(lambda: "Explique ce code ligne par ligne.", None, msg_in, queue=False)
                sc_bugs.click(lambda: "Trouve les bugs et propose les corrections.", None, msg_in, queue=False)
                sc_summary.click(lambda: "Résume ce document et donne les points clés.", None, msg_in, queue=False)
                new_btn.click(new_conv, [], [chatbot, chat_st, sid_state]).then(refresh_sess, [], [sess_dd])
                ref_btn.click(refresh_sess, [], [sess_dd])
                sess_dd.change(load_sess, [sess_dd], [chatbot, chat_st, sid_state])

            # ══ FINANCE ═══════════════════════════════════════════════════════
            with gr.TabItem("💹 Finance"):
                gr.Markdown(
                    "### Analyse boursière & crypto en temps réel\n"
                    "Données réelles · RSI · MACD · Bollinger · Graphiques · Portefeuille · Conseiller Pro"
                )
                with gr.Tabs():

                    # ── Marchés (indices + crypto + sentiment + devises) ──────
                    with gr.TabItem("🌐 Marchés"):
                        gr.Markdown("Vue d'ensemble : indices/matières, crypto, sentiment (Fear & Greed), devises")
                        with gr.Row():
                            with gr.Column(scale=1):
                                db_btn = gr.Button("🔄 Indices & marchés", variant="primary", size="lg")
                                mk_coins = gr.Textbox(label="Cryptos (vide = top 8)", placeholder="BTC,ETH,SOL")
                                mk_crypto_btn = gr.Button("🪙 Crypto + Fear & Greed", variant="primary")
                                mk_cur = gr.Textbox(label="Devises depuis EUR", value="USD,GBP,JPY,CHF")
                                mk_cur_btn = gr.Button("💱 Taux de change", size="sm")
                            with gr.Column(scale=2):
                                db_out = gr.Markdown("*Choisis une vue à afficher.*")
                        db_btn.click(market_dashboard_fn, [], [db_out], queue=False)
                        mk_crypto_btn.click(crypto_market_fn, [mk_coins], [db_out], queue=False)
                        mk_cur_btn.click(currency_rates_fn, [mk_cur], [db_out], queue=False)

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
                        f_btn.click(analyze_finance, [f_ticker, f_period], [f_out, f_chart], queue=False)

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

                        pf_btn.click(portfolio_analyze_fn, [pf_in], [pf_out, pf_chart], queue=False)
                        pf_save_btn.click(
                            portfolio_save_fn, [pf_name, pf_in],
                            [pf_dd, pf_dd, pf_status], queue=False,
                        )
                        pf_load_btn.click(
                            portfolio_load_fn, [pf_dd],
                            [pf_in, pf_status], queue=False,
                        )
                        pf_delete_btn.click(
                            portfolio_delete_fn, [pf_dd],
                            [pf_dd, pf_in, pf_status], queue=False,
                        )

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
                        fa_btn.click(finance_agent_analysis, [fa_in], [fa_out], queue=False)

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

            # ══ SANTÉ (Vita) ══════════════════════════════════════════════════
            with gr.TabItem("🩺 Santé"):
                gr.Markdown(
                    "### Analyse tes données santé / sport / habitudes\n"
                    "3 façons : **charge un fichier**, colle un **chemin**, ou mets l'**URL de ton "
                    "endpoint Vita** (`https://…/api/export?key=SECRET&account=ID`) → l'agent récupère "
                    "tes données en direct. Il joue le rôle d'un **coach** (tendances, corrélations, recommandations).\n\n"
                    "🔒 *L'analyse est envoyée au modèle cloud (Groq). Pour du très sensible, anonymise avant.*"
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        h_file = gr.File(
                            label="📎 Charger un export (JSON/CSV)",
                            file_count="single", type="filepath",
                        )
                        h_path = gr.Textbox(
                            label="… OU chemin de fichier, OU URL Vita",
                            placeholder="https://mon-app.vercel.app/api/export?key=SECRET&account=ID",
                        )
                        h_q = gr.Textbox(
                            label="Sur quoi te concentrer ? (optionnel)",
                            placeholder="Ex: mon sommeil · ma progression au sport · ma régularité",
                            lines=2,
                        )
                        h_btn = gr.Button("🩺 Analyser mes données", variant="primary", size="lg")
                    with gr.Column(scale=2):
                        h_out = gr.Markdown("*Ton bilan santé apparaîtra ici.*")
                h_btn.click(analyze_health_fn, [h_path, h_q, h_file], [h_out], queue=False)

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

        # Rafraîchit le compteur de tokens à CHAQUE chargement de page
        # (sinon la valeur reste figée à celle du démarrage du serveur → « repart à 0 »).
        demo.load(_usage_md, None, usage_md)

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
