from __future__ import annotations

import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import db, adb
from services import matchmaking, reputation
from utils.permissions import admin_only


class SkillVoteView(discord.ui.View):
    """One view per team. Enforces unique-skill-per-team by disabling
    a skill button for everyone on that team once someone picks it."""

    def __init__(self, match_id: int, team: str, team_player_ids: set[int]):
        super().__init__(timeout=config.VOTE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.team = team
        self.team_player_ids = team_player_ids
        self.taken_skills: set[str] = set()
        for skill in config.OPERATOR_SKILLS:
            self.add_item(self._make_button(skill))

    def _make_button(self, skill: str) -> discord.ui.Button:
        button = discord.ui.Button(label=skill, style=discord.ButtonStyle.secondary)

        async def callback(interaction: discord.Interaction):
            player = await adb.get_player_by_discord_id(interaction.user.id)
            if not player or player["id"] not in self.team_player_ids:
                await interaction.response.send_message("This isn't your team's vote.", ephemeral=True)
                return
            if skill in self.taken_skills:
                await interaction.response.send_message(
                    f"**{skill}** was already picked by a teammate — operator skills must be unique per team.",
                    ephemeral=True,
                )
                return
            await adb.cast_skill_vote(self.match_id, player["id"], self.team, skill)
            self.taken_skills.add(skill)
            button.disabled = True
            button.label = f"{skill} ✓ ({player['ign']})"
            await interaction.response.edit_message(view=self)

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
        self._lock = asyncio.Lock()

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
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You need to `/register` and be approved first.", ephemeral=True)
            return
        if player["status"] != "approved":
            await interaction.response.send_message(f"Your registration is `{player['status']}`, not approved yet.", ephemeral=True)
            return

        if player.get("region") != region:
            await interaction.response.send_message(
                f"Your registered region is **{player.get('region')}**. You cannot join the **{region}** queue.",
                ephemeral=True
            )
            return

        eligible, reason = reputation.is_queue_eligible(player)
        if not eligible:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        async with self._lock:
            entry = await adb.queue_join(player["id"])
            if entry is None:
                await interaction.response.send_message("You're already in the queue.", ephemeral=True)
                return

            current_queue = await adb.queue_current(region=region)
            await view.update_view_state(current_queue)
            embed = make_queue_embed(region, current_queue)
            await interaction.response.edit_message(embed=embed, view=view)

    async def handle_leave(self, interaction: discord.Interaction, region: str, view: RegionQueueView):
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return

        async with self._lock:
            current_queue = await adb.queue_current(region=region)
            in_queue = any(p["player_id"] == player["id"] for p in current_queue)
            if not in_queue:
                await interaction.response.send_message("You're not in the queue.", ephemeral=True)
                return

            await adb.queue_leave(player["id"])
            current_queue = await adb.queue_current(region=region)
            await view.update_view_state(current_queue)
            embed = make_queue_embed(region, current_queue)
            await interaction.response.edit_message(embed=embed, view=view)

    async def handle_start_match(self, interaction: discord.Interaction, region: str, view: RegionQueueView):
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return

        async with self._lock:
            current_queue = await adb.queue_current(region=region)
            if len(current_queue) < 10:
                await interaction.response.send_message("The queue no longer has 10 players.", ephemeral=True)
                await view.update_view_state(current_queue)
                embed = make_queue_embed(region, current_queue)
                await interaction.message.edit(embed=embed, view=view)
                return

            queued_player_ids = {p["player_id"] for p in current_queue}
            if player["id"] not in queued_player_ids:
                await interaction.response.send_message(
                    "Only players currently in the queue can start the match.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)
            pop = current_queue[:10]
            player_ids = [p["player_id"] for p in pop]
            await adb.queue_mark_matched(player_ids)

            # Reset the persistent queue panel message back to current queue state (minus the matched 10)
            remaining_queue = await adb.queue_current(region=region)
            new_view = RegionQueueView(region, self)
            await new_view.update_view_state(remaining_queue)
            new_embed = make_queue_embed(region, remaining_queue)
            await interaction.message.edit(embed=new_embed, view=new_view)

            # Spawn the match creation and setup
            players_list = [p["players"] for p in pop]
            await self._start_match_flow(interaction, players_list, player["id"], region)
            await interaction.followup.send("Match started successfully!", ephemeral=True)

    async def _start_match_flow(self, interaction: discord.Interaction, players: list[dict], host_player_id: int, region: str):
        channel = interaction.channel
        player_ids = [p["id"] for p in players]
        bootstrap = matchmaking.is_bootstrap_match(player_ids)

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
            name="Team Defender",
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
        embed_teams.set_footer(text=f"Mode: {'🎲 bootstrap' if bootstrap else '📊 analysis-balanced'}")
        await text_channel.send(embed=embed_teams)

        # Map selection and announcement (no vote)
        team_a_ids = {p["id"] for p in team_a}
        team_b_ids = {p["id"] for p in team_b}
        maps = matchmaking.pick_map_candidates(list(team_a_ids), list(team_b_ids), bootstrap, n=3)
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
            f"Host {host_mention}: share the room code in this channel by sending `+room <code>`."
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.channel.name.startswith("qc-"):
            return

        content = message.content.strip()
        code = None
        if content.lower().startswith("+room "):
            code = content[len("+room "):].strip()
        elif content.lower().startswith("+change room code "):
            code = content[len("+change room code "):].strip()

        if not code:
            return

        match_code = message.channel.name[len("qc-"):].upper()
        match = await adb.get_match_by_code(match_code)
        if not match:
            return

        player = await adb.get_player_by_discord_id(message.author.id)
        if not player or match.get("room_code_shared_by") != player["id"]:
            await message.channel.send("Only the match host can set or change the room code.", delete_after=5)
            return

        await adb.update_match(match["id"], {
            "room_code": code,
            "status": "in_progress"
        })
        await message.channel.send(f"Room code updated to **{code}**. Match is now live!")


async def setup(bot: commands.Bot):
    cog = Queue(bot)
    await bot.add_cog(cog)
    for region in ["East", "West"]:
        bot.add_view(RegionQueueView(region, cog))
