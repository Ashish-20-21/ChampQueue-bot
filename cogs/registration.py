import re

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import adb

VALID_REGIONS = ["East", "West"]
COD_UID_PATTERN = re.compile(r"^\d{19}$")  # exactly 19 digits, numeric only


class Registration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="register", description="Register for Champion's Queue")
    @app_commands.describe(
        cod_uid="Your COD Mobile UID — 19 digits, exactly as shown in-game",
        ign="Your current in-game name",
        region="Your competitive region",
        organization="Your organization, if any (optional)",
    )
    @app_commands.choices(region=[
        app_commands.Choice(name="East", value="East"),
        app_commands.Choice(name="West", value="West"),
    ])
    async def register(self, interaction: discord.Interaction, cod_uid: str, ign: str,
                        region: str, organization: str | None = None):
        # Belt-and-suspenders: app_commands.choices restricts the Discord UI
        # dropdown, but validate again here in case of stale command cache
        # or any other path that bypasses the dropdown.
        if region not in VALID_REGIONS:
            await interaction.response.send_message(
                f"Invalid region `{region}`. Must be exactly one of: {', '.join(VALID_REGIONS)}.",
                ephemeral=True,
            )
            return

        cod_uid = cod_uid.strip()
        if not COD_UID_PATTERN.match(cod_uid):
            await interaction.response.send_message(
                "That doesn't look like a valid COD Mobile UID — it must be exactly 19 digits, "
                "numbers only (check Settings > Account in-game for your UID). "
                "Nothing was saved — just run `/register` again with the correct UID.",
                ephemeral=True,
            )
            return

        existing = await adb.get_player_by_discord_id(interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"You're already registered as **{existing['ign']}** (status: `{existing['status']}`). "
                f"Use `/update-ign` if you need to change your display name.",
                ephemeral=True,
            )
            return

        uid_taken = await adb.get_player_by_uid(cod_uid)
        if uid_taken:
            await interaction.response.send_message(
                "That COD Mobile UID is already registered to another Discord account. "
                "If this is your UID, contact an admin.",
                ephemeral=True,
            )
            return

        try:
            player = await adb.create_player(interaction.user.id, cod_uid, ign, region, organization)
        except Exception as e:
            # Handles races where the same request gets processed twice
            # (confirmed cause: Discord client-side retry/double-fire on a
            # slow response — not manual double-submission; seen twice in
            # testing, 2026-07-13/14, same UID landing successfully once,
            # second near-simultaneous attempt hitting the DB's unique
            # constraint milliseconds later). Two different constraints can
            # fire — check which one, then check whether the row that
            # exists now belongs to THIS account before saying "someone
            # else" took it, since that's misleading when it was actually
            # their own request's echo.
            err_str = str(e).lower()
            is_discord_id_conflict = "players_discord_id_key" in err_str
            is_uid_conflict = "players_cod_uid_key" in err_str or "duplicate key" in err_str

            if is_discord_id_conflict:
                existing_now = await adb.get_player_by_discord_id(interaction.user.id)
                if existing_now:
                    await interaction.response.send_message(
                        f"You're already registered as **{existing_now['ign']}** "
                        f"(status: `{existing_now['status']}`). That last click just duplicated "
                        f"your own request — no action needed.",
                        ephemeral=True,
                    )
                    return
                raise

            if is_uid_conflict:
                winner = await adb.get_player_by_uid(cod_uid)
                if winner and str(winner.get("discord_id")) == str(interaction.user.id):
                    await interaction.response.send_message(
                        f"You're already registered as **{winner['ign']}** (status: `{winner['status']}`). "
                        f"That last click just duplicated your own request — no action needed.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "That COD Mobile UID is already registered to another Discord account. "
                        "If this is your UID, contact an admin.",
                        ephemeral=True,
                    )
                return
            raise

        await adb.approve_player(player["id"], approved_by="auto")
        await interaction.response.send_message(
            f"You're registered and approved, **{ign}**! (UID `{cod_uid}`, region `{region}`) "
            f"You can head to your region's queue channel and join now.",
            ephemeral=True,
        )

    @app_commands.command(name="update-ign", description="Update your display IGN (your career stats stay attached to your UID)")
    async def update_ign(self, interaction: discord.Interaction, new_ign: str):
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered yet — use `/register` first.", ephemeral=True)
            return
        await adb.update_ign(player["id"], new_ign)
        await interaction.response.send_message(f"IGN updated to **{new_ign}**. Your stats and history are unaffected.", ephemeral=True)

    @app_commands.command(name="whoami", description="Check your registration status")
    async def whoami(self, interaction: discord.Interaction):
        player = await adb.get_player_by_discord_id(interaction.user.id)
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