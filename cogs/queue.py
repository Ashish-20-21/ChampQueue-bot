from __future__ import annotations

import asyncio
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database.db import db, adb
from services import matchmaking, mmr_engine, reputation
from utils.permissions import admin_only

logger = logging.getLogger("champions_queue")


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

    def __init__(self, match_id: int, team: str, team_player_ids: set[int]):
        super().__init__(timeout=config.VOTE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.team = team
        self.team_player_ids = team_player_ids
        self.taken_skills: set[str] = set()
        self.voted_player_ids: set[int] = set()
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
            # Stop Discord's 3-second clock FIRST, before any DB calls.
            # Under concurrent votes (8-10 players clicking within the same
            # window), get_player_by_discord_id + the vote write compete
            # for the same connection pool — by the later clicks, those two
            # round trips alone can exceed 3s even though nothing is
            # actually broken. defer() is a single fast Discord-side call
            # with no DB dependency, so it wins that race every time.
            #
            # ephemeral=True: if the later edit_original_response ever fails
            # (stale token under load — see the 5th-voter race note below),
            # a non-ephemeral defer makes Discord render a public red
            # "interaction failed" to the player even though their vote was
            # saved. Ephemeral keeps any failure quiet and consistent with
            # the followup fallback. Fix 2026-07-30 after a live report of
            # exactly this on the team-completing (5th) vote.
            await interaction.response.defer(ephemeral=True)

            player = await adb.get_player_by_discord_id(interaction.user.id)
            if not player or player["id"] not in self.team_player_ids:
                await interaction.followup.send("This isn't your team's vote.", ephemeral=True)
                return
            if player["id"] in self.voted_player_ids:
                await interaction.followup.send(
                    "You've already picked an operator skill for this match — it's locked in, "
                    "you can't change it. Check the button showing your name for what you picked.",
                    ephemeral=True,
                )
                return
            if skill in self.taken_skills:
                await interaction.followup.send(
                    f"**{skill}** was already picked by a teammate — operator skills must be unique per team.",
                    ephemeral=True,
                )
                return

            # In-memory lock-in — instant, same as before, no DB round
            # trip in the critical path of the click itself.
            self.taken_skills.add(skill)
            self.voted_player_ids.add(player["id"])
            self.pending_votes.append({
                "match_id": self.match_id,
                "player_id": player["id"],
                "team": self.team,
                "skill": skill,
            })
            button.disabled = True
            button.label = f"{skill} ✓ ({player['ign']})"

            # Update the player's UI FIRST, before any DB write. Fix
            # 2026-07-30: previously, when this click was the 5th (team-
            # completing) vote, _flush_votes() ran inline HERE — a real
            # Supabase round-trip inserted between the defer and the
            # response edit. Under concurrent load (pool contention while
            # 8-10 players click at once) that extra latency could push
            # edit_original_response past its valid token window, so it
            # threw and the player saw "interaction failed" even though
            # their vote was saved. The 5th voter did strictly more work
            # in the critical path than voters 1-4, making them
            # structurally the one who fails. Now the UI edit happens
            # first (fast, no DB), and the flush moves after it.
            edit_failed = False
            try:
                await interaction.edit_original_response(view=self)
            except (discord.errors.NotFound, discord.errors.HTTPException) as e:
                edit_failed = True
                logger.warning(
                    "SkillVoteView: edit_original_response failed for player_id=%s, skill=%s (vote will still be recorded): %s",
                    player["id"], skill, e,
                )

            # Now flush to the DB, AFTER the player's UI has already been
            # answered — the player is never waiting on this write, so its
            # latency can no longer break their interaction response. Once
            # the whole team (5/5) has picked, this is the single bulk
            # write that collapses 5 individual writes into 1; otherwise
            # the pending votes wait for the next completing click or the
            # on_timeout fallback.
            if len(self.voted_player_ids) >= len(self.team_player_ids):
                await self._flush_votes()

            # Only if the visual update actually failed do we send the
            # plain-text confirmation fallback, so the player still knows
            # their pick locked in even though the button display didn't
            # refresh on their end.
            if edit_failed:
                try:
                    await interaction.followup.send(
                        f"Your pick (**{skill}**) is locked in — your vote was saved successfully "
                        f"even though the button display didn't update.",
                        ephemeral=True,
                    )
                except discord.errors.HTTPException as e2:
                    logger.warning(
                        "SkillVoteView: fallback followup also failed for player_id=%s, skill=%s "
                        "(vote already recorded, player will see it as failed on their end): %s",
                        player["id"], skill, e2,
                    )

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
    @admin_only()
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

    async def handle_join(self, interaction: discord.Interaction, queue_key: str, view: RegionQueueView):
        # Defer FIRST, before any DB round trip — same fix as SkillVoteView
        # above. Under concurrent clicks (queue filling up), the sequential
        # get_player_by_discord_id + queue_current + queue_join round trips
        # can exceed Discord's 3-second ack window on their own even though
        # nothing is actually broken; deferring first wins that race every
        # time instead of leaving the first response call to gamble on it
        # (see the 10062 "Unknown interaction" write-up in DECISIONS.md).
        await interaction.response.defer()

        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.followup.send("You need to `/register` and be approved first.", ephemeral=True)
            return
        if player["status"] != "approved":
            await interaction.followup.send(f"Your registration is `{player['status']}`, not approved yet.", ephemeral=True)
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
            await interaction.followup.send(reason, ephemeral=True)
            return

        async with self._locks[queue_key]:
            current_queue = await adb.queue_current(queue_key=queue_key)
            if len(current_queue) >= 10:
                await interaction.followup.send(
                    "Queue is full (10/10) — a match is about to start. Try again in a moment.",
                    ephemeral=True,
                )
                return

            entry = await adb.queue_join(player["id"], queue_key)
            if entry is None:
                await interaction.followup.send("You're already in the queue.", ephemeral=True)
                return

            current_queue = await adb.queue_current(queue_key=queue_key)
            await view.update_view_state(current_queue)
            embed = make_queue_embed(queue_key, current_queue)
            # DB write above already succeeded — that's the source of
            # truth. This is just the visual ack; fall back to a log entry
            # instead of an unhandled exception if the interaction token
            # went stale (e.g. network jitter), so the player's join is
            # never lost even if the button UI doesn't refresh for them.
            try:
                await interaction.edit_original_response(embed=embed, view=view)
            except (discord.errors.NotFound, discord.errors.HTTPException) as e:
                logger.warning("handle_join: edit_original_response failed for player_id=%s (join already saved): %s", player["id"], e)

    async def handle_leave(self, interaction: discord.Interaction, queue_key: str, view: RegionQueueView):
        # Defer first — see handle_join above for why.
        await interaction.response.defer()

        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.followup.send("You're not registered.", ephemeral=True)
            return

        async with self._locks[queue_key]:
            current_queue = await adb.queue_current(queue_key=queue_key)
            in_queue = any(p["player_id"] == player["id"] for p in current_queue)
            if not in_queue:
                await interaction.followup.send("You're not in the queue.", ephemeral=True)
                return

            await adb.queue_leave(player["id"])
            current_queue = await adb.queue_current(queue_key=queue_key)
            await view.update_view_state(current_queue)
            embed = make_queue_embed(queue_key, current_queue)
            try:
                await interaction.edit_original_response(embed=embed, view=view)
            except (discord.errors.NotFound, discord.errors.HTTPException) as e:
                logger.warning("handle_leave: edit_original_response failed for player_id=%s (leave already saved): %s", player["id"], e)

    async def handle_start_match(self, interaction: discord.Interaction, queue_key: str, view: RegionQueueView):
        # Defer first, before the get_player_by_discord_id / queue_current /
        # lock-wait chain below — same fix as handle_join/handle_leave.
        await interaction.response.defer(ephemeral=True)

        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.followup.send("You're not registered.", ephemeral=True)
            return

        async with self._locks[queue_key]:
            current_queue = await adb.queue_current(queue_key=queue_key)
            if len(current_queue) < 10:
                await interaction.followup.send("The queue no longer has 10 players.", ephemeral=True)
                await view.update_view_state(current_queue)
                embed = make_queue_embed(queue_key, current_queue)
                await interaction.message.edit(embed=embed, view=view)
                return

            queued_player_ids = {p["player_id"] for p in current_queue}
            if player["id"] not in queued_player_ids:
                await interaction.followup.send(
                    "Only players currently in the queue can start the match.", ephemeral=True
                )
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
                await interaction.followup.send(
                    f"Can't start this match — these players have invalid Discord IDs and can't be "
                    f"added to a real channel: {', '.join(bad_ids)}. (This usually means test/fake "
                    f"data is still in the queue — clear it before testing Start Match.)",
                    ephemeral=True,
                )
                return

            player_ids = [p["player_id"] for p in pop]
            await adb.queue_mark_matched(player_ids)

            # Reset the persistent queue panel message back to current queue state (minus the matched 10)
            remaining_queue = await adb.queue_current(queue_key=queue_key)
            new_view = RegionQueueView(queue_key, self)
            await new_view.update_view_state(remaining_queue)
            new_embed = make_queue_embed(queue_key, remaining_queue)
            await interaction.message.edit(embed=new_embed, view=new_view)
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
            await interaction.followup.send("Match started successfully!", ephemeral=True)
        except Exception:
            logger.exception(
                f"_start_match_flow failed for queue_key={queue_key}, host_player_id={player['id']}. "
                f"Rolling back {len(player_ids)} players to 'waiting'."
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
            except Exception:
                logger.exception(
                    f"Rollback ALSO failed for player_ids={player_ids} in queue_key={queue_key} — "
                    f"these players may be stuck as 'matched' with no channel. Needs manual DB check."
                )
            # Message reworded 2026-08-19: avoid implying the bot itself
            # is broken (players read "something went wrong" as a bot
            # malfunction). This is framed as an automatic safety measure
            # catching a Discord-side hiccup (channel/VC creation,
            # permissions, rate limits — see comment above this try
            # block) or a rare internal ID conflict, not a bot failure.
            await interaction.followup.send(
                "This match couldn't be started due to a brief sync issue with Discord — "
                "as a precaution, you've been placed back in queue automatically. "
                "No action needed on your end, just try Start Match again.",
                ephemeral=True,
            )

    async def _start_match_flow(self, interaction: discord.Interaction, players: list[dict], host_player_id: int, queue_key: str):
        channel = interaction.channel
        player_ids = [p["id"] for p in players]
        bootstrap = await matchmaking.is_bootstrap_match(player_ids)

        # Call balancing function (captains removed from return type)
        _ = matchmaking.balance_teams(players, bootstrap=bootstrap)

        # Split 10 queued players by index (Defender vs Attacker)
        # Players 1,3,5,7,9 (0, 2, 4, 6, 8) = Defender
        # Players 2,4,6,8,10 (1, 3, 5, 7, 9) = Attacker
        team_a = [players[0], players[2], players[4], players[6], players[8]]  # Defender
        team_b = [players[1], players[3], players[5], players[7], players[9]]  # Attacker

        # Create match (no captains assigned)
        match = await adb.create_match(is_bootstrap=bootstrap, queue_key=queue_key)
        await adb.update_match(match["id"], {
            "room_code_shared_by": host_player_id
        })

        for p in team_a:
            await adb.add_match_player(match["id"], p["id"], "A", is_captain=False)
        for p in team_b:
            await adb.add_match_player(match["id"], p["id"], "B", is_captain=False)

        guild = channel.guild
        category = channel.category
        # Unified 2026-07-29: was a single admin_role lookup. Now loops
        # over every role in ADMIN_ROLE_IDS (HOD + admin team all get
        # identical visibility into match channels) — see
        # utils/permissions.py's is_admin() for the same set used
        # elsewhere. Missing/invalid role IDs are silently skipped
        # (guild.get_role returns None) rather than raising, consistent
        # with the old single-role "if admin_role:" fail-open pattern.
        admin_roles = [r for r in (guild.get_role(rid) for rid in config.ADMIN_ROLE_IDS) if r]

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

        await adb.update_match(match["id"], {
            "text_channel_id": str(text_channel.id),
            "voice_channel_a_id": str(vc_a.id),
            "voice_channel_b_id": str(vc_b.id),
            "status": "forming"
        })

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
            value="\n\n".join(f"**{p['ign']}**\n<@{p['discord_id']}>" for p in team_a),
            inline=True,
        )
        embed_teams.add_field(
            name="⚔️ Team Attacker",
            value="\n\n".join(f"**{p['ign']}**\n<@{p['discord_id']}>" for p in team_b),
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
        maps = await matchmaking.pick_map_candidates(list(team_a_ids), list(team_b_ids), bootstrap, n=1)
        await adb.update_match(match["id"], {
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
        await text_channel.send(
            # f"{mentions}\n\n"
            f"Voice: {vc_a.mention} (Defender) / {vc_b.mention} (Attacker)\n\n"
            f"Host {host_mention}: share the room code here with `+rc<code>` "
            f"(or `/rc <code>`). Made a typo? Use `+urc<code>` to correct it.\n"
            f"Make sure to select your operator skill above ⬆️ — no rush, select whenever you're ready."
        )

        # Skill votes — no blocking wait here anymore. Views are sent and
        # the flow ends; each view batches its own team's writes (flushed
        # at 5/5, or as a fallback on Discord's own view timeout — see
        # SkillVoteView.on_timeout). Nothing downstream (room code,
        # match-log, MMR, approval) depends on skill votes being complete,
        # so there's nothing here to wait on before finishing the flow.
        view_a = SkillVoteView(match["id"], "A", team_a_ids)
        view_b = SkillVoteView(match["id"], "B", team_b_ids)
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


    @app_commands.command(name="afk", description="Report a player (including the host) who isn't following through on this match")
    @app_commands.describe(target="The player who's gone AFK/unresponsive", reason="Optional — what happened")
    async def afk(self, interaction: discord.Interaction, target: discord.Member, reason: str = "No reason given"):
        if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.name.startswith("cq-"):
            await interaction.response.send_message("This only works inside a match channel.", ephemeral=True)
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

        is_host = reported["id"] == match.get("room_code_shared_by")
        await interaction.response.send_message(
            f"Report sent to admins for review — no action has been taken automatically.", ephemeral=True
        )

        if not config.AFK_CHANNEL_ID:
            logger.warning("AFK_CHANNEL_ID not configured — /AFK report for match_id=%s was not posted anywhere", match["id"])
            return
        afk_channel = self.bot.get_channel(config.AFK_CHANNEL_ID)
        if not afk_channel:
            logger.warning("AFK_CHANNEL_ID=%s not found/accessible", config.AFK_CHANNEL_ID)
            return

        embed = discord.Embed(
            title=f"⚠️ AFK Report — Match {match['match_id']}",
            description=(
                f"**Reported:** {target.mention} ({reported['ign']}){' — this is the match Host' if is_host else ''}\n"
                f"**Reported by:** {interaction.user.mention} ({reporter['ign']})\n"
                f"**Reason:** {reason}"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="No automatic action taken. Requires admin review — see /admin-scrap-match.")
        # Unified 2026-07-29: was a single admin_role mention. Now pings
        # every role in ADMIN_ROLE_IDS so any admin (or HOD) gets
        # notified, not just whoever held the old single role.
        admin_roles = [interaction.guild.get_role(rid) for rid in config.ADMIN_ROLE_IDS] if interaction.guild else []
        admin_roles = [r for r in admin_roles if r]
        content = " ".join(r.mention for r in admin_roles) if admin_roles else None
        await afk_channel.send(content=content, embed=embed)

    @tasks.loop(minutes=config.CLEANUP_SWEEP_INTERVAL_MINUTES)
    async def cleanup_sweep(self):
        """DB-backed, not an in-memory timer — a scheduled deletion
        survives a bot restart because the due-timestamp lives in the
        matches table, not in a coroutine's memory. See DECISIONS.md."""
        now_iso = discord.utils.utcnow().isoformat()
        try:
            due = await adb.get_due_cleanups(now_iso)
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
            except discord.HTTPException:
                logger.exception("cleanup_sweep: failed to delete channel_id=%s for match_id=%s", channel_id, match["id"])
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