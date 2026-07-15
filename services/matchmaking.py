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
    mmr = player.get("mmr", 1000)
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
    In analysis mode: avoid maps where the historical win rate for either
    team's current roster is heavily lopsided (best-effort — depends on
    match history existing per map, which needs a map-level aggregate;
    for now this uses a simple heuristic over match_players + matches
    and can be swapped for a proper materialized view once volume is
    high enough to justify it).
    """
    if bootstrap:
        return random.sample(config.HARDPOINT_MAPS, k=min(n, len(config.HARDPOINT_MAPS)))

    # Analysis mode: rank maps by how "balanced" recent history has been
    # for these specific players, lowest historical MMR-swing-per-map first.
    scored: list[tuple[str, float]] = []
    for map_name in config.HARDPOINT_MAPS:
        res = await asyncio.to_thread(
            lambda map_name=map_name: (
                db.client.table("matches")
                .select("id, winner_team, match_players(player_id, team)")
                .eq("map", map_name)
                .eq("status", "completed")
                .execute()
            )
        )
        relevant = [
            m for m in res.data
            if any(mp["player_id"] in team_a_ids or mp["player_id"] in team_b_ids for mp in m.get("match_players", []))
        ]
        if not relevant:
            scored.append((map_name, 0.0))  # no data = neutral, still eligible
            continue
        a_wins = sum(1 for m in relevant if m["winner_team"] == "A")
        skew = abs(a_wins - (len(relevant) - a_wins)) / len(relevant)
        scored.append((map_name, skew))

    scored.sort(key=lambda x: x[1])  # most balanced first
    return [m for m, _ in scored[:n]]
