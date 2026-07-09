"""
Decides whether an extracted scoreboard can be auto-accepted or must be
routed to admin review. Two independent triggers, either one forces review:

  1. Statistical outlier: any player's extracted stat is more than
     config.STAT_OUTLIER_STD_DEVS away from THAT PLAYER'S OWN rolling
     history (not the lobby average — a player's personal baseline is a
     much stronger signal than comparing across different skill levels).
  2. Vote mismatch: the winner declared by the in-Discord player vote
     disagrees with the winner implied by the scoreboard.

Either trigger sets match.status = 'awaiting_review' instead of 'completed'.
"""

from __future__ import annotations
import statistics
from typing import Any

import config
from database.db import db


def check_stat_outliers(player_id: int, new_stats: dict) -> list[str]:
    """Returns a list of human-readable flags for any field that's an outlier
    versus this player's own history. Empty list = nothing suspicious."""
    history = db.player_recent_matches(player_id, limit=15)
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


def check_vote_mismatch(match_id: int, scoreboard_winner: str, player_votes: list[dict]) -> bool:
    """Returns True if there's a meaningful mismatch that should block auto-accept."""
    if not config.VOTE_MISMATCH_BLOCKS_AUTO_ACCEPT or not player_votes:
        return False
    disagreeing = [v for v in player_votes if v.get("winner") != scoreboard_winner]
    # Any disagreement at all is enough to force a human look, since the
    # cost of a bad auto-accept (permanent stat record) is high.
    return len(disagreeing) > 0


def validate_submission(match_id: int, extraction: dict, player_votes: list[dict]) -> dict[str, Any]:
    """
    Returns:
        {"auto_accept": bool, "flags": {player_id: [flag strings]}, "vote_mismatch": bool}
    """
    all_flags: dict[int, list[str]] = {}
    for p in extraction.get("players", []):
        player = db.get_player_by_uid(p.get("cod_uid", "")) or db.get_player_by_discord_id(p.get("discord_id", ""))
        if not player:
            continue
        flags = check_stat_outliers(player["id"], p)
        if flags:
            all_flags[player["id"]] = flags

    scoreboard_winner = extraction.get("winner_team")
    vote_mismatch = check_vote_mismatch(match_id, scoreboard_winner, player_votes)

    auto_accept = not all_flags and not vote_mismatch
    return {"auto_accept": auto_accept, "flags": all_flags, "vote_mismatch": vote_mismatch}
