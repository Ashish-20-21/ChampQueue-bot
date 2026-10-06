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

# Result submission entry points (2026-09-28). Two ways for a host (or an
# admin on their behalf) to submit the match scoreboard:
#   RESULT_TEXT_TRIGGER  -> "+result" typed in the match channel with the
#                           screenshot attached to the SAME message
#   RESULT_SLASH_COMMAND -> /match-submit in the result-upload channel
# Both run the same shared pipeline (Match._run_submission), so either can
# be switched off if it misbehaves and the other keeps working. A switched-
# off entry point tells the host once to use the other one. Default: both
# ON. Read at startup, so changing either needs a bot restart.
RESULT_TEXT_TRIGGER = os.getenv("RESULT_TEXT_TRIGGER", "true").strip().lower() == "true"
RESULT_SLASH_COMMAND = os.getenv("RESULT_SLASH_COMMAND", "true").strip().lower() == "true"

# Guard: both off would leave NO way to submit a result, and every match
# would stall in awaiting_result. That is never what anyone means, so it is
# treated as a config mistake: log it loudly and keep both on.
if not RESULT_TEXT_TRIGGER and not RESULT_SLASH_COMMAND:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "RESULT_TEXT_TRIGGER and RESULT_SLASH_COMMAND are both false — that would "
        "block every result submission, so both stay ON. Fix the env vars."
    )
    RESULT_TEXT_TRIGGER = True
    RESULT_SLASH_COMMAND = True

# Per-interaction timing meter (2026-10): one INTERACTION_TIMING log line per
# button press / slash command / modal submit, showing where the time went
# (see utils/interaction_timer.py). Read-only: it only reads clocks and logs.
# Default ON. Set INTERACTION_TIMING=false and restart to turn it off (nothing
# is patched then). Read at startup.
INTERACTION_TIMING = os.getenv("INTERACTION_TIMING", "true").strip().lower() == "true"

# Nightly maintenance-restart notice (2026-10). Only controls the BOT's
# messages (the "nap" notice, the edit back to "refreshed", the skipped-
# restart check, the #botlog boot line). The restart itself is a Katabump
# schedule, so to stop the restarts switch that schedule off in the panel.
# Default OFF: deploying the code changes nothing until this is "true" and
# MAINTENANCE_CHANNEL_ID is set. Read at startup, so a change needs a restart.
NIGHTLY_RESTART_NOTICES = os.getenv("NIGHTLY_RESTART_NOTICES", "false").strip().lower() == "true"
