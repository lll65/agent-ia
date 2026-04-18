"""
Bot Telegram — répond rapidement via Groq (mode rapide) ou agent complet.
Prérequis: TELEGRAM_TOKEN dans .env (obtenu via @BotFather)
"""
import logging
from config import config

logger = logging.getLogger(__name__)

AGENT_PREFIXES = ("agent:", "mode complet:", "utilise tes outils:", "/agent")


async def run_telegram_bot():
    if not config.TELEGRAM_TOKEN:
        logger.info("Telegram: aucun token configuré (TELEGRAM_TOKEN dans .env).")
        return

    try:
        from telegram import Update
        from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
    except ImportError:
        logger.error("python-telegram-bot non installé: pip install python-telegram-bot")
        return

    async def _fast_reply(text: str, history: list = None) -> str:
        from llm.client import chat
        messages = [
            {"role": "system", "content": "Tu es MasterAgent, un assistant IA personnel chaleureux. Tu réponds en français, de façon concise. Tu es sur Telegram."},
        ]
        if history:
            messages.extend(history[-6:])
        messages.append({"role": "user", "content": text})
        return chat(messages, temperature=0.7)

    async def _agent_reply(text: str, user_id: str) -> str:
        from agent.core import run_agent
        from plugins import get_loader
        cfg = {
            "id": f"tg_{user_id}",
            "name": "MasterAgent Telegram",
            "system_prompt": "Tu es un agent IA puissant sur Telegram. Tu utilises tous les outils disponibles. Réponds en français.",
            "tools": list(get_loader().list_all().keys()),
            "model": config.LLM_MODEL,
        }
        result = await run_agent(text, cfg, f"tg_{user_id}")
        answer = result.get("answer", "Pas de réponse.")
        used = {s["action"] for s in result.get("steps", []) if s.get("action")}
        if used:
            answer += f"\n\n🔧 _Outils: {', '.join(sorted(used))}_"
        return answer

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 *MasterAgent-Gros* connecté!\n\n"
            "Envoie-moi n'importe quelle question.\n"
            "Préfixe `Agent:` pour utiliser tous les outils.\n\n"
            "Commandes: /help /clear /status",
            parse_mode="Markdown",
        )

    async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from plugins import get_loader
        tools = list(get_loader().list_all().keys())
        await update.message.reply_text(
            f"🔧 *Outils disponibles:*\n" + "\n".join(f"  • `{t}`" for t in tools),
            parse_mode="Markdown",
        )

    async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from memory import get_memory
        user_id = str(update.effective_user.id)
        get_memory().clear(f"tg_{user_id}")
        await update.message.reply_text("✅ Mémoire effacée.")

    async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from agent.self_heal import health_monitor
        report = health_monitor.report()
        if not report:
            await update.message.reply_text("✅ Aucun outil utilisé encore. Tout va bien!")
            return
        lines = [f"• {t}: {s.get('error_rate', '0%')} erreurs" for t, s in report.items()]
        await update.message.reply_text("📊 *Statut:*\n" + "\n".join(lines), parse_mode="Markdown")

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        text = update.message.text or ""
        if not text.strip():
            return

        # Détecte si mode agent demandé
        use_agent = any(text.lower().strip().startswith(p) for p in AGENT_PREFIXES)
        clean = text
        if use_agent:
            for p in AGENT_PREFIXES:
                if text.lower().strip().startswith(p):
                    clean = text[len(p):].strip() or text
                    break

        thinking = await update.message.reply_text("⏳ Réflexion..." if use_agent else "💬 ...")

        try:
            if use_agent:
                answer = await _agent_reply(clean, user_id)
            else:
                answer = await _fast_reply(clean)

            if len(answer) > 4000:
                answer = answer[:4000] + "\n…_(réponse tronquée)_"
            await thinking.edit_text(answer)
        except Exception as e:
            await thinking.edit_text(f"❌ Erreur: {e}")

    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot Telegram démarré (provider: {config.LLM_PROVIDER}).")
    await app.run_polling()
