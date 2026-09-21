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

# --- P4: player reports + match-log channel ---
# Both channels are created manually in Discord (bot doesn't create them) —
# grab each channel's ID and set it here or in .env. Optional at import time
# (default None) so the bot doesn't crash on boot if these haven't been
# created yet; the features that need them just no-op with a log warning
# until they're set.
#
# REPORT_CHANNEL_ID (renamed from AFK_CHANNEL_ID): the one channel where
# BOTH /afk and /report post — same audience (admins/moderators), same
# purpose (something happened in a match, please review). The old env var
# name is still read as a fallback so an existing host .env that hasn't
# been updated yet keeps working instead of silently dropping every report.
# Prefer REPORT_CHANNEL_ID going forward; AFK_CHANNEL_ID can be deleted
# from .env once the host has been switched over.
_report_channel_raw = os.getenv("REPORT_CHANNEL_ID") or os.getenv("AFK_CHANNEL_ID")
REPORT_CHANNEL_ID = int(_report_channel_raw) if _report_channel_raw else None
MATCH_LOG_CHANNEL_ID = int(os.getenv("MATCH_LOG_CHANNEL_ID")) if os.getenv("MATCH_LOG_CHANNEL_ID") else None

# --- Incident logging (2026-08-24) ---
# #botlog channel — created manually in Discord same as AFK/MATCH_LOG above.
# Same fail-open pattern: unset means utils/incident_log.py falls back to
# console-only logging instead of crashing the bot on boot. This channel
# only ever receives messages from inside existing except blocks (see
# utils/incident_log.py) — it has no other code path and no background
# task, so it costs nothing when nothing is failing.
BOTLOG_CHANNEL_ID = int(os.getenv("BOTLOG_CHANNEL_ID")) if os.getenv("BOTLOG_CHANNEL_ID") else None
HALL_OF_FAME_CHANNEL_ID = int(os.getenv("HALL_OF_FAME_CHANNEL_ID")) if os.getenv("HALL_OF_FAME_CHANNEL_ID") else None

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

# 2026-09: per-match voice channels (Defender/Attacker VCs, created
# alongside the text channel in _create_match_channels / cogs/queue.py)
# went largely unused in practice — most players stick to their own
# voice call outside Discord. Rather than rip the creation code out,
# gate it behind this switch: default OFF (no VCs created for the next
# queue), flip back to True to restore the old behavior with zero code
# changes needed. Every downstream read of voice_channel_a_id/
# voice_channel_b_id (cleanup, /admin-scrap-match, /admin-swap-player)
# already treats a missing/None VC id as a normal no-op, so turning
# creation off here doesn't require touching anything else.
CREATE_MATCH_VOICE_CHANNELS = os.getenv("CREATE_MATCH_VOICE_CHANNELS", "false").strip().lower() == "true"

# Operator-skill votes: whether picks are written to operator_skill_votes.
# Default ON (today's behaviour). Nothing in the bot reads that table yet, so
# turning it OFF is safe: buttons still lock and show picks, the DB just gets
# no vote writes (saves ~2 writes per match). Read at startup, so changing it
# needs a bot restart. Accepts true/false.
STORE_SKILL_VOTES = os.getenv("STORE_SKILL_VOTES", "true").strip().lower() == "true"

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

# /report + /afk share ONE per-reporter budget (see cogs/queue.py) so a
# player can't dodge the limit by alternating between the two commands.
# discord.py's cooldown is "N uses per window", so this reads literally:
# at most REPORT_COOLDOWN_USES reports per REPORT_COOLDOWN_WINDOW_SECONDS,
# per reporting player. Keyed on the reporter (NOT the channel, unlike the
# host-roll-map/correction cooldowns above) — otherwise one player using
# up the budget would lock every teammate out of reporting in that match.
REPORT_COOLDOWN_USES = 5
REPORT_COOLDOWN_WINDOW_SECONDS = 3600

# Per-channel cooldown on /host-roll-map — cheap guard against a host
# reroll-spamming to try to land a specific map, not a hard limit on
# how many times a match's map can be rerolled overall (no cap on that
# by design — see the command's docstring).
HOST_ROLL_MAP_COOLDOWN_SECONDS = 15

# How often the abandoned/completed-match channel cleanup sweep runs.
# Independent of the 1hr cleanup delay itself — this just controls how
# often the bot checks "is anything due yet".
CLEANUP_SWEEP_INTERVAL_MINUTES = 5
MATCH_CHANNEL_CLEANUP_DELAY_SECONDS = 900    # 15-min grace window before deletion

# Optional channel for one-line match status updates (result submitted, etc.).
# Unset = feature off (same pattern as MATCH_LOG_CHANNEL_ID).
MATCH_STATUS_CHANNEL_ID = int(os.getenv("MATCH_STATUS_CHANNEL_ID")) if os.getenv("MATCH_STATUS_CHANNEL_ID") else None

# /host-replace-player: max replacements a host can make per match.
# 0 = feature disabled. Read at startup (needs restart to change).
HOST_REPLACE_LIMIT = int(os.getenv("HOST_REPLACE_LIMIT", "2"))

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
VOTE_TIMEOUT_SECONDS = 600           # timeout for operator-skill / map votes before fallback
AFK_REQUEUE_PENALTY = -5             # reputation hit for not confirming in time


# Bootstrap phase: use pure-random team/map assignment until a player
# has logged at least this many official matches. Once the *pool* of
# players with >= this many matches is large enough, analysis-based
# assignment kicks in for that pool. Match-count based, NOT calendar-day based.
BOOTSTRAP_MATCH_THRESHOLD = 10
BOOTSTRAP_MIN_ELIGIBLE_POOL = 20     # need at least this many "graduated" players before analysis mode goes live

# Team split (2026-08, analysis mode only — see services/matchmaking.py
# module docstring for the full design). Exhaustive C(10,5) search finds
# the true-optimal 5v5 split by composite score; this epsilon widens the
# pool to every split within N composite-points of optimal, and one is
# picked at random from that pool. Prevents the same 10 players (common
# in a small community) always getting an identical lineup, without ever
# admitting a genuinely worse split for the sake of variety. Calibrated
# against 145 real historical match pops: guaranteed >=2 candidates per
# pop at this value, median ~8 candidates, sampled diff stayed at a
# 7-14 point median/mean — negligible against this project's MMR scale.
TEAM_SPLIT_EPSILON = 10

# Uncertainty discount (2026-08, analysis mode only — see
# services/matchmaking.py's _uncertainty_discount docstring for the
# full design). A newer graduated player's composite score is
# discounted more than a veteran's before the balancer runs, since a
# player at exactly BOOTSTRAP_MATCH_THRESHOLD matches has far less
# data behind their stats than one with 90+. Same design used by
# competitive matchmakers like Rainbow Six Siege / OpenSkill — track a
# skill estimate AND a separate confidence in that estimate.
# sigma = TEAM_SIGMA_BASE / sqrt(total_matches / TEAM_SIGMA_HALF_LIFE + 1)
# Calibrated against 197 real historical match pops: this pair had the
# smallest average-balance cost (+0.53 pts vs the no-sigma baseline of
# 6.87) of every combination tested, while still discounting a
# 10-match player by ~40 points and a 97-match veteran by only ~21 —
# proportionate, not punishing either end.
TEAM_SIGMA_BASE = 50
TEAM_SIGMA_HALF_LIFE = 20

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
# not a plain string — see migration_015_ro1.sql).
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
# --- Season 2: Points System & Prize Pool ---
# (originally migration_029/032/035 — reworked by migration_036 into
#  the 2000-SP-pool-unlock + Oct-10-deadline model. See that file's
#  header for the full model description before touching any of this.)
# Points awarded per match — independent of MMR, no MVP bonus.
POINTS_WIN = 5
POINTS_LOSS = -3

# The FIRST player to reach this SP total permanently flips the
# season's "pool unlocked" flag (seasons.sp_pool_unlocked). This is
# NOT a lock trigger — it doesn't end the season or freeze anyone's
# points. The race for rank 1/2/3 keeps going until SEASON_END_DATE
# regardless of who tripped this. It only changes the PAYOUT FORMULA
# applied once the season eventually locks: flat PRIZE_1ST/2ND/3RD by
# final rank, instead of SP-proportional. See migration_036.
SEASON_POOL_UNLOCK_THRESHOLD = 2000

# The ONLY thing that actually locks the season now is a date —
# seasons.end_date on the active season row, checked lazily (match
# approval + leaderboard reload, no cron) via
# check_and_lock_season_by_deadline(). Not a Python constant: the live
# value always comes from the DB, and until it's set there the season
# simply never locks. Set it explicitly, converting the real-world EOD
# cutoff to its UTC-equivalent instant:
#   update seasons set end_date = '2026-10-10 23:59:59+05:30' where id = <season_id>;

# Prize pool ₹1500 total, split three ways — but the split branches on
# whether the pool was ever unlocked this season (see
# SEASON_POOL_UNLOCK_THRESHOLD above):
#   - pool unlocked:  flat payouts by final rank, regardless of exact SP.
#   - never unlocked: top 3 by final SP get points÷POINTS_TO_RUPEE each,
#     UNCAPPED, no floor up to ₹1500 — a low-scoring season can
#     legitimately pay out less than the full pool in this branch.
PRIZE_1ST = 700
PRIZE_2ND = 500
PRIZE_3RD = 300
POINTS_TO_RUPEE = 5  # 5 points = ₹1 (only matters in the never-unlocked branch)

# Shield tiers (migration_036) — three tiers, each with its own
# duration and win bonus. All three give 0 SP lost on any loss; they
# differ in win bonus, duration, and price. Premium's day-1 +50 (vs
# +10 on days 2-3) is applied automatically — compared in SQL against
# the shield's own shield_starts_at, no manual admin top-up needed
# (unlike the old boost_200 tier's day-1 bonus this replaces, which
# HAD to be manual because no per-shield start timestamp existed to
# check against at the time).
#
# credits_cost: SP price via the free "Use Credits" path. Only
# 'normal' has one — 2x_normal and premium are Boost(cash)-only.
SHIELD_TIERS = {
    "normal": {
        "label": "Normal Shield",
        "duration_hours": 72,
        "win_sp": 10,
        "credits_cost": 150,
        "boost_rupees": 30,
    },
    "2x_normal": {
        "label": "2x Normal Shield",
        "duration_hours": 144,   # 6 days — bundle discount vs. buying two Normal Shields (₹60) separately
        "win_sp": 10,
        "credits_cost": None,    # Boost-only
        "boost_rupees": 50,
    },
    "premium": {
        "label": "Premium Shield",
        "duration_hours": 72,
        "win_sp": 10,            # days 2-3 rate
        "day1_win_sp": 50,       # first 24h from shield_starts_at, applied automatically
        "credits_cost": None,    # Boost-only
        "boost_rupees": 60,
    },
}

# Support channel for ticket-based payments
SUPPORT_CHANNEL_ID = 1524062713590976562

# Channels (same fail-open pattern — unset means features no-op with a warning)
SHIELD_CHANNEL_ID = int(os.getenv("SHIELD_CHANNEL_ID")) if os.getenv("SHIELD_CHANNEL_ID") else None
POINTS_LEADERBOARD_CHANNEL_ID = int(os.getenv("POINTS_LEADERBOARD_CHANNEL_ID")) if os.getenv("POINTS_LEADERBOARD_CHANNEL_ID") else None
HOD_APPROVAL_CHANNEL_ID = int(os.getenv("HOD_APPROVAL_CHANNEL_ID")) if os.getenv("HOD_APPROVAL_CHANNEL_ID") else None

# HOD role IDs — separate from ADMIN_ROLE_IDS. Used for the two-person
# shield approval flow (cash path). An admin initiates, a different
# person with an HOD role confirms. If unset, cash-path shield grants
# are disabled (points path still works).
HOD_ROLE_IDS = {int(x.strip()) for x in os.getenv("HOD_ROLE_IDS", "").split(",") if x.strip()}

# Moderator role IDs — separate from ADMIN_ROLE_IDS on purpose (see
# utils/permissions.py is_moderator). Comma-separated in .env, optional:
# unset/empty means no moderators exist and every gate behaves exactly as
# it did before this feature.
MODERATOR_ROLE_IDS = {int(x.strip()) for x in os.getenv("MODERATOR_ROLE_IDS", "").split(",") if x.strip()}

# Largest single /admin-adjust-mmr change a Moderator (not an admin) may
# make, in either direction. One normal match moves MMR by roughly +25 /
# -20 (+10 for MVP, see MMR_WIN_BASE etc.), so 50 is about one match's worth
# of correction. Admins are uncapped. Every adjustment is logged with who
# ran it regardless (mmr_adjustment_log.adjusted_by).
MODERATOR_MMR_ADJUST_LIMIT = 50

# Points leaderboard display
POINTS_LEADERBOARD_PAGE_SIZE = 50
POINTS_LEADERBOARD_COOLDOWN_SECONDS = 60  # per-user reload rate limit