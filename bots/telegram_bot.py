"""
Bot Telegram — répond via Groq (mode rapide) ou agent complet (ReAct + outils).
Prérequis: TELEGRAM_TOKEN dans .env (obtenu via @BotFather sur Telegram)

Démarrage automatique si TELEGRAM_TOKEN est défini dans .env.
Le bot partage le même LLM et les mêmes plugins que l'interface web.
"""
import asyncio
import logging
from config import config

logger = logging.getLogger(__name__)

AGENT_PREFIXES = ("agent:", "mode complet:", "utilise tes outils:", "/agent")

# Historique de conversation en mémoire (max 20 messages par utilisateur)
_HISTORIES: dict[str, list] = {}

_FAST_SYS = (
    "Tu es MasterAgent-Gros, un assistant IA expert et direct. "
    "Tu réponds en français, de façon concise et actionnable. "
    "Tu es sur Telegram — texte simple, sans Markdown complexe, max 500 mots par réponse. "
    "Tu assumes tes opinions et ne te défiles jamais derrière 'ça dépend'."
)

_FINANCE_HINTS = (
    "bourse", "action ", "crypto", "bitcoin", "ethereum", "trading",
    "investir", "dividende", "cotation", "achat vente", "faut il acheter",
    "vaut il acheter", "analyse technique", "bullish", "bearish",
    "cours de", "prix de l'action", "solana", "soitec", "apple stock",
    "nvidia stock", "tesla stock", "or ", "pétrole", "forex",
)


def _is_finance(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _FINANCE_HINTS)


async def run_telegram_bot():
    if not config.TELEGRAM_TOKEN:
        logger.info("Telegram: aucun token configuré (ajoute TELEGRAM_TOKEN dans .env).")
        return

    try:
        from telegram import Update
        from telegram.ext import (
            ApplicationBuilder, CommandHandler, MessageHandler,
            filters, ContextTypes,
        )
    except ImportError:
        logger.error("python-telegram-bot non installé: pip install python-telegram-bot==21.3")
        return

    # ── Handlers LLM ─────────────────────────────────────────────────────────

    async def _fast_reply(text: str, user_id: str) -> str:
        from llm.client import chat
        from agent.core import _off
        history = _HISTORIES.get(user_id, [])
        msgs = [{"role": "system", "content": _FAST_SYS}]
        msgs.extend(history[-8:])
        msgs.append({"role": "user", "content": text})
        # ⚠️ `chat()` est bloquant : appelé directement, il gelait la boucle partagée avec
        # le serveur web (un message Telegram suspendait le streaming de Nova).
        answer = await _off(chat, msgs, temperature=0.7)
        # Sauvegarde de l'historique
        history.append({"role": "user",      "content": text})
        history.append({"role": "assistant",  "content": answer})
        _HISTORIES[user_id] = history[-20:]
        return answer

    async def _agent_reply(text: str, user_id: str) -> str:
        from agent.core import run_agent, outils_pour_conversation
        from plugins import get_loader
        cfg = {
            "id": f"tg_{user_id}",
            "name": "MasterAgent Telegram",
            "system_prompt": (
                "Tu es un agent IA puissant sur Telegram. "
                "Tu utilises tous les outils disponibles. "
                "Réponds en français, en texte simple sans Markdown complexe."
            ),
            # ⚠️ C'était TOUS les outils, exec_python et apply_self_modification
            # compris : du Python arbitraire sur le serveur, donc os.environ et
            # toutes les clés, depuis une simple conversation Telegram.
            "tools": outils_pour_conversation(get_loader().list_all().keys()),
            "model": config.LLM_MODEL,
        }
        result = await run_agent(text, cfg, f"tg_{user_id}")
        answer = result.get("answer", "Pas de réponse.")
        used = {s["action"] for s in result.get("steps", []) if s.get("action")}
        if used:
            answer += f"\n\n🔧 Outils: {', '.join(sorted(used))}"
        return answer

    def _finance_reply_sync(text: str) -> str:
        """Analyse financière directe (ticker → données → LLM)."""
        try:
            from plugins.builtin.finance import StockAnalysisPlugin, MarketNewsPlugin
            from llm.client import chat as llm_chat
            import re

            # 1. Extraire le ticker
            ticker_prompt = (
                f'Question: "{text}"\n'
                "Extrais le symbole boursier exact. Conversions:\n"
                "Soitec→SOI.PA | Apple→AAPL | Tesla→TSLA | Nvidia→NVDA | "
                "Bitcoin→BTC-USD | Ethereum→ETH-USD | LVMH→MC.PA | CAC40→^FCHI\n"
                "Réponds UNIQUEMENT avec le ticker ou GENERAL."
            )
            ticker_raw = llm_chat(
                [{"role": "user", "content": ticker_prompt}], temperature=0.0
            ).strip().upper()
            tokens = re.split(r"[,\s]+", ticker_raw)
            tickers = [t for t in tokens if re.match(r"^\^?[A-Z]{1,5}[\-\.]?[A-Z0-9]{0,4}(=F|=X)?$", t)][:2]

            data_parts = []
            for tk in tickers:
                # Essais yfinance
                for period in ("3mo", "6mo"):
                    try:
                        r = StockAnalysisPlugin().run(ticker=tk, period=period)
                        if r and "❌" not in r[:10]:
                            data_parts.append(r[:2000])
                            break
                    except Exception:
                        pass
                else:
                    # Fallback HTTP
                    from plugins.builtin.finance import _fetch_ticker_http
                    d = _fetch_ticker_http(tk, "3mo")
                    if d:
                        data_parts.append(
                            f"{tk}: Prix={d['price']:.2f} {d['currency']}, "
                            f"24h={d['chg']:+.2f}%, "
                            f"H={d['h']:.2f}, L={d['l']:.2f}"
                        )

            if not data_parts:
                data_ctx = "Données temps réel non accessibles — analyse depuis tes connaissances."
            else:
                data_ctx = "\n\n".join(data_parts)

            # 2. Synthèse LLM
            answer = llm_chat([
                {"role": "system", "content": (
                    "Tu es un trader d'élite. Analyse concise pour Telegram (max 400 mots). "
                    "Donne : OUI/NON/ATTENDRE + 3 raisons chiffrées + niveaux TP1/SL. "
                    "Texte simple, pas de Markdown complexe."
                )},
                {"role": "user", "content": f"DONNÉES:\n{data_ctx}\n\nQUESTION: {text}"},
            ], temperature=0.5)
            return answer or "Analyse indisponible."
        except Exception as e:
            logger.error(f"[Telegram finance] {e}")
            return f"❌ Erreur analyse: {e}"

    # ── Filtre du propriétaire ────────────────────────────────────────────────
    async def _autorise(update) -> bool:
        """N'importe qui sur Telegram peut écrire à ce bot : il suffit de connaître
        son @nom. Sans ce filtre, un inconnu obtenait une conversation avec l'agent
        COMPLET — donc un accès aux mails, à Drive et à l'agenda connectés — et son
        chat devenait une cible de diffusion pour les alertes et les résultats
        d'automatisation. Seul le propriétaire est servi (voir bots/telegram_push)."""
        from bots.telegram_push import est_proprietaire
        chat = update.effective_chat
        if chat is not None and est_proprietaire(chat.id):
            return True
        who = getattr(update.effective_user, "id", "?")
        logger.warning(f"[Telegram] Message refusé — chat inconnu (user {who}).")
        try:
            await update.message.reply_text(
                "Ce bot est personnel et ne répond qu'à son propriétaire.")
        except Exception:
            pass
        return False

    # ── Commandes ─────────────────────────────────────────────────────────────

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await _autorise(update):
            return
        # Enregistre ce chat pour recevoir les alertes proactives (PEA Watcher)
        try:
            from bots.telegram_push import register_chat
            register_chat(update.effective_chat.id)
        except Exception:
            pass
        # ⚠️ Ce message annonçait « Ce chat recevra les alertes PEA automatiques »
        # à tout le monde, alors que la veille PEA est DÉSACTIVÉE par défaut
        # (WATCHER_ENABLED=false). Lohan a donc cru avoir activé une surveillance
        # qu'il n'avait pas demandée — et qui n'existait pas. On ne promet que ce qui
        # est réellement en marche.
        lignes = ["🤖 Nova est connectée à ce chat.", "",
                  "Envoie-moi n'importe quelle question.",
                  "Préfixe « agent: » pour le mode complet avec tes apps."]
        actifs = []
        if config.TELEGRAM_TOKEN:
            actifs.append("• les résultats de tes automatisations arrivent ici")
        if getattr(config, "WATCHER_ENABLED", False):
            actifs.append("• les alertes PEA arrivent ici (WATCHER_ENABLED=true)")
        if getattr(config, "BRIEFING_ENABLED", False):
            actifs.append(f"• le briefing du matin arrive ici (à {config.BRIEFING_HOUR} h)")
        if actifs:
            lignes += ["", "Ce qui est ACTIF en ce moment :"] + actifs
        eteints = []
        if not getattr(config, "WATCHER_ENABLED", False):
            eteints.append("veille PEA")
        if not getattr(config, "BRIEFING_ENABLED", False):
            eteints.append("briefing du matin")
        if eteints:
            lignes += ["", "Éteint : " + ", ".join(eteints) + "."]
        lignes += ["", "Commandes : /help /clear /status /watch"]
        await update.message.reply_text("\n".join(lignes))

    async def watch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Déclenche un scan PEA immédiat et répond avec les alertes en cours."""
        if not await _autorise(update):
            return
        try:
            from bots.telegram_push import register_chat
            register_chat(update.effective_chat.id)
        except Exception:
            pass
        await update.message.reply_text("🔔 Scan des positions en cours…")
        try:
            from agent.pea_watcher import run_once
            loop = asyncio.get_event_loop()
            # dry_run=True : on répond ici sans re-pousser (évite le doublon)
            res = await loop.run_in_executor(None, run_once, True)
            if res["tickers"] == 0:
                msg = (
                    "Watchlist vide. Ajoute des valeurs dans data/watchlist.txt "
                    "(une par ligne) ou sauvegarde un portefeuille dans l'interface web."
                )
            elif not res["alerts"]:
                msg = f"✅ {res['tickers']} valeurs surveillées — aucun seuil franchi actuellement."
            else:
                msg = f"🔔 {len(res['alerts'])} alerte(s) sur {res['tickers']} valeurs :\n\n" + "\n\n".join(res["alerts"])
            await update.message.reply_text(msg[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ Erreur scan: {str(e)[:300]}")

    async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await _autorise(update):
            return
        from plugins import get_loader
        from agent.core import outils_pour_conversation
        # On ne liste que ce que le bot accepte reellement d'utiliser : afficher
        # l'outillage interne complet etait en soi une divulgation.
        tools = outils_pour_conversation(get_loader().list_all().keys())
        txt = "🔧 Outils disponibles:\n" + "\n".join(f"  • {t}" for t in tools[:12])
        if len(tools) > 12:
            txt += f"\n  … +{len(tools) - 12} autres"
        await update.message.reply_text(txt)

    async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await _autorise(update):
            return
        user_id = str(update.effective_user.id)
        _HISTORIES.pop(user_id, None)
        try:
            from memory import get_memory
            get_memory().clear(f"tg_{user_id}")
        except Exception:
            pass
        await update.message.reply_text("✅ Mémoire effacée.")

    async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await _autorise(update):
            return
        from agent.self_improve import get_stats
        stats = get_stats()
        msg = (
            f"📊 MasterAgent-Gros\n\n"
            f"Runs évalués: {stats.get('runs', 0)}\n"
            f"Score moyen: {stats.get('avg_score', 0):.1f}/10\n"
            f"Provider LLM: {config.LLM_PROVIDER}\n"
            f"Modèle: {config.LLM_MODEL}"
        )
        await update.message.reply_text(msg)

    # ── Gestion des messages ──────────────────────────────────────────────────

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await _autorise(update):
            return
        user_id  = str(update.effective_user.id)
        username = update.effective_user.username or user_id
        text     = (update.message.text or "").strip()
        if not text:
            return

        # Enregistre le chat pour les alertes proactives (idempotent)
        try:
            from bots.telegram_push import register_chat
            register_chat(update.effective_chat.id)
        except Exception:
            pass

        # Détection du mode
        use_agent = any(text.lower().startswith(p) for p in AGENT_PREFIXES)
        clean = text
        if use_agent:
            for p in AGENT_PREFIXES:
                if text.lower().startswith(p):
                    clean = text[len(p):].strip() or text
                    break

        is_fin = not use_agent and _is_finance(clean)

        icon = (
            "📊 Analyse financière en cours..."
            if is_fin else (
                "⚙️ Mode Agent — réflexion..."
                if use_agent else "💬 ..."
            )
        )
        logger.info(f"[Telegram] {username}: {text[:60]}")
        thinking = await update.message.reply_text(icon)

        try:
            if use_agent:
                answer = await _agent_reply(clean, user_id)
            elif is_fin:
                loop = asyncio.get_event_loop()
                answer = await loop.run_in_executor(None, _finance_reply_sync, clean)
            else:
                answer = await _fast_reply(clean, user_id)

            if len(answer) > 4000:
                answer = answer[:3900] + "\n\n…(réponse tronquée — pose une question plus précise)"

            # PAS de parse_mode : le LLM peut générer du Markdown invalide pour Telegram
            await thinking.edit_text(answer)

        except Exception as e:
            logger.error(f"[Telegram] Erreur handler: {e}")
            try:
                await thinking.edit_text(f"❌ Erreur: {str(e)[:300]}")
            except Exception:
                pass

    # ── Construction du bot ───────────────────────────────────────────────────

    bot_app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_cmd))
    bot_app.add_handler(CommandHandler("clear", clear_cmd))
    bot_app.add_handler(CommandHandler("status", status_cmd))
    bot_app.add_handler(CommandHandler("watch", watch_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # ── Démarrage intégré à la boucle uvicorn/FastAPI ─────────────────────────
    # IMPORTANT: run_polling() est SYNCHRONE et crée sa propre boucle asyncio
    # → incompatible avec uvicorn. On utilise initialize/start/updater.start_polling
    # pour s'intégrer proprement dans la boucle existante.
    logger.info(f"🤖 Bot Telegram démarré (provider: {config.LLM_PROVIDER}). Envoie /start sur Telegram!")
    try:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        # Bloque jusqu'à ce que la tâche soit annulée (arrêt serveur)
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Bot Telegram: arrêt propre.")
    except Exception as e:
        logger.error(f"Bot Telegram erreur fatale: {e}", exc_info=True)
    finally:
        try:
            if bot_app.updater.running:
                await bot_app.updater.stop()
            if bot_app.running:
                await bot_app.stop()
            await bot_app.shutdown()
        except Exception:
            pass
