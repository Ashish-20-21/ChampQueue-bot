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


def is_moderator(interaction: discord.Interaction) -> bool:
    """True if the user holds any configured Moderator role.

    Deliberately a SEPARATE set from ADMIN_ROLE_IDS: is_admin() is used in
    places moderators must NOT inherit (unlimited /ign-change, the
    scoreboard-upload-on-host's-behalf bypass, /host-roll-map override, and
    every admin-only command not explicitly opened to moderators). Putting a
    Moderator role into ADMIN_ROLE_IDS would silently hand all of that over.

    Fails closed: if MODERATOR_ROLE_IDS is unset/empty this is always False,
    so deploying the code before creating the role changes nothing."""
    if not isinstance(interaction.user, discord.Member):
        return False
    if not config.MODERATOR_ROLE_IDS:
        return False
    return any(role.id in config.MODERATOR_ROLE_IDS for role in interaction.user.roles)


def is_mod_or_admin(interaction: discord.Interaction) -> bool:
    return is_admin(interaction) or is_moderator(interaction)


def mod_or_admin_only():
    """Permission gate that allows EITHER admin OR Moderator role holders.
    Used only on the operational commands moderators share the load on
    (queue clean/replace, match review/scrap/map-change, panels, ...).
    Anything destructive to rankings beyond a capped MMR correction stays
    on admin_only()."""
    def predicate(interaction: discord.Interaction) -> bool:
        return is_mod_or_admin(interaction)
    return app_commands.check(predicate)
