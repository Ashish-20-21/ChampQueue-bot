from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from database.db import db
from services import mmr_engine
from utils.embeds import profile_card, leaderboard_embed, comparison_embed


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="profile", description="View your (or another player's) Champion's Queue profile")
    async def profile(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        player = db.get_player_by_discord_id(target.id)
        if not player:
            await interaction.response.send_message(f"{target.mention} isn't registered.", ephemeral=True)
            return
        achievements = db.get_player_achievements(player["id"])
        await interaction.response.send_message(embed=profile_card(player, achievements))

    @app_commands.command(name="leaderboard", description="View the Champion's Queue leaderboard")
    @app_commands.choices(metric=[
        app_commands.Choice(name="MMR", value="mmr"),
        app_commands.Choice(name="MVP Count", value="mvp_count"),
        app_commands.Choice(name="Wins", value="wins"),
    ])
    async def leaderboard(self, interaction: discord.Interaction, metric: app_commands.Choice[str] = None):
        field = metric.value if metric else "mmr"
        players = db.leaderboard(order_by=field, limit=10)
        label = metric.name if metric else "MMR"
        await interaction.response.send_message(embed=leaderboard_embed(players, metric_label=label))

    @app_commands.command(name="compare-last-match", description="Compare your latest match to the one before it")
    async def compare_last_match(self, interaction: discord.Interaction):
        player = db.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return
        history = db.player_recent_matches(player["id"], limit=2)
        completed = [h for h in history if h.get("matches", {}).get("status") == "completed"]
        if len(completed) < 2:
            await interaction.response.send_message("You need at least 2 completed matches to compare.", ephemeral=True)
            return
        latest, previous = completed[0], completed[1]
        await interaction.response.send_message(embed=comparison_embed(player["ign"], previous, latest))

    @app_commands.command(name="rank-progress", description="See your progress toward the next rank/division")
    async def rank_progress(self, interaction: discord.Interaction):
        player = db.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return
        tier, division = mmr_engine.derive_rank(player["mmr"])
        await interaction.response.send_message(
            f"**{player['ign']}** — {tier} {division} — {player['mmr']} MMR\n"
            f"(Peak: {player['peak_rank']} at {player['peak_mmr']} MMR)"
        )

    @app_commands.command(name="achievements", description="View your earned achievements")
    async def achievements(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        player = db.get_player_by_discord_id(target.id)
        if not player:
            await interaction.response.send_message(f"{target.mention} isn't registered.", ephemeral=True)
            return
        earned = db.get_player_achievements(player["id"])
        if not earned:
            await interaction.response.send_message(f"{player['ign']} hasn't earned any achievements yet.")
            return
        by_category: dict[str, list[str]] = {}
        for e in earned:
            cat = e["achievements"]["category"]
            by_category.setdefault(cat, []).append(e["achievements"]["name"])
        embed = discord.Embed(title=f"{player['ign']} — Achievements", color=discord.Color.gold())
        for cat, names in by_category.items():
            embed.add_field(name=cat.title(), value="\n".join(names), inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
