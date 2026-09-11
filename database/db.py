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
_RETRYABLE_EXCEPTIONS = (
    httpx.RemoteProtocolError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError,
    # 2026-07-29: confirmed live during smoke testing — httpcore's HTTP/2
    # state machine occasionally throws "Received pseudo-header in
    # trailer" followed by a KeyError in its own stream cleanup, when
    # several requests fire in quick succession (e.g. the concurrent
    # recompute_player_career_stats calls added this session). This is a
    # transport-layer flake surfaced via httpx's "local" framing-error
    # class, not a real application-level protocol violation — safe to
    # retry same as RemoteProtocolError.
    httpx.LocalProtocolError,
    # 2026-08-16: CQ-2578 — a broken-pipe ReadError mid-read on
    # player_recent_matches (services/validation.py's
    # check_stat_outliers) crashed match_submit uncaught, since
    # with_retry passed it straight through on the first attempt
    # without this being in the tuple. Same transient transport-layer
    # character as the others here, not an application error — safe
    # to retry. See incident note comparing this to CQ-8758 (2026-08-16
    # session) for why these are two distinct bugs, not one.
    httpx.ReadError,
    # 2026-09-11: same broken-pipe/reset-by-peer transport flake as
    # ReadError above, but on the write side — httpx.WriteError.
    # Confirmed live hitting recompute_player_career_stats's concurrent
    # asyncio.gather burst (one write per player, 5-10 at once per
    # match) as "Connection reset by peer" on match_id=598 and again on
    # match_id=612. WriteError was never in this tuple, so every
    # occurrence fell straight through on attempt 1/3 — zero retries
    # ever ran — and landed in MATCH_STAT_RECOMPUTE_FAIL looking like
    # exhausted retries when none were attempted. Confirmed via
    # httpx's exception hierarchy that WriteError is a sibling of
    # ReadError under NetworkError, not a subclass of anything already
    # listed here — this was a genuine gap, not redundant.
    #
    # NOTE: httpx.WriteTimeout and httpx.CloseError sit in the exact
    # same sibling position (NetworkError / TimeoutException family)
    # and have the identical uncovered gap. Not added here since
    # neither has a confirmed live occurrence yet, matching this
    # project's established pattern of adding entries only against a
    # real incident (see every comment above) — but worth watching for
    # in future MATCH_STAT_RECOMPUTE_FAIL reports.
    httpx.WriteError,
)


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
        That fix added a defensive DELETE-then-UPDATE two-step.

        Found live 2026-09-03: the 2026-07-19 fix wasn't enough — it was
        still one bulk UPDATE ... WHERE player_id IN (...) statement, and
        that has its own race window. The lock protecting queue_entries
        is released right after queue_mark_matched() succeeds, before
        _start_match_flow even starts (deliberately — see DECISIONS.md,
        the 10062 write-up), so a player can click Join again on any
        queue while their match is still silently being built in the
        background. If _start_match_flow then fails and this rollback
        fires, that player already has a legitimate fresh 'waiting' row
        — and one collision among 10 caused Postgres to reject the
        entire bulk statement, leaving all 10 stuck as 'matched' with no
        channel (confirmed: DELETE succeeded, PATCH failed 177ms later
        with a 23505 on one player_id, all 10 unrestored).

        Fix: per-player, independently caught. A collision now only ever
        excludes the ONE player who already recovered on their own —
        never the other 9. Slower (N round trips instead of 1) but this
        only runs on the rollback/failure path, not the hot path."""
        for player_id in player_ids:
            try:
                self.client.table("queue_entries").delete().eq(
                    "player_id", player_id
                ).eq("status", "waiting").execute()
                self.client.table("queue_entries").update({"status": "waiting"}).eq(
                    "player_id", player_id
                ).eq("status", "matched").execute()
            except Exception:
                logger.warning(
                    "queue_mark_waiting: could not restore player_id=%s to waiting "
                    "(likely already re-joined a queue on their own in the meantime) "
                    "— skipping this player only, other players in this rollback are unaffected",
                    player_id,
                )

    def queue_clean_all(self, queue_key: str) -> int:
        """Bulk-wipe every 'waiting' row for one queue_key, flipping
        status to 'left' — same terminal status queue_leave() uses for a
        normal voluntary leave. Built for /admin-queue-clean (2026-08-20):
        an admin recovery tool for the recurring case where players who
        joined the queue go AFK/unresponsive by the time it actually
        fills and a match tries to form. Scoped to one queue_key so a
        stuck queue in one region can be cleared without touching the
        other 3. Returns the number of rows actually flipped, so the
        caller can report an accurate count back to the admin (0 is a
        valid, expected result — not an error — if the queue was already
        empty)."""
        res = (
            self.client.table("queue_entries")
            .update({"status": "left"})
            .eq("status", "waiting")
            .eq("queue_key", queue_key)
            .execute()
        )
        return len(res.data or [])


    def generate_match_id(self) -> str:
        # Fix 2026-08-19 (quick prod fix): was a bare random 4-digit pick
        # with NO collision check — matches.match_id is a permanent
        # unique constraint (old completed matches' codes are never
        # freed), so with only 10,000 possible values a collision becomes
        # likely well before 10,000 matches lifetime (birthday paradox).
        # Hit live 2026-08-18 as a 409 Conflict on CQ-7875, which then
        # cascaded into handle_start_match's except-block failing too
        # (see cogs/queue.py) and 10 players silently vanishing from the
        # queue with no channel. Now checks the DB before returning a
        # candidate, retrying up to 10 times. No longer @staticmethod
        # since it needs self.client for the existence check.
        for _ in range(10):
            suffix = "".join(random.choices(string.digits, k=4))
            candidate = f"CQ-{suffix}"
            existing = self.client.table("matches").select("id").eq("match_id", candidate).execute()
            if not existing.data:
                return candidate
        raise RuntimeError("generate_match_id: could not find a free match_id after 10 attempts")

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

    def get_last_played_map(self, queue_key: str) -> Optional[str]:
        """Returns the map_pool[0] of the most recently CREATED match in
        this queue_key (ordered by id desc, not by any status filter —
        includes forming/cancelled matches too, since the point is just
        "what map did this queue's Start Match button pick last", not
        "what map was actually completed"). Used by
        matchmaking.pick_map_candidates() for the no-immediate-repeat
        fix (2026-09-xx) — players were seeing the same map (e.g.
        Takeoff, Arsenal) 2-3 times in a row under pure random.sample()
        with only 5 maps in the pool, which is expected behavior for
        true randomness over a small pool but felt broken to players.
        Returns None if this queue has no match history yet (safe —
        pick_map_candidates treats None as "nothing to exclude")."""
        res = (
            self.client.table("matches")
            .select("map_pool")
            .eq("queue_key", queue_key)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data or not res.data[0].get("map_pool"):
            return None
        return res.data[0]["map_pool"][0]

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

    def remove_match_player(self, match_id: int, player_id: int) -> None:
        """Deletes one player's match_players row. Built for
        /admin-queue-replace (2026-08-20) — paired with add_match_player
        to swap an AFK/unavailable player for a new one on the same team,
        without touching anyone else's row. Not used anywhere else; the
        normal match lifecycle never removes a match_players row once
        added."""
        self.client.table("match_players").delete().eq(
            "match_id", match_id
        ).eq("player_id", player_id).execute()

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
    # SEASON POINTS — ADMIN ADJUSTMENTS (disciplinary, not match-driven)
    # ------------------------------------------------------------------
    # NOTE: deliberately NOT a plain table insert + field update like
    # apply_mmr_adjustment above. Season points, unlike MMR, gets
    # routinely rebuilt from scratch by recompute_player_season_points()
    # / recompute_all_season_points() (both sum(delta) purely from
    # season_point_events) — a manual adjustment written only to a
    # side audit table would silently vanish the next time anyone runs
    # /admin-recompute-points. So this goes through the
    # apply_sp_adjustment() RPC (migration_035), which writes a real
    # season_point_events row (match_id = null) alongside the audit
    # log — the adjustment IS part of the same sum() recompute already
    # trusts, not a special case sitting outside it.
    def apply_sp_adjustment(self, player_id: int, season_id: int, delta: int,
                             reason: str, adjusted_by: str) -> dict:
        """Admin-issued Season Points change (e.g. disciplinary penalty
        or a manual correction) — logged in sp_adjustment_log (who/why,
        mirrors mmr_adjustment_log) AND written as a season_point_events
        row with match_id=null (mirrors update_season_points_for_match's
        write shape) so it survives any future recompute. Raises if the
        season's points are already locked — same guard match-driven
        point changes respect. See migration_035_sp_adjustment.sql.

        The RPC's RETURNS TABLE columns are named out_player_id/
        out_season_id/out_points, not player_id/season_id/points —
        deliberately renamed in SQL to dodge a PL/pgSQL "column
        reference season_id is ambiguous" error (confirmed live
        2026-09-11, code 42702) against the function's own output
        column of that name. RETURN QUERY maps its SELECT list to the
        output columns POSITIONALLY, not by alias, so the SQL side
        can't rename its way back to friendly keys internally — this
        translation has to happen here instead, so every caller
        (adjust_sp in admin.py, and anything else added later) can
        keep reading ['player_id']/['season_id']/['points'] without
        needing to know this SQL-layer detail exists."""
        res = self.client.rpc("apply_sp_adjustment", {
            "p_player_id": player_id,
            "p_season_id": season_id,
            "p_delta": delta,
            "p_reason": reason,
            "p_adjusted_by": str(adjusted_by),
        }).execute()
        if not res.data:
            return None
        row = res.data[0]
        return {
            "player_id": row["out_player_id"],
            "season_id": row["out_season_id"],
            "points": row["out_points"],
        }

    def get_sp_adjustment_log(self, player_id: int, season_id: int | None = None,
                               limit: int = 10) -> list[dict]:
        res = self.client.rpc("get_sp_adjustment_log", {
            "p_player_id": player_id,
            "p_season_id": season_id,
            "p_limit": limit,
        }).execute()
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

    def get_season_by_id(self, season_id: int) -> Optional[dict]:
        """Look up any season by id, active or not — needed for /admin-dispatch's
        season_id override (e.g. re-running Season 1's Hall of Fame after
        Season 2 is already active)."""
        res = self.client.table("seasons").select("*").eq("id", season_id).execute()
        return res.data[0] if res.data else None

    def season_recap_stats(self, season_id: int) -> Optional[dict]:
        """Season-wide totals for the recap embed (migration_026). Single
        row, not a leaderboard — see that migration's header for why the
        numbers are computed the way they are (real matches only, not
        every status='completed' row; rounds are RO3/RO1-agnostic)."""
        res = self.client.rpc("season_recap_stats", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def record_hall_of_fame(self, season_id: int, category: str, player_id: int, value: str) -> dict:
        res = self.client.table("hall_of_fame").upsert(
            {"season_id": season_id, "category": category, "player_id": player_id, "value": value},
            on_conflict="season_id,category",
        ).execute()
        return res.data[0]

    # Hall of Fame category winners (migration_024). Each returns a single
    # row dict or None if the >=8-match floor excludes everyone (e.g. a
    # brand-new season with too little data yet), never an empty list
    # crash — callers must handle None per category.
    def hof_most_consistent(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_most_consistent", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_fastest_climber(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_fastest_climber", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_highest_total_kills(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_highest_total_kills", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_best_avg_kills(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_best_avg_kills", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_best_avg_deaths(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_best_avg_deaths", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_most_mvps(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_most_mvps", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_most_matches_played(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_most_matches_played", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_best_kd(self, season_id: int) -> Optional[dict]:
        res = self.client.rpc("hof_best_kd", {"p_season_id": season_id}).execute()
        return res.data[0] if res.data else None

    def hof_highest_mmr(self) -> Optional[dict]:
        res = self.client.rpc("hof_highest_mmr", {}).execute()
        return res.data[0] if res.data else None


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


def _replace_match_round_data(self: Database, match_id: int, round_number: int,
                               round_results: list[dict], player_stats: list[dict]) -> None:
    """migration_017: replaces the old _replace_match_round_results +
    _replace_match_player_stats pair. Both tables' delete+insert now
    happen inside a single Postgres transaction via the
    replace_match_round_data RPC -- either the whole round (both
    tables) lands, or none of it does. Fixes the write-race documented
    in incident_CQ-8758_2026-08-12.txt Root Cause #1: the old version
    did DELETE-then-INSERT as separate non-atomic HTTP calls, and the
    two tables weren't atomic with each other either. See
    migration_017_atomic_round_data_write.sql for the SQL side."""
    self.client.rpc("replace_match_round_data", {
        "p_match_id": match_id,
        "p_round_number": round_number,
        "p_round_results": round_results,
        "p_player_stats": player_stats,
    }).execute()


def _get_match_round_results(self: Database, match_id: int) -> list[dict]:
    return self.client.table("match_round_results").select("*").eq("match_id", match_id).order("round_number").execute().data


def _approve_match(self: Database, match_id: int, approved_by: int) -> list[dict]:
    return self.client.rpc("approve_match", {"p_match_id": match_id, "p_approved_by": approved_by}).execute().data


# Backward-compat Python binding, mirrors the SQL-level alias
# (migration_015_ro1.sql) -- kept so a missed call-site rename in
# match.py during the RO1 conversion fails loudly via the *old*
# RPC's own 10-row assertion, not via a Python AttributeError before
# ever reaching Supabase. Safe to remove once a repo-wide grep for
# `approve_ro3_match` in cogs/ confirms zero remaining callers.
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


def _recompute_player_career_stats(self: Database, player_id: int) -> None:
    """Calls the Postgres function of the same name — full recompute
    from match_player_stats + match_round_results, not an increment.
    Called once per player (10x per match) from match.py._do_approve,
    right after approve_match succeeds."""
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


def _live_player_titles(self: Database, player_id: int) -> list[dict]:
    """Calls the Postgres function of the same name (migration_020) —
    computed fresh every call, nothing stored. Returns [{"title_code":
    ..., "title_name": ...}, ...] for whichever "currently #1" titles
    this specific player holds right now (ladder position tier +
    most-MVPs/most-matches/highest-KD-ever). Can be an empty list — most
    players hold none of these at any given moment, same as the weekly
    badges."""
    return self.client.rpc("live_player_titles", {"p_player_id": player_id}).execute().data


Database.get_players_by_ids = _get_players_by_ids
Database.upsert_match_screenshot = _upsert_match_screenshot
Database.replace_match_round_data = _replace_match_round_data  # migration_017, replaces the two lines below
Database.get_match_round_results = _get_match_round_results
Database.approve_match = _approve_match
Database.approve_ro3_match = _approve_ro3_match  # backward-compat, see comment above
Database.has_open_issue = _has_open_issue
Database.create_match_issue = _create_match_issue
Database.resolve_match_issue = _resolve_match_issue
Database.get_match_issue = _get_match_issue
Database.get_open_issues_for_match = _get_open_issues_for_match
Database.get_overdue_pending_matches = _get_overdue_pending_matches
Database.set_approval_deadline = _set_approval_deadline
Database.get_match_screenshot = _get_match_screenshot
Database.correct_match_round_result = _correct_match_round_result
Database.recompute_player_career_stats = _recompute_player_career_stats
Database.region_leaderboard = _region_leaderboard
Database.weekly_leaders = _weekly_leaders
Database.live_player_titles = _live_player_titles


# ── /admin-reset-match (2026-08-15) ──────────────────────────────
def _reset_match_for_resubmission(self: Database, match_id: int) -> dict:
    """Clear all submission artifacts for a match and reset its status
    to 'awaiting_result' so the host can re-upload screenshots through
    the normal pipeline. Does NOT touch match_players (roster is created
    at queue formation, not submission). Returns the counts of deleted
    child rows + the match_players count for caller verification.

    Ground-truth query taken directly from the manually-run SQL that
    resolved CQ-8758 and CQ-1612 (see incident_CQ-8758_2026-08-12.txt
    and the session handoff doc for the full investigation trail)."""
    screenshots = self.client.table("match_screenshots").delete().eq("match_id", match_id).execute()
    stats = self.client.table("match_player_stats").delete().eq("match_id", match_id).execute()
    results = self.client.table("match_round_results").delete().eq("match_id", match_id).execute()
    issues = self.client.table("match_issues").delete().eq("match_id", match_id).execute()

    self.client.table("matches").update({
        "status": "awaiting_result",
        "scoreboard_image_url": None,
        "raw_extraction": None,
        "completed_at": None,
        "winner_team": None,
        "final_score": None,
        "mvp_player_id": None,
        "approved_by": None,
        "approved_at": None,
    }).eq("id", match_id).execute()

    roster = self.client.table("match_players").select("id").eq("match_id", match_id).execute()
    return {
        "screenshots_deleted": len(screenshots.data),
        "stats_deleted": len(stats.data),
        "results_deleted": len(results.data),
        "issues_deleted": len(issues.data),
        "match_players_count": len(roster.data),
    }


Database.reset_match_for_resubmission = _reset_match_for_resubmission


# ── /admin-match-card (2026-08-15) ───────────────────────────────
def _get_match_player_stats(self: Database, match_id: int) -> list[dict]:
    """Return all match_player_stats rows for a match."""
    res = self.client.table("match_player_stats").select("*").eq("match_id", match_id).execute()
    return res.data


Database.get_match_player_stats = _get_match_player_stats


# ── /ign-change rate-limit (2026-08-15) ──────────────────────────
def _log_ign_change(self: Database, player_id: int, old_ign: str, new_ign: str, changed_by: str) -> dict:
    """Insert an ign_change_history row. changed_by is 'self' for
    player-initiated changes or the admin's discord_id for admin changes."""
    res = self.client.table("ign_change_history").insert({
        "player_id": player_id,
        "old_ign": old_ign,
        "new_ign": new_ign,
        "changed_by": changed_by,
    }).execute()
    return res.data[0] if res.data else {}


def _count_recent_ign_changes(self: Database, player_id: int, since_iso: str) -> int:
    """Count SELF-initiated ign_change_history rows for a player since
    the given timestamp. Used by /ign-change to enforce the 2-per-7-days
    rate limit for non-admin players.

    Filtered to changed_by == 'self' deliberately (2026-08-19 fix) —
    without this filter, an admin fixing a player's IGN (changed_by =
    admin's discord_id) silently ate into that player's own weekly
    quota too, since both land in the same history table. Found live:
    admin changed a test player's IGN once, then that same player's own
    very next self-service attempt was blocked as if they'd already used
    2 changes. Admin-initiated changes are unlimited and must never
    count against a player's own allowance."""
    res = (self.client.table("ign_change_history").select("id", count="exact")
           .eq("player_id", player_id).eq("changed_by", "self").gte("changed_at", since_iso).execute())
    return res.count or 0


Database.log_ign_change = _log_ign_change
Database.count_recent_ign_changes = _count_recent_ign_changes


# ── /admin-enter-result (2026-08-15) ─────────────────────────────
def _insert_match_round_results_batch(self: Database, rows: list[dict]) -> list[dict]:
    """Bulk-insert match_round_results rows. Used by /admin-enter-result
    to populate the exact same table the OCR pipeline writes to, so
    approve_match() sees identical input regardless of entry method."""
    res = self.client.table("match_round_results").insert(rows).execute()
    return res.data


def _insert_match_player_stats_batch(self: Database, rows: list[dict]) -> list[dict]:
    """Bulk-insert match_player_stats rows. Same table the OCR pipeline
    writes to — see _insert_match_round_results_batch above."""
    res = self.client.table("match_player_stats").insert(rows).execute()
    return res.data


Database.insert_match_round_results_batch = _insert_match_round_results_batch
Database.insert_match_player_stats_batch = _insert_match_player_stats_batch
# ── Season Points & Shields (migration_029) ──────────────────

def _get_season_points(self: Database, player_id: int, season_id: int) -> Optional[dict]:
    res = (self.client.table("season_points")
           .select("*").eq("player_id", player_id).eq("season_id", season_id).execute())
    return res.data[0] if res.data else None


def _season_points_leaderboard(self: Database, season_id: int) -> list[dict]:
    return self.client.rpc("season_points_leaderboard", {"p_season_id": season_id}).execute().data


def _is_season_points_locked(self: Database, season_id: int) -> bool:
    return self.client.rpc("is_season_points_locked", {"p_season_id": season_id}).execute().data


def _recompute_player_season_points(self: Database, player_id: int, season_id: int) -> None:
    self.client.rpc("recompute_player_season_points", {
        "p_player_id": player_id, "p_season_id": season_id
    }).execute()


def _recompute_all_season_points(self: Database, season_id: int) -> None:
    self.client.rpc("recompute_all_season_points", {"p_season_id": season_id}).execute()


def _recompute_season_points_for_match(self: Database, match_id: int) -> None:
    self.client.rpc("recompute_season_points_for_match", {"p_match_id": match_id}).execute()


def _get_active_shield(self: Database, player_id: int, season_id: int) -> Optional[dict]:
    """Return the currently active (not expired, not pending) shield for
    this player in this season, or None. Checks timestamps in Python
    since Supabase filters don't support now() comparisons easily."""
    res = (self.client.table("point_shields")
           .select("*")
           .eq("player_id", player_id)
           .eq("season_id", season_id)
           .eq("status", "active")
           .execute())
    if not res.data:
        return None
    from datetime import datetime, timezone
    import re as _re
    now = datetime.now(timezone.utc)
    for row in res.data:
        ends = row.get("shield_ends_at")
        if ends:
            if isinstance(ends, str):
                # Same parsing shape as cogs/points.py's _iso_to_ts —
                # normalize Z, space-vs-T separator, and a colonless
                # UTC offset (Python 3.10's fromisoformat rejects
                # "+00" but accepts "+00:00"; this project runs 3.10).
                # This exact bug crashed a live shield-active check
                # uncaught on 2026-09-07 before this fix.
                cleaned = ends.replace("Z", "+00:00").replace(" ", "T", 1)
                cleaned = _re.sub(r'([+-]\d{2})$', r'\1:00', cleaned)
                try:
                    ends_dt = datetime.fromisoformat(cleaned)
                except (ValueError, AttributeError, TypeError):
                    # Unparseable — treat as not-active rather than
                    # crash the caller. A shield we can't confirm the
                    # expiry of should not silently protect someone.
                    continue
            else:
                ends_dt = ends
            if ends_dt > now:
                return row
    return None


def _get_pending_shields(self: Database, season_id: int) -> list[dict]:
    """All shields waiting for HOD confirmation."""
    return (self.client.table("point_shields")
            .select("*, players(ign, discord_id)")
            .eq("season_id", season_id)
            .eq("status", "pending_hod_confirmation")
            .order("created_at")
            .execute().data)


def _create_shield_points_path(self: Database, player_id: int, season_id: int,
                                cost_points: int) -> dict:
    """Self-serve shield purchase with points. Deducts points from
    season_points and creates an immediately-active shield."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    ends = now + timedelta(hours=config.SHIELD_DURATION_HOURS)

    # Deduct points
    sp = self._get_season_points_raw(player_id, season_id)
    if not sp or sp["points"] < cost_points:
        raise ValueError("Insufficient points")
    new_points = sp["points"] - cost_points
    self.client.table("season_points").update({
        "points": new_points, "updated_at": now.isoformat()
    }).eq("season_id", season_id).eq("player_id", player_id).execute()

    # Create shield row
    res = self.client.table("point_shields").insert({
        "season_id": season_id,
        "player_id": player_id,
        "payment_method": "points",
        "cost_points": cost_points,
        "tier": "credits",
        "status": "active",
        "shield_starts_at": now.isoformat(),
        "shield_ends_at": ends.isoformat(),
    }).execute()
    return res.data[0]


def _get_season_points_raw(self: Database, player_id: int, season_id: int) -> Optional[dict]:
    """Internal helper — same as get_season_points but used within
    other Database methods that need to check balance before writing."""
    res = (self.client.table("season_points")
           .select("*").eq("player_id", player_id).eq("season_id", season_id).execute())
    return res.data[0] if res.data else None


def _create_shield_cash_pending(self: Database, player_id: int, season_id: int,
                                 initiated_by: str, cost_rupees: int = 100,
                                 tier: str = "boost_100") -> dict:
    """Admin/HOD initiates a cash-path shield — status = pending_hod_confirmation.

    If the player already has a matching 'player_consented' row (they
    clicked "I Agree" on the consent screen before the admin ran this
    command — the normal flow), that row is UPDATED in place rather
    than a second row being inserted. Before this fix, every consent
    click + admin grant produced two permanently-orphaned rows in
    point_shields — the original consent row never got linked to or
    touched by the actual grant, defeating the point of recording
    consent at all (no way to trace which consent led to which grant)
    and leaving dead rows accumulating in what's meant to be a clean
    audit trail for real-money transactions.

    Falls back to a fresh insert if no matching consent row exists —
    e.g. an admin grants a shield without the player having gone
    through the consent screen first (edge case, still supported)."""
    existing = (self.client.table("point_shields")
                .select("*")
                .eq("player_id", player_id)
                .eq("season_id", season_id)
                .eq("status", "player_consented")
                .eq("tier", tier)
                .order("created_at", desc=True)
                .limit(1)
                .execute())

    if existing.data:
        shield_id = existing.data[0]["id"]
        res = self.client.table("point_shields").update({
            "cost_rupees": cost_rupees,
            "initiated_by": str(initiated_by),
            "initiated_at": "now()",
            "status": "pending_hod_confirmation",
        }).eq("id", shield_id).eq("status", "player_consented").execute()
        if res.data:
            return res.data[0]
        # Fell through — the consent row was claimed by another grant
        # attempt between our select and update (race condition).
        # Fall back to a fresh insert rather than fail the command.

    res = self.client.table("point_shields").insert({
        "season_id": season_id,
        "player_id": player_id,
        "payment_method": "cash",
        "cost_rupees": cost_rupees,
        "tier": tier,
        "initiated_by": str(initiated_by),
        "initiated_at": "now()",
        "status": "pending_hod_confirmation",
    }).execute()
    return res.data[0]


def _confirm_shield(self: Database, shield_id: int, confirmed_by: str) -> dict:
    """HOD confirms a pending cash-path shield — activates it."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    ends = now + timedelta(hours=config.SHIELD_DURATION_HOURS)
    res = self.client.table("point_shields").update({
        "status": "active",
        "confirmed_by": str(confirmed_by),
        "confirmed_at": now.isoformat(),
        "shield_starts_at": now.isoformat(),
        "shield_ends_at": ends.isoformat(),
    }).eq("id", shield_id).eq("status", "pending_hod_confirmation").execute()
    return res.data[0] if res.data else {}


def _reject_shield(self: Database, shield_id: int, rejected_by: str) -> dict:
    """HOD rejects a pending cash-path shield."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    res = self.client.table("point_shields").update({
        "status": "rejected",
        "rejected_by": str(rejected_by),
        "rejected_at": now.isoformat(),
    }).eq("id", shield_id).eq("status", "pending_hod_confirmation").execute()
    return res.data[0] if res.data else {}


def _get_shield_by_id(self: Database, shield_id: int) -> Optional[dict]:
    res = self.client.table("point_shields").select("*").eq("id", shield_id).execute()
    return res.data[0] if res.data else None


def _expire_shields(self: Database) -> int:
    return self.client.rpc("expire_shields", {}).execute().data


def _get_season_point_events_for_match(self: Database, match_id: int) -> list[dict]:
    """Return all point events for a given match — used by recompute/audit."""
    return (self.client.table("season_point_events")
            .select("*").eq("match_id", match_id).execute().data)


Database.get_season_points = _get_season_points
Database.season_points_leaderboard = _season_points_leaderboard
Database.is_season_points_locked = _is_season_points_locked
Database.recompute_player_season_points = _recompute_player_season_points
Database.recompute_all_season_points = _recompute_all_season_points
Database.recompute_season_points_for_match = _recompute_season_points_for_match
Database.get_active_shield = _get_active_shield
Database.get_pending_shields = _get_pending_shields
Database.create_shield_points_path = _create_shield_points_path
Database.create_shield_cash_pending = _create_shield_cash_pending
Database.confirm_shield = _confirm_shield
Database.reject_shield = _reject_shield
Database.get_shield_by_id = _get_shield_by_id


def _create_shield_consent(self: Database, player_id: int, season_id: int,
                            tier: str, cost_rupees: int) -> dict:
    """Player clicked 'I Agree' on the consent screen — records their
    intent to purchase a boost. No shield is activated yet; this row
    sits at status='player_consented' until an admin/HOD runs
    /admin-grant-shield and an HOD confirms."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    res = self.client.table("point_shields").insert({
        "season_id": season_id,
        "player_id": player_id,
        "payment_method": "cash",
        "cost_rupees": cost_rupees,
        "tier": tier,
        "status": "player_consented",
        "consented_at": now.isoformat(),
    }).execute()
    return res.data[0]


Database.create_shield_consent = _create_shield_consent
Database.expire_shields = _expire_shields
Database.get_season_point_events_for_match = _get_season_point_events_for_match
Database._get_season_points_raw = _get_season_points_raw


# ── /cs-stats (2026-09-11) ────────────────────────────────────────
def _current_season_stats(self: Database, player_id: int, season_id: int) -> Optional[dict]:
    """Season-scoped equivalent of the player row's all-time stat
    columns (total_matches/avg_kills/wins/losses/mvp_count/etc) —
    computed fresh via the current_season_stats() SQL function
    (migration_034), same pattern as the hof_* calls above (no
    precomputed table exists for this, unlike season_points). Win/loss
    uses the mmr_delta-minus-MVP-bonus signal, same as
    recompute_player_career_stats() — see engineering-rules' MVP-sign-
    flip trap: a losing team's position-1 MVP scores -3+5=+2, positive
    despite losing, so win/loss is never inferred from mmr_delta sign
    alone. Returns None if the player has zero completed matches this
    season, so callers can show an explicit empty state rather than a
    card full of zeros."""
    res = self.client.rpc("current_season_stats", {
        "p_player_id": player_id, "p_season_id": season_id
    }).execute()
    if not res.data or not res.data[0] or res.data[0]["matches"] == 0:
        return None
    return res.data[0]


Database.current_season_stats = _current_season_stats