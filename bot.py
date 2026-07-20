import asyncio
import logging
import logging.handlers
import os
from concurrent.futures import ThreadPoolExecutor

import discord
from discord.ext import commands

import config

# File handler alongside the existing stdout one — recommended in the
# post-P6 architecture review (§6.3) since the bot currently runs as a
# local `python bot.py` process, not under systemd/journald, meaning
# anything only logged to stdout is lost the moment the terminal
# session ends or scrolls past. RotatingFileHandler caps disk usage
# (5MB x 3 backups = 15MB max) so this can't grow unbounded. Once this
# actually moves under systemd for real hosting, journald makes this
# redundant (not harmful, just unnecessary) — safe to leave either way.
os.makedirs("logs", exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                     handlers=[logging.StreamHandler(), _file_handler])
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
    # Every DB call goes through asyncio.to_thread (see database/db.py's
    # __getattr__ proxy), which by default shares Python's stdlib thread
    # pool — sized to min(32, cpu_count + 4), i.e. often just 8-12 threads.
    # That's shared across EVERY concurrent DB call from every region's
    # queue, every match's skill votes, every /profile or /stats command,
    # all at once. At real multi-queue, multi-match concurrency, that
    # ceiling can genuinely saturate — calls start queueing for a free
    # thread, reintroducing the same kind of latency the interaction-token
    # races earlier tonight were caused by, just one layer deeper. This is
    # cheap and safe to raise: these are I/O-bound (waiting on network),
    # not CPU-bound, so having more threads mostly-idle-waiting costs
    # nothing meaningful.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=50, thread_name_prefix="db-io"))

    bot = ChampionsQueueBot()
    async with bot:
        await bot.start(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())