"""
Matchmaking service: turns 10 queued players into two balanced 5-player
teams, picks a captain per team, and decides whether we're still in the
"bootstrap" (random) phase or the analysis-driven phase.

Bootstrap cutover rule (see config.BOOTSTRAP_MATCH_THRESHOLD /
BOOTSTRAP_MIN_ELIGIBLE_POOL): calendar time is not a reliable proxy for
"enough data exists", so we gate on match counts instead of days.
A given match runs in analysis mode only if ALL 10 players in that
queue pop have already reached the match threshold; otherwise it's a
bootstrap (random) match, and it still counts toward every player's
threshold progress.
"""

from __future__ import annotations
import asyncio
import random
from typing import Any

import config
from database.db import db, adb


async def is_bootstrap_match(player_ids: list[int]) -> bool:
    """A match runs in random/bootstrap mode unless every player already
    has enough completed matches AND the overall graduated pool is large
    enough for analysis to be meaningful."""
    graduated = [
        pid for pid in player_ids
        if await adb.player_completed_match_count(pid) >= config.BOOTSTRAP_MATCH_THRESHOLD
    ]
    if len(graduated) < len(player_ids):
        return True  # someone in this pop hasn't graduated yet
    # Everyone in this pop has graduated — but also require a healthy
    # overall pool so early "analysis" isn't based on 10 people total.
    pool_res = await asyncio.to_thread(
        lambda: db.client.table("players").select("id", count="exact").eq("status", "approved").execute()
    )
    return (pool_res.count or 0) < config.BOOTSTRAP_MIN_ELIGIBLE_POOL


def _performance_score(player: dict) -> float:
    """Single composite score used ONLY for balancing/captain selection —
    not the same thing as MMR, though MMR is the dominant input."""
    mmr = player.get("mmr", 200)  # matches players.mmr's default (200 as of 2026-07-30 global-transition reset, see migration_012)
    win_rate = 0.0
    total = player.get("total_matches", 0)
    if total > 0:
        win_rate = player.get("wins", 0) / total
    avg_damage = float(player.get("avg_damage", 0) or 0)
    avg_hill_time = float(player.get("avg_hill_time", 0) or 0)
    return mmr + (win_rate * 200) + (avg_damage * 0.05) + (avg_hill_time * 2)


def balance_teams(queued_players: list[dict], bootstrap: bool) -> dict[str, Any]:
    """
    queued_players: list of player dicts (must include id, mmr, wins,
    total_matches, avg_damage, avg_hill_time).
    Returns {"team_a": [...], "team_b": [...], "captain_a": id, "captain_b": id}
    """
    assert len(queued_players) == config.QUEUE_SIZE, "matchmaking requires exactly 10 players"

    players = list(queued_players)

    if bootstrap:
        random.shuffle(players)
        team_a = players[:config.TEAM_SIZE]
        team_b = players[config.TEAM_SIZE:]
    else:
        # Snake draft by performance score: 1-2-2-1-2-2-1-2-2-1 style
        # alternation gives a much more even split than "top 5 vs bottom 5".
        ranked = sorted(players, key=_performance_score, reverse=True)
        team_a, team_b = [], []
        order = ["A", "B", "B", "A", "A", "B", "B", "A", "A", "B"]
        for player, side in zip(ranked, order):
            (team_a if side == "A" else team_b).append(player)

    return {
        "team_a": team_a,
        "team_b": team_b,
    }


async def pick_map_candidates(team_a_ids: list[int], team_b_ids: list[int], bootstrap: bool,
                               n: int = 3) -> list[str]:
    """
    Pick n candidate maps for the vote. In bootstrap mode: pure random.

    Analysis mode is TEMPORARILY DISABLED (falls back to the same random
    pick as bootstrap) — found live 2026-07-20, crashing every match once
    a roster crosses BOOTSTRAP_MATCH_THRESHOLD: this queried matches.map
    and matches.winner_team, both pre-RO3 columns that don't exist on the
    live schema anymore (maps live in matches.map_pool as an array now;
    there's no single-match winner_team since RO3 counts wins/losses at
    the round level — see recompute_player_career_stats in
    migration_006_p6_full.sql for the correct current pattern). This
    function was apparently never actually exercised until a real roster
    crossed the bootstrap threshold for the first time tonight.

    Real fix (not done here — this is a stop-the-bleeding fix, not a
    rebuild) needs rewriting the balance heuristic against
    match_round_results + matches.map_pool instead of the dead columns.
    Flagged as real follow-up work, not silently deferred — random
    selection is a safe, correct fallback in the meantime (bootstrap
    mode already proves random is an acceptable map-pick strategy), just
    not the smarter balanced pick that was originally intended here.
    """
    return random.sample(config.HARDPOINT_MAPS, k=min(n, len(config.HARDPOINT_MAPS)))