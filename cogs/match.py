from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import adb
from services import vision_extraction, validation, mmr_engine, stats_engine, reputation
from utils.embeds import result_card


class WinnerVoteView(discord.ui.View):
    def __init__(self, match_id: int, all_player_ids: set[int]):
        super().__init__(timeout=config.VOTE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.all_player_ids = all_player_ids
        self.votes: dict[int, str] = {}  # player_id -> "A"/"B"

    async def _vote(self, interaction: discord.Interaction, team: str):
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player or player["id"] not in self.all_player_ids:
            await interaction.response.send_message("This isn't your match's vote.", ephemeral=True)
            return
        self.votes[player["id"]] = team
        await interaction.response.send_message(f"Vote recorded: Team {team}.", ephemeral=True)

    @discord.ui.button(label="Team A Won", style=discord.ButtonStyle.success)
    async def team_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "A")

    @discord.ui.button(label="Team B Won", style=discord.ButtonStyle.danger)
    async def team_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "B")


class Match(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    async def _is_match_participant(match_id: int, player_id: int) -> bool:
        match_players = await adb.get_match_players(match_id)
        return any(mp["player_id"] == player_id for mp in match_players)

    @app_commands.command(name="match-roomcode", description="Share the in-game room code for a match")
    async def match_roomcode(self, interaction: discord.Interaction, match_id: str, code: str):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match or match["status"] != "awaiting_room":
            await interaction.response.send_message("Match not found or not awaiting a room code.", ephemeral=True)
            return
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player or not await self._is_match_participant(match["id"], player["id"]):
            await interaction.response.send_message(
                "Only players in this match can share its room code.", ephemeral=True
            )
            return
        await adb.update_match(match["id"], {
            "room_code": code,
            "room_code_shared_by": player["id"] if player else None,
            "status": "in_progress",
        })
        await interaction.response.send_message(f"Room code for **{match_id}** set. Match is live — GLHF!")

    @app_commands.command(name="match-submit", description="Upload the final scoreboard screenshot for a match")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)", screenshot="Final scoreboard screenshot")
    async def match_submit(self, interaction: discord.Interaction, match_id: str, screenshot: discord.Attachment):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match or match["status"] not in ("in_progress", "awaiting_result"):
            await interaction.response.send_message("Match not found or not awaiting a result.", ephemeral=True)
            return

        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player or not await self._is_match_participant(match["id"], player["id"]):
            await interaction.response.send_message(
                "Only players in this match can submit its result.", ephemeral=True
            )
            return

        if not (screenshot.content_type or "").startswith("image/"):
            await interaction.response.send_message("Please upload an image file.", ephemeral=True)
            return
        if screenshot.size > config.MAX_SCOREBOARD_UPLOAD_BYTES:
            await interaction.response.send_message(
                f"Image is too large (max {config.MAX_SCOREBOARD_UPLOAD_BYTES // (1024*1024)}MB).", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        image_bytes = await screenshot.read()
        media_type = screenshot.content_type or "image/png"

        try:
            extraction = vision_extraction.extract_scoreboard(image_bytes, media_type)
        except Exception as e:
            await interaction.followup.send(
                f"Couldn't extract data from that screenshot ({e}). An admin can enter stats manually with "
                f"`/admin-correct-stat`, or try re-uploading a clearer screenshot."
            )
            await adb.update_match(match["id"], {"status": "awaiting_review", "scoreboard_image_url": screenshot.url})
            return

        await adb.update_match(match["id"], {
            "status": "awaiting_result",
            "scoreboard_image_url": screenshot.url,
            "raw_extraction": extraction,
            "map": extraction.get("map") or match.get("map"),
            "final_score": extraction.get("final_score"),
        })

        match_players = await adb.get_match_players(match["id"])
        all_ids = {mp["player_id"] for mp in match_players}
        view = WinnerVoteView(match["id"], all_ids)
        await interaction.followup.send(
            f"Scoreboard extracted for **{match_id}**. All 10 players: vote on the winner below "
            f"(cross-checked against the scoreboard).",
            view=view,
        )
        await asyncio.sleep(config.VOTE_TIMEOUT_SECONDS)
        await self._finalize(interaction.channel, match["id"], extraction, view.votes)

    async def _finalize(self, channel: discord.abc.Messageable, match_id: int, extraction: dict, votes: dict[int, str]):
        match = await adb.get_match(match_id)
        match_players = await adb.get_match_players(match_id)

        # Map extracted rows onto match_players by IGN match (best-effort;
        # falls back to leaving nulls for admin correction if no IGN match found).
        extracted_by_ign = {p["ign"].strip().lower(): p for p in extraction.get("players", [])}
        for mp in match_players:
            ign = mp["players"]["ign"].strip().lower()
            row = extracted_by_ign.get(ign)
            if row:
                await adb.update_match_player(match_id, mp["player_id"], {
                    "kills": row.get("kills"),
                    "deaths": row.get("deaths"),
                    "assists": row.get("assists"),
                    "damage": row.get("damage"),
                    "hill_time": row.get("hill_time"),
                    "impact": row.get("impact"),
                    "score": row.get("score"),
                })

        match_players = await adb.get_match_players(match_id)  # refresh with new stats

        # Determine scoreboard-implied winner from raw extraction if present, else from vote majority.
        vote_tally = {"A": 0, "B": 0}
        for v in votes.values():
            vote_tally[v] += 1
        vote_winner = max(vote_tally, key=vote_tally.get) if any(vote_tally.values()) else None
        scoreboard_winner = extraction.get("winner_team") or vote_winner

        vote_records = [{"player_id": pid, "winner": team} for pid, team in votes.items()]
        result = await validation.validate_submission(match_id, extraction, vote_records)

        if not result["auto_accept"]:
            await adb.update_match(match_id, {"status": "awaiting_review"})
            flag_lines = []
            for pid, flags in result["flags"].items():
                flagged_player = await adb.get_player_by_id(pid)
                flag_lines.append(f"<@{flagged_player['discord_id']}>: {', '.join(flags)}")
            flag_summary = "\n".join(flag_lines) or "—"
            await channel.send(
                f"⚠️ Match **{match['match_id']}** flagged for admin review.\n"
                f"Vote mismatch: {result['vote_mismatch']}\nStat flags:\n{flag_summary}"
            )
            return

        # --- Auto-accept path: compute MMR, MVP, finalize ---
        team_a_stats = [mp for mp in match_players if mp["team"] == "A"]
        team_b_stats = [mp for mp in match_players if mp["team"] == "B"]
        avg_a = mmr_engine.team_average(team_a_stats)
        avg_b = mmr_engine.team_average(team_b_stats)

        winner = scoreboard_winner or "A"
        mvp_candidate = max(match_players, key=lambda mp: (mp.get("impact") or 0))

        for mp in match_players:
            team_avg = avg_a if mp["team"] == "A" else avg_b
            won = mp["team"] == winner
            is_mvp = mp["player_id"] == mvp_candidate["player_id"] and mp["team"] == winner
            player = await adb.get_player_by_id(mp["player_id"])
            change = mmr_engine.calculate_mmr_change(mp, team_avg, won, is_mvp)
            new_mmr = max(0, player["mmr"] + change)
            await adb.update_match_player(match_id, mp["player_id"], {
                "mmr_before": player["mmr"],
                "mmr_after": new_mmr,
                "mmr_change": change,
                "is_mvp": is_mvp,
            })
            await adb.update_player_fields(player["id"], {"mmr": new_mmr})

        await adb.update_match(match_id, {"status": "completed", "completed_at": "now()", "winner_team": winner,
                                           "mvp_player_id": mvp_candidate["player_id"]})

        match_players = await adb.get_match_players(match_id)
        for mp in match_players:
            stats_engine.process_post_match(mp["player_id"])

        match = await adb.get_match(match_id)
        embed = result_card(match, match_players)
        await channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Match(bot))