"""
Bot Discord — interagit avec ton agent depuis ton serveur Discord.
Prérequis: crée ton bot sur discord.com/developers/applications et mets le token dans .env

⚠️ SÉCURITÉ. Ce bot répondait à N'IMPORTE QUI — pire encore que le bot Telegram, qui
avait le même défaut : il se donnait TOUS les outils, y compris ceux qui exécutent du
Python arbitraire sur le serveur et réécrivent le code de Nova. N'importe quel membre
du serveur pouvait donc écrire « !ia … » ou mentionner le bot pour obtenir l'agent
complet — mails, Drive, agenda connectés — et « !outils » listait tout l'outillage
interne à qui le demandait. Sur un serveur public, c'était ouvert au monde entier.
Le bot n'a plus qu'un propriétaire, et les outils dangereux ne sont plus exposés.
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

    from agent.core import run_agent, outils_pour_conversation
    from bots.telegram_push import est_proprietaire
    from plugins import get_loader

    DEFAULT_AGENT = {
        "id": "discord",
        "name": "Agent Discord",
        "system_prompt": "Tu es un assistant IA sur Discord. Réponds de façon concise.",
        # ⚠️ C'était `list(get_loader().list_all().keys())` : TOUT, exec_python et
        # apply_self_modification compris.
        "tools": outils_pour_conversation(get_loader().list_all().keys()),
        "model": config.OLLAMA_MODEL,
    }

    def _proprietaire_configure() -> str:
        return str(getattr(config, "DISCORD_OWNER_ID", "") or "").strip()

    async def _autorise(ctx_ou_message) -> bool:
        """Seul le propriétaire est servi.

        Il est désigné par DISCORD_OWNER_ID, ou à défaut par la première personne qui
        parle au bot — retenue définitivement, comme pour Telegram. Le partage du même
        registre est volontaire : c'est la même personne des deux côtés.
        """
        auteur = getattr(ctx_ou_message, "author", None)
        uid = str(getattr(auteur, "id", "") or "")
        fixe = _proprietaire_configure()
        if fixe:
            ok = uid == fixe
        else:
            ok = est_proprietaire(f"discord:{uid}") if uid else False
        if not ok:
            logger.warning(f"[Discord] Message refusé — utilisateur {uid or '?'} inconnu.")
            try:
                await ctx_ou_message.reply(
                    "Ce bot est personnel et ne répond qu'à son propriétaire.")
            except Exception:
                pass
        return ok

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
        if not await _autorise(ctx):
            return
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
        if not await _autorise(ctx):
            return
        from memory import get_memory
        get_memory().clear(f"discord_{ctx.author.id}")
        await ctx.reply("✅ Mémoire effacée.")

    @bot.command(name="outils")
    async def list_tools(ctx):
        """Liste les outils disponibles."""
        if not await _autorise(ctx):
            return
        # On ne montre que ce que le bot accepte réellement d'utiliser : afficher
        # l'outillage interne complet était en soi une divulgation.
        tools = {k: v for k, v in get_loader().list_all().items()
                 if k in (DEFAULT_AGENT["tools"] or [])}
        text = "**Outils disponibles:**\n" + "\n".join(f"• `{k}`: {v}" for k, v in tools.items())
        await ctx.reply(text[:2000])

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return
        if bot.user.mentioned_in(message) and not message.mention_everyone:
            question = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if question and await _autorise(message):
                user_id = str(message.author.id)
                async with message.channel.typing():
                    result = await run_agent(question, DEFAULT_AGENT, f"discord_{user_id}")
                    answer = result.get("answer", "Pas de réponse.")[:2000]
                    await message.reply(answer)
        await bot.process_commands(message)

    logger.info("Bot Discord démarré.")
    await bot.start(config.DISCORD_TOKEN)
