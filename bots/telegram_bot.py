"""
Bot Telegram — envoie des messages à ton agent via Telegram.
Prérequis: crée ton bot via @BotFather et mets le token dans .env
"""
import asyncio
import logging
from config import config

logger = logging.getLogger(__name__)


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

    from agent.core import run_agent
    from plugins import get_loader

    DEFAULT_AGENT = {
        "id": "telegram",
        "name": "Agent Telegram",
        "system_prompt": "Tu es un assistant IA personnel sur Telegram. Réponds de façon concise et utile.",
        "tools": list(get_loader().list_all().keys()),
        "model": config.OLLAMA_MODEL,
    }

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 Agent IA Personnel connecté!\n"
            "Envoie-moi n'importe quelle question ou tâche.\n"
            "Commandes: /help /clear /status"
        )

    async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from plugins import get_loader
        tools = ", ".join(get_loader().list_all().keys())
        await update.message.reply_text(f"Outils disponibles: {tools}")

    async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from memory import get_memory
        user_id = str(update.effective_user.id)
        get_memory().clear(f"telegram_{user_id}")
        await update.message.reply_text("✅ Mémoire effacée.")

    async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from agent.self_heal import health_monitor
        report = health_monitor.report()
        text = "📊 Statut des outils:\n"
        for tool, stats in report.items():
            text += f"  • {tool}: {stats.get('error_rate', '0%')} d'erreurs\n"
        await update.message.reply_text(text or "Aucun outil utilisé encore.")

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        text = update.message.text
        thinking_msg = await update.message.reply_text("⏳ En cours...")
        try:
            result = await run_agent(text, DEFAULT_AGENT, f"telegram_{user_id}")
            answer = result.get("answer", "Pas de réponse.")
            if len(answer) > 4000:
                answer = answer[:4000] + "\n...(tronqué)"
            await thinking_msg.edit_text(answer)
        except Exception as e:
            await thinking_msg.edit_text(f"❌ Erreur: {e}")

    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot Telegram démarré.")
    await app.run_polling()
