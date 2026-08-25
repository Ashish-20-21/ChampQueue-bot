"""
Central #botlog incident reporting (2026-08-24).

This is a GUARD, not a monitor: it has zero presence in the normal flow.
It is not a background task, does not poll, and holds no state between
calls. The only thing that ever calls post() is code already inside an
`except` block — i.e. code that only runs once something has already
gone wrong. On every successful match/queue/approval, this module is
never touched.

If posting to #botlog itself fails (Discord API hiccup while trying to
report an error), that failure is swallowed and falls back to the normal
Python logger. A guard that raises while reporting a crash would make
things worse than having no guard at all — this must never be able to
cascade into a second, unrelated failure.

Usage (from inside an except block only):

    from utils import incident_log

    except Exception as exc:
        await incident_log.post(
            bot,
            category="MATCH_OCR_FAIL",
            summary="OCR/extraction raised an exception during submission",
            exc=exc,
            match=match,              # optional, dict with match_id/id
            players=[(ign, discord_id), ...],  # optional
        )

Category naming: QUEUE_* for cogs/queue.py, MATCH_* for cogs/match.py —
namespaced by prefix so the channel stays legible and a category alone
tells you which cog it came from, without unifying unrelated failure
kinds under one meaningless bucket. Categories are plain strings, not an
enum — new ones can be added at any call site without touching this file.
"""

import logging
import discord

import config

logger = logging.getLogger(__name__)


async def post(
    bot: discord.Client,
    *,
    category: str,
    summary: str,
    exc: Exception | None = None,
    match: dict | None = None,
    players: list[tuple[str, int]] | None = None,
) -> None:
    """Post one incident to #botlog. Never raises — falls back to the
    console logger on any failure, including a missing/unconfigured
    channel, so a logging problem can never cascade into a second crash
    on top of whatever except block called this."""
    # Always land in the console logger regardless of whether the Discord
    # post below succeeds — console is the guaranteed fallback, never
    # the primary, so this line runs unconditionally rather than only
    # inside the except below.
    logger.error(
        "[%s] %s%s",
        category, summary,
        f" | exc={exc!r}" if exc is not None else "",
    )

    if not config.BOTLOG_CHANNEL_ID:
        # Same fail-open pattern as AFK_CHANNEL_ID / MATCH_LOG_CHANNEL_ID —
        # channel not configured yet, console log above is sufficient,
        # do not block or warn further.
        return

    try:
        channel = bot.get_channel(config.BOTLOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(config.BOTLOG_CHANNEL_ID)

        lines = [f"**[{category}]** {summary}"]
        if match:
            match_code = match.get("match_code") or match.get("id") or "?"
            match_id = match.get("id", "?")
            lines.append(f"Match: `{match_code}` (id=`{match_id}`)")
        if players:
            player_bits = ", ".join(f"{ign} (`{did}`)" for ign, did in players)
            lines.append(f"Player(s): {player_bits}")
        if exc is not None:
            lines.append(f"Error: `{exc!r}`")
        lines.append(f"Time: {discord.utils.utcnow().isoformat()}")

        await channel.send(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        # Posting to Discord failed (permissions, bad channel ID, API
        # outage, etc.) — already logged to console above, so just note
        # the secondary failure and stop. Never re-raise: this function
        # is called from inside except blocks, and letting a logging
        # failure escape would blow past whatever error handling the
        # caller was already in the middle of.
        logger.exception("incident_log.post: failed to post to #botlog channel")
