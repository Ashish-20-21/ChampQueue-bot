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
    ADMIN_ROLE_ID              Discord role ID allowed to approve players / review flagged matches
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
ADMIN_ROLE_ID = int(_require("ADMIN_ROLE_ID"))

# --- P4: AFK reporting + match-log channel ---
# Both channels are created manually in Discord (bot doesn't create them) —
# grab each channel's ID and set it here or in .env. Optional at import time
# (default None) so the bot doesn't crash on boot if these haven't been
# created yet; the features that need them just no-op with a log warning
# until they're set.
AFK_CHANNEL_ID = int(os.getenv("AFK_CHANNEL_ID")) if os.getenv("AFK_CHANNEL_ID") else None
MATCH_LOG_CHANNEL_ID = int(os.getenv("MATCH_LOG_CHANNEL_ID")) if os.getenv("MATCH_LOG_CHANNEL_ID") else None

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
# 3 of these are picked per match (see matchmaking.pick_map_candidates) and
# assigned one-per-round for the Ro3 — there is NO player vote on this list,
# it's announced as a fixed embed ("Map chosen are: X, Y, Z"). See
# cogs/queue.py's map announcement step and matches.map_pool in the schema.
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