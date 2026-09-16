"""Shared timestamp helpers.

iso_to_ts() normalizes a Postgres/Supabase timestamp string for
Python 3.10's datetime.fromisoformat(), which — unlike 3.11+ — rejects
the exact shape Postgres returns: space-separated (not 'T') and a
colonless UTC offset ("+00" not "+00:00"). This project runs 3.10
(confirmed via Windows traceback paths in prior sessions), so this
normalization is required at every call site that parses a DB
timestamp string, not optional.

This exact bug crashed a live shield-active check uncaught on
2026-09-07 (see engineering-rules: "grep for fromisoformat before
adding new timestamp handling"). cogs/points.py's `_iso_to_ts` and
database/db.py's `get_active_shield`/`get_all_active_shields` each
carry their own inline copy of this same normalization, predating
this shared version — not consolidated here to avoid touching
already-verified working code for no functional gain. New call sites
(migration_036 onward) should use this one instead of adding a fourth
copy.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger("champions_queue")


def iso_to_ts(iso_str: str | None) -> int:
    """Convert an ISO datetime string (as returned by Supabase/Postgres)
    to a Unix timestamp, for Discord's <t:...> formatting.

    Falls back to 0 (renders as 1 Jan 1970 in Discord) only if truly
    unparseable — but logs when that happens, since a silent fallback
    here means a wrong date gets shown to real people without anyone
    knowing why."""
    if not iso_str:
        logger.warning("iso_to_ts called with empty/None iso_str — will render as 1970 epoch")
        return 0
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        cleaned = cleaned.replace(" ", "T", 1)  # Postgres space-separator -> ISO 'T'
        cleaned = re.sub(r'([+-]\d{2})$', r'\1:00', cleaned)  # colonless offset -> colon
        dt = datetime.fromisoformat(cleaned)
        return int(dt.timestamp())
    except (ValueError, AttributeError, TypeError) as exc:
        logger.warning("iso_to_ts failed to parse %r — falling back to 1970 epoch: %s", iso_str, exc)
        return 0
