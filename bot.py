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


async def main():
    bot = ChampionsQueueBot()
    async with bot:
        await bot.start(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
