import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import db


class Registration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="register", description="Register for Champion's Queue (one-time; requires admin approval)")
    @app_commands.checks.cooldown(1, config.REGISTER_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    @app_commands.describe(
        cod_uid="Your COD Mobile UID (this becomes your permanent player identity)",
        ign="Your current in-game name",
        region="Your competitive region",
        organization="Your organization, if any (optional)",
    )
    async def register(self, interaction: discord.Interaction, cod_uid: str, ign: str,
                        region: str, organization: str | None = None):
        existing = db.get_player_by_discord_id(interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"You're already registered as **{existing['ign']}** (status: `{existing['status']}`). "
                f"Use `/update-ign` if you need to change your display name.",
                ephemeral=True,
            )
            return

        uid_taken = db.get_player_by_uid(cod_uid)
        if uid_taken:
            await interaction.response.send_message(
                "That COD Mobile UID is already registered to another Discord account. "
                "If this is your UID, contact an admin.",
                ephemeral=True,
            )
            return

        player = db.create_player(interaction.user.id, cod_uid, ign, region, organization)
        await interaction.response.send_message(
            f"Registration submitted for **{ign}** (UID `{cod_uid}`, region `{region}`). "
            f"An admin needs to approve you before you can join the queue.",
            ephemeral=True,
        )

        # Notify admin channel/role if configured — left as a follow-up
        # since it depends on which channel you want approvals posted to.

    @app_commands.command(name="update-ign", description="Update your display IGN (your career stats stay attached to your UID)")
    async def update_ign(self, interaction: discord.Interaction, new_ign: str):
        player = db.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered yet — use `/register` first.", ephemeral=True)
            return
        db.update_ign(player["id"], new_ign)
        await interaction.response.send_message(f"IGN updated to **{new_ign}**. Your stats and history are unaffected.", ephemeral=True)

    @register.error
    async def register_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Slow down — try `/register` again in {error.retry_after:.0f}s.", ephemeral=True
            )
        else:
            raise error

    @app_commands.command(name="whoami", description="Check your registration status")
    async def whoami(self, interaction: discord.Interaction):
        player = db.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered yet — use `/register` first.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**{player['ign']}** — UID `{player['cod_uid']}` — status: `{player['status']}` "
            f"— rank: {player['current_rank']} {player['current_division']} — MMR: {player['mmr']}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Registration(bot))
