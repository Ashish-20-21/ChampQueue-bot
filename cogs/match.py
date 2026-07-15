from __future__ import annotations

import asyncio
import re
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import adb
from services import localization, mmr_engine, validation, vision_extraction
from utils.embeds import ro3_result_card, ro3_verification_card


_INTEGER_FIELDS = ("position", "kills", "deaths", "assists", "damage", "score")
_INTEGER_RE = re.compile(r"^\d+$")
_HILL_TIME_RE = re.compile(r"^\d+\.\d+$")
_SCORE_RE = re.compile(r"^(\d+)\s*[:\-]\s*(\d+)$")


class HostApprovalView(discord.ui.View):
    def __init__(self, cog: "Match", match_id: int):
        super().__init__(timeout=3600)
        self.cog = cog
        self.match_id = match_id

    @discord.ui.button(label="Approve Result", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.approve_result(interaction, self.match_id)


class Match(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        localization.load_map_translations()

    @app_commands.command(name="match-roomcode", description="Share the in-game room code for a match")
    async def match_roomcode(self, interaction: discord.Interaction, match_id: str, code: str):
        match = await adb.get_match_by_code(match_id)
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not match or match["status"] != "awaiting_room":
            await interaction.response.send_message("Match not found or not awaiting a room code.", ephemeral=True)
            return
        if not player or match.get("room_code_shared_by") != player["id"]:
            await interaction.response.send_message("Only the Match Host can share the room code.", ephemeral=True)
            return
        await adb.update_match(match["id"], {"room_code": code, "status": "awaiting_result"})
        await interaction.response.send_message(f"Room code for **{match_id}** set. Play all three rounds, then upload the scoreboards.")

    @app_commands.command(name="match-submit", description="Host upload of all three RO3 scoreboard screenshots")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)", screenshot_1="Round 1 scoreboard", screenshot_2="Round 2 scoreboard", screenshot_3="Round 3 scoreboard")
    async def match_submit(self, interaction: discord.Interaction, match_id: str,
                           screenshot_1: discord.Attachment, screenshot_2: discord.Attachment,
                           screenshot_3: discord.Attachment):
        match = await adb.get_match_by_code(match_id)
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not match or match.get("status") != "awaiting_result":
            await interaction.response.send_message("Match not found or not awaiting its three scoreboards.", ephemeral=True)
            return
        if not player or match.get("room_code_shared_by") != player["id"]:
            await interaction.response.send_message("Only the Match Host can upload scoreboards.", ephemeral=True)
            return

        attachments = (screenshot_1, screenshot_2, screenshot_3)
        for attachment in attachments:
            if not (attachment.content_type or "").startswith("image/"):
                await interaction.response.send_message("All three uploads must be image files.", ephemeral=True)
                return
            if attachment.size > config.MAX_SCOREBOARD_UPLOAD_BYTES:
                await interaction.response.send_message("Each image must be within the configured upload limit.", ephemeral=True)
                return

        maps = match.get("map_pool") or []
        if len(maps) != 3:
            await interaction.response.send_message("This match has no valid three-map announcement; it requires admin review.", ephemeral=True)
            await adb.update_match(match["id"], {"status": "awaiting_review"})
            return

        await interaction.response.defer(thinking=True)
        payloads = await asyncio.gather(*(attachment.read() for attachment in attachments))
        try:
            extractions = await asyncio.gather(*(
                asyncio.to_thread(vision_extraction.extract_scoreboard, image_bytes, attachment.content_type or "image/png")
                for image_bytes, attachment in zip(payloads, attachments)
            ))
        except Exception as exc:
            await adb.update_match(match["id"], {"status": "awaiting_review"})
            await interaction.followup.send(f"OCR failed ({exc}). This match has been routed to admin review.")
            return

        match_players = await adb.get_match_players(match["id"])
        round_data, review_reasons = self._prepare_rounds(match_players, maps, extractions)

        # Preserve the raw OCR audit record for every submitted screenshot,
        # even when one of them cannot safely be accepted.
        await asyncio.gather(*(
            adb.upsert_match_screenshot(match["id"], number, attachment.url, player["id"], extraction,
                                         extraction.get("ocr_confidence"))
            for number, (attachment, extraction) in enumerate(zip(attachments, extractions), start=1)
        ))

        if review_reasons:
            await adb.update_match(match["id"], {"status": "awaiting_review"})
            await interaction.followup.send("Submission routed to admin review: " + "; ".join(review_reasons))
            return

        # Each valid round is individually queryable immediately. These are
        # provisional records only: the approval RPC is the sole place that
        # can ever mutate players.mmr.
        await asyncio.gather(*(
            adb.replace_match_round_results(match["id"], item["round_number"], item["results"])
            for item in round_data
        ))

        validations = await asyncio.gather(*(
            validation.validate_submission(match["id"], extraction)
            for extraction in extractions
        ))
        flags = {pid: issues for result in validations for pid, issues in result["flags"].items()}
        if flags:
            await adb.update_match(match["id"], {"status": "awaiting_review"})
            players = {item["id"]: item for item in await adb.get_players_by_ids(list(flags))}
            summary = "; ".join(f"{players.get(pid, {}).get('ign', pid)}: {', '.join(issues)}" for pid, issues in flags.items())
            await interaction.followup.send(f"Submission routed to admin review for stat validation: {summary}")
            return

        await adb.update_match(match["id"], {"status": "pending_verification"})
        await interaction.followup.send(embed=ro3_verification_card(match, round_data), view=HostApprovalView(self, match["id"]))

    @staticmethod
    def _prepare_rounds(match_players: list[dict], maps: list[str], extractions: list[dict]) -> tuple[list[dict], list[str]]:
        roster = {mp["players"]["ign"].strip().lower(): mp for mp in match_players}
        rounds: list[dict] = []
        reasons: list[str] = []
        for round_number, (announced_map, extraction) in enumerate(zip(maps, extractions), start=1):
            resolved_map = localization.resolve_map_name(str(extraction.get("map") or ""))
            if resolved_map != announced_map.upper():
                reasons.append(f"round {round_number}: map is unrecognized or does not match announced {announced_map}")
            score = str(extraction.get("final_score") or "")
            score_match = _SCORE_RE.fullmatch(score)
            if not score_match or score_match.group(1) == score_match.group(2):
                reasons.append(f"round {round_number}: final score is unreadable")
                continue
            winner = "A" if int(score_match.group(1)) > int(score_match.group(2)) else "B"
            results: list[dict] = []
            seen_players: set[int] = set()
            per_team = Counter()
            for row in extraction.get("players", []):
                ign = str(row.get("ign") or "").strip().lower()
                mp = roster.get(ign)
                if not mp:
                    reasons.append(f"round {round_number}: unknown OCR IGN {row.get('ign')!r}")
                    continue
                if mp["player_id"] in seen_players:
                    reasons.append(f"round {round_number}: duplicate OCR player {row.get('ign')}")
                    continue
                if row.get("team") != mp["team"]:
                    reasons.append(f"round {round_number}: team mismatch for {row.get('ign')}")
                    continue
                invalid = [field for field in _INTEGER_FIELDS if not _INTEGER_RE.fullmatch(str(row.get(field, "")))]
                if not _HILL_TIME_RE.fullmatch(str(row.get("hill_time", ""))):
                    invalid.append("hill_time")
                if invalid:
                    reasons.append(f"round {round_number}: invalid OCR digit format for {row.get('ign')} ({', '.join(invalid)})")
                    continue
                position = int(row["position"])
                if not 1 <= position <= 5:
                    reasons.append(f"round {round_number}: invalid position for {row.get('ign')}")
                    continue
                if not isinstance(row.get("is_mvp"), bool):
                    reasons.append(f"round {round_number}: MVP flag is missing or invalid for {row.get('ign')}")
                    continue
                is_mvp = row["is_mvp"]
                results.append({"player_id": mp["player_id"], "position": position, "is_mvp": is_mvp,
                                "mmr_delta": mmr_engine.calculate_mmr_change(position, mp["team"] == winner, is_mvp),
                                "team": mp["team"], "discord_id": mp["players"]["discord_id"]})
                seen_players.add(mp["player_id"])
                per_team[mp["team"]] += 1
            if len(results) != 10 or set(seen_players) != {mp["player_id"] for mp in match_players} or per_team != Counter({"A": 5, "B": 5}):
                reasons.append(f"round {round_number}: scoreboard does not contain one valid row for every match player")
            for team in ("A", "B"):
                if sum(1 for row in results if row["team"] == team and row["is_mvp"]) != 1:
                    reasons.append(f"round {round_number}: Team {team} must have exactly one game-provided MVP")
            rounds.append({"round_number": round_number, "map_name": announced_map, "final_score": score, "results": results})
        return rounds, reasons

    async def approve_result(self, interaction: discord.Interaction, match_id: int):
        match = await adb.get_match(match_id)
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not match or match.get("status") != "pending_verification":
            await interaction.response.send_message("This result is no longer awaiting host approval.", ephemeral=True)
            return
        if not player or match.get("room_code_shared_by") != player["id"]:
            await interaction.response.send_message("Only the Match Host can approve this result.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            await adb.approve_ro3_match(match_id, player["id"])
        except Exception as exc:
            await interaction.followup.send(f"Approval could not be committed safely: {exc}", ephemeral=True)
            return
        match = await adb.get_match(match_id)
        match_players, round_results = await asyncio.gather(adb.get_match_players(match_id), adb.get_match_round_results(match_id))
        await interaction.followup.send(embed=ro3_result_card(match, match_players, round_results, match.get("map_pool") or []))


async def setup(bot: commands.Bot):
    await bot.add_cog(Match(bot))
