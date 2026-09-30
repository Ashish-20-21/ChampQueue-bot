"""
Decides whether an extracted scoreboard can be auto-accepted or must be
routed to admin review. Two independent triggers, either one forces review:

  1. Statistical outlier: any player's extracted stat is more than
     config.STAT_OUTLIER_STD_DEVS away from THAT PLAYER'S OWN rolling
     history (not the lobby average — a player's personal baseline is a
     much stronger signal than comparing across different skill levels).
Either trigger sets match.status = 'awaiting_review' instead of being
eligible for host approval.
"""

from __future__ import annotations
import asyncio
import statistics
from typing import Any

import config
from database.db import adb, with_retry


async def check_stat_outliers(player_id: int, new_stats: dict) -> list[str]:
    """Returns a list of human-readable flags for any field that's an outlier
    versus this player's own history. Empty list = nothing suspicious."""
    history = await with_retry(adb.player_recent_matches, player_id, limit=15)
    if len(history) < 5:
        return []  # not enough history to judge yet — don't false-flag new players

    flags = []
    for field in ("kills", "deaths", "damage", "hill_time"):
        past_values = [h.get(field) for h in history if h.get(field) is not None]
        if len(past_values) < 5:
            continue
        mean = statistics.mean(past_values)
        stdev = statistics.pstdev(past_values) or 1.0
        new_value = new_stats.get(field)
        if new_value is None:
            continue
        z = abs((new_value - mean) / stdev)
        if z > config.STAT_OUTLIER_STD_DEVS:
            flags.append(f"{field}={new_value} is {z:.1f} std devs from this player's average ({mean:.1f})")
    return flags


# How many players' history lookups run at the same time. The old loop asked
# for them one after another (10 round trips in a row, ~3 s in the Sep 2026
# logs); firing all 10 at once would recreate the simultaneous burst on the
# shared HTTP/2 connection that branch 8 exists to remove. 3 at a time is the
# middle: ~4 short rounds instead of 10, never a burst.
HISTORY_LOOKUP_CONCURRENCY = 3


async def validate_submission(match_id: int, extraction: dict, player_votes: list[dict] | None = None,
                              match_players: list[dict] | None = None) -> dict[str, Any]:
    """
    match_players: pass the roster the caller already loaded to skip a second
    identical get_match_players round trip. None = load it here (old behaviour).

    Returns:
        {"auto_accept": bool, "flags": {player_id: [flag strings]}}
    """
    all_flags: dict[int, list[str]] = {}
    if match_players is None:
        match_players = await with_retry(adb.get_match_players, match_id)
    players_by_ign = {
        candidate.get("players", {}).get("ign", "").strip().lower(): candidate
        for candidate in match_players
    }
    checks: list[tuple[int, dict]] = []
    for p in extraction.get("players", []):
        ign = str(p.get("ign") or "").strip().lower()
        player = players_by_ign.get(ign)
        if not player:
            continue
        checks.append((player["player_id"], p))

    gate = asyncio.Semaphore(HISTORY_LOOKUP_CONCURRENCY)

    async def _check(player_id: int, stats: dict) -> tuple[int, list[str]]:
        async with gate:
            return player_id, await check_stat_outliers(player_id, stats)

    # gather() returns results in input order, so the flags dict is built in
    # the same order the old sequential loop built it.
    for player_id, flags in await asyncio.gather(*(_check(pid, st) for pid, st in checks)):
        if flags:
            all_flags[player_id] = flags

    return {"auto_accept": not all_flags, "flags": all_flags}
