from __future__ import annotations

import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import db
from services import matchmaking, reputation


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
            player = db.get_player_by_discord_id(interaction.user.id)
            if not player or player["id"] not in self.team_player_ids:
                await interaction.response.send_message("This isn't your team's vote.", ephemeral=True)
                return
            if skill in self.taken_skills:
                await interaction.response.send_message(
                    f"**{skill}** was already picked by a teammate — operator skills must be unique per team.",
                    ephemeral=True,
                )
                return
            db.cast_skill_vote(self.match_id, player["id"], self.team, skill)
            self.taken_skills.add(skill)
            button.disabled = True
            button.label = f"{skill} ✓ ({player['ign']})"
            await interaction.response.edit_message(view=self)

        button.callback = callback
        return button


class MapVoteView(discord.ui.View):
    def __init__(self, match_id: int, candidates: list[str], all_player_ids: set[int]):
        super().__init__(timeout=config.VOTE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.all_player_ids = all_player_ids
        self.votes: dict[str, int] = {m: 0 for m in candidates}
        self.voters: set[int] = set()
        for map_name in candidates:
            self.add_item(self._make_button(map_name))

    def _make_button(self, map_name: str) -> discord.ui.Button:
        button = discord.ui.Button(label=map_name, style=discord.ButtonStyle.primary)

        async def callback(interaction: discord.Interaction):
            player = db.get_player_by_discord_id(interaction.user.id)
            if not player or player["id"] not in self.all_player_ids:
                await interaction.response.send_message("This isn't your match's vote.", ephemeral=True)
                return
            if player["id"] in self.voters:
                await interaction.response.send_message("You already voted.", ephemeral=True)
                return
            db.cast_map_vote(self.match_id, player["id"], map_name)
            self.votes[map_name] += 1
            self.voters.add(player["id"])
            await interaction.response.send_message(f"Voted for **{map_name}**.", ephemeral=True)

        button.callback = callback
        return button

    def winner(self) -> str:
        return max(self.votes.items(), key=lambda kv: kv[1])[0]


class Queue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()

    @app_commands.command(name="queue-join", description="Join the Champion's Queue matchmaking queue")
    async def queue_join(self, interaction: discord.Interaction):
        player = db.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You need to `/register` and be approved first.", ephemeral=True)
            return
        if player["status"] != "approved":
            await interaction.response.send_message(f"Your registration is `{player['status']}`, not approved yet.", ephemeral=True)
            return

        eligible, reason = reputation.is_queue_eligible(player)
        if not eligible:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        async with self._lock:
            entry = db.queue_join(player["id"])
            if entry is None:
                await interaction.response.send_message("You're already in the queue.", ephemeral=True)
                return

            current = db.queue_current()
            await interaction.response.send_message(
                f"Joined the queue (**{len(current)}/{config.QUEUE_SIZE}**).", ephemeral=True
            )

            if len(current) >= config.QUEUE_SIZE:
                pop = current[:config.QUEUE_SIZE]
                db.queue_mark_matched([p["player_id"] for p in pop])
                players = [p["players"] for p in pop]
                await self._start_match(interaction, players)

    @app_commands.command(name="queue-leave", description="Leave the queue")
    async def queue_leave(self, interaction: discord.Interaction):
        player = db.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return
        db.queue_leave(player["id"])
        await interaction.response.send_message("Left the queue.", ephemeral=True)

    @app_commands.command(name="queue-status", description="See who's currently in queue")
    async def queue_status(self, interaction: discord.Interaction):
        current = db.queue_current()
        names = ", ".join(p["players"]["ign"] for p in current) or "empty"
        await interaction.response.send_message(f"**Queue ({len(current)}/{config.QUEUE_SIZE}):** {names}")

    # ------------------------------------------------------------------
    async def _start_match(self, interaction: discord.Interaction, players: list[dict]):
        channel = interaction.channel
        player_ids = [p["id"] for p in players]
        bootstrap = matchmaking.is_bootstrap_match(player_ids)
        match = db.create_match(is_bootstrap=bootstrap)

        balanced = matchmaking.balance_teams(players, bootstrap=bootstrap)
        team_a, team_b = balanced["team_a"], balanced["team_b"]
        captain_a_id, captain_b_id = balanced["captain_a"], balanced["captain_b"]

        for p in team_a:
            db.add_match_player(match["id"], p["id"], "A", is_captain=(p["id"] == captain_a_id))
        for p in team_b:
            db.add_match_player(match["id"], p["id"], "B", is_captain=(p["id"] == captain_b_id))

        captain_a = next(p for p in team_a if p["id"] == captain_a_id)
        captain_b = next(p for p in team_b if p["id"] == captain_b_id)
        db.update_match(match["id"], {"team_a_captain_id": captain_a_id, "team_b_captain_id": captain_b_id})

        mode_note = "🎲 bootstrap (random)" if bootstrap else "📊 analysis-balanced"
        embed = discord.Embed(
            title=f"Match {match['match_id']} — Teams Formed ({mode_note})",
            color=discord.Color.blue(),
        )
        embed.add_field(name=f"Team A — Captain: {captain_a['ign']}", value="\n".join(p["ign"] for p in team_a), inline=True)
        embed.add_field(name=f"Team B — Captain: {captain_b['ign']}", value="\n".join(p["ign"] for p in team_b), inline=True)
        await channel.send(embed=embed)

        # --- Operator skill vote (parallel, one view per team) ---
        team_a_ids = {p["id"] for p in team_a}
        team_b_ids = {p["id"] for p in team_b}
        view_a = SkillVoteView(match["id"], "A", team_a_ids)
        view_b = SkillVoteView(match["id"], "B", team_b_ids)
        msg_a = await channel.send(f"**Team A** — vote your operator skill (unique per team):", view=view_a)
        msg_b = await channel.send(f"**Team B** — vote your operator skill (unique per team):", view=view_b)
        await asyncio.sleep(config.VOTE_TIMEOUT_SECONDS)
        await self._finalize_skill_vote(match["id"], "A", team_a, view_a)
        await self._finalize_skill_vote(match["id"], "B", team_b, view_b)

        # --- Map vote (round of 3, all 10 vote) ---
        db.update_match(match["id"], {"status": "map_vote"})
        candidates = matchmaking.pick_map_candidates(list(team_a_ids), list(team_b_ids), bootstrap)
        all_ids = team_a_ids | team_b_ids
        map_view = MapVoteView(match["id"], candidates, all_ids)
        await channel.send(f"**Map vote** — choose from: {', '.join(candidates)}", view=map_view)
        await asyncio.sleep(config.VOTE_TIMEOUT_SECONDS)
        chosen_map = map_view.winner() if any(map_view.votes.values()) else random.choice(candidates)
        db.update_match(match["id"], {"map": chosen_map})

        # --- Channel + voice channel creation ---
        guild = channel.guild
        category = channel.category
        text_channel = await guild.create_text_channel(f"match-{match['match_id'].lower()}", category=category)
        vc_a = await guild.create_voice_channel(f"Team {captain_a['ign']}", category=category)
        vc_b = await guild.create_voice_channel(f"Team {captain_b['ign']}", category=category)
        db.update_match(match["id"], {
            "text_channel_id": str(text_channel.id),
            "voice_channel_a_id": str(vc_a.id),
            "voice_channel_b_id": str(vc_b.id),
            "status": "awaiting_room",
        })

        mentions = " ".join(f"<@{p['discord_id']}>" for p in team_a + team_b)
        await text_channel.send(
            f"{mentions}\n\n**Match {match['match_id']}** — Map: **{chosen_map}**\n"
            f"Voice: {vc_a.mention} (Team A) / {vc_b.mention} (Team B)\n\n"
            f"One participant: use `/match-roomcode {match['match_id']} <code>` here to share the room code."
        )

    async def _finalize_skill_vote(self, match_id: int, team: str, team_players: list[dict], view: SkillVoteView):
        votes = db.get_skill_votes(match_id, team)
        voted_ids = {v["player_id"] for v in votes}
        missing = [p for p in team_players if p["id"] not in voted_ids]
        available_skills = [s for s in config.OPERATOR_SKILLS if s not in view.taken_skills]
        for p in missing:
            skill = available_skills.pop(0) if available_skills else random.choice(config.OPERATOR_SKILLS)
            db.cast_skill_vote(match_id, p["id"], team, skill)
        view.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(Queue(bot))
