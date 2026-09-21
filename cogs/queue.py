from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database.db import db, adb, with_retry
from services import matchmaking, mmr_engine, reputation
from utils.permissions import admin_only, mod_or_admin_only
from utils import incident_log

logger = logging.getLogger("champions_queue")


# ---------------------------------------------------------------------------
# Webhook-429 isolation + click debounce (2026-09-19, live-load fix)
#
# Incident: under a hyper-active queue, Discord's per-webhook rate limit (429)
# began dropping button acks. Players saw nothing happen and re-clicked
# 20-50x each, which (a) burned the same webhook budget faster, and (b) fed
# the rollback race in db.queue_mark_waiting. Two independent defences:
#
#  1. _safe_ack(): EVERY player-facing Discord message in join/leave goes
#     through one helper that can never raise and never retries. The DB write
#     is the source of truth and has already succeeded (or failed and been
#     reported) by the time this runs, so a lost ack costs nothing but a
#     stale button. A 429 here is logged at DEBUG-ish volume, not escalated.
#
#  2. _click_gate(): a tiny per-(player, action) in-memory cooldown that drops
#     rapid repeat clicks BEFORE they touch the DB or Discord. It is
#     deliberately in-memory and best-effort: a bot restart just clears it,
#     and it can only ever suppress a click (never create state), so it fails
#     safe. It is intentionally NOT applied to Start Match.
# ---------------------------------------------------------------------------
_CLICK_COOLDOWN_SECONDS = 2.0
_click_last: dict[tuple[int, str], float] = {}
_CLICK_MAP_MAX = 5000  # hard cap so this can never grow unbounded


def _click_gate(discord_user_id: int, action: str) -> bool:
    """True = let this click through. False = drop it as a repeat click.
    Never raises."""
    try:
        now = time.monotonic()
        key = (discord_user_id, action)
        last = _click_last.get(key)
        if last is not None and (now - last) < _CLICK_COOLDOWN_SECONDS:
            return False
        if len(_click_last) >= _CLICK_MAP_MAX:
            cutoff = now - _CLICK_COOLDOWN_SECONDS
            for k in [k for k, v in _click_last.items() if v < cutoff]:
                _click_last.pop(k, None)
            if len(_click_last) >= _CLICK_MAP_MAX:
                _click_last.clear()
        _click_last[key] = now
        return True
    except Exception:
        return True  # fail open: never block a real player on a gate bug


async def _safe_ack(send_coro_factory, *, what: str, player_id=None) -> bool:
    """Run a Discord-message coroutine; swallow every failure. Returns True
    if it was delivered. send_coro_factory is a zero-arg callable returning
    the awaitable (so it is only created if we actually run it)."""
    try:
        await send_coro_factory()
        return True
    except (discord.errors.NotFound, discord.errors.HTTPException) as e:
        logger.warning("ack dropped (%s) player_id=%s: %s", what, player_id, getattr(e, "status", e))
        return False
    except Exception:
        logger.exception("ack unexpectedly failed (%s) player_id=%s", what, player_id)
        return False


class SkillVoteView(discord.ui.View):
    """One view per team. Enforces unique-skill-per-team by disabling
    a skill button for everyone on that team once someone picks it, AND
    locks each player to their first vote — once you've picked, you can't
    switch to a different skill. This matters beyond UI polish: if a
    player could silently swap picks mid-vote, teammates and the match-log
    record could show a different skill than what the player actually
    ends up using in-game, which risks a false /AFK or dispute report.

    Vote writes are batched, not per-click: picks accumulate in
    self.pending_votes (in-memory) and only hit the DB once, either when
    the whole team (5/5) has picked, or as a fallback when Discord's own
    View timeout fires (see on_timeout) — whichever happens first. UI
    lock-in (button disabled/relabeled) is still instant on every click,
    same as before; only the DB write timing changed."""

    def __init__(self, match_id: int, team: str, team_player_ids: set[int],
                 roster: dict[int, dict]):
        super().__init__(timeout=config.VOTE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.team = team
        self.team_player_ids = team_player_ids
        # roster: {discord_user_id (int): {"id": players.id, "ign": str}} for
        # THIS team, built once at match start. A click resolves the player
        # from here — no DB read, so nothing slow sits before the reply.
        self.roster = roster
        self.taken_skills: set[str] = set()
        self.voted_player_ids: set[int] = set()
        self.player_picks: dict[int, str] = {}   # players.id -> skill, for "you already picked X"
        self.pending_votes: list[dict] = []
        self._flushed = False
        for skill in config.OPERATOR_SKILLS:
            self.add_item(self._make_button(skill))

    async def _flush_votes(self) -> None:
        """Writes whatever's currently in self.pending_votes in a single
        bulk call, then clears it. Safe to call more than once — later
        calls just have nothing new to send. Not tied to any player-facing
        message or forced skill assignment; purely a backend write."""
        if not self.pending_votes:
            return
        votes_to_write = self.pending_votes
        self.pending_votes = []
        try:
            await adb.cast_skill_votes_bulk(votes_to_write)
        except Exception:
            logger.exception(
                "SkillVoteView: bulk vote flush failed for match_id=%s team=%s (%d votes lost)",
                self.match_id, self.team, len(votes_to_write),
            )

    async def on_timeout(self) -> None:
        # Discord-library-level callback — fires automatically after
        # config.VOTE_TIMEOUT_SECONDS of view inactivity. Not a sleep we
        # wrote, doesn't block or touch _start_match_flow. Only job here:
        # make sure any votes that were cast but never hit 5/5 (so never
        # auto-flushed) still get saved. No forced/random skill assignment
        # for anyone who didn't vote — they simply have no row.
        await self._flush_votes()

    def _make_button(self, skill: str) -> discord.ui.Button:
        button = discord.ui.Button(label=skill, style=discord.ButtonStyle.secondary)

        async def callback(interaction: discord.Interaction):
            # ONE Discord call per click, and no DB read.
            #
            # History: this callback used to defer() first (to beat the 3s
            # clock) because a get_player_by_discord_id round trip sat before
            # the reply. That read is gone (roster is in memory), so every
            # check below is instant and the reply goes out straight away:
            #   accepted click -> response.edit_message(view=self)   (ack + repaint in one call)
            #   rejected click -> response.send_message(..., ephemeral=True)
            # The 5th-vote DB flush still runs AFTER the reply, as before.
            #
            # First-click-wins: there is no await between the checks and the
            # lock-in below, and the event loop is single-threaded, so two
            # players cannot both take the same skill.
            player = self.roster.get(interaction.user.id)
            if player is None or player["id"] not in self.team_player_ids:
                await _safe_ack(lambda: interaction.response.send_message(
                    "This isn't your team's vote.", ephemeral=True), what="vote-not-your-team")
                return
            pid = player["id"]
            if pid in self.voted_player_ids:
                picked = self.player_picks.get(pid, "an operator skill")
                await _safe_ack(lambda: interaction.response.send_message(
                    f"You've already picked **{picked}** for this match — it's locked in, "
                    f"you can't change it.", ephemeral=True), what="vote-already-voted", player_id=pid)
                return
            if skill in self.taken_skills:
                await _safe_ack(lambda: interaction.response.send_message(
                    f"**{skill}** was already picked by a teammate — operator skills must be unique per team.",
                    ephemeral=True), what="vote-skill-taken", player_id=pid)
                return

            # In-memory lock-in — instant, no DB round trip in the click path.
            self.taken_skills.add(skill)
            self.voted_player_ids.add(pid)
            self.player_picks[pid] = skill
            if config.STORE_SKILL_VOTES:
                self.pending_votes.append({
                    "match_id": self.match_id,
                    "player_id": pid,
                    "team": self.team,
                    "skill": skill,
                })
            button.disabled = True
            button.label = f"{skill} ✓ ({player['ign']})"

            # The single reply: acknowledges the click AND repaints the panel.
            # If it fails (429 / expired token) we log ONE warning and move on:
            # no fallback message, no retry — extra Discord calls would only
            # hit a limiter that has already tripped. The vote is already
            # locked in memory and will still be saved; the panel repaints on
            # the next accepted vote, and this player's next click is told
            # which skill they picked (see the already-voted branch above).
            await _safe_ack(lambda: interaction.response.edit_message(view=self),
                            what="vote-edit", player_id=pid)

            # Flush AFTER the reply, so the player never waits on the write.
            # One bulk write once the whole team (5/5) has picked; otherwise
            # pending votes wait for the on_timeout fallback. When
            # STORE_SKILL_VOTES is off pending_votes stays empty and this is a no-op.
            if len(self.voted_player_ids) >= len(self.team_player_ids):
                await self._flush_votes()

        button.callback = callback
        return button


def make_queue_embed(queue_key: str, current_queue: list[dict]) -> discord.Embed:
    player_lines = []
    for idx, p in enumerate(current_queue, 1):
        player_info = p["players"]
        ign = player_info.get("ign", "Unknown")
        mmr = player_info.get("mmr", 200)  # matches players.mmr's default (200 as of 2026-07-30 global-transition reset)
        # Rank derived live from mmr, not read from player_info's stored
        # current_rank/current_division — that column only updates at
        # match-approval time and can silently disagree with what mmr
        # actually maps to (test-seeded rows, manual DB edits, or any
        # player who hasn't been through a real approval since the tier
        # bands last changed). Found live 2026-07-19 — see
        # utils/embeds.py's player_stats_card docstring for the full
        # writeup; same fix applied here since the queue panel is one of
        # the most-viewed surfaces in the bot.
        rank, _ = mmr_engine.derive_rank(mmr)

        player_lines.append(f"`{idx:02d}` **{ign}** [{rank}] — MMR: {mmr}")

    names = "\n".join(player_lines) if player_lines else "*No players in queue. Be the first to join!*"

    embed = discord.Embed(
        title=f"🛡️ Champion's Queue — {queue_key.replace('_', '/')}",
        description=f"Join the competitive matchmaking lobby for the **{queue_key.replace('_', '/')}** queue.",
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.add_field(name=f"👥 Active Queue ({len(current_queue)}/10)", value=names, inline=False)
    embed.set_footer(text="Champions Queue Matchmaker • First 10 players can start the match.")
    return embed


class RegionQueueView(discord.ui.View):
    # Class name kept as RegionQueueView (not renamed to QueueKeyView) to
    # minimize diff surface across bot.py's persistent-view re-registration
    # on restart — it's one of 4 identical views, one per queue_key, same
    # pattern as before, just no longer tied to players.region.
    def __init__(self, queue_key: str, cog: Queue):
        super().__init__(timeout=None)
        self.queue_key = queue_key
        self.cog = cog

        self.join_button = discord.ui.Button(
            label="Join Queue",
            style=discord.ButtonStyle.success,
            custom_id=f"join_queue_{queue_key}"
        )
        self.join_button.callback = self.join_callback
        self.add_item(self.join_button)

        self.leave_button = discord.ui.Button(
            label="Leave Queue",
            style=discord.ButtonStyle.danger,
            custom_id=f"leave_queue_{queue_key}"
        )
        self.leave_button.callback = self.leave_callback
        self.add_item(self.leave_button)

        self.start_match_button = discord.ui.Button(
            label="Start Match",
            style=discord.ButtonStyle.primary,
            custom_id=f"start_match_{queue_key}"
        )
        self.start_match_button.callback = self.start_match_callback

    async def update_view_state(self, current_queue: list[dict]):
        if len(current_queue) >= 10:
            if self.start_match_button not in self.children:
                self.add_item(self.start_match_button)
        else:
            if self.start_match_button in self.children:
                self.remove_item(self.start_match_button)

    async def join_callback(self, interaction: discord.Interaction):
        await self.cog.handle_join(interaction, self.queue_key, self)

    async def leave_callback(self, interaction: discord.Interaction):
        await self.cog.handle_leave(interaction, self.queue_key, self)

    async def start_match_callback(self, interaction: discord.Interaction):
        await self.cog.handle_start_match(interaction, self.queue_key, self)


class QueueActionRetryView(discord.ui.View):
    """Shown when a Join/Leave/Start-Match click dies mid-flight because a DB
    call failed even after with_retry's built-in retries (e.g. a Supabase
    HTTP/2 connection drop — the RemoteProtocolError class confirmed live
    2026-08-26, hitting Join Queue and an unrelated admin command at the
    same instant). Without this, the player was just left on a dead
    "Interaction Failed" with no way to recover except guessing whether
    their click landed and re-clicking the original panel button blind.

    Deliberately NOT persistent (no custom_id, real timeout, no
    bot.add_view() registration) — same reasoning as RankProgressView in
    stats.py. This is a short-lived recovery affordance tied to one failed
    interaction, not a permanent panel control. panel_message is captured
    from the ORIGINAL failed interaction (interaction.message, which for a
    component interaction is the actual queue panel message) so the retry
    can refresh the real panel directly — this retry button lives on a
    separate ephemeral message, so interaction.edit_original_response()
    inside the retry click would hit the wrong message."""

    def __init__(self, cog: "Queue", action: str, queue_key: str,
                 panel_message: discord.Message, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.action = action
        self.queue_key = queue_key
        self.panel_message = panel_message

    @discord.ui.button(label="Retry", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        # This click is its OWN interaction, separate from the one that
        # originally failed — do NOT pre-ack it here. handle_join/handle_leave
        # do their own interaction.response.defer(ephemeral=True) as the
        # first thing on the panel_message-path (see there for why).
        handlers = {"join": self.cog.handle_join, "leave": self.cog.handle_leave}
        await handlers[self.action](interaction, self.queue_key, panel_message=self.panel_message)


class Queue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-queue locks, not one shared lock — a match forming in one
        # queue should never block Join/Leave clicks in another. See the
        # 10062 "Unknown interaction" bug write-up in DECISIONS.md for why
        # this matters: a single lock, combined with handle_start_match
        # holding it through the whole skill-vote wait, starved unrelated
        # button clicks past Discord's 3-second interaction-ack window.
        # Unified 2026-07-29: was 2 locks (East/West) keyed by region;
        # now 4 locks keyed by config.QUEUE_KEYS, since the 4 physical
        # queues are what actually need independent locking — region
        # never did (it was only ever a proxy for queue membership).
        self._locks: dict[str, asyncio.Lock] = {key: asyncio.Lock() for key in config.QUEUE_KEYS}

    def cog_unload(self):
        self.cleanup_sweep.cancel()

    @app_commands.command(name="queue-post", description="Post the persistent queue panel for a specific queue")
    @app_commands.describe(queue="Which of the 4 queues (EU/AF, NA/Latam, India/ME, Japan)")
    @app_commands.choices(queue=[
        app_commands.Choice(name="EU / AF", value="EU_AF"),
        app_commands.Choice(name="NA / Latam", value="NA_LATAM"),
        app_commands.Choice(name="India / ME", value="INDIA_ME"),
        app_commands.Choice(name="Japan", value="JAPAN"),
    ])
    @mod_or_admin_only()
    async def queue_post(self, interaction: discord.Interaction, queue: app_commands.Choice[str]):
        queue_key = queue.value
        await interaction.response.defer(thinking=True)
        current_queue = await adb.queue_current(queue_key=queue_key)
        view = RegionQueueView(queue_key, self)
        await view.update_view_state(current_queue)

        embed = make_queue_embed(queue_key, current_queue)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send(f"Successfully posted the persistent queue panel for **{queue.name}**.", ephemeral=True)

    @app_commands.command(name="queue-status", description="See who's currently in queue")
    @app_commands.describe(queue="Which of the 4 queues (EU/AF, NA/Latam, India/ME, Japan)")
    @app_commands.choices(queue=[
        app_commands.Choice(name="EU / AF", value="EU_AF"),
        app_commands.Choice(name="NA / Latam", value="NA_LATAM"),
        app_commands.Choice(name="India / ME", value="INDIA_ME"),
        app_commands.Choice(name="Japan", value="JAPAN"),
    ])
    async def queue_status(self, interaction: discord.Interaction, queue: app_commands.Choice[str]):
        current = await adb.queue_current(queue_key=queue.value)
        names = ", ".join(p["players"]["ign"] for p in current) or "empty"
        await interaction.response.send_message(f"**{queue.name} Queue ({len(current)}/10):** {names}")

    async def _report_queue_action_failure(
        self, interaction: discord.Interaction, exc: Exception, *,
        action: str, queue_key: str, panel_message: discord.Message,
        player: dict | None = None,
    ) -> None:
        """Called when a DB call inside handle_join/handle_leave fails even
        after with_retry's built-in retries (or raises something
        non-retryable). Two things this fixes vs. before 2026-08-27:
        1. This used to vanish with no trace beyond the raw discord.py
           console/file log — now it also lands in #botlog via
           incident_log.post(), same as every other failure category.
        2. The player used to be left on a dead "Interaction Failed" with
           no way to tell if their click landed. Now they get an ephemeral
           Retry button instead."""
        logger.exception("handle_%s: DB call failed for queue_key=%s", action, queue_key)
        await incident_log.post(
            self.bot,
            category=f"QUEUE_{action.upper()}_DB_FAIL",
            summary=f"handle_{action}: DB call failed for queue_key={queue_key} after retries exhausted — {exc!r}",
            exc=exc,
            players=[(player["ign"], player["discord_id"])] if player else None,
        )
        retry_view = QueueActionRetryView(self, action, queue_key, panel_message)
        message = (
            "Something went wrong talking to the database — your click may not have gone through. "
            "Tap **Retry** to try again."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, view=retry_view, ephemeral=True)
            else:
                await interaction.response.send_message(message, view=retry_view, ephemeral=True)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            logger.warning("handle_%s: failed to send retry-button followup (interaction token likely stale)", action)

    async def handle_join(
        self, interaction: discord.Interaction, queue_key: str,
        view: RegionQueueView | None = None, *, panel_message: discord.Message | None = None,
    ):
        # Two entry paths share this function:
        #  - Normal panel click: `view` is the live persistent RegionQueueView,
        #    and interaction.edit_original_response() below correctly targets
        #    the panel message itself (component-interaction default).
        #  - Retry-button click (QueueActionRetryView): that's a DIFFERENT
        #    interaction living on its own ephemeral message, so editing the
        #    real panel has to go through the captured `panel_message`
        #    directly instead of interaction.edit_original_response().
        is_retry = panel_message is not None

        # Repeat-click debounce (2026-09-19). A dropped click MUST still be
        # acknowledged or Discord shows the player "interaction failed" —
        # so we defer (one cheap call, no DB, no followup message) and stop.
        # Retry-button clicks are exempt: that's a deliberate second attempt.
        if not is_retry and not _click_gate(interaction.user.id, "join"):
            await _safe_ack(lambda: interaction.response.defer(), what="join-debounce")
            return

        if is_retry:
            view = RegionQueueView(queue_key, self)

        # Defer FIRST, before any DB round trip — same fix as SkillVoteView
        # above. Under concurrent clicks (queue filling up), the sequential
        # get_player_by_discord_id + queue_current + queue_join round trips
        # can exceed Discord's 3-second ack window on their own even though
        # nothing is actually broken; deferring first wins that race every
        # time instead of leaving the first response call to gamble on it
        # (see the 10062 "Unknown interaction" write-up in DECISIONS.md).
        # ephemeral=True on the retry path — this defer's "original response"
        # is the ephemeral retry message, not the panel.
        await interaction.response.defer(ephemeral=is_retry)

        try:
            player = await with_retry(adb.get_player_by_discord_id, interaction.user.id)
        except Exception as exc:
            await self._report_queue_action_failure(interaction, exc, action="join", queue_key=queue_key, panel_message=panel_message or interaction.message)
            return

        if not player:
            await _safe_ack(lambda: interaction.followup.send("You need to `/register` and be approved first.", ephemeral=True), what="join-unregistered")
            return
        if player["status"] != "approved":
            await _safe_ack(lambda: interaction.followup.send(f"Your registration is `{player['status']}`, not approved yet.", ephemeral=True), what="join-unapproved", player_id=player["id"])
            return

        # Unified 2026-07-29: the players.region == queue_key gate is
        # REMOVED here — that was the whole point of the unification.
        # A player's registered region is informational only now; any
        # approved player can join any of the 4 queues regardless of
        # what they picked at registration. Discord's own role-gated
        # channel visibility (managed outside this bot, via the dynamo
        # role-sync bot) is what determines which queue channels a
        # player can even see in the first place — this handler doesn't
        # need to re-enforce that at the DB layer.

        eligible, reason = reputation.is_queue_eligible(player)
        if not eligible:
            await _safe_ack(lambda: interaction.followup.send(reason, ephemeral=True), what="join-ineligible", player_id=player["id"])
            return

        async with self._locks[queue_key]:
            try:
                current_queue = await with_retry(adb.queue_current, queue_key=queue_key)
            except Exception as exc:
                await self._report_queue_action_failure(interaction, exc, action="join", queue_key=queue_key, panel_message=panel_message or interaction.message, player=player)
                return

            # Queue-full / already-in-queue are normal control flow, NOT DB
            # failures — moved outside the try above deliberately. Bug
            # 2026-08-30 (live, ~50 msg spam): these followup.send() calls
            # used to be INSIDE the try/except Exception block. Under a
            # genuine high-traffic burst (10 players clicking within
            # seconds), Discord's own webhook rate limit (429 "Rate limit
            # reached for webhook") on THIS send() — not on any DB call —
            # was being caught by the broad except and misreported as a
            # DB failure. That triggered _report_queue_action_failure,
            # which fired MORE Discord API calls (an incident_log.post()
            # + a retry-button followup) into the same already-rate-limited
            # window, compounding the 429s into every other player's
            # normal response failing too — a self-inflicted spam cascade,
            # not 50 independent bugs. Fix: only the actual with_retry(adb.*)
            # calls are try/excepted now; a 429 on our own message-send is
            # just logged and returned, never escalated into more sends.
            if len(current_queue) >= 10:
                await _safe_ack(lambda: interaction.followup.send(
                    "Queue is full (10/10) — a match is about to start. Try again in a moment.",
                    ephemeral=True,
                ), what="join-queue-full", player_id=player["id"])
                return

            try:
                entry = await with_retry(adb.queue_join, player["id"], queue_key)
            except Exception as exc:
                await self._report_queue_action_failure(interaction, exc, action="join", queue_key=queue_key, panel_message=panel_message or interaction.message, player=player)
                return

            if entry is None:
                await _safe_ack(lambda: interaction.followup.send("You're already in the queue.", ephemeral=True), what="join-already-in", player_id=player["id"])
                return

            try:
                current_queue = await with_retry(adb.queue_current, queue_key=queue_key)
            except Exception as exc:
                await self._report_queue_action_failure(interaction, exc, action="join", queue_key=queue_key, panel_message=panel_message or interaction.message, player=player)
                return

            await view.update_view_state(current_queue)
            embed = make_queue_embed(queue_key, current_queue)

            if is_retry:
                await _safe_ack(lambda: panel_message.edit(embed=embed, view=view), what="join-retry-panel", player_id=player["id"])
                await _safe_ack(lambda: interaction.edit_original_response(content="✅ You're in the queue.", view=None), what="join-retry-ack", player_id=player["id"])
                return

            # DB write above already succeeded — that's the source of
            # truth. This is just the visual ack; fall back to a log entry
            # instead of an unhandled exception if the interaction token
            # went stale (e.g. network jitter), so the player's join is
            # never lost even if the button UI doesn't refresh for them.
            await _safe_ack(lambda: interaction.edit_original_response(embed=embed, view=view), what="join-ack", player_id=player["id"])

    async def handle_leave(
        self, interaction: discord.Interaction, queue_key: str,
        view: RegionQueueView | None = None, *, panel_message: discord.Message | None = None,
    ):
        # See handle_join above for the two-entry-path explanation.
        is_retry = panel_message is not None

        if not is_retry and not _click_gate(interaction.user.id, "leave"):
            await _safe_ack(lambda: interaction.response.defer(), what="leave-debounce")
            return

        if is_retry:
            view = RegionQueueView(queue_key, self)

        await interaction.response.defer(ephemeral=is_retry)

        try:
            player = await with_retry(adb.get_player_by_discord_id, interaction.user.id)
        except Exception as exc:
            await self._report_queue_action_failure(interaction, exc, action="leave", queue_key=queue_key, panel_message=panel_message or interaction.message)
            return

        if not player:
            await _safe_ack(lambda: interaction.followup.send("You're not registered.", ephemeral=True), what="leave-unregistered")
            return

        async with self._locks[queue_key]:
            try:
                current_queue = await with_retry(adb.queue_current, queue_key=queue_key)
            except Exception as exc:
                await self._report_queue_action_failure(interaction, exc, action="leave", queue_key=queue_key, panel_message=panel_message or interaction.message, player=player)
                return

            # "Not in queue" is normal control flow, not a DB failure — see
            # the 2026-08-30 spam-cascade writeup in handle_join above for
            # why this is deliberately outside the try/except.
            in_queue = any(p["player_id"] == player["id"] for p in current_queue)
            if not in_queue:
                await _safe_ack(lambda: interaction.followup.send("You're not in the queue.", ephemeral=True), what="leave-not-in", player_id=player["id"])
                return

            try:
                await with_retry(adb.queue_leave, player["id"])
                current_queue = await with_retry(adb.queue_current, queue_key=queue_key)
            except Exception as exc:
                await self._report_queue_action_failure(interaction, exc, action="leave", queue_key=queue_key, panel_message=panel_message or interaction.message, player=player)
                return

            await view.update_view_state(current_queue)
            embed = make_queue_embed(queue_key, current_queue)

            if is_retry:
                await _safe_ack(lambda: panel_message.edit(embed=embed, view=view), what="leave-retry-panel", player_id=player["id"])
                await _safe_ack(lambda: interaction.edit_original_response(content="✅ You've left the queue.", view=None), what="leave-retry-ack", player_id=player["id"])
                return

            await _safe_ack(lambda: interaction.edit_original_response(embed=embed, view=view), what="leave-ack", player_id=player["id"])

    async def handle_start_match(self, interaction: discord.Interaction, queue_key: str, view: RegionQueueView):
        # Defer first, before the get_player_by_discord_id / queue_current /
        # lock-wait chain below — same fix as handle_join/handle_leave.
        await interaction.response.defer(ephemeral=True)

        # 2026-09-19: these reads were bare adb calls — one transient
        # network blip left the host on an endless "thinking..." spinner
        # with no message and no incident. with_retry + a reported failure
        # matches what handle_join/handle_leave already do.
        try:
            player = await with_retry(adb.get_player_by_discord_id, interaction.user.id)
        except Exception as exc:
            logger.exception("handle_start_match: get_player failed for queue_key=%s", queue_key)
            await incident_log.post(self.bot, category="QUEUE_START_DB_FAIL",
                summary=f"handle_start_match: get_player failed for queue_key={queue_key} — {exc!r}", exc=exc)
            await _safe_ack(lambda: interaction.followup.send(
                "Couldn't start the match right now (temporary connection issue). Nothing was changed — try Start Match again.",
                ephemeral=True), what="start-getplayer-fail")
            return
        if not player:
            await _safe_ack(lambda: interaction.followup.send("You're not registered.", ephemeral=True), what="start-unregistered")
            return

        async with self._locks[queue_key]:
            try:
                current_queue = await with_retry(adb.queue_current, queue_key=queue_key)
            except Exception as exc:
                logger.exception("handle_start_match: queue_current failed for queue_key=%s", queue_key)
                await incident_log.post(self.bot, category="QUEUE_START_DB_FAIL",
                    summary=f"handle_start_match: queue_current failed for queue_key={queue_key} — {exc!r}", exc=exc)
                await _safe_ack(lambda: interaction.followup.send(
                    "Couldn't start the match right now (temporary connection issue). Nothing was changed — try Start Match again.",
                    ephemeral=True), what="start-queuecurrent-fail")
                return
            if len(current_queue) < 10:
                await _safe_ack(lambda: interaction.followup.send("The queue no longer has 10 players.", ephemeral=True), what="start-not-10")
                await view.update_view_state(current_queue)
                embed = make_queue_embed(queue_key, current_queue)
                await _safe_ack(lambda: interaction.message.edit(embed=embed, view=view), what="start-not-10-panel")
                return

            queued_player_ids = {p["player_id"] for p in current_queue}
            if player["id"] not in queued_player_ids:
                await _safe_ack(lambda: interaction.followup.send(
                    "Only players currently in the queue can start the match.", ephemeral=True
                ), what="start-not-in-queue", player_id=player["id"])
                return

            # Validate every player has a real, resolvable Discord ID BEFORE
            # touching the DB at all. This is the fix for the fake-test-data
            # crash: catch it here, with zero side effects, instead of
            # partway through channel creation after players are already
            # marked matched.
            pop = current_queue[:10]  # take the first 10 players in queue
            players_list = [p["players"] for p in pop]
            bad_ids = [p["ign"] for p in players_list if not str(p.get("discord_id", "")).isdigit()]
            if bad_ids:
                await _safe_ack(lambda: interaction.followup.send(
                    f"Can't start this match — these players have invalid Discord IDs and can't be "
                    f"added to a real channel: {', '.join(bad_ids)}. (This usually means test/fake "
                    f"data is still in the queue — clear it before testing Start Match.)",
                    ephemeral=True,
                ), what="start-bad-ids")
                return

            player_ids = [p["player_id"] for p in pop]
            try:
                await with_retry(adb.queue_mark_matched, player_ids)
            except Exception as exc:
                # Nothing was flipped (or we can't tell) — abort BEFORE any
                # channel is created; players are still 'waiting'.
                logger.exception("handle_start_match: queue_mark_matched failed for queue_key=%s", queue_key)
                await incident_log.post(self.bot, category="QUEUE_START_DB_FAIL",
                    summary=f"handle_start_match: queue_mark_matched failed for queue_key={queue_key} — {exc!r}",
                    exc=exc, players=[(p["ign"], p["discord_id"]) for p in players_list])
                await _safe_ack(lambda: interaction.followup.send(
                    "Couldn't start the match right now (temporary connection issue). Nobody was removed from the queue — try Start Match again.",
                    ephemeral=True), what="start-markmatched-fail")
                return

            # Reset the persistent queue panel message back to current queue state (minus the matched 10).
            # 2026-09-19: the DB flip above already succeeded, so a failure to
            # REFRESH THE PANEL (429 / stale message) must never abort the
            # match — previously an exception here skipped _start_match_flow
            # entirely and left all 10 players 'matched' with no channel.
            try:
                remaining_queue = await with_retry(adb.queue_current, queue_key=queue_key)
                new_view = RegionQueueView(queue_key, self)
                await new_view.update_view_state(remaining_queue)
                new_embed = make_queue_embed(queue_key, remaining_queue)
                await _safe_ack(lambda: interaction.message.edit(embed=new_embed, view=new_view), what="start-panel-reset")
            except Exception:
                logger.exception("handle_start_match: panel refresh failed for queue_key=%s (match proceeds regardless)", queue_key)
            # Lock released here — everything below (channel creation, the
            # skill-vote views) is slow, and the 10 players are already
            # marked matched + off the queue panel, so there's nothing left
            # for the lock to protect. Holding it through this used to
            # freeze Join/Leave for this whole queue (worse: for ALL 4
            # queues, before the per-queue_key split existed) — see
            # DECISIONS.md for the 10062 write-up.

        # Spawn the match creation and setup. Everything past this point
        # touches Discord's API (channel/VC creation) which can fail for
        # reasons outside our control (permissions, rate limits, etc).
        # If it does, roll the 10 players back to 'waiting' instead of
        # leaving them stranded in a dead 'forming' match with no path
        # back into the queue.
        try:
            await self._start_match_flow(interaction, players_list, player["id"], queue_key)
        except Exception as exc:
            logger.exception(
                f"_start_match_flow failed for queue_key={queue_key}, host_player_id={player['id']}. "
                f"Rolling back {len(player_ids)} players to 'waiting'."
            )
            await incident_log.post(
                self.bot,
                category="QUEUE_MATCH_CREATE_FAIL",
                summary=f"_start_match_flow failed for queue_key={queue_key}, rolling back {len(player_ids)} players",
                exc=exc,
                players=[(p["ign"], p["discord_id"]) for p in players_list],
            )
            # Fix 2026-08-19 (quick prod fix): the rollback call itself
            # used to be unguarded — if queue_mark_waiting ALSO threw
            # (e.g. a player already had a stale 'waiting' row, hitting
            # idx_queue_entries_one_waiting_per_player), this whole
            # except block died right here. interaction.followup.send()
            # below never ran, so the clicking player got zero message,
            # and any players the rollback didn't reach stayed stuck as
            # 'matched' with no channel — the "queue goes empty, no
            # channel created" incident from 2026-08-18. Now the
            # rollback's own failure is caught and logged separately so
            # it can never prevent the player-facing message from going
            # out, whether or not the rollback itself succeeded.
            try:
                await adb.queue_mark_waiting(player_ids)
            except Exception as rollback_exc:
                logger.exception(
                    f"Rollback ALSO failed for player_ids={player_ids} in queue_key={queue_key} — "
                    f"these players may be stuck as 'matched' with no channel. Needs manual DB check."
                )
                await incident_log.post(
                    self.bot,
                    category="QUEUE_ROLLBACK_FAIL",
                    summary=f"Rollback ALSO failed for queue_key={queue_key} — players may be stuck as 'matched' with no channel, needs manual DB check",
                    exc=rollback_exc,
                    players=[(p["ign"], p["discord_id"]) for p in players_list],
                )
            # Message reworded 2026-08-19: avoid implying the bot itself
            # is broken (players read "something went wrong" as a bot
            # malfunction). This is framed as an automatic safety measure
            # catching a Discord-side hiccup (channel/VC creation,
            # permissions, rate limits — see comment above this try
            # block) or a rare internal ID conflict, not a bot failure.
            await _safe_ack(lambda: interaction.followup.send(
                "This match couldn't be started due to a brief sync issue with Discord — "
                "as a precaution, you've been placed back in queue automatically. "
                "No action needed on your end, just try Start Match again.",
                ephemeral=True,
            ), what="start-rollback-msg", player_id=player["id"])

    async def _create_match_safely(self, bootstrap: bool, queue_key: str, season_id) -> dict:
        """Create the matches row, surviving a transient DB drop WITHOUT
        ever creating a duplicate match. See the note at the call site."""
        # Step 1: reserve an id. Pure reads — with_retry is always safe here.
        match_code = await with_retry(adb.generate_match_id)

        payload = {
            "match_id": match_code,
            "status": "forming",
            "is_bootstrap": bootstrap,
            "region": queue_key,      # still NOT NULL in the schema — see db.create_match
            "queue_key": queue_key,
            "season_id": season_id,
        }

        def _insert():
            return db.client.table("matches").insert(payload).execute()

        def _fetch_existing():
            res = db.client.table("matches").select("*").eq("match_id", match_code).execute()
            return res.data[0] if res.data else None

        last_exc = None
        for attempt in range(3):
            try:
                res = await asyncio.to_thread(_insert)
                return res.data[0]
            except Exception as exc:
                last_exc = exc
                # The insert may have LANDED before the connection dropped.
                # Look for our own pinned id before doing anything else.
                try:
                    existing = await with_retry(asyncio.to_thread, _fetch_existing)
                except Exception:
                    existing = None
                if existing is not None:
                    logger.warning(
                        "_create_match_safely: insert raised %r but match %s already exists — using it (no duplicate created)",
                        exc, match_code,
                    )
                    return existing
                # Not there. Only retry on a genuine transient network error;
                # anything else (a real DB/constraint error) must surface.
                from database.db import _RETRYABLE_EXCEPTIONS
                if not isinstance(exc, _RETRYABLE_EXCEPTIONS):
                    raise
                if attempt < 2:
                    logger.warning(
                        "_create_match_safely: transient error on attempt %d/3 for %s: %r — retrying",
                        attempt + 1, match_code, exc,
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise last_exc

    async def _start_match_flow(self, interaction: discord.Interaction, players: list[dict], host_player_id: int, queue_key: str):
        channel = interaction.channel
        player_ids = [p["id"] for p in players]
        # 2026-09-20: this call (11 sequential DB reads inside) was the #1
        # cause of "queue can't start" — 3 of 5 live failures on Sep 19-20
        # died here on a transient RemoteProtocolError('Server disconnected'),
        # and rolled back a perfectly good pop of 10 players. It's pure
        # reads, so retrying is always safe. with_retry already fixed 41/41
        # of the same drops elsewhere in the log on the first retry.
        bootstrap = await with_retry(matchmaking.is_bootstrap_match, player_ids)

        # Team split (2026-08): wired up to balance_teams()'s actual
        # output. Previously discarded (`_ = ...`) and replaced with
        # hardcoded even-odd join-order indexing — flagged in
        # DECISIONS.md as "a real gap, not intentional" since
        # balance_teams() already computed a real split every match.
        # Historical replay against 145 real match pops showed even-odd
        # produced a mean team-MMR gap of ~116-262 (measurement-method
        # dependent); the wired-up exhaustive+epsilon split brings that
        # down to a 7-14 point median/mean on the same real data. See
        # services/matchmaking.py's module docstring for the full design.
        result = matchmaking.balance_teams(players, bootstrap=bootstrap)
        team_a = result["team_a"]  # Defender
        team_b = result["team_b"]  # Attacker

        # Create match (no captains assigned)
        # season_id (2026-08): matches.season_id existed in schema but was
        # never populated — create_match() always defaulted it to None.
        # Fetch the active season here (one extra read per match formation,
        # not a hot path) rather than caching it in memory, so a season
        # transition takes effect on the very next match with no stale
        # in-memory state to worry about. See migration_023_season_activation.sql
        # and migration_025_season_2_transition.sql (the latter also adds
        # a DB-level unique-active-season index, so this read can never
        # come back with more than one candidate row).
        active_season = await with_retry(adb.get_active_season)
        season_id = active_season["id"] if active_season else None
        if season_id is None:
            logger.warning(
                "No active season found in `seasons` table — match %s will be created with season_id=NULL. "
                "Run migration_023_season_activation.sql / migration_025_season_2_transition.sql if this is unexpected.",
                queue_key,
            )
        # 2026-09-20: create_match was 2 of the 5 live "queue can't start"
        # failures (a DB connection drop, one at the match_id availability
        # check, one at the INSERT itself). It can't just be wrapped in
        # with_retry: create_match() generates a FRESH random match_id on
        # every call, so if the INSERT reached the server but its reply was
        # lost, a blind retry would insert a SECOND match row for the same
        # pop and orphan the first. Fix: reserve the match_id ONCE (a pure
        # read, safe to retry), then retry the INSERT using that same
        # pinned id. matches.match_id is UNIQUE, so a retry after an
        # already-landed insert is rejected instead of duplicating — and we
        # then just fetch the row that is already there.
        match = await self._create_match_safely(bootstrap, queue_key, season_id)
        await with_retry(adb.update_match, match["id"], {
            "room_code_shared_by": host_player_id
        })

        # add_match_player is idempotent (UNIQUE (match_id, player_id)), so
        # retrying it can never double-add anyone.
        for p in team_a:
            await with_retry(adb.add_match_player, match["id"], p["id"], "A", is_captain=False)
        for p in team_b:
            await with_retry(adb.add_match_player, match["id"], p["id"], "B", is_captain=False)

        guild = channel.guild
        category = channel.category
        # Unified 2026-07-29: was a single admin_role lookup. Now loops
        # over every role in ADMIN_ROLE_IDS (HOD + admin team all get
        # identical visibility into match channels) — see
        # utils/permissions.py's is_admin() for the same set used
        # elsewhere. Missing/invalid role IDs are silently skipped
        # (guild.get_role returns None) rather than raising, consistent
        # with the old single-role "if admin_role:" fail-open pattern.
        # Moderators get the same private-channel visibility as admins — they
        # act on reports/scraps/map-changes inside these channels.
        admin_roles = [r for r in (guild.get_role(rid) for rid in (config.ADMIN_ROLE_IDS | config.MODERATOR_ROLE_IDS)) if r]

        # Private text channel overwrites
        overwrites_text = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        for admin_role in admin_roles:
            overwrites_text[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        for p in players:
            member = guild.get_member(int(p["discord_id"]))
            if member:
                overwrites_text[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        text_channel = await guild.create_text_channel(
            name=match['match_id'].lower(),
            category=category,
            overwrites=overwrites_text
        )

        # Per-match VCs (2026-09): gated behind config.CREATE_MATCH_VOICE_CHANNELS
        # — default off, since these went largely unused in practice.
        # vc_a/vc_b stay None when off, and the DB update below already
        # only writes their ids conditionally, so every downstream reader
        # of voice_channel_a_id/voice_channel_b_id sees the same "no VC
        # for this match" shape it already knows how to skip past.
        vc_a = vc_b = None
        if config.CREATE_MATCH_VOICE_CHANNELS:
            # Private VC A overwrites (Defender Team)
            overwrites_vc_a = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
            }
            for admin_role in admin_roles:
                overwrites_vc_a[admin_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
            for p in team_a:
                member = guild.get_member(int(p["discord_id"]))
                if member:
                    overwrites_vc_a[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

            vc_a = await guild.create_voice_channel(
                name=f"🛡️ {match['match_id']} ",
                category=category,
                overwrites=overwrites_vc_a
            )

            # Private VC B overwrites (Attacker Team)
            overwrites_vc_b = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
            }
            for admin_role in admin_roles:
                overwrites_vc_b[admin_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
            for p in team_b:
                member = guild.get_member(int(p["discord_id"]))
                if member:
                    overwrites_vc_b[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

            vc_b = await guild.create_voice_channel(
                name=f"⚔️ {match['match_id']}",
                category=category,
                overwrites=overwrites_vc_b
            )

        match_update = {"text_channel_id": str(text_channel.id), "status": "forming"}
        if vc_a is not None:
            match_update["voice_channel_a_id"] = str(vc_a.id)
        if vc_b is not None:
            match_update["voice_channel_b_id"] = str(vc_b.id)
        await with_retry(adb.update_match, match["id"], match_update)  # idempotent UPDATE by id

        host_player = next(p for p in players if p["id"] == host_player_id)
        host_member = guild.get_member(int(host_player["discord_id"]))
        host_mention = host_member.mention if host_member else f"<@{host_player['discord_id']}>"
        await text_channel.send(f"{host_mention} is the Match Host.")

        # Post team embed
        embed_teams = discord.Embed(
            title=f"Match {match['match_id']} — Teams Formed ({queue_key.replace('_', '/')})",
            color=discord.Color.blue()
        )
        # Roster-display fix (2026-08-08): players couldn't tell who's who
        # in voice chat, since Discord usernames rarely match IGNs. A
        # sync_nickname feature (writing IGN into the Discord server
        # nickname) was built, tested, then deliberately reverted — real
        # ongoing maintenance cost (re-sync on every IGN change, bot-role
        # hierarchy dependency) for a problem solvable at display time
        # instead. This is that display-time fix: IGN and the real
        # <@discord_id> mention stacked on two lines per player, not
        # combined onto one. A single combined line ("IGN — @mention")
        # was tried and rejected — Discord mentions render at whatever
        # length the person's actual username is, which routinely pushes
        # a combined line past mobile width and wraps mid-mention. Stacked
        # lines can't wrap unpredictably since IGN alone is short and the
        # mention is a single atomic pill either way.
        embed_teams.add_field(
            name="🛡️ Team Defender",
            value="\n".join(f"**{p['ign']}**\n<@{p['discord_id']}>" for p in team_a),
            inline=True,
        )
        # Spacer field (2026-08-20): forces Team Attacker onto its own row
        # instead of packing tight against Defender's field boundary.
        # inline=False so it takes the full row width — an inline=True
        # spacer would instead sit beside Defender/Attacker as a third
        # column on desktop, which isn't the intent here.
        embed_teams.add_field(name="\u200b", value="\u200b", inline=False)
        embed_teams.add_field(
            name="⚔️ Team Attacker",
            value="\n".join(f"**{p['ign']}**\n<@{p['discord_id']}>" for p in team_b),
            inline=True,
        )
        # Mode footer intentionally not shown to players — bootstrap is an
        # internal matchmaking detail, not player-facing info. Still stored
        # on the match row (is_bootstrap) for later analysis.
        await text_channel.send(embed=embed_teams)

        # Map selection and announcement (no vote)
        team_a_ids = {p["id"] for p in team_a}
        team_b_ids = {p["id"] for p in team_b}
        # RO1 (2026-08): n=1 instead of n=3 - one Hardpoint round per
        # match now, not three. map_pool stays a 1-element array
        # (["Summit"]), not a string - indexed [0] below rather than
        # changing the column type, per the RO1 migration plan.
        maps = await with_retry(matchmaking.pick_map_candidates, list(team_a_ids), list(team_b_ids), bootstrap, n=1, queue_key=queue_key)
        await with_retry(adb.update_match, match["id"], {
            "map_pool": maps,
            "status": "awaiting_room"
        })

        embed_maps = discord.Embed(
            title="🗺️ Map Selection",
            description=f"Map: **{maps[0]}**",
            color=discord.Color.gold()
        )
        await text_channel.send(embed=embed_maps)

        # NOTE (2026-08-08): redundant re-ping of all 10 players removed —
        # they're already individually tagged in the "Teams Formed" embed
        # posted just above (each IGN is followed by their <@mention> on
        # its own line, per the roster-display fix). Kept here, commented,
        # in case we want to reintroduce a single combined ping or change
        # the notification format later.
        # mentions = " ".join(f"<@{p['discord_id']}>" for p in players)
        voice_line = (
            f"Voice: {vc_a.mention} (Defender) / {vc_b.mention} (Attacker)\n\n"
            if vc_a is not None and vc_b is not None else ""
        )
        await text_channel.send(
            # f"{mentions}\n\n"
            f"{voice_line}"
            f"Host {host_mention}: share the room code here with `+rc<code>` "
            f"(or `/rc <code>`). Made a typo? Use `+urc<code>` to correct it.\n\n"
            f"Make sure to select your operator skill above ⬆️ — no rush, select whenever you're ready."
        )

        # Skill votes — no blocking wait here anymore. Views are sent and
        # the flow ends; each view batches its own team's writes (flushed
        # at 5/5, or as a fallback on Discord's own view timeout — see
        # SkillVoteView.on_timeout). Nothing downstream (room code,
        # match-log, MMR, approval) depends on skill votes being complete,
        # so there's nothing here to wait on before finishing the flow.
        # Rosters (discord id -> db id + IGN) so a vote click needs no DB read.
        roster_a = {int(p["discord_id"]): {"id": p["id"], "ign": p["ign"]} for p in team_a}
        roster_b = {int(p["discord_id"]): {"id": p["id"], "ign": p["ign"]} for p in team_b}
        view_a = SkillVoteView(match["id"], "A", team_a_ids, roster_a)
        view_b = SkillVoteView(match["id"], "B", team_b_ids, roster_b)
        await text_channel.send(f"**Defender Team** — vote your operator skill (unique per team):", view=view_a)
        await text_channel.send(f"**Attacker Team** — vote your operator skill (unique per team):", view=view_b)

    async def _handle_room_code_share(self, message_or_interaction, channel: discord.TextChannel,
                                        author_id: int, code: str, respond, allow_overwrite: bool = True) -> None:
        """Shared logic for +rc / +urc / the /rc slash command — same
        host-privilege check, same DB write, same match-log post either
        way. `respond` is a callable(str) that sends feedback back
        through whichever entry point was used. (Text-command prefixes
        renamed 2026-07-29 from +roomcode/+updateroomcode to +rc/+urc.)

        allow_overwrite=False (the +rc text-command case) refuses to
        change an already-set code — that mistake used to be silent and
        is exactly what +urc exists to require explicit intent for. /rc
        (the slash command) keeps allow_overwrite=True since it's
        documented as a single share-or-correct command."""
        if not channel.name.startswith("cq-"):
            await respond("Room codes can only be shared in a match channel.")
            return

        # Digits-only, no fixed length enforced — every real room code
        # observed so far is numeric (e.g. 123465, 412563), and rejecting
        # here up front means a mistyped letter never reaches the DB at
        # all, avoiding the extra +updateroomcode round-trip a host would
        # otherwise need. Deliberately not locking to an exact digit
        # count (e.g. "must be 6") since that's not been confirmed as a
        # hard game rule — a stricter length check can be added later if
        # a wrong-length code ever actually shows up in practice, rather
        # than guessed at now.
        if not code.isdigit():
            await respond("Room code must be numbers only — check for a typo and try again.")
            return

        match_code = channel.name.upper()
        match = await adb.get_match_by_code(match_code)
        if not match:
            await respond("Couldn't find a match tied to this channel.")
            return

        player = await adb.get_player_by_discord_id(author_id)
        if not player or match.get("room_code_shared_by") != player["id"]:
            await respond("Only the match host can set or change the room code.")
            return

        is_first_share = match.get("room_code") is None
        if not is_first_share and not allow_overwrite:
            await respond(
                f"A room code is already set for this match. Use `+urc{code}` "
                f"if you need to correct it — `+rc` won't overwrite an existing one."
            )
            return

        await adb.update_match(match["id"], {
            "room_code": code,
            "status": "awaiting_result"
        })
        # NOTE: was "in_progress" — a leftover from before the RO3 rewrite.
        # cogs/match.py's /match-roomcode (and /match-submit's gate) both
        # use "awaiting_result" as the post-room-code state; this listener
        # writing a different value meant every host using the documented
        # "+room <code>" text syntax got silently stuck — /match-submit
        # would reject with "Match not found or not awaiting its three
        # scoreboards" no matter how correct everything else was. Found via
        # live testing 2026-07-17.
        await channel.send(
            f"@everyone Room code updated to **{code}**. Match is now live!",
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )

        # Match-log entry: only post fresh on the *first* share. A
        # correction just updates the room code in place — re-posting a
        # whole new log entry on every typo-fix would clutter the log
        # channel with duplicates for the same match. Note: this can
        # happen while skill votes are still in progress on either team —
        # that's expected and fine, the two are independent (see
        # DECISIONS.md).
        if is_first_share:
            await self._post_match_log(match["id"], code)
        else:
            await self._update_match_log_room_code(match["id"], code)

    async def _post_match_log(self, match_id: int, room_code: str) -> None:
        if not config.MATCH_LOG_CHANNEL_ID:
            logger.warning("MATCH_LOG_CHANNEL_ID not configured — skipping match-log post for match_id=%s", match_id)
            return
        channel = self.bot.get_channel(config.MATCH_LOG_CHANNEL_ID)
        if not channel:
            logger.warning("MATCH_LOG_CHANNEL_ID=%s not found/accessible — skipping match-log post", config.MATCH_LOG_CHANNEL_ID)
            return

        match = await adb.get_match(match_id)
        match_players = await adb.get_match_players(match_id)
        host = next((mp["players"] for mp in match_players if mp["players"]["id"] == match.get("room_code_shared_by")), None)
        team_a = [mp["players"]["ign"] for mp in match_players if mp["team"] == "A"]
        team_b = [mp["players"]["ign"] for mp in match_players if mp["team"] == "B"]

        map_pool = match.get("map_pool") or ["—"]
        maps_display = "\n".join(f"Round {i+1}: **{m}**" for i, m in enumerate(map_pool))

        embed = discord.Embed(
            title=f"Match {match['match_id']} — Hardpoint Started",
            color=discord.Color.green(),
        )
        embed.add_field(name="Mode", value="Hardpoint", inline=True)
        embed.add_field(name="Host", value=host["ign"] if host else "—", inline=True)
        embed.add_field(name="Room ID", value=f"```{room_code}```", inline=False)
        embed.add_field(name="🗺️ Maps", value=maps_display, inline=False)
        embed.add_field(name="🛡️ Defender", value="\n".join(team_a) or "—", inline=True)
        embed.add_field(name="⚔️ Attacker", value="\n".join(team_b) or "—", inline=True)
        msg = await channel.send(embed=embed)
        await adb.update_match(match_id, {"match_log_message_id": str(msg.id)})

        # VC rename-with-room-code loop removed entirely (was here,
        # renaming both VCs once on first room-code share). Room code is
        # intentionally NOT shown in VC names — see DECISIONS.md: "a
        # permanent... public log channel showing every match's room code
        # meant anyone browsing history could walk into someone else's
        # ongoing match." Found live 2026-07-20 still doing this despite
        # that decision. VCs already get their correct name (label +
        # match_id, no code) at creation time in _start_match_flow — with
        # the room code excluded, there's nothing left for this function
        # to rename.

    async def _update_match_log_room_code(self, match_id: int, new_code: str) -> None:
        """Corrects the Room ID field on an already-posted log entry
        instead of spamming a second entry — see _post_match_log."""
        if not config.MATCH_LOG_CHANNEL_ID:
            return
        match = await adb.get_match(match_id)
        log_msg_id = match.get("match_log_message_id")
        channel = self.bot.get_channel(config.MATCH_LOG_CHANNEL_ID)
        if not channel or not log_msg_id:
            return
        try:
            msg = await channel.fetch_message(int(log_msg_id))
        except (discord.NotFound, discord.HTTPException):
            return
        if not msg.embeds:
            return
        embed = msg.embeds[0]
        for i, field in enumerate(embed.fields):
            if field.name == "Room ID":
                embed.set_field_at(i, name="Room ID", value=f"```{new_code}``` *(corrected)*", inline=False)
                break
        await msg.edit(embed=embed)

    @app_commands.command(name="rc", description="Share or correct the room code for your match (host only)")
    @app_commands.describe(code="The in-game room code")
    async def rc(self, interaction: discord.Interaction, code: str):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This only works inside a match channel.", ephemeral=True)
            return

        async def respond(text: str):
            await interaction.channel.send(text)

        await interaction.response.send_message("Got it.", ephemeral=True, delete_after=1)
        # /rc is documented as share-OR-correct in one command — unlike the
        # two separate text triggers below, it's allowed to overwrite.
        await self._handle_room_code_share(interaction, interaction.channel, interaction.user.id, code.strip(), respond, allow_overwrite=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.channel.name.startswith("cq-"):
            return

        content = message.content.strip()
        code = None
        is_update = False
        # Unified 2026-07-29: renamed from +roomcode/+updateroomcode to
        # +rc/+urc per the unified-server text-command shortening. Neither
        # prefix is a substring/prefix of the other ("+urc" doesn't start
        # with "+rc"), but the more-specific check is still done first as
        # defensive practice, consistent with the old ordering rationale.
        if content.lower().startswith("+urc"):
            code = content[len("+urc"):].strip()
            is_update = True
        elif content.lower().startswith("+rc"):
            code = content[len("+rc"):].strip()

        if not code:
            return

        async def respond(text: str):
            await message.channel.send(text, delete_after=5 if "Only the match host" in text else None)

        # +rc is first-share only — a typo'd re-send with the wrong
        # prefix used to silently overwrite an already-set code, which is
        # exactly the mistake +urc exists to require intent for. Found
        # live 2026-07-18 (originally as +roomcode/+updateroomcode).
        await self._handle_room_code_share(message, message.channel, message.author.id, code, respond, allow_overwrite=is_update)


    # ── /afk and /report — one shared pipeline ───────────────────
    # Both commands do the same job (a player flags someone in their own
    # match; admins/moderators review it in REPORT_CHANNEL_ID) and only
    # differ in the embed title/colour and the footer hint, so the whole
    # flow lives in _submit_match_report() rather than being copy-pasted.
    # Two copies would inevitably drift: a fix to one (say, blocking
    # self-reports) would silently miss the other.
    #
    # Nothing is written to the database — the Discord post IS the record.
    # (Deliberate, see the 2026-09-19 decision: complaint volume is low
    # enough for admins to manage from the channel; add a reports table
    # only if that stops being true.)

    # Per-reporter budget shared by BOTH commands — see the comment on
    # REPORT_COOLDOWN_USES in config.py. Module-level mapping (not a
    # decorator on each command) because two separate @cooldown
    # decorators would give /afk and /report independent budgets.
    _report_cooldown = commands.CooldownMapping.from_cooldown(
        config.REPORT_COOLDOWN_USES,
        config.REPORT_COOLDOWN_WINDOW_SECONDS,
        commands.BucketType.user,
    )

    async def _submit_match_report(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        details: str,
        *,
        kind: str,  # "afk" or "report" — only affects wording/colour
    ) -> None:
        # Cheap, no-DB rejections first.
        if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.name.startswith("cq-"):
            await interaction.response.send_message("This only works inside a match channel.", ephemeral=True)
            return

        # New in /report (2026-09-19). /afk never blocked this; a player
        # reporting themselves is always a mistake or a joke, and either
        # way it would page the admin/moderator roles for nothing.
        if target.id == interaction.user.id:
            await interaction.response.send_message("You can't report yourself.", ephemeral=True)
            return
        if target.bot:
            await interaction.response.send_message("You can't report a bot.", ephemeral=True)
            return

        reporter = await adb.get_player_by_discord_id(interaction.user.id)
        reported = await adb.get_player_by_discord_id(target.id)
        if not reporter or not reported:
            await interaction.response.send_message("Both players need to be registered.", ephemeral=True)
            return

        match_code = interaction.channel.name.upper()
        match = await adb.get_match_by_code(match_code)
        if not match:
            await interaction.response.send_message("Couldn't find a match tied to this channel.", ephemeral=True)
            return

        match_players = await adb.get_match_players(match["id"])
        match_player_ids = {mp["player_id"] for mp in match_players}
        if reporter["id"] not in match_player_ids or reported["id"] not in match_player_ids:
            await interaction.response.send_message("Both players need to be part of this match.", ephemeral=True)
            return

        # Rate limit LAST among the rejections, so a request that was
        # going to be refused anyway (wrong channel, not in the match,
        # typo'd target) never burns one of the player's 5 hourly slots.
        # CooldownMapping expects something message-shaped (reads
        # .author.id for BucketType.user); a raw Interaction has .user.
        # Same tiny shim points.py's leaderboard reload already uses.
        class _Ctx:
            author = interaction.user
        retry_after = self._report_cooldown.get_bucket(_Ctx()).update_rate_limit()
        if retry_after:
            minutes = max(1, int(retry_after // 60) + 1)
            await interaction.response.send_message(
                f"You've hit the report limit ({config.REPORT_COOLDOWN_USES} per hour). "
                f"Try again in about {minutes} minute{'s' if minutes != 1 else ''}. "
                "If something is urgent, ping an admin directly.",
                ephemeral=True,
            )
            return

        is_host = reported["id"] == match.get("room_code_shared_by")
        await interaction.response.send_message(
            "Report sent to admins for review — no action has been taken automatically.", ephemeral=True
        )

        # Fail-open, same as before: the player has already been told
        # "sent", and a missing/renamed channel must not crash the command.
        if not config.REPORT_CHANNEL_ID:
            logger.warning("REPORT_CHANNEL_ID not configured — /%s report for match_id=%s was not posted anywhere",
                           kind, match["id"])
            return
        report_channel = self.bot.get_channel(config.REPORT_CHANNEL_ID)
        if not report_channel:
            logger.warning("REPORT_CHANNEL_ID=%s not found/accessible", config.REPORT_CHANNEL_ID)
            return

        if kind == "afk":
            title = f"⚠️ AFK Report — Match {match['match_id']}"
            color = discord.Color.orange()
            footer = "No automatic action taken. Requires admin review — see /admin-scrap-match."
        else:
            title = f"🚩 Player Report — Match {match['match_id']}"
            color = discord.Color.red()
            footer = "No automatic action taken. Requires admin review."

        embed = discord.Embed(
            title=title,
            description=(
                f"**Reported:** {target.mention} ({reported['ign']}){' — this is the match Host' if is_host else ''}\n"
                f"**Reported by:** {interaction.user.mention} ({reporter['ign']})\n"
                f"**Details:** {details}"
            ),
            color=color,
        )
        embed.set_footer(text=footer)
        # Unified 2026-07-29: pings every role in ADMIN_ROLE_IDS so any
        # admin (or HOD) gets notified. Moderator roles are included too
        # (2026-09-19) — otherwise moderators could act on reports but
        # never get told about them.
        admin_roles = [interaction.guild.get_role(rid) for rid in (config.ADMIN_ROLE_IDS | config.MODERATOR_ROLE_IDS)] if interaction.guild else []
        admin_roles = [r for r in admin_roles if r]
        content = " ".join(r.mention for r in admin_roles) if admin_roles else None
        try:
            await report_channel.send(content=content, embed=embed)
        except discord.HTTPException as exc:
            # The player was already told "sent". Without this the report
            # would vanish with only a traceback; route it to the incident
            # log so an admin at least learns a report was lost.
            logger.exception("report post failed for match_id=%s", match["id"])
            await incident_log.post(
                self.bot,
                category="REPORT_POST_FAIL",
                summary=f"/{kind} report for match {match['match_id']} could not be posted to REPORT_CHANNEL_ID={config.REPORT_CHANNEL_ID}",
                exc=exc,
                match=match,
            )

    @app_commands.command(name="afk", description="Report a player (including the host) who isn't following through on this match")
    @app_commands.describe(target="The player who's gone AFK/unresponsive", reason="Optional — what happened")
    async def afk(self, interaction: discord.Interaction, target: discord.Member, reason: str = "No reason given"):
        await self._submit_match_report(interaction, target, reason, kind="afk")

    @app_commands.command(name="report", description="Report a player in this match (wrong operator, cheating, toxicity, etc.)")
    @app_commands.describe(
        target="The player you're reporting",
        details="What happened — write it in your own words, the more specific the better",
    )
    async def report(self, interaction: discord.Interaction, target: discord.Member,
                     details: app_commands.Range[str, 5, 900]):
        await self._submit_match_report(interaction, target, details, kind="report")

    @tasks.loop(minutes=config.CLEANUP_SWEEP_INTERVAL_MINUTES)
    async def cleanup_sweep(self):
        """DB-backed, not an in-memory timer — a scheduled deletion
        survives a bot restart because the due-timestamp lives in the
        matches table, not in a coroutine's memory. See DECISIONS.md."""
        now_iso = discord.utils.utcnow().isoformat()
        try:
            # with_retry (2026-09-11): was a bare adb call — a single
            # transient network blip (RemoteProtocolError,
            # ReadError, etc.) skipped this ENTIRE sweep cycle rather
            # than just retrying the one call, unlike every other DB
            # call site in this file. Confirmed live 4 times (Sept
            # 3-7) via MATCH_APPROVAL_SWEEP_FAIL's sibling category on
            # this exact call. Low real-world impact (next sweep runs
            # CLEANUP_SWEEP_INTERVAL_MINUTES later and catches the same
            # due matches), but free to fix — with_retry is already
            # imported and used everywhere else in this file.
            due = await with_retry(adb.get_due_cleanups, now_iso)
        except Exception:
            logger.exception("cleanup_sweep: get_due_cleanups failed")
            return

        for match in due:
            channel_id = match.get("text_channel_id")
            if not channel_id:
                await adb.clear_cleanup(match["id"])
                continue
            channel = self.bot.get_channel(int(channel_id))
            try:
                if channel:
                    await channel.delete(reason="Scheduled cleanup — match completed/abandoned, grace window elapsed")
            except discord.HTTPException as exc:
                logger.exception("cleanup_sweep: failed to delete channel_id=%s for match_id=%s", channel_id, match["id"])
                await incident_log.post(
                    self.bot,
                    category="QUEUE_DISCORD_API_FAIL",
                    summary=f"cleanup_sweep: failed to delete channel_id={channel_id} for match_id={match['id']} — will retry next sweep",
                    exc=exc,
                    match=match,
                )
                # Don't clear cleanup_at on failure — leave it due so the next sweep retries.
                continue
            await adb.clear_cleanup(match["id"])

    @cleanup_sweep.before_loop
    async def before_cleanup_sweep(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    cog = Queue(bot)
    await bot.add_cog(cog)
    cog.cleanup_sweep.start()
    for queue_key in config.QUEUE_KEYS:
        # Fix 2026-07-29: every runtime call site (handle_join,
        # handle_leave, handle_start_match, _start_match_flow) calls
        # update_view_state() right after constructing/reusing a view —
        # this boot-time registration was the one path that skipped it.
        # If a queue already had >=10 waiting players at the moment the
        # bot restarted, the freshly-registered view's start_match_button
        # was never re-added as a child, even though Discord still showed
        # the old message with the button rendered. Click -> dead
        # interaction -> silent "didn't respond in time", zero logs.
        view = RegionQueueView(queue_key, cog)
        current_queue = await adb.queue_current(queue_key=queue_key)
        await view.update_view_state(current_queue)
        bot.add_view(view)