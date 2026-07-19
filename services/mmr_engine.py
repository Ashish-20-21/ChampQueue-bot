"""Position-table MMR calculation for one RO3 round."""

from __future__ import annotations

_WINNING_DELTAS = {1: 9, 2: 8, 3: 6, 4: 4, 5: 3}
_LOSING_DELTAS = {1: -3, 2: -4, 3: -6, 4: -8, 5: -9}


def calculate_mmr_change(position: int, won: bool, is_mvp: bool) -> int:
    """Return the MMR change for one player in one round.

    ``position`` and ``is_mvp`` are game-provided scoreboard values; this
    function intentionally does not derive either from other stat columns.
    """
    if position not in _WINNING_DELTAS:
        raise ValueError("position must be an integer from 1 through 5")
    delta = (_WINNING_DELTAS if won else _LOSING_DELTAS)[position]
    return delta + (5 if is_mvp else 0)


def derive_rank(mmr: int) -> tuple[str, str]:
    """Map non-negative MMR to the confirmed player-facing rank tier.

    IMPORTANT: this must stay in sync BY HAND with the identical CASE
    chain inside approve_ro3_match() in
    database/migration_006_p6_stats_and_ranks.sql. That SQL function is
    the one that actually writes players.current_rank / peak_rank — this
    Python function is used for display purposes elsewhere (e.g.
    /rank-progress) and is not itself read by the approval path. There is
    no single source of truth at the code level; if you change one,
    change the other in the same commit. 150-point bands, confirmed
    2026-07-19 (was 100-point bands through P5).
    """
    mmr = max(0, mmr)
    tiers = (
        (1501, "Titans"),
        (1351, "Legendary2"),
        (1201, "Legendary1"),
        (1051, "Grandmaster2"),
        (901, "Grandmaster1"),
        (751, "Master2"),
        (601, "Master1"),
        (451, "PRO2"),
        (301, "PRO1"),
        (151, "Elite2"),
        (0, "Elite1"),
    )
    for floor, tier in tiers:
        if mmr >= floor:
            # Keep the established two-value interface without inventing a
            # second division for names that already include their level.
            return tier, ""
    raise AssertionError("unreachable")