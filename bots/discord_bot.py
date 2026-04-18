"""
Bot Discord — interagit avec ton agent depuis ton serveur Discord.
Prérequis: crée ton bot sur discord.com/developers/applications et mets le token dans .env
"""
import logging
from config import config

logger = logging.getLogger(__name__)


async def run_discord_bot():
    if not config.DISCORD_TOKEN:
        logger.info("Discord: aucun token configuré (DISCORD_TOKEN dans .env).")
        return

    try:
        import discord
        from discord.ext import commands
    except ImportError:
        logger.error("discord.py non installé: pip install discord.py")
        return

    from agent.core import run_agent
    from plugins import get_loader

    DEFAULT_AGENT = {
        "id": "discord",
        "name": "Agent Discord",
        "system_prompt": "Tu es un assistant IA sur Discord. Réponds de façon concise.",
        "tools": list(get_loader().list_all().keys()),
        "model": config.OLLAMA_MODEL,
    }

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        logger.info(f"Bot Discord connecté: {bot.user}")
        if config.DISCORD_GUILD_ID:
            await bot.tree.sync(guild=discord.Object(id=config.DISCORD_GUILD_ID))

    @bot.command(name="ia")
    async def ask_agent(ctx, *, question: str):
        """Pose une question à l'agent: !ia <question>"""
        user_id = str(ctx.author.id)
        async with ctx.typing():
            try:
                result = await run_agent(question, DEFAULT_AGENT, f"discord_{user_id}")
                answer = result.get("answer", "Pas de réponse.")
                if len(answer) > 2000:
                    answer = answer[:1990] + "..."
                await ctx.reply(answer)
            except Exception as e:
                await ctx.reply(f"❌ Erreur: {e}")

    @bot.command(name="clear")
    async def clear_memory(ctx):
        """Efface la mémoire de la conversation."""
        from memory import get_memory
        get_memory().clear(f"discord_{ctx.author.id}")
        await ctx.reply("✅ Mémoire effacée.")

    @bot.command(name="outils")
    async def list_tools(ctx):
        """Liste les outils disponibles."""
        tools = get_loader().list_all()
        text = "**Outils disponibles:**\n" + "\n".join(f"• `{k}`: {v}" for k, v in tools.items())
        await ctx.reply(text[:2000])

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return
        if bot.user.mentioned_in(message) and not message.mention_everyone:
            question = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if question:
                user_id = str(message.author.id)
                async with message.channel.typing():
                    result = await run_agent(question, DEFAULT_AGENT, f"discord_{user_id}")
                    answer = result.get("answer", "Pas de réponse.")[:2000]
                    await message.reply(answer)
        await bot.process_commands(message)

    logger.info("Bot Discord démarré.")
    await bot.start(config.DISCORD_TOKEN)
