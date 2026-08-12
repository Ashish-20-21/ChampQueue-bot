"""
Central configuration for Champion's Queue.
All secrets come from environment variables — never hardcode tokens/keys here.

Required env vars (put these in a .env file, see .env.example):
    DISCORD_BOT_TOKEN
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    VISION_PROVIDER            one of: "anthropic" (default), "openai", "qwen"
    ANTHROPIC_API_KEY          required if VISION_PROVIDER=anthropic
    OPENAI_API_KEY             required if VISION_PROVIDER=openai
    QWEN_API_KEY               required if VISION_PROVIDER=qwen
    ADMIN_ROLE_IDS             Comma-separated Discord role IDs allowed to approve players,
                               review flagged matches, and (as of the unified-region launch)
                               upload scoreboards on a host's behalf. Multiple roles supported
                               so HOD + admin team all carry identical full admin power — see
                               DECISIONS.md 2026-07-29. Example: "111111111,222222222"
    GUILD_ID                   Discord server (guild) ID the bot operates in
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


# --- Discord ---
DISCORD_BOT_TOKEN = _require("DISCORD_BOT_TOKEN")
GUILD_ID = int(_require("GUILD_ID"))

# Unified 2026-07-29: was a single ADMIN_ROLE_ID. HOD + admin team both need
# identical full admin power (approve/reject players, force-approve matches,
# AND — new as of this change — upload scoreboards on a host's behalf), so
# this is now a set of role IDs rather than one. Comma-separated in .env;
# whitespace around commas is stripped so "111, 222" and "111,222" both work.
# Old single-role .env files break loudly here (KeyError on ADMIN_ROLE_ID
# elsewhere) rather than silently — see migration/deploy notes in DECISIONS.md
# for the required .env rename from ADMIN_ROLE_ID to ADMIN_ROLE_IDS.
ADMIN_ROLE_IDS = {int(x.strip()) for x in _require("ADMIN_ROLE_IDS").split(",") if x.strip()}

# --- P4: AFK reporting + match-log channel ---
# Both channels are created manually in Discord (bot doesn't create them) —
# grab each channel's ID and set it here or in .env. Optional at import time
# (default None) so the bot doesn't crash on boot if these haven't been
# created yet; the features that need them just no-op with a log warning
# until they're set.
AFK_CHANNEL_ID = int(os.getenv("AFK_CHANNEL_ID")) if os.getenv("AFK_CHANNEL_ID") else None
MATCH_LOG_CHANNEL_ID = int(os.getenv("MATCH_LOG_CHANNEL_ID")) if os.getenv("MATCH_LOG_CHANNEL_ID") else None

# --- Unified global region + 4-queue matchmaking (2026-07-29) ---
# Server moved to one unified show with four ticket-counter queues, kept
# separate for matchmaking throughput / ping reasons only. Everything
# downstream of "a match happened" collapses into ONE pool: one upload
# channel, one approval channel, one match-log (already was), one
# leaderboard. QUEUE_KEYS is the source of truth for which 4 queues
# exist — used for queue-post buttons, per-queue locks, and validating
# /queue-post's region argument. This is DELIBERATELY separate from
# players.region (see REGIONS below) — queue_key is "which physical
# queue is this match/entry in", region is "informational label the
# player picked at registration", and the whole point of this change is
# that the two no longer have to match.
QUEUE_KEYS = ["EU_AF", "NA_LATAM", "INDIA_ME", "JAPAN"]

# Registration-time region label. Informational only as of this change —
# never read by queue/match/channel logic. Old East/West values are left
# alone in the DB (see migration_010) and intentionally still valid here
# so nothing chokes on historical data; only NEW registrations should use
# the 4 new values going forward.
REGIONS = ["East", "West", "EU_AF", "NA_LATAM", "INDIA_ME", "JAPAN"]

# --- P5/P6: result upload + approval channels ---
# Unified 2026-07-29 — was per-region (_EAST/_WEST dict) as of P6, keyed
# off the match's own `region` column. That split existed only because
# East/West were role-gated into separate channel visibility; the new
# unified-region design explicitly wants ONE upload channel and ONE
# approval channel for all 4 queues, so the per-region dict is gone.
# If RESULT_UPLOAD_CHANNEL_ID / RESULT_APPROVAL_CHANNEL_ID aren't set,
# the features that need them no-op with a log warning (same fail-open
# pattern as AFK_CHANNEL_ID / MATCH_LOG_CHANNEL_ID above) rather than
# crashing the bot on boot.
RESULT_UPLOAD_CHANNEL_ID = int(os.getenv("RESULT_UPLOAD_CHANNEL_ID")) if os.getenv("RESULT_UPLOAD_CHANNEL_ID") else None
RESULT_APPROVAL_CHANNEL_ID = int(os.getenv("RESULT_APPROVAL_CHANNEL_ID")) if os.getenv("RESULT_APPROVAL_CHANNEL_ID") else None

# --- Priority fixes: admin IGN-change channel ---
# 2026-08-08: players frequently change their in-game name after queueing
# (sometimes mid-match), and OCR/roster mismatches were traced back to this
# more than once. registration.py deliberately dropped self-service
# /update-ign (see its own comment) in favor of admin-only correction here.
# Same fail-open pattern as above: if unset, /admin-ign-change works from
# any channel rather than crashing the bot on boot.
IGN_CHANGE_CHANNEL_ID = int(os.getenv("IGN_CHANGE_CHANNEL_ID")) if os.getenv("IGN_CHANGE_CHANNEL_ID") else None

# --- Correction/review system ---
# Intake: new match_issues rows post here (OCR failures routed automatically,
# plus anything filed via /correction-result). Outbound: resolutions get
# logged here with the player @mention, separate from intake so the two
# don't get mixed together in one scrolling feed.
ISSUE_INTAKE_CHANNEL_ID = int(os.getenv("ISSUE_INTAKE_CHANNEL_ID")) if os.getenv("ISSUE_INTAKE_CHANNEL_ID") else None
ISSUE_RESOLVED_CHANNEL_ID = int(os.getenv("ISSUE_RESOLVED_CHANNEL_ID")) if os.getenv("ISSUE_RESOLVED_CHANNEL_ID") else None

# How long a host has to Approve before the sweep auto-approves for them.
# Deliberately short (not the old 3600s) — auto-approve exists as a
# guardrail against a host going AFK after upload, not as the normal path.
# An open match_issues row blocks both manual and auto approval either way,
# so filing a correction is never raced by this timer.
APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes
APPROVAL_SWEEP_INTERVAL_SECONDS = 30  # how often the sweep checks for overdue matches

# How often a host/player can re-invoke /correction-result on the same
# match — prevents spam/duplicate filing if multiple players in the same
# match try to flag something around the same time.
CORRECTION_COMMAND_COOLDOWN_SECONDS = 8

# How often the abandoned/completed-match channel cleanup sweep runs.
# Independent of the 1hr cleanup delay itself — this just controls how
# often the bot checks "is anything due yet".
CLEANUP_SWEEP_INTERVAL_MINUTES = 5
MATCH_CHANNEL_CLEANUP_DELAY_SECONDS = 3600   # 1hr grace window before deletion

# --- Supabase / Postgres ---
SUPABASE_URL = _require("SUPABASE_URL")
SUPABASE_SERVICE_KEY = _require("SUPABASE_SERVICE_KEY")

# --- Vision AI (swappable provider, see services/vision_extraction.py) ---
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "anthropic").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-5.4-mini")
# gpt-5.4-mini chosen after live A/B testing against gpt-4.1-nano (2026-07-17):
# nano showed a column-mapping bug (damage/score/impact values swapped when
# a Damage column wasn't present) and a Simzy/Simpy-style IGN misread that
# recurred across multiple screenshots. gpt-5.4-mini, single-pass, matched
# ground truth exactly (score/impact/kills/deaths/hill_time) across 3
# separate real screenshots with zero numeric errors. Don't downgrade this
# default without re-running that comparison.
QWEN_API_KEY = os.getenv("QWEN_API_KEY")
NVIDIA_NIM_API_KEY = os.getenv("NVIDIA_NIM_API_KEY")
NVIDIA_NIM_BASE_URL = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com")

# --- Queue / Matchmaking ---
QUEUE_SIZE = 10
TEAM_SIZE = 5
VOTE_TIMEOUT_SECONDS = 120           # timeout for operator-skill / map votes before fallback
AFK_REQUEUE_PENALTY = -5             # reputation hit for not confirming in time


# Bootstrap phase: use pure-random team/map assignment until a player
# has logged at least this many official matches. Once the *pool* of
# players with >= this many matches is large enough, analysis-based
# assignment kicks in for that pool. Match-count based, NOT calendar-day based.
BOOTSTRAP_MATCH_THRESHOLD = 10
BOOTSTRAP_MIN_ELIGIBLE_POOL = 20     # need at least this many "graduated" players before analysis mode goes live

# --- MMR weights (tunable; win/loss dominates by design) ---
MMR_WIN_BASE = 25
MMR_LOSS_BASE = -20
MMR_DAMAGE_WEIGHT = 0.01     # per point of damage above/below team average
MMR_HILL_TIME_WEIGHT = 0.15  # per second of hill time above/below team average
MMR_IMPACT_WEIGHT = 0.5      # per point of impact score above/below team average
MMR_KD_WEIGHT = 3.0          # per 1.0 of KD above/below 1.0
MMR_MVP_BONUS = 10

# --- Reputation thresholds -> consequences ---
REPUTATION_WARN_THRESHOLD = 70
REPUTATION_PRIORITY_DROP_THRESHOLD = 50   # queued last among simultaneous joiners
REPUTATION_BAN_THRESHOLD = 25             # temporary queue ban, admin must review

# --- Abuse / cost-control guards ---
MAX_SCOREBOARD_UPLOAD_BYTES = 8 * 1024 * 1024   # 8MB cap before sending to Vision AI (cost + DoS guard)
# NOTE: registration cooldown was removed entirely (DECISIONS.md — final,
# 2026-07-13). REGISTER_COOLDOWN_SECONDS deliberately deleted here since it
# was dead/unused in registration.py; don't re-add without re-opening that decision.
QUEUE_JOIN_COOLDOWN_SECONDS = 10                 # per-user cooldown on /queue-join (spam guard)

# --- Suspicious-submission thresholds (route to admin review instead of auto-accept) ---
STAT_OUTLIER_STD_DEVS = 2.5       # flag any player stat more than N std devs from their own rolling average
VOTE_MISMATCH_BLOCKS_AUTO_ACCEPT = True  # if player winner-vote disagrees with scoreboard winner, force review

# --- Ranks ---
RANK_TIERS = ["Elite", "PRO", "Master", "Grandmaster", "Legendary", "Titans"]
RANK_DIVISIONS = ["II", "I"]

# --- Official competitive Hardpoint maps ---
# 1 of these is picked per match (see matchmaking.pick_map_candidates) —
# there is NO player vote on this list, it's announced as a fixed embed
# ("Map: X"). See cogs/queue.py's map announcement step and
# matches.map_pool in the schema (still stored as a 1-element array,
# not a plain string — see migration_014_ro1.sql).
HARDPOINT_MAPS = [
    "Summit",
    "Hacienda",
    "Combine",
    "Takeoff",
    "Arsenal",
]

# --- Operator skills available for the pre-match vote ---
OPERATOR_SKILLS = [
    "Annihilator",
    "Claw",
    "Death Machine",
    "Equalizer",
    "Gravity Spikes",
    "Gravity Vortex",
    "Gun Purifier",
    "Sparrow",
    "Tempest",
    "War Machine",
]