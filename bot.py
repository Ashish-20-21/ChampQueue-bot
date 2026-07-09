import asyncio
import logging

import discord
from discord.ext import commands

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("champions_queue")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True

COGS = [
    "cogs.registration",
    "cogs.queue",
    "cogs.match",
    "cogs.stats",
    "cogs.admin",
    "cogs.digest",
]


class ChampionsQueueBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!cq-", intents=INTENTS)

    async def setup_hook(self):
        for cog in COGS:
            await self.load_extension(cog)
            log.info(f"Loaded {cog}")

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} slash commands to guild {config.GUILD_ID}")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (id={self.user.id})")
        # Sweep in case the bot was added to a foreign guild while offline
        # (belt-and-suspenders alongside on_guild_join, and alongside
        # disabling "Public Bot" in the Developer Portal, which is the
        # primary control — see SECURITY.md).
        for guild in list(self.guilds):
            if guild.id != config.GUILD_ID:
                log.warning(f"Bot is in unauthorized guild '{guild.name}' ({guild.id}) — leaving.")
                await guild.leave()

    async def on_guild_join(self, guild: discord.Guild):
        if guild.id != config.GUILD_ID:
            log.warning(f"Added to unauthorized guild '{guild.name}' ({guild.id}) — leaving immediately.")
            try:
                if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                    await guild.system_channel.send(
                        "This bot is privately configured for a specific server and isn't available here. Leaving."
                    )
            except discord.Forbidden:
                pass
            await guild.leave()


async def main():
    bot = ChampionsQueueBot()
    async with bot:
        await bot.start(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
