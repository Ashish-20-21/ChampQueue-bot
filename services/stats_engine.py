"""
Runs after a match is confirmed 'completed'. Recomputes each player's
denormalized career aggregates (kept on the players row for fast profile
reads) and checks achievement conditions.
"""

from __future__ import annotations

from database.db import adb
from services import mmr_engine


async def recompute_career_stats(player_id: int) -> dict:
    history = await adb.player_recent_matches(player_id, limit=10_000)  # all matches
    completed = [h for h in history if h.get("matches", {}).get("status") == "completed"]
    if not completed:
        return await adb.get_player_by_id(player_id)

    total = len(completed)
    wins = sum(1 for h in completed if h["team"] == h["matches"]["winner_team"])
    losses = total - wins
    mvp_count = sum(1 for h in completed if h.get("is_mvp"))

    def avg(field):
        vals = [h.get(field) for h in completed if h.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0

    fields = {
        "total_matches": total,
        "wins": wins,
        "losses": losses,
        "mvp_count": mvp_count,
        "avg_kills": avg("kills"),
        "avg_deaths": avg("deaths"),
        "avg_damage": avg("damage"),
        "avg_hill_time": avg("hill_time"),
    }
    return await adb.update_player_fields(player_id, fields)


async def update_rank(player_id: int) -> dict:
    player = await adb.get_player_by_id(player_id)
    tier, division = mmr_engine.derive_rank(player["mmr"])
    fields = {"current_rank": tier, "current_division": division}
    if player["mmr"] > player["peak_mmr"]:
        fields["peak_mmr"] = player["mmr"]
        fields["peak_rank"] = tier
    return await adb.update_player_fields(player_id, fields)


async def check_general_achievements(player_id: int) -> list[str]:
    player = await adb.get_player_by_id(player_id)
    granted = []
    if player["wins"] >= 1:
        if await adb.grant_achievement(player_id, "first_win"):
            granted.append("first_win")
    if player["total_matches"] >= 100:
        if await adb.grant_achievement(player_id, "matches_100"):
            granted.append("matches_100")
    total_kills = round(player["avg_kills"] * player["total_matches"])
    if total_kills >= 500:
        if await adb.grant_achievement(player_id, "kills_500"):
            granted.append("kills_500")
    return granted


async def check_streak_achievements(player_id: int) -> list[str]:
    history = await adb.player_recent_matches(player_id, limit=10)
    completed = [h for h in history if h.get("matches", {}).get("status") == "completed"]
    granted = []

    # win streak
    win_streak = 0
    for h in completed:
        if h["team"] == h["matches"]["winner_team"]:
            win_streak += 1
        else:
            break
    if win_streak >= 10 and await adb.grant_achievement(player_id, "win_streak_10"):
        granted.append("win_streak_10")

    # MVP streak
    mvp_streak = 0
    for h in completed:
        if h.get("is_mvp"):
            mvp_streak += 1
        else:
            break
    if mvp_streak >= 3 and await adb.grant_achievement(player_id, "mvp_streak"):
        granted.append("mvp_streak")

    # positive KD streak (5 games)
    pos_kd_streak = 0
    for h in completed:
        deaths = max(h.get("deaths") or 0, 1)
        if (h.get("kills") or 0) / deaths > 1.0:
            pos_kd_streak += 1
        else:
            break
    if pos_kd_streak >= 5 and await adb.grant_achievement(player_id, "positive_kd_streak"):
        granted.append("positive_kd_streak")

    return granted


async def process_post_match(player_id: int) -> dict:
    """Call this once per player after a match is finalized."""
    await recompute_career_stats(player_id)
    await update_rank(player_id)
    general = await check_general_achievements(player_id)
    streaks = await check_streak_achievements(player_id)
    return {"new_achievements": general + streaks}
