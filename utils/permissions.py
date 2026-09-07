import discord
from discord import app_commands

import config


def is_admin(interaction: discord.Interaction) -> bool:
    # Unified 2026-07-29: ADMIN_ROLE_ID (singular) -> ADMIN_ROLE_IDS (set).
    # HOD + admin team share identical full admin power, so this checks
    # membership in ANY of the configured admin roles, not equality
    # against one fixed role. This is the single seam every admin gate in
    # the bot funnels through (admin_only() below, cogs/match.py's
    # scoreboard-upload-on-host's-behalf exception, and every @admin_only()
    # decorator in cogs/admin.py, cogs/queue.py, cogs/stats.py) — changing
    # it here is sufficient, no other file needs its own role-set logic.
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id in config.ADMIN_ROLE_IDS for role in interaction.user.roles)


def admin_only():
    def predicate(interaction: discord.Interaction) -> bool:
        return is_admin(interaction)
    return app_commands.check(predicate)


def is_hod(interaction: discord.Interaction) -> bool:
    """Check if the user has any configured HOD role (separate from admin)."""
    if not isinstance(interaction.user, discord.Member):
        return False
    if not config.HOD_ROLE_IDS:
        return False
    return any(role.id in config.HOD_ROLE_IDS for role in interaction.user.roles)


def hod_or_admin_only():
    """Permission gate that allows EITHER admin OR HOD role holders.
    Used for /admin-grant-shield — HOD members need to be able to
    initiate shield grants themselves, not just approve them."""
    def predicate(interaction: discord.Interaction) -> bool:
        return is_admin(interaction) or is_hod(interaction)
    return app_commands.check(predicate)