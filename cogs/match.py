from __future__ import annotations

import asyncio
import difflib
import re
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import adb
from services import localization, mmr_engine, validation, vision_extraction
from utils.embeds import ro3_result_card, ro3_verification_card


_INTEGER_FIELDS = ("position", "kills", "deaths", "assists", "score")
# NOTE: "damage" deliberately excluded. mmr_engine.calculate_mmr_change() is
# position-table based (position + won + is_mvp only) and never reads damage —
# requiring it here was a leftover from the old win/loss-average MMR engine.
# Several real scoreboard views (e.g. the post-match "Match Details" screen)
# don't show a Damage column at all, so treating it as required rejected
# otherwise-valid matches for zero benefit to the actual calculation. If a
# future MMR formula version wants damage, it needs to be re-added here
# deliberately, not by accident.
_INTEGER_RE = re.compile(r"^\d+$")
_HILL_TIME_RE = re.compile(r"^\d+(\.\d+)?$")
# NOTE: was r"^\d+\.\d+$" (required a literal decimal point). The vision
# prompt returns hill_time as whole seconds (e.g. "63"), which never
# contains a decimal point — the old regex would have rejected every
# single player in every round, 100% of the time, routing every
# submission to admin review regardless of whether the data was correct.
# Fixed to accept plain integers; still accepts decimals if a future
# prompt/provider version returns fractional seconds.
_SCORE_RE = re.compile(r"^(\d+)\s*[:\-]\s*(\d+)$")
_DISCORD_MESSAGE_LIMIT = 2000


def _truncate_for_discord(prefix: str, parts: list[str], sep: str = "; ") -> str:
    """Join `parts` onto `prefix`, trimming to stay under Discord's 2000-char
    message cap. Cuts whole parts (never mid-sentence) and appends a
    "+N more" note so admins know the list was cut, not truncated silently."""
    text = prefix
    included = 0
    for part in parts:
        candidate = text + (sep if included else "") + part
        if len(candidate) > _DISCORD_MESSAGE_LIMIT - 40:  # headroom for the "+N more" suffix
            break
        text = candidate
        included += 1
    remaining = len(parts) - included
    if remaining > 0:
        text += f" (+{remaining} more — see admin review panel for full detail)"
    return text


class HostApprovalView(discord.ui.View):
    def __init__(self, cog: "Match", match_id: int):
        super().__init__(timeout=3600)
        self.cog = cog
        self.match_id = match_id

    @discord.ui.button(label="Approve Result", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.approve_result(interaction, self.match_id)


class MatchSubmitModal(discord.ui.Modal, title="Submit Match Results"):
    """Modal opened from the persistent panel button. Discord modals can't
    take file attachments, so this only collects the match ID and then
    points the host at /match-submit for the actual 3-screenshot upload —
    see Match.start_submission below for why this two-step exists."""

    match_id_input = discord.ui.TextInput(
        label="Match ID",
        placeholder="e.g. CQ-0001",
        required=True,
        max_length=16,
    )

    def __init__(self, cog: "Match"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.start_submission(interaction, self.match_id_input.value.strip())


class SubmissionPanelView(discord.ui.View):
    """Persistent panel posted once via /match-submit-post. Registered
    with a fixed custom_id in setup() below so it survives bot restarts,
    same pattern as RegionQueueView in cogs/queue.py."""

    def __init__(self, cog: "Match"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Submit Match Results", style=discord.ButtonStyle.primary,
                        custom_id="match_submit_panel_button")
    async def submit(self, interaction: discord.Interaction, _: discord.ui.Button):
        if config.RESULT_UPLOAD_CHANNEL_ID and interaction.channel_id != config.RESULT_UPLOAD_CHANNEL_ID:
            await interaction.response.send_message(
                "Match results can only be submitted in the configured result-upload channel.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(MatchSubmitModal(self.cog))


class Match(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        localization.load_map_translations()

    def _in_upload_channel(self, interaction: discord.Interaction) -> bool:
        # Fail open (not block) if the env var isn't set yet, so a missing
        # config value doesn't brick the whole command for the server —
        # matches the AFK_CHANNEL_ID / MATCH_LOG_CHANNEL_ID no-op pattern.
        if not config.RESULT_UPLOAD_CHANNEL_ID:
            return True
        return interaction.channel_id == config.RESULT_UPLOAD_CHANNEL_ID

    async def _approval_channel(self) -> discord.abc.Messageable | None:
        if not config.RESULT_APPROVAL_CHANNEL_ID:
            return None
        return self.bot.get_channel(config.RESULT_APPROVAL_CHANNEL_ID)

    @app_commands.command(name="match-submit-post", description="Post the persistent match-results submission panel in this channel")
    @app_commands.checks.has_role(config.ADMIN_ROLE_ID)
    async def match_submit_post(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Submit Match Results",
                description="Match Host: click below after all three RO3 rounds are played.",
                color=discord.Color.blurple(),
            ),
            view=SubmissionPanelView(self),
        )

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
        if not self._in_upload_channel(interaction):
            channel_mention = f"<#{config.RESULT_UPLOAD_CHANNEL_ID}>"
            await interaction.response.send_message(
                f"Match results can only be submitted in {channel_mention}.", ephemeral=True
            )
            return

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

        await interaction.response.defer(thinking=True, ephemeral=True)
        payloads = await asyncio.gather(*(attachment.read() for attachment in attachments))
        try:
            extractions = await asyncio.gather(*(
                asyncio.to_thread(vision_extraction.extract_scoreboard, image_bytes, attachment.content_type or "image/png")
                for image_bytes, attachment in zip(payloads, attachments)
            ))
        except Exception as exc:
            await adb.update_match(match["id"], {"status": "awaiting_review"})
            await interaction.followup.send(f"OCR failed ({exc}). This match has been routed to admin review.", ephemeral=True)
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
            await interaction.followup.send(_truncate_for_discord("Submission routed to admin review: ", review_reasons), ephemeral=True)
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
            summary_parts = [f"{players.get(pid, {}).get('ign', pid)}: {', '.join(issues)}" for pid, issues in flags.items()]
            await interaction.followup.send(_truncate_for_discord("Submission routed to admin review for stat validation: ", summary_parts), ephemeral=True)
            return

        await adb.update_match(match["id"], {"status": "pending_verification"})

        # Channel lock: the verification card always posts in the
        # configured approval channel, never wherever /match-submit
        # happened to run.
        approval_channel = await self._approval_channel()
        if approval_channel is None:
            # Fail safe rather than fail silent — the match is validly at
            # pending_verification in the DB, but nobody can see the card
            # to approve it until this env var is set. Tell the uploader.
            await interaction.followup.send(
                "Scoreboards accepted, but RESULT_APPROVAL_CHANNEL_ID isn't configured — "
                "an admin needs to set it before this match can be approved.",
                ephemeral=True,
            )
            return
        await approval_channel.send(embed=ro3_verification_card(match, round_data), view=HostApprovalView(self, match["id"]))
        await interaction.followup.send(f"Submitted. Check {approval_channel.mention} to approve once you've verified the rounds.", ephemeral=True)

    async def start_submission(self, interaction: discord.Interaction, match_id: str):
        """Entry point from the persistent-panel modal. Discord modals
        can't collect file attachments, so this validates the match/host
        up front (fail fast on a bad match ID or wrong host) and then
        points the Host at /match-submit for the actual upload — that
        command re-validates match/host/status independently, so nothing
        here is a security boundary, only a faster failure message."""
        match = await adb.get_match_by_code(match_id)
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not match or match.get("status") != "awaiting_result":
            await interaction.response.send_message("Match not found or not awaiting its three scoreboards.", ephemeral=True)
            return
        if not player or match.get("room_code_shared_by") != player["id"]:
            await interaction.response.send_message("Only the Match Host can upload scoreboards.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Match **{match_id}** confirmed. Now run `/match-submit match_id:{match_id}` "
            f"in this channel and attach all three round screenshots to that command.",
            ephemeral=True,
        )

    @staticmethod
    def _resolve_ign(raw_ign: str, roster: dict) -> tuple[dict | None, str | None]:
        """Look up an OCR-read IGN against this match's 10-player roster.

        Exact match (case-insensitive) first, since that's the overwhelming
        common case and carries zero risk. Only on an exact miss do we try
        a fuzzy match — and only against this match's own 10 players, never
        the full player base, to keep the collision risk low. A fuzzy match
        is only accepted when exactly one roster IGN is a clearly closer
        match than every other candidate; if two roster IGNs are close
        enough that a one-character OCR slip could plausibly mean either,
        we refuse to guess and surface both candidates instead.

        Returns (match_player_or_None, ambiguity_note_or_None). The note is
        set only when we deliberately declined an ambiguous fuzzy match, so
        the caller can produce a "did you mean X or Y?" message instead of
        a bare "unknown IGN".
        """
        ign = raw_ign.strip().lower()
        exact = roster.get(ign)
        if exact:
            return exact, None

        candidates = difflib.get_close_matches(ign, roster.keys(), n=3, cutoff=0.75)
        if not candidates:
            return None, None
        if len(candidates) == 1:
            return roster[candidates[0]], None

        # Multiple candidates: only auto-accept if the top one is decisively
        # closer than the runner-up, not just tied for "close enough".
        scores = [(c, difflib.SequenceMatcher(None, ign, c).ratio()) for c in candidates]
        scores.sort(key=lambda item: item[1], reverse=True)
        best_ign, best_score = scores[0]
        runner_ign, runner_score = scores[1]
        if best_score - runner_score >= 0.15:
            return roster[best_ign], None

        display = ", ".join(roster[c]["players"]["ign"] for c, _ in scores[:2])
        return None, f"ambiguous — could be {display}"

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
                mp, ambiguity = Match._resolve_ign(str(row.get("ign") or ""), roster)
                if not mp:
                    if ambiguity:
                        reasons.append(f"round {round_number}: OCR IGN {row.get('ign')!r} is {ambiguity} — needs manual confirmation")
                    else:
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
        if config.RESULT_APPROVAL_CHANNEL_ID and interaction.channel_id != config.RESULT_APPROVAL_CHANNEL_ID:
            await interaction.response.send_message(
                "This result can only be approved in the configured result-approval channel.", ephemeral=True
            )
            return
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
    cog = Match(bot)
    await bot.add_cog(cog)
    bot.add_view(SubmissionPanelView(cog))