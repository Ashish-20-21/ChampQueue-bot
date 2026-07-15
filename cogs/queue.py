from __future__ import annotations

import asyncio
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database.db import db, adb
from services import matchmaking, reputation
from utils.permissions import admin_only

logger = logging.getLogger("champions_queue")


class SkillVoteView(discord.ui.View):
    """One view per team. Enforces unique-skill-per-team by disabling
    a skill button for everyone on that team once someone picks it, AND
    locks each player to their first vote — once you've picked, you can't
    switch to a different skill. This matters beyond UI polish: if a
    player could silently swap picks mid-vote, teammates and the match-log
    record could show a different skill than what the player actually
    ends up using in-game, which risks a false /AFK or dispute report."""

    def __init__(self, match_id: int, team: str, team_player_ids: set[int]):
        super().__init__(timeout=config.VOTE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.team = team
        self.team_player_ids = team_player_ids
        self.taken_skills: set[str] = set()
        self.voted_player_ids: set[int] = set()
        for skill in config.OPERATOR_SKILLS:
            self.add_item(self._make_button(skill))

    def _make_button(self, skill: str) -> discord.ui.Button:
        button = discord.ui.Button(label=skill, style=discord.ButtonStyle.secondary)

        async def callback(interaction: discord.Interaction):
            # Stop Discord's 3-second clock FIRST, before any DB calls.
            # Under concurrent votes (8-10 players clicking within the same
            # window), get_player_by_discord_id + cast_skill_vote compete
            # for the same connection pool — by the later clicks, those two
            # round trips alone can exceed 3s even though nothing is
            # actually broken. defer() is a single fast Discord-side call
            # with no DB dependency, so it wins that race every time.
            await interaction.response.defer()

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

            await adb.cast_skill_vote(self.match_id, player["id"], self.team, skill)
            self.taken_skills.add(skill)
            self.voted_player_ids.add(player["id"])
            button.disabled = True
            button.label = f"{skill} ✓ ({player['ign']})"

            # The DB write above already succeeded — that's the source of
            # truth. This visual update can still fail on a stale/expired
            # interaction token in rare cases, which would otherwise
            # leave the player thinking their vote didn't register even
            # though it did. Fall back to a plain confirmation message so
            # they always know their pick locked in correctly.
            try:
                await interaction.edit_original_response(view=self)
            except (discord.errors.NotFound, discord.errors.HTTPException) as e:
                logger.warning(
                    "SkillVoteView: edit_original_response failed for player_id=%s, skill=%s (vote already saved): %s",
                    player["id"], skill, e,
                )
                try:
                    await interaction.followup.send(
                        f"Your pick (**{skill}**) is locked in — your vote was saved successfully "
                        f"even though the button display didn't update.",
                        ephemeral=True,
                    )
                except discord.errors.HTTPException as e2:
                    logger.warning(
                        "SkillVoteView: fallback followup also failed for player_id=%s, skill=%s "
                        "(vote already saved, player will see it as failed on their end): %s",
                        player["id"], skill, e2,
                    )

        button.callback = callback
        return button


def make_queue_embed(region: str, current_queue: list[dict]) -> discord.Embed:
    player_lines = []
    for idx, p in enumerate(current_queue, 1):
        player_info = p["players"]
        ign = player_info.get("ign", "Unknown")
        mmr = player_info.get("mmr", 1000)
        rank = player_info.get("current_rank")
        division = player_info.get("current_division")

        rank_str = f" [{rank} {division}]" if rank else ""
        player_lines.append(f"`{idx:02d}` **{ign}**{rank_str} — MMR: {mmr}")

    names = "\n".join(player_lines) if player_lines else "*No players in queue. Be the first to join!*"

    embed = discord.Embed(
        title=f"🛡️ Champion's Queue — {region.upper()} Region",
        description=f"Join the competitive matchmaking lobby for the **{region.upper()}** region.",
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.add_field(name=f"👥 Active Queue ({len(current_queue)}/10)", value=names, inline=False)
    embed.set_footer(text="Champions Queue Matchmaker • First 10 players can start the match.")
    return embed


class RegionQueueView(discord.ui.View):
    def __init__(self, region: str, cog: Queue):
        super().__init__(timeout=None)
        self.region = region
        self.cog = cog

        self.join_button = discord.ui.Button(
            label="Join Queue",
            style=discord.ButtonStyle.success,
            custom_id=f"join_queue_{region}"
        )
        self.join_button.callback = self.join_callback
        self.add_item(self.join_button)

        self.leave_button = discord.ui.Button(
            label="Leave Queue",
            style=discord.ButtonStyle.danger,
            custom_id=f"leave_queue_{region}"
        )
        self.leave_button.callback = self.leave_callback
        self.add_item(self.leave_button)

        self.start_match_button = discord.ui.Button(
            label="Start Match",
            style=discord.ButtonStyle.primary,
            custom_id=f"start_match_{region}"
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
        await self.cog.handle_join(interaction, self.region, self)

    async def leave_callback(self, interaction: discord.Interaction):
        await self.cog.handle_leave(interaction, self.region, self)

    async def start_match_callback(self, interaction: discord.Interaction):
        await self.cog.handle_start_match(interaction, self.region, self)


class Queue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-region locks, not one shared lock — a match forming in East
        # should never block Join/Leave clicks in West. See the 10062
        # "Unknown interaction" bug write-up in DECISIONS.md for why this
        # matters: the old single lock, combined with handle_start_match
        # holding it through the whole skill-vote wait, starved unrelated
        # button clicks past Discord's 3-second interaction-ack window.
        self._locks: dict[str, asyncio.Lock] = {"East": asyncio.Lock(), "West": asyncio.Lock()}

    def cog_unload(self):
        self.cleanup_sweep.cancel()

    @app_commands.command(name="queue-post", description="Post the persistent queue panel for a specific region")
    @app_commands.describe(region="The competitive region (East, West)")
    @admin_only()
    async def queue_post(self, interaction: discord.Interaction, region: str):
        region_norm = region.capitalize()
        if region_norm not in ["East", "West"]:
            await interaction.response.send_message(
                "Invalid region. Please specify either 'East' or 'West'.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        current_queue = await adb.queue_current(region=region_norm)
        view = RegionQueueView(region_norm, self)
        await view.update_view_state(current_queue)

        embed = make_queue_embed(region_norm, current_queue)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send(f"Successfully posted the persistent queue panel for **{region_norm}**.", ephemeral=True)

    @app_commands.command(name="queue-status", description="See who's currently in queue for a region")
    @app_commands.describe(region="The competitive region (East, West)")
    async def queue_status(self, interaction: discord.Interaction, region: str):
        region_norm = region.capitalize()
        if region_norm not in ["East", "West"]:
            await interaction.response.send_message(
                "Invalid region. Please specify either 'East' or 'West'.", ephemeral=True
            )
            return

        current = await adb.queue_current(region=region_norm)
        names = ", ".join(p["players"]["ign"] for p in current) or "empty"
        await interaction.response.send_message(f"**{region_norm} Queue ({len(current)}/10):** {names}")

    async def handle_join(self, interaction: discord.Interaction, region: str, view: RegionQueueView):
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

        if player.get("region") != region:
            await interaction.followup.send(
                f"Your registered region is **{player.get('region')}**. You cannot join the **{region}** queue.",
                ephemeral=True
            )
            return

        eligible, reason = reputation.is_queue_eligible(player)
        if not eligible:
            await interaction.followup.send(reason, ephemeral=True)
            return

        async with self._locks[region]:
            current_queue = await adb.queue_current(region=region)
            if len(current_queue) >= 10:
                await interaction.followup.send(
                    "Queue is full (10/10) — a match is about to start. Try again in a moment.",
                    ephemeral=True,
                )
                return

            entry = await adb.queue_join(player["id"])
            if entry is None:
                await interaction.followup.send("You're already in the queue.", ephemeral=True)
                return

            current_queue = await adb.queue_current(region=region)
            await view.update_view_state(current_queue)
            embed = make_queue_embed(region, current_queue)
            # DB write above already succeeded — that's the source of
            # truth. This is just the visual ack; fall back to a log entry
            # instead of an unhandled exception if the interaction token
            # went stale (e.g. network jitter), so the player's join is
            # never lost even if the button UI doesn't refresh for them.
            try:
                await interaction.edit_original_response(embed=embed, view=view)
            except (discord.errors.NotFound, discord.errors.HTTPException) as e:
                logger.warning("handle_join: edit_original_response failed for player_id=%s (join already saved): %s", player["id"], e)

    async def handle_leave(self, interaction: discord.Interaction, region: str, view: RegionQueueView):
        # Defer first — see handle_join above for why.
        await interaction.response.defer()

        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.followup.send("You're not registered.", ephemeral=True)
            return

        async with self._locks[region]:
            current_queue = await adb.queue_current(region=region)
            in_queue = any(p["player_id"] == player["id"] for p in current_queue)
            if not in_queue:
                await interaction.followup.send("You're not in the queue.", ephemeral=True)
                return

            await adb.queue_leave(player["id"])
            current_queue = await adb.queue_current(region=region)
            await view.update_view_state(current_queue)
            embed = make_queue_embed(region, current_queue)
            try:
                await interaction.edit_original_response(embed=embed, view=view)
            except (discord.errors.NotFound, discord.errors.HTTPException) as e:
                logger.warning("handle_leave: edit_original_response failed for player_id=%s (leave already saved): %s", player["id"], e)

    async def handle_start_match(self, interaction: discord.Interaction, region: str, view: RegionQueueView):
        # Defer first, before the get_player_by_discord_id / queue_current /
        # lock-wait chain below — same fix as handle_join/handle_leave.
        await interaction.response.defer(ephemeral=True)

        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.followup.send("You're not registered.", ephemeral=True)
            return

        async with self._locks[region]:
            current_queue = await adb.queue_current(region=region)
            if len(current_queue) < 10:
                await interaction.followup.send("The queue no longer has 10 players.", ephemeral=True)
                await view.update_view_state(current_queue)
                embed = make_queue_embed(region, current_queue)
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
            remaining_queue = await adb.queue_current(region=region)
            new_view = RegionQueueView(region, self)
            await new_view.update_view_state(remaining_queue)
            new_embed = make_queue_embed(region, remaining_queue)
            await interaction.message.edit(embed=new_embed, view=new_view)
            # Lock released here — everything below (channel creation, the
            # 120s skill-vote wait) is slow, and the 10 players are already
            # marked matched + off the queue panel, so there's nothing left
            # for the lock to protect. Holding it through this used to
            # freeze Join/Leave for this whole region (worse: for BOTH
            # regions, before the per-region split above) until the skill
            # vote timer expired — see DECISIONS.md for the 10062 write-up.

        # Spawn the match creation and setup. Everything past this point
        # touches Discord's API (channel/VC creation) which can fail for
        # reasons outside our control (permissions, rate limits, etc).
        # If it does, roll the 10 players back to 'waiting' instead of
        # leaving them stranded in a dead 'forming' match with no path
        # back into the queue.
        try:
            await self._start_match_flow(interaction, players_list, player["id"], region)
            await interaction.followup.send("Match started successfully!", ephemeral=True)
        except Exception:
            logger.exception(
                f"_start_match_flow failed for region={region}, host_player_id={player['id']}. "
                f"Rolling back {len(player_ids)} players to 'waiting'."
            )
            await adb.queue_mark_waiting(player_ids)
            await interaction.followup.send(
                "Something went wrong setting up the match — you've been returned to the queue. "
                "An admin has been notified.",
                ephemeral=True,
            )

    async def _start_match_flow(self, interaction: discord.Interaction, players: list[dict], host_player_id: int, region: str):
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
        match = await adb.create_match(is_bootstrap=bootstrap)
        await adb.update_match(match["id"], {
            "room_code_shared_by": host_player_id
        })

        for p in team_a:
            await adb.add_match_player(match["id"], p["id"], "A", is_captain=False)
        for p in team_b:
            await adb.add_match_player(match["id"], p["id"], "B", is_captain=False)

        guild = channel.guild
        category = channel.category
        admin_role = guild.get_role(config.ADMIN_ROLE_ID)

        # Private text channel overwrites
        overwrites_text = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        if admin_role:
            overwrites_text[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        for p in players:
            member = guild.get_member(int(p["discord_id"]))
            if member:
                overwrites_text[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        text_channel = await guild.create_text_channel(
            name=f"qc-{match['match_id'].lower()}",
            category=category,
            overwrites=overwrites_text
        )

        # Private VC A overwrites (Defender Team)
        overwrites_vc_a = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        }
        if admin_role:
            overwrites_vc_a[admin_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        for p in team_a:
            member = guild.get_member(int(p["discord_id"]))
            if member:
                overwrites_vc_a[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        vc_a = await guild.create_voice_channel(
            name=f"🛡️ {match['match_id']} Defender",
            category=category,
            overwrites=overwrites_vc_a
        )

        # Private VC B overwrites (Attacker Team)
        overwrites_vc_b = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        }
        if admin_role:
            overwrites_vc_b[admin_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        for p in team_b:
            member = guild.get_member(int(p["discord_id"]))
            if member:
                overwrites_vc_b[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        vc_b = await guild.create_voice_channel(
            name=f"⚔️ {match['match_id']} Attacker",
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
            title=f"Match {match['match_id']} — Teams Formed ({region.upper()})",
            color=discord.Color.blue()
        )
        embed_teams.add_field(name="🛡️ Team Defender", value="\n".join(p["ign"] for p in team_a), inline=True)
        embed_teams.add_field(name="⚔️ Team Attacker", value="\n".join(p["ign"] for p in team_b), inline=True)
        # Mode footer intentionally not shown to players — bootstrap is an
        # internal matchmaking detail, not player-facing info. Still stored
        # on the match row (is_bootstrap) for later analysis.
        await text_channel.send(embed=embed_teams)

        # Map selection and announcement (no vote)
        team_a_ids = {p["id"] for p in team_a}
        team_b_ids = {p["id"] for p in team_b}
        maps = await matchmaking.pick_map_candidates(list(team_a_ids), list(team_b_ids), bootstrap, n=3)
        await adb.update_match(match["id"], {
            "map_pool": maps,
            "map": maps[0],
            "status": "awaiting_room"
        })

        embed_maps = discord.Embed(
            title="🗺️ Map Selection",
            description=f"Map chosen are: **{maps[0]}**, **{maps[1]}**, **{maps[2]}**",
            color=discord.Color.gold()
        )
        embed_maps.set_footer(text="Round 1: Map 1 | Round 2: Map 2 | Round 3: Map 3")
        await text_channel.send(embed=embed_maps)

        mentions = " ".join(f"<@{p['discord_id']}>" for p in players)
        await text_channel.send(
            f"{mentions}\n\n"
            f"Voice: {vc_a.mention} (Defender) / {vc_b.mention} (Attacker)\n\n"
            f"Host {host_mention}: share the room code here with `+roomcode<code>` "
            f"(or `/rc <code>`). Made a typo? Use `+updateroomcode<code>` to correct it."
        )

        # Skill votes
        view_a = SkillVoteView(match["id"], "A", team_a_ids)
        view_b = SkillVoteView(match["id"], "B", team_b_ids)
        await text_channel.send(f"**Defender Team** — vote your operator skill (unique per team):", view=view_a)
        await text_channel.send(f"**Attacker Team** — vote your operator skill (unique per team):", view=view_b)
        await asyncio.sleep(config.VOTE_TIMEOUT_SECONDS)
        await self._finalize_skill_vote(match["id"], "A", team_a, view_a)
        await self._finalize_skill_vote(match["id"], "B", team_b, view_b)

    async def _finalize_skill_vote(self, match_id: int, team: str, team_players: list[dict], view: SkillVoteView):
        votes = await adb.get_skill_votes(match_id, team)
        voted_ids = {v["player_id"] for v in votes}
        missing = [p for p in team_players if p["id"] not in voted_ids]
        available_skills = [s for s in config.OPERATOR_SKILLS if s not in view.taken_skills]
        for p in missing:
            skill = available_skills.pop(0) if available_skills else random.choice(config.OPERATOR_SKILLS)
            await adb.cast_skill_vote(match_id, p["id"], team, skill)
        view.stop()

    async def _handle_room_code_share(self, message_or_interaction, channel: discord.TextChannel,
                                        author_id: int, code: str, respond) -> None:
        """Shared logic for both the +roomcode text trigger and the /rc
        slash alias — same host-privilege check, same DB write, same
        match-log post either way. `respond` is a callable(str) that sends
        feedback back through whichever entry point was used."""
        if not channel.name.startswith("qc-"):
            await respond("Room codes can only be shared in a match channel.")
            return

        match_code = channel.name[len("qc-"):].upper()
        match = await adb.get_match_by_code(match_code)
        if not match:
            await respond("Couldn't find a match tied to this channel.")
            return

        player = await adb.get_player_by_discord_id(author_id)
        if not player or match.get("room_code_shared_by") != player["id"]:
            await respond("Only the match host can set or change the room code.")
            return

        is_first_share = match.get("room_code") is None
        await adb.update_match(match["id"], {
            "room_code": code,
            "status": "in_progress"
        })
        await respond(f"Room code updated to **{code}**. Match is now live!")

        # Match-log entry: only post fresh on the *first* share. A
        # correction just updates the room code in place — re-posting a
        # whole new log entry on every typo-fix would clutter the log
        # channel with duplicates for the same match.
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

        map_pool = match.get("map_pool") or [match.get("map", "—")]
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

        # Rename both VCs to include the room code, once — not on every
        # correction, since Discord only allows 2 name/topic edits per 10
        # minutes per channel, and a fast +updateroomcode right after would
        # burn that budget. VC name already carries the match ID from
        # creation, so this is purely the "which room to jump into" cue.
        guild = channel.guild
        for vc_field, label in (("voice_channel_a_id", "🛡️ Defender"), ("voice_channel_b_id", "⚔️ Attacker")):
            vc_id = match.get(vc_field)
            if not vc_id:
                continue
            vc = guild.get_channel(int(vc_id))
            if vc:
                try:
                    await vc.edit(name=f"{label} · {room_code}")
                except discord.HTTPException as e:
                    logger.warning("Failed to rename VC %s with room code for match_id=%s: %s", vc_id, match_id, e)

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
        await self._handle_room_code_share(interaction, interaction.channel, interaction.user.id, code.strip(), respond)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.channel.name.startswith("qc-"):
            return

        content = message.content.strip()
        code = None
        # Order matters: "+updateroomcode" also starts with "+room" if you
        # check loosely, so the more specific prefix is checked first.
        if content.lower().startswith("+updateroomcode"):
            code = content[len("+updateroomcode"):].strip()
        elif content.lower().startswith("+roomcode"):
            code = content[len("+roomcode"):].strip()

        if not code:
            return

        async def respond(text: str):
            await message.channel.send(text, delete_after=5 if "Only the match host" in text else None)

        await self._handle_room_code_share(message, message.channel, message.author.id, code, respond)


    @app_commands.command(name="afk", description="Report a player (including the host) who isn't following through on this match")
    @app_commands.describe(target="The player who's gone AFK/unresponsive", reason="Optional — what happened")
    async def afk(self, interaction: discord.Interaction, target: discord.Member, reason: str = "No reason given"):
        if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.name.startswith("qc-"):
            await interaction.response.send_message("This only works inside a match channel.", ephemeral=True)
            return

        reporter = await adb.get_player_by_discord_id(interaction.user.id)
        reported = await adb.get_player_by_discord_id(target.id)
        if not reporter or not reported:
            await interaction.response.send_message("Both players need to be registered.", ephemeral=True)
            return

        match_code = interaction.channel.name[len("qc-"):].upper()
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
        admin_role = interaction.guild.get_role(config.ADMIN_ROLE_ID) if interaction.guild else None
        content = admin_role.mention if admin_role else None
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
    for region in ["East", "West"]:
        bot.add_view(RegionQueueView(region, cog))