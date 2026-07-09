from __future__ import annotations

import datetime

import discord
from discord.ext import commands, tasks

from database.db import db

DIGEST_HOUR_UTC = 12  # edit to taste


class Digest(commands.Cog):
    def __init__(self, bot: commands.Bot, digest_channel_id: int | None = None):
        self.bot = bot
        self.digest_channel_id = digest_channel_id
        self.daily_digest.start()

    def cog_unload(self):
        self.daily_digest.cancel()

    @tasks.loop(time=datetime.time(hour=DIGEST_HOUR_UTC, tzinfo=datetime.timezone.utc))
    async def daily_digest(self):
        if not self.digest_channel_id:
            return
        channel = self.bot.get_channel(self.digest_channel_id)
        if not channel:
            return

        top_mmr = db.leaderboard(order_by="mmr", limit=5)
        top_mvp = db.leaderboard(order_by="mvp_count", limit=5)
        top_damage = db.leaderboard(order_by="avg_damage", limit=5)

        embed = discord.Embed(
            title="📊 Champion's Queue — Daily Digest",
            color=discord.Color.orange(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name="Top MMR",
            value="\n".join(f"{p['ign']} — {p['mmr']}" for p in top_mmr) or "—",
            inline=True,
        )
        embed.add_field(
            name="Most MVPs",
            value="\n".join(f"{p['ign']} — {p['mvp_count']}" for p in top_mvp) or "—",
            inline=True,
        )
        embed.add_field(
            name="Highest Avg Damage",
            value="\n".join(f"{p['ign']} — {p['avg_damage']}" for p in top_damage) or "—",
            inline=True,
        )
        # "Fastest climbers" needs a day-over-day MMR delta, which requires
        # snapshotting MMR daily — add a `mmr_snapshots` table if you want
        # this computed rather than left as a manual admin call for now.
        await channel.send(embed=embed)

    @daily_digest.before_loop
    async def before_digest(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    import os
    digest_channel_id = os.getenv("DIGEST_CHANNEL_ID")
    await bot.add_cog(Digest(bot, int(digest_channel_id) if digest_channel_id else None))
