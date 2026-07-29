"""
Thin data-access layer over Supabase. Every other module talks to the
database ONLY through this file — no raw supabase-py calls scattered
around cogs/services. Makes it trivial to swap Supabase for raw
psycopg2/asyncpg later if you ever outgrow it.
"""

from __future__ import annotations
import asyncio
import logging
import random
import string
from typing import Any, Optional

import httpx
from supabase import create_client, Client
import config

logger = logging.getLogger("champions_queue")

# Transient transport-layer failures worth a retry — NOT application errors
# (bad payload, schema mismatch, permission denied). Found live 2026-07-19,
# twice in one session, hitting two different unrelated DB calls
# (match_round_results write, then player_recent_matches read) — this is
# a real, recurring characteristic of the Supabase connection under this
# session's load, not a one-off fluke worth a narrow one-off fix.
_RETRYABLE_EXCEPTIONS = (httpx.RemoteProtocolError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError)


async def with_retry(coro_fn, *args, attempts: int = 3, base_delay: float = 0.5, **kwargs):
    """Runs coro_fn(*args, **kwargs), retrying on _RETRYABLE_EXCEPTIONS only.
    Anything else (a real application error) propagates immediately on the
    first attempt — retrying those would just delay a failure that retrying
    can't fix, and could mask a genuine bug behind a few seconds of silence.
    Delay backs off linearly (0.5s, 1s) rather than instantly hammering a
    connection that may still be recovering.

    Shared across modules (match.py, validation.py, ...) rather than
    reimplemented per-caller — the underlying fault is at the DB/transport
    layer, so the fix belongs here, not duplicated at every call site that
    happens to get hit by it."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return await coro_fn(*args, **kwargs)
        except _RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < attempts - 1:
                logger.warning(
                    "Transient network error on attempt %d/%d for %s: %r — retrying in %.1fs",
                    attempt + 1, attempts, getattr(coro_fn, "__name__", coro_fn), exc, base_delay * (attempt + 1),
                )
                await asyncio.sleep(base_delay * (attempt + 1))
    raise last_exc


class Database:
    def __init__(self) -> None:
        self.client: Client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)

    # ------------------------------------------------------------------
    # PLAYERS
    # ------------------------------------------------------------------
    def get_player_by_discord_id(self, discord_id: str) -> Optional[dict]:
        res = self.client.table("players").select("*").eq("discord_id", str(discord_id)).execute()
        return res.data[0] if res.data else None

    def get_player_by_uid(self, cod_uid: str) -> Optional[dict]:
        res = self.client.table("players").select("*").eq("cod_uid", cod_uid).execute()
        return res.data[0] if res.data else None

    def get_player_by_id(self, player_id: int) -> Optional[dict]:
        res = self.client.table("players").select("*").eq("id", player_id).execute()
        return res.data[0] if res.data else None

    def create_player(self, discord_id: str, cod_uid: str, ign: str, region: str,
                       organization: Optional[str] = None) -> dict:
        payload = {
            "discord_id": str(discord_id),
            "cod_uid": cod_uid,
            "ign": ign,
            "region": region,
            "organization": organization,
            "status": "pending",
        }
        res = self.client.table("players").insert(payload).execute()
        return res.data[0]

    def approve_player(self, player_id: int, approved_by: str) -> dict:
        res = (
            self.client.table("players")
            .update({"status": "approved", "approved_by": approved_by, "approved_at": "now()"})
            .eq("id", player_id)
            .execute()
        )
        return res.data[0]

    def reject_player(self, player_id: int) -> dict:
        res = self.client.table("players").update({"status": "rejected"}).eq("id", player_id).execute()
        return res.data[0]

    def update_ign(self, player_id: int, new_ign: str) -> dict:
        # UID stays the anchor; IGN is purely cosmetic and never touches stats.
        res = self.client.table("players").update({"ign": new_ign}).eq("id", player_id).execute()
        return res.data[0]

    def update_player_fields(self, player_id: int, fields: dict) -> dict:
        res = self.client.table("players").update(fields).eq("id", player_id).execute()
        return res.data[0]

    def leaderboard(self, order_by: str = "mmr", limit: int = 10) -> list[dict]:
        res = (
            self.client.table("players")
            .select("*")
            .eq("status", "approved")
            .order(order_by, desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    # ------------------------------------------------------------------
    # QUEUE
    # ------------------------------------------------------------------
    def queue_join(self, player_id: int, queue_key: str) -> Optional[dict]:
        # Unified 2026-07-29: queue_key is which of the 4 physical queues
        # this join is for — set from the button the player clicked, NOT
        # from players.region (that's now purely informational, see
        # config.py's REGIONS vs QUEUE_KEYS comment). A player is still
        # only allowed one active 'waiting' row at a time regardless of
        # which queue it's in — that invariant is unchanged, just no
        # longer tied to their registered region.
        existing = (
            self.client.table("queue_entries")
            .select("*")
            .eq("player_id", player_id)
            .eq("status", "waiting")
            .execute()
        )
        if existing.data:
            return None  # already in queue
        res = self.client.table("queue_entries").insert(
            {"player_id": player_id, "status": "waiting", "queue_key": queue_key}
        ).execute()
        return res.data[0]

    def queue_leave(self, player_id: int) -> None:
        self.client.table("queue_entries").update({"status": "left"}).eq(
            "player_id", player_id
        ).eq("status", "waiting").execute()

    def queue_current(self, queue_key: Optional[str] = None) -> list[dict]:
        """Pass queue_key to scope to one of the 4 physical queues — this
        is the ping-throughput split (EU/AF, NA/Latam, India/ME, Japan),
        kept for matchmaking reasons only. Unified 2026-07-29: this used
        to filter on the joined players.region (registered region doubled
        as queue membership). Now filters on queue_entries.queue_key
        directly, which is set at join time from the button clicked —
        decoupled from the player's registered region entirely, so a
        player can queue in any of the 4 regardless of what they picked
        at registration. Filtered client-side rather than in the query
        itself, since queue volume at any moment is at most a few dozen
        rows — a second round-trip or a fragile nested-table filter isn't
        worth it at this scale (same reasoning as the original)."""
        res = (
            self.client.table("queue_entries")
            .select("*, players(*)")
            .eq("status", "waiting")
            .order("joined_at")
            .execute()
        )
        rows = res.data
        if queue_key is not None:
            rows = [r for r in rows if r.get("queue_key") == queue_key]
        return rows

    def queue_mark_matched(self, player_ids: list[int]) -> None:
        self.client.table("queue_entries").update({"status": "matched"}).in_(
            "player_id", player_ids
        ).eq("status", "waiting").execute()

    def queue_mark_waiting(self, player_ids: list[int]) -> None:
        """Rollback counterpart to queue_mark_matched — used when
        _start_match_flow fails partway through (e.g. Discord channel
        creation error) so the 10 players aren't permanently stranded
        outside the queue with no way back in. Only flips rows that are
        currently 'matched' back to 'waiting', scoped to these player_ids.

        Found live 2026-07-19: if any of these players already had a
        stale leftover 'waiting' row (e.g. from earlier test-session
        churn that never got cleaned up), flipping matched->waiting for
        them collides with idx_queue_entries_one_waiting_per_player and
        the WHOLE rollback fails — the exact players this function exists
        to protect end up stuck in 'matched' with no path back into the
        queue, worse than the original failure it was recovering from.
        Since this is the safety net, it needs to be defensive: clear any
        pre-existing waiting row for these specific players first, so the
        update can never collide."""
        self.client.table("queue_entries").delete().in_(
            "player_id", player_ids
        ).eq("status", "waiting").execute()
        self.client.table("queue_entries").update({"status": "waiting"}).in_(
            "player_id", player_ids
        ).eq("status", "matched").execute()

    # ------------------------------------------------------------------
    # MATCHES
    # ------------------------------------------------------------------
    @staticmethod
    def generate_match_id() -> str:
        suffix = "".join(random.choices(string.digits, k=4))
        return f"CQ-{suffix}"

    def create_match(self, is_bootstrap: bool, queue_key: str, season_id: Optional[int] = None) -> dict:
        # Unified 2026-07-29: matches.region is still NOT NULL (migration_008)
        # and matches_region_check still requires a valid value, so we keep
        # writing queue_key's value into region too — it's one of the 4 new
        # values (EU_AF/NA_LATAM/INDIA_ME/JAPAN), which the widened
        # migration_010 constraint accepts. region is otherwise dead: no
        # downstream code (upload/approval channel, leaderboard, match-log)
        # reads matches.region anymore — queue_key is what's actually used
        # for per-queue provenance/debugging. Kept in sync rather than
        # dropped so a future report/query against matches.region for
        # historical reasons doesn't silently get nulls for every match
        # created after this change.
        payload = {
            "match_id": self.generate_match_id(),
            "status": "forming",
            "is_bootstrap": is_bootstrap,
            "region": queue_key,
            "queue_key": queue_key,
            "season_id": season_id,
        }
        res = self.client.table("matches").insert(payload).execute()
        return res.data[0]

    def get_match(self, match_id: int) -> Optional[dict]:
        res = self.client.table("matches").select("*").eq("id", match_id).execute()
        return res.data[0] if res.data else None

    def get_match_by_code(self, match_code: str) -> Optional[dict]:
        res = self.client.table("matches").select("*").eq("match_id", match_code).execute()
        return res.data[0] if res.data else None

    def update_match(self, match_id: int, fields: dict) -> dict:
        res = self.client.table("matches").update(fields).eq("id", match_id).execute()
        return res.data[0]

    def add_match_player(self, match_id: int, player_id: int, team: str,
                          is_captain: bool = False) -> dict:
        res = self.client.table("match_players").insert(
            {"match_id": match_id, "player_id": player_id, "team": team, "is_captain": is_captain}
        ).execute()
        return res.data[0]

    def get_match_players(self, match_id: int) -> list[dict]:
        res = (
            self.client.table("match_players")
            .select("*, players(*)")
            .eq("match_id", match_id)
            .execute()
        )
        return res.data

    def update_match_player(self, match_id: int, player_id: int, fields: dict) -> dict:
        res = (
            self.client.table("match_players")
            .update(fields)
            .eq("match_id", match_id)
            .eq("player_id", player_id)
            .execute()
        )
        return res.data[0]

    def player_recent_matches(self, player_id: int, limit: int = 2) -> list[dict]:
        res = (
            self.client.table("match_players")
            .select("*, matches(*)")
            .eq("player_id", player_id)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    def player_completed_match_count(self, player_id: int) -> int:
        res = (
            self.client.table("match_players")
            .select("id, matches!inner(status)", count="exact")
            .eq("player_id", player_id)
            .eq("matches.status", "completed")
            .execute()
        )
        return res.count or 0

    # ------------------------------------------------------------------
    # VOTES
    # ------------------------------------------------------------------
    def cast_skill_vote(self, match_id: int, player_id: int, team: str, skill: str) -> dict:
        res = self.client.table("operator_skill_votes").upsert(
            {"match_id": match_id, "player_id": player_id, "team": team, "skill": skill},
            on_conflict="match_id,player_id",
        ).execute()
        return res.data[0]

    def cast_skill_votes_bulk(self, votes: list[dict]) -> list[dict]:
        """Batched version of cast_skill_vote — one upsert call for
        multiple rows instead of one call per player. `votes` is a list of
        {"match_id", "player_id", "team", "skill"} dicts. Used by
        SkillVoteView to flush an entire team's picks in a single write
        instead of firing cast_skill_vote on every individual click."""
        if not votes:
            return []
        res = self.client.table("operator_skill_votes").upsert(
            votes,
            on_conflict="match_id,player_id",
        ).execute()
        return res.data

    def get_skill_votes(self, match_id: int, team: Optional[str] = None) -> list[dict]:
        q = self.client.table("operator_skill_votes").select("*").eq("match_id", match_id)
        if team:
            q = q.eq("team", team)
        return q.execute().data

    # cast_map_vote / get_map_votes removed — confirmed dead (zero call
    # sites anywhere), consistent with the no-map-vote decision. See
    # migration_009_drop_map_votes.sql for the paired schema drop.

    # ------------------------------------------------------------------
    # REPUTATION
    # ------------------------------------------------------------------
    def apply_reputation_delta(self, player_id: int, delta: int, reason: str,
                                match_id: Optional[int] = None) -> dict:
        self.client.table("reputation_log").insert(
            {"player_id": player_id, "delta": delta, "reason": reason, "match_id": match_id}
        ).execute()
        player = self.get_player_by_id(player_id)
        new_rep = max(0, min(100, player["reputation"] + delta))
        return self.update_player_fields(player_id, {"reputation": new_rep})

    # ------------------------------------------------------------------
    # MMR — ADMIN ADJUSTMENTS (disciplinary, not match-driven)
    # ------------------------------------------------------------------
    def apply_mmr_adjustment(self, player_id: int, delta: int, reason: str,
                              adjusted_by: str) -> dict:
        """Admin-issued MMR change (e.g. after repeated AFK warnings), logged
        separately from match-driven mmr_before/after changes in
        match_players so it's never an unexplained jump in /profile or
        /rank-progress later. See mmr_adjustment_log in
        migration_004_p4_afk_and_cleanup.sql."""
        self.client.table("mmr_adjustment_log").insert(
            {"player_id": player_id, "delta": delta, "reason": reason, "adjusted_by": str(adjusted_by)}
        ).execute()
        player = self.get_player_by_id(player_id)
        new_mmr = max(0, player["mmr"] + delta)
        return self.update_player_fields(player_id, {"mmr": new_mmr})

    def get_mmr_adjustment_log(self, player_id: int, limit: int = 10) -> list[dict]:
        res = (
            self.client.table("mmr_adjustment_log")
            .select("*")
            .eq("player_id", player_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    # ------------------------------------------------------------------
    # MATCH ABANDONMENT / CLEANUP SWEEP
    # ------------------------------------------------------------------
    def mark_match_abandoned(self, match_id: int, cleanup_at: str) -> dict:
        """Called by /admin-scrap-match after a host/player AFK report is
        confirmed by a human. Sets status + a due-timestamp for the text
        channel; VC deletion happens immediately in the caller, not here —
        this only schedules the *text* channel, which gets a grace window."""
        res = (
            self.client.table("matches")
            .update({"status": "abandoned", "cleanup_at": cleanup_at})
            .eq("id", match_id)
            .execute()
        )
        return res.data[0]

    def schedule_match_cleanup(self, match_id: int, cleanup_at: str) -> dict:
        """Used for completed matches too (same 1hr grace window rule) —
        distinct from mark_match_abandoned since status doesn't change here."""
        res = (
            self.client.table("matches")
            .update({"cleanup_at": cleanup_at})
            .eq("id", match_id)
            .execute()
        )
        return res.data[0]

    def get_due_cleanups(self, now_iso: str) -> list[dict]:
        """Polled every CLEANUP_SWEEP_INTERVAL_MINUTES by the background
        task in cogs/queue.py. DB-backed (not an in-memory asyncio.sleep)
        specifically so a bot restart mid-window doesn't silently lose the
        scheduled deletion — see DECISIONS.md for the reasoning."""
        res = (
            self.client.table("matches")
            .select("*")
            .not_.is_("cleanup_at", "null")
            .not_.is_("text_channel_id", "null")
            .lte("cleanup_at", now_iso)
            .execute()
        )
        return res.data

    def clear_cleanup(self, match_id: int) -> None:
        """Called after the sweep successfully deletes a channel, so it's
        never picked up again on the next poll."""
        self.client.table("matches").update(
            {"cleanup_at": None, "text_channel_id": None}
        ).eq("id", match_id).execute()

    # ------------------------------------------------------------------
    # ACHIEVEMENTS
    # ------------------------------------------------------------------
    def grant_achievement(self, player_id: int, achievement_code: str,
                           season_id: Optional[int] = None) -> Optional[dict]:
        ach = (
            self.client.table("achievements").select("*").eq("code", achievement_code).execute()
        )
        if not ach.data:
            return None
        achievement_id = ach.data[0]["id"]
        existing = (
            self.client.table("player_achievements")
            .select("*")
            .eq("player_id", player_id)
            .eq("achievement_id", achievement_id)
            .execute()
        )
        if existing.data:
            return None  # already earned
        res = self.client.table("player_achievements").insert(
            {"player_id": player_id, "achievement_id": achievement_id, "season_id": season_id}
        ).execute()
        return res.data[0]

    def get_player_achievements(self, player_id: int) -> list[dict]:
        res = (
            self.client.table("player_achievements")
            .select("*, achievements(*)")
            .eq("player_id", player_id)
            .execute()
        )
        return res.data

    # ------------------------------------------------------------------
    # SEASONS / HALL OF FAME
    # ------------------------------------------------------------------
    def get_active_season(self) -> Optional[dict]:
        res = self.client.table("seasons").select("*").eq("is_active", True).execute()
        return res.data[0] if res.data else None

    def record_hall_of_fame(self, season_id: int, category: str, player_id: int, value: str) -> dict:
        res = self.client.table("hall_of_fame").upsert(
            {"season_id": season_id, "category": category, "player_id": player_id, "value": value},
            on_conflict="season_id,category",
        ).execute()
        return res.data[0]


db = Database()


# ======================================================================
# ASYNC SAFETY LAYER
# ----------------------------------------------------------------------
# supabase-py is synchronous. Calling db.<method>(...) directly inside an
# `async def` cog handler blocks the ENTIRE bot event loop for the length
# of that HTTP round-trip — every other player's button click, command,
# and Discord's own gateway heartbeat freezes until it returns. At 50-60
# matches/day this can silently look like "occasional lag"; under any
# real concurrent load (multiple matches finishing near-simultaneously)
# it causes missed 3-second interaction acks and gateway timeouts.
#
# Fix: every DB call from a cog goes through `adb` instead of `db`.
# `adb.<same method name>(...)` runs the identical synchronous method in
# a worker thread via asyncio.to_thread, so the event loop stays free.
# Nothing about Database's 30+ methods changes — this is purely additive,
# so it's safe to introduce mid-sprint without touching cogs that haven't
# been migrated yet (they can keep using `db.<method>` unchanged until
# you get to them).
#
# Usage in a cog:
#     from database.db import adb
#     player = await adb.get_player_by_discord_id(interaction.user.id)
# ======================================================================
class _AsyncDatabaseProxy:
    """Wraps every callable attribute of a Database instance so it can be
    awaited without blocking the event loop. See module docstring above."""

    def __init__(self, sync_db: Database) -> None:
        self._db = sync_db

    def __getattr__(self, name: str):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        async def _wrapper(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attr, *args, **kwargs)

        return _wrapper


adb = _AsyncDatabaseProxy(db)


# RO3 additions are intentionally appended so existing data-access methods
# remain untouched for concurrent work on other cogs.
def _get_players_by_ids(self: Database, player_ids: list[int]) -> list[dict]:
    return self.client.table("players").select("*").in_("id", player_ids).execute().data


def _upsert_match_screenshot(self: Database, match_id: int, round_number: int,
                              image_url: str, uploaded_by: int, raw_extraction: dict,
                              ocr_confidence: float | None = None) -> dict:
    """uploaded_by: as of migration_010 (2026-07-29) this is the Discord
    user ID of whoever actually submitted the screenshots — the host in
    the normal case, or an admin's Discord ID when the admin-upload
    exception was used (see cogs/match.py's match_submit). No longer FK'd
    to players(id), since an uploading admin may not have a players row
    at all. Pre-migration rows still contain players.id values; there's
    no rewrite of historical data, only the constraint changed."""
    payload = {"match_id": match_id, "round_number": round_number, "image_url": image_url,
               "uploaded_by": uploaded_by, "raw_extraction": raw_extraction,
               "ocr_confidence": ocr_confidence}
    res = self.client.table("match_screenshots").upsert(payload, on_conflict="match_id,round_number").execute()
    return res.data[0]


def _replace_match_round_results(self: Database, match_id: int, round_number: int,
                                 results: list[dict]) -> list[dict]:
    self.client.table("match_round_results").delete().eq("match_id", match_id).eq("round_number", round_number).execute()
    if not results:
        return []
    payload = [{**row, "match_id": match_id, "round_number": round_number} for row in results]
    return self.client.table("match_round_results").insert(payload).execute().data


def _get_match_round_results(self: Database, match_id: int) -> list[dict]:
    return self.client.table("match_round_results").select("*").eq("match_id", match_id).order("round_number").execute().data


def _approve_ro3_match(self: Database, match_id: int, approved_by: int) -> list[dict]:
    return self.client.rpc("approve_ro3_match", {"p_match_id": match_id, "p_approved_by": approved_by}).execute().data


def _has_open_issue(self: Database, match_id: int) -> bool:
    """The single guard used by BOTH the manual Approve button and the
    auto-approve sweep — filing /correction-result blocks approval either
    way, no separate 'pause the timer' mechanism needed. Checks the
    partial index on (match_id) where status='open', so this stays cheap
    regardless of how many resolved historical issues accumulate."""
    res = (
        self.client.table("match_issues")
        .select("id", count="exact")
        .eq("match_id", match_id)
        .eq("status", "open")
        .limit(1)
        .execute()
    )
    return (res.count or 0) > 0


def _create_match_issue(self: Database, match_id: int, reported_by: int, reason: str,
                         detail_text: str | None = None, round_number: int | None = None) -> dict:
    payload = {"match_id": match_id, "reported_by": reported_by, "reason": reason,
               "detail_text": detail_text, "round_number": round_number}
    return self.client.table("match_issues").insert(payload).execute().data[0]


def _resolve_match_issue(self: Database, issue_id: int, resolved_by: int, resolution_note: str | None = None) -> dict:
    payload = {"status": "resolved", "resolved_by": resolved_by, "resolved_at": "now()", "resolution_note": resolution_note}
    return self.client.table("match_issues").update(payload).eq("id", issue_id).execute().data[0]


def _get_match_issue(self: Database, issue_id: int) -> Optional[dict]:
    res = self.client.table("match_issues").select("*").eq("id", issue_id).execute()
    return res.data[0] if res.data else None


def _get_open_issues_for_match(self: Database, match_id: int) -> list[dict]:
    return self.client.table("match_issues").select("*").eq("match_id", match_id).eq("status", "open").execute().data


def _get_overdue_pending_matches(self: Database, now_iso: str) -> list[dict]:
    """Matches for the auto-approve sweep: still pending_verification,
    deadline has passed. The open-issue check happens separately (via
    has_open_issue) rather than as a join here, since it needs to run
    again right before each approve call anyway to avoid a race between
    the sweep reading this list and a correction being filed a moment
    later — see match.py's sweep task."""
    return (
        self.client.table("matches")
        .select("*")
        .eq("status", "pending_verification")
        .not_.is_("approval_deadline", "null")
        .lte("approval_deadline", now_iso)
        .execute()
        .data
    )


def _set_approval_deadline(self: Database, match_id: int, deadline_iso: str) -> dict:
    return self.client.table("matches").update({"approval_deadline": deadline_iso}).eq("id", match_id).execute().data[0]


def _get_match_screenshot(self: Database, match_id: int, round_number: int) -> Optional[dict]:
    res = (
        self.client.table("match_screenshots")
        .select("*")
        .eq("match_id", match_id)
        .eq("round_number", round_number)
        .execute()
    )
    return res.data[0] if res.data else None


def _correct_match_round_result(self: Database, row_id: int, position: int, is_mvp: bool, mmr_delta: int) -> dict:
    payload = {"position": position, "is_mvp": is_mvp, "mmr_delta": mmr_delta}
    return self.client.table("match_round_results").update(payload).eq("id", row_id).execute().data[0]


def _replace_match_player_stats(self: Database, match_id: int, round_number: int,
                                 stats_rows: list[dict]) -> list[dict]:
    """Same delete-then-insert shape as replace_match_round_results —
    naturally idempotent, safe to retry blindly. See P6 migration for
    why this table exists: raw per-round stats were being validated in
    _prepare_rounds and then discarded instead of persisted."""
    self.client.table("match_player_stats").delete().eq("match_id", match_id).eq("round_number", round_number).execute()
    if not stats_rows:
        return []
    payload = [{**row, "match_id": match_id, "round_number": round_number} for row in stats_rows]
    return self.client.table("match_player_stats").insert(payload).execute().data


def _recompute_player_career_stats(self: Database, player_id: int) -> None:
    """Calls the Postgres function of the same name — full recompute
    from match_player_stats + match_round_results, not an increment.
    Called once per player (10x per match) from match.py._do_approve,
    right after approve_ro3_match succeeds."""
    self.client.rpc("recompute_player_career_stats", {"p_player_id": player_id}).execute()


def _region_leaderboard(self: Database) -> list[dict]:
    """Full roster, MMR-ordered, no LIMIT — deliberately separate from the
    older leaderboard() method (still used as-is by digest.py, top-N only).
    This one is for the persistent leaderboard panel: everyone approved,
    growing as registration adds more.

    Unified 2026-07-29: was region-scoped (p_region arg) as of P6. Now
    global across all 4 queues/regions per the unified-region decision —
    see migration_010_unified_region_queue.sql for the RPC body change.
    Function/method name kept as-is (not renamed to e.g. global_leaderboard)
    to minimize the diff; only the signature lost its argument."""
    return self.client.rpc("region_leaderboard", {}).execute().data


def _weekly_leaders(self: Database) -> dict[str, dict]:
    """Returns {category: {"player_id": ..., "value": ...}} for the 5
    weekly badge categories, one round-trip. A category can be absent from
    the result (e.g. top_impact with zero impact data this week) —
    callers must handle missing keys, not assume all 5.

    Unified 2026-07-29: was region-scoped (p_region arg) as of P6. Now
    global — see migration_010_unified_region_queue.sql."""
    rows = self.client.rpc("weekly_leaders", {}).execute().data
    return {row["category"]: {"player_id": row["player_id"], "value": row["value"]} for row in rows}


Database.get_players_by_ids = _get_players_by_ids
Database.upsert_match_screenshot = _upsert_match_screenshot
Database.replace_match_round_results = _replace_match_round_results
Database.get_match_round_results = _get_match_round_results
Database.approve_ro3_match = _approve_ro3_match
Database.has_open_issue = _has_open_issue
Database.create_match_issue = _create_match_issue
Database.resolve_match_issue = _resolve_match_issue
Database.get_match_issue = _get_match_issue
Database.get_open_issues_for_match = _get_open_issues_for_match
Database.get_overdue_pending_matches = _get_overdue_pending_matches
Database.set_approval_deadline = _set_approval_deadline
Database.get_match_screenshot = _get_match_screenshot
Database.correct_match_round_result = _correct_match_round_result
Database.replace_match_player_stats = _replace_match_player_stats
Database.recompute_player_career_stats = _recompute_player_career_stats
Database.region_leaderboard = _region_leaderboard
Database.weekly_leaders = _weekly_leaders