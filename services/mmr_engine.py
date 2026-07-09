"""
MMR engine. Win/Loss is the dominant factor by design (per the product
doc: "Winning remains the most important factor"); everything else is a
smaller modifier around that base, computed relative to the player's
OWN team average that match so a stacked/weak lobby doesn't distort
individual credit.
"""

from __future__ import annotations
from typing import Any

import config


def calculate_mmr_change(player_stat: dict, team_avg: dict, won: bool, is_mvp: bool) -> int:
    """
    player_stat: {kills, deaths, damage, hill_time, impact}
    team_avg:    {damage, hill_time, impact} — average of the player's own team that match
    """
    base = config.MMR_WIN_BASE if won else config.MMR_LOSS_BASE

    kills = player_stat.get("kills") or 0
    deaths = max(player_stat.get("deaths") or 0, 1)  # avoid div by zero
    kd = kills / deaths

    damage_delta = (player_stat.get("damage") or 0) - (team_avg.get("damage") or 0)
    hill_delta = float(player_stat.get("hill_time") or 0) - float(team_avg.get("hill_time") or 0)
    impact_delta = float(player_stat.get("impact") or 0) - float(team_avg.get("impact") or 0)
    kd_delta = kd - 1.0

    modifier = (
        damage_delta * config.MMR_DAMAGE_WEIGHT
        + hill_delta * config.MMR_HILL_TIME_WEIGHT
        + impact_delta * config.MMR_IMPACT_WEIGHT
        + kd_delta * config.MMR_KD_WEIGHT
    )

    mvp_bonus = config.MMR_MVP_BONUS if is_mvp else 0

    total = base + modifier + mvp_bonus

    # Clamp a single match's swing so one outlier game can't be catastrophic.
    total = max(-40, min(40, total))
    return round(total)


def team_average(stats: list[dict]) -> dict[str, float]:
    if not stats:
        return {"damage": 0, "hill_time": 0, "impact": 0}
    n = len(stats)
    return {
        "damage": sum((s.get("damage") or 0) for s in stats) / n,
        "hill_time": sum(float(s.get("hill_time") or 0) for s in stats) / n,
        "impact": sum(float(s.get("impact") or 0) for s in stats) / n,
    }


def derive_rank(mmr: int) -> tuple[str, str]:
    """
    Maps raw MMR to (tier, division). Thresholds below are a starting
    point — tune once real MMR distributions exist post-bootstrap.
    """
    thresholds = [
        ("Elite", 0),
        ("PRO", 1000),
        ("Master", 1300),
        ("Grandmaster", 1600),
        ("Legendary", 1900),
        ("Titans", 2200),
    ]
    tier = thresholds[0][0]
    for name, floor in thresholds:
        if mmr >= floor:
            tier = name
    # crude division split: top half of a tier's band = division I
    tier_index = [t[0] for t in thresholds].index(tier)
    floor = thresholds[tier_index][1]
    ceiling = thresholds[tier_index + 1][1] if tier_index + 1 < len(thresholds) else floor + 300
    division = "I" if mmr >= floor + (ceiling - floor) / 2 else "II"
    return tier, division
