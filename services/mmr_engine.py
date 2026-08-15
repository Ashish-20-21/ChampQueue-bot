"""Position-table MMR calculation for one match (RO1: one round per match)."""

from __future__ import annotations

_WINNING_DELTAS = {1: 9, 2: 8, 3: 6, 4: 4, 5: 3}
_LOSING_DELTAS = {1: -3, 2: -4, 3: -6, 4: -8, 5: -9}

# Ascending tier ladder — (floor_mmr, tier_name), lowest first. This is the
# single list `derive_rank()` and `next_tier_progress()` both read from, so
# adding/renaming a tier only requires editing this one tuple. NOTE: the SQL
# CASE chain in database/migration_015_ro1.sql's approve_match() is a
# hand-synced duplicate of these same floors (see derive_rank's docstring) —
# any change here must also land there in the same commit.
_TIER_LADDER = (
    (0, "Elite1"),
    (201, "Elite2"),
    (401, "PRO1"),
    (601, "PRO2"),
    (801, "Master1"),
    (1001, "Master2"),
    (1201, "Grandmaster1"),
    (1401, "Grandmaster2"),
    (1601, "Legendary1"),
    (1801, "Legendary2"),
    (2001, "Titans"),
)


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
    chain inside approve_match() in
    database/migration_015_ro1.sql (carried over verbatim from
    migration_012_rank_band_widen_and_global_reset.sql's
    approve_ro3_match — only the row-count assertion changed for RO1,
    the CASE chain itself is untouched). That SQL function is the one
    that actually writes players.current_rank / peak_rank — this
    Python function is used for display purposes elsewhere (e.g.
    /rank-progress) and is not itself
    read by the approval path. There is no single source of truth at
    the code level; if you change one, change the other in the same
    commit. 200-point bands, confirmed 2026-07-30 (was 150-point bands
    through the unified-region-test session; 100-point bands through
    P5) — widened as part of the esports -> global transition, alongside
    a one-time reset of every existing player's MMR to 200.
    """
    mmr = max(0, mmr)
    for floor, tier in reversed(_TIER_LADDER):
        if mmr >= floor:
            # Keep the established two-value interface without inventing a
            # second division for names that already include their level.
            return tier, ""
    raise AssertionError("unreachable")


def next_tier_progress(mmr: int) -> tuple[str, int] | None:
    """Return (next_tier_name, mmr_still_needed) for the player's current
    MMR, or None if they're already at the top tier (Titans has no ceiling).

    Distance is always to the *immediately next* tier, never a far-off one —
    e.g. a 190-MMR player 8 points under Elite2 sees "8 to Elite2", not a
    discouraging "1811 to Legendary2". Deliberate product choice, not a
    simplification: see the 2026-08 /rank-progress design discussion.
    """
    mmr = max(0, mmr)
    for i, (floor, _tier) in enumerate(_TIER_LADDER):
        if mmr < floor:
            return _TIER_LADDER[i][1], floor - mmr
    return None  # already in the top tier (Titans)


def tier_ladder() -> tuple[tuple[int, str], ...]:
    """Expose the ladder read-only for display purposes (e.g. /rank-progress'
    full-ladder view). Callers should not mutate or re-derive tier floors
    from anywhere else — this is the single source of truth on the Python
    side (see module docstring on _TIER_LADDER for the SQL-side duplicate)."""
    return _TIER_LADDER
