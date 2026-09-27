"""
Runtime feature switches — standalone true/false toggles for optional
bot behavior.

Scope, deliberately narrow: this file is ONLY for flat, independent
on/off flags. It is NOT for:
  - config values (channel IDs, thresholds, weights, cooldowns) — those
    stay in config.py.
  - per-item registries where "active" is one field among several on a
    bigger record (e.g. QUEUES, SHIELD_TIERS in config.py) — those stay
    in config.py as data, since the flag there describes one attribute
    of a thing, not a standalone switch.

Moved out of config.py on 2026-09-27 (see engineering-rules.md / that
session's chat for the reasoning): config.py had been accumulating both
shapes under one file, and it was worth splitting them out once a
second unrelated flag needed a home.
"""
import os

# Per-match VCs (2026-09): gated behind this switch. Default OFF (no VCs
# created for the next queue), flip to True to restore old behavior with
# zero other code changes needed. Every downstream read of
# voice_channel_a_id/voice_channel_b_id (cleanup, /admin-scrap-match,
# /admin-swap-player) already treats a missing/None VC id as a normal
# no-op, so turning creation off here doesn't require touching anything
# else. Read at startup — needs a bot restart to change.
CREATE_MATCH_VOICE_CHANNELS = os.getenv("CREATE_MATCH_VOICE_CHANNELS", "false").strip().lower() == "true"

# Operator-skill votes: whether picks are written to operator_skill_votes.
# Default ON (today's behaviour). Nothing in the bot reads that table yet,
# so turning it OFF is safe: buttons still lock and show picks, the DB
# just gets no vote writes (saves ~2 writes per match). Read at startup,
# so changing it needs a bot restart. Accepts true/false.
STORE_SKILL_VOTES = os.getenv("STORE_SKILL_VOTES", "true").strip().lower() == "true"
