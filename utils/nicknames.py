"""Shared helper for keeping a player's Discord server nickname in sync
with their registered IGN. Added 2026-07-29 — real problem found live:
players in voice chat couldn't tag teammates by IGN, since usernames
rarely match IGNs and most players don't have Developer Mode enabled to
copy a raw Discord ID. Setting the server NICKNAME (not the account
username, which the bot can never touch) to the player's IGN means
Discord's own native @-mention autocomplete just works — no bot lookup
command needed, no Developer Mode needed, and it degrades safely to a
no-op if the bot lacks permission rather than blocking registration/
approval.

Requires the bot's role to have the "Manage Nicknames" permission AND
be positioned ABOVE the target member in the server's role hierarchy —
Discord silently refuses (discord.Forbidden) if either condition isn't
met, most commonly for admins/mods whose own roles outrank the bot. That
failure is expected and handled here as a no-op, not an error the caller
needs to worry about — a failed nickname sync should NEVER block
registration or approval, which is why every call site treats this as
fire-and-forget with logging, not a step that can fail the surrounding
command.
"""

import logging

import discord

logger = logging.getLogger(__name__)


async def sync_nickname(member: discord.Member, ign: str) -> bool:
    """Set member's server nickname to ign. Returns True on success,
    False if skipped/failed for any reason (already correct, missing
    permission, role-hierarchy block, member left, etc.) — callers
    should not treat False as an error to surface to the player; log
    and move on. Discord nicknames are capped at 32 characters; IGNs
    longer than that are truncated rather than rejected, since a
    truncated-but-present nickname is still more useful for @-mention
    lookup than none at all."""
    truncated = ign[:32]
    if member.nick == truncated:
        return True  # already correct, nothing to do
    try:
        await member.edit(nick=truncated)
        return True
    except discord.Forbidden:
        # Bot lacks Manage Nicknames, or the target outranks the bot in
        # the role hierarchy (common for admins/mods) — expected, not a
        # bug. Logged at debug level since this can fire routinely for
        # admin accounts and shouldn't spam warning-level logs.
        logger.debug("sync_nickname: Forbidden for %s (%s) — missing permission or role hierarchy", member.id, ign)
        return False
    except discord.HTTPException as e:
        logger.warning("sync_nickname: HTTPException for %s (%s): %s", member.id, ign, e)
        return False
