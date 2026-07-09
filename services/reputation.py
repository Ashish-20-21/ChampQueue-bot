"""
Reputation isn't just a number that sits on a profile — it has real
consequences, tiered by config thresholds:

    >= REPUTATION_WARN_THRESHOLD (70)      : normal
    >= REPUTATION_PRIORITY_DROP_THRESHOLD (50) : queued last among simultaneous joiners
    <  REPUTATION_BAN_THRESHOLD (25)       : temporary queue ban, admin must review to lift
"""

from __future__ import annotations

import config
from database.db import db

PENALTIES = {
    "afk": -8,
    "rage_quit": -15,
    "toxicity": -10,
    "fake_submission": -25,
    "match_dodge": -6,
}


def apply_penalty(player_id: int, reason: str, match_id: int | None = None) -> dict:
    if reason not in PENALTIES:
        raise ValueError(f"Unknown reputation penalty reason: {reason}")
    return db.apply_reputation_delta(player_id, PENALTIES[reason], reason, match_id)


def get_status(reputation: int) -> str:
    if reputation < config.REPUTATION_BAN_THRESHOLD:
        return "banned"
    if reputation < config.REPUTATION_PRIORITY_DROP_THRESHOLD:
        return "low_priority"
    if reputation < config.REPUTATION_WARN_THRESHOLD:
        return "warned"
    return "good_standing"


def is_queue_eligible(player: dict) -> tuple[bool, str | None]:
    status = get_status(player["reputation"])
    if status == "banned":
        return False, (
            f"Reputation ({player['reputation']}) is below the queue-ban threshold "
            f"({config.REPUTATION_BAN_THRESHOLD}). An admin must review before you can queue again."
        )
    return True, None


def queue_priority_rank(player: dict) -> int:
    """Lower number = higher priority when multiple players join simultaneously
    and only some spots remain. Low-priority players sort to the back."""
    status = get_status(player["reputation"])
    return 1 if status == "low_priority" else 0
