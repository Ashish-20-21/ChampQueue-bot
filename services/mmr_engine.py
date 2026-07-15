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
    """Map non-negative MMR to the confirmed player-facing rank tier."""
    mmr = max(0, mmr)
    tiers = (
        (1000, "Titans"),
        (900, "Legendary2"),
        (800, "Legendary1"),
        (700, "Grandmaster2"),
        (600, "Grandmaster1"),
        (500, "Master2"),
        (400, "Master1"),
        (300, "PRO2"),
        (200, "PRO1"),
        (100, "Elite2"),
        (0, "Elite1"),
    )
    for floor, tier in tiers:
        if mmr >= floor:
            # Keep the established two-value interface without inventing a
            # second division for names that already include their level.
            return tier, ""
    raise AssertionError("unreachable")
