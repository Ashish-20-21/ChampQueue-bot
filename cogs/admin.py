import discord
from discord import app_commands
from discord.ext import commands

from database.db import db
from services import reputation, stats_engine, mmr_engine
from utils.permissions import admin_only
from utils.embeds import result_card


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="admin-approve", description="[Admin] Approve a pending player by their Discord user")
    @admin_only()
    async def approve(self, interaction: discord.Interaction, user: discord.Member):
        player = db.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message(f"{user.mention} hasn't registered.", ephemeral=True)
            return
        db.approve_player(player["id"], str(interaction.user.id))
        await interaction.response.send_message(f"Approved **{player['ign']}** ({user.mention}).", ephemeral=True)

    @app_commands.command(name="admin-reject", description="[Admin] Reject a pending registration")
    @admin_only()
    async def reject(self, interaction: discord.Interaction, user: discord.Member):
        player = db.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message(f"{user.mention} hasn't registered.", ephemeral=True)
            return
        db.reject_player(player["id"])
        await interaction.response.send_message(f"Rejected registration for **{player['ign']}**.", ephemeral=True)

    @app_commands.command(name="admin-review-queue", description="[Admin] List matches awaiting review")
    @admin_only()
    async def review_queue(self, interaction: discord.Interaction):
        res = db.client.table("matches").select("*").eq("status", "awaiting_review").execute()
        if not res.data:
            await interaction.response.send_message("No matches currently need review.", ephemeral=True)
            return
        lines = [f"`{m['match_id']}` — map: {m.get('map', '—')} — created {m['created_at']}" for m in res.data]
        await interaction.response.send_message("**Matches awaiting review:**\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="admin-approve-match", description="[Admin] Force-accept a flagged match as-submitted")
    @admin_only()
    async def approve_match(self, interaction: discord.Interaction, match_id: str):
        match = db.get_match_by_code(match_id)
        if not match or match["status"] != "awaiting_review":
            await interaction.response.send_message("Match not found or not awaiting review.", ephemeral=True)
            return
        db.update_match(match["id"], {"status": "completed", "completed_at": "now()"})
        match_players = db.get_match_players(match["id"])
        for mp in match_players:
            stats_engine.process_post_match(mp["player_id"])
        embed = result_card(match, match_players)
        await interaction.response.send_message("Match approved and finalized.", embed=embed, ephemeral=True)

    @app_commands.command(name="admin-correct-stat", description="[Admin] Correct a single stat field on a match player before finalizing")
    @admin_only()
    async def correct_stat(self, interaction: discord.Interaction, match_id: str, user: discord.Member,
                            field: str, value: int):
        match = db.get_match_by_code(match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        player = db.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        valid_fields = {"kills", "deaths", "assists", "damage", "hill_time", "impact", "score"}
        if field not in valid_fields:
            await interaction.response.send_message(f"Invalid field. Must be one of: {', '.join(valid_fields)}", ephemeral=True)
            return
        db.update_match_player(match["id"], player["id"], {field: value, "stat_confirmed": True})
        await interaction.response.send_message(f"Updated `{field}` = {value} for **{player['ign']}** on {match_id}.", ephemeral=True)

    @app_commands.command(name="admin-adjust-reputation", description="[Admin] Manually adjust a player's reputation")
    @admin_only()
    async def adjust_reputation(self, interaction: discord.Interaction, user: discord.Member, delta: int, reason: str):
        player = db.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        updated = db.apply_reputation_delta(player["id"], delta, f"admin_adjustment: {reason}")
        await interaction.response.send_message(
            f"**{player['ign']}** reputation now **{updated['reputation']}** ({'+' if delta >= 0 else ''}{delta}, reason: {reason})",
            ephemeral=True,
        )

    @approve.error
    @reject.error
    @review_queue.error
    @approve_match.error
    @correct_stat.error
    @adjust_reputation.error
    async def on_admin_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
