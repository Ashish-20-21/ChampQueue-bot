from __future__ import annotations

import asyncio
import difflib
import re
from collections import Counter
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import logging
from database.db import adb
from services import localization, mmr_engine, validation, vision_extraction
from utils.embeds import ro3_result_card, ro3_verification_card
from utils.permissions import is_admin

logger = logging.getLogger(__name__)


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


class IssueResolveModal(discord.ui.Modal, title="Resolve Issue"):
    note = discord.ui.TextInput(label="Resolution note (optional)", required=False, max_length=300, style=discord.TextStyle.paragraph)

    def __init__(self, cog: "Match", issue_id: int, original_message: discord.Message):
        super().__init__()
        self.cog = cog
        self.issue_id = issue_id
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._finish_resolving_issue(interaction, self.issue_id, self.original_message, self.note.value or None)


class IssueResolveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"issue_resolve:(?P<issue_id>[0-9]+)"):
    """Attached to every intake-channel post — one button, works for any
    issue reason (informational or correction). Admin-only via the same
    permission role check the rest of the admin surface uses.

    Uses DynamicItem (discord.py 2.4+) instead of a plain View button:
    the custom_id embeds issue_id and gets regex-matched, so this stays
    clickable for issues created long after the bot process that's
    currently running was started — a restart doesn't quietly break old
    Resolve buttons, no per-instance bot.add_view() registration needed.
    Registered once, generically, in setup() below.
    """

    def __init__(self, issue_id: int):
        super().__init__(
            discord.ui.Button(label="Resolve", style=discord.ButtonStyle.success, custom_id=f"issue_resolve:{issue_id}")
        )
        self.issue_id = issue_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: "re.Match[str]"):
        return cls(int(match["issue_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can resolve reports.", ephemeral=True)
            return
        cog = interaction.client.get_cog("Match")
        await interaction.response.send_modal(IssueResolveModal(cog, self.issue_id, interaction.message))


class IssueResolveView(discord.ui.View):
    """Thin wrapper so call sites can keep doing view=IssueResolveView(cog, issue_id)
    without needing to know about DynamicItem internals."""

    def __init__(self, cog: "Match", issue_id: int):
        super().__init__(timeout=None)
        self.add_item(IssueResolveButton(issue_id))


class CorrectionReasonView(discord.ui.View):
    """Case A (before approve): full reason set. Case B (already approved,
    "approved by mistake"): a single, narrower reason — filed after the
    fact means the host is flagging their own approval, not the data
    itself, so it doesn't need the same options."""

    def __init__(self, cog: "Match", match_id: int, host_player_id: int, already_approved: bool):
        super().__init__(timeout=120)
        self.cog = cog
        self.match_id = match_id
        self.host_player_id = host_player_id
        self.already_approved = already_approved

        if already_approved:
            options = [discord.SelectOption(label="Approved by mistake", value="approved_by_mistake")]
        else:
            options = [
                discord.SelectOption(label="Player stat correction needed", value="stat_correction"),
                discord.SelectOption(label="Result issue (map, score, roster, etc.)", value="result_issue"),
            ]
        select = discord.ui.Select(placeholder="Choose a reason", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        reason = interaction.data["values"][0]
        await interaction.response.send_modal(CorrectionDetailModal(self.cog, self.match_id, self.host_player_id, reason))


class CorrectionDetailModal(discord.ui.Modal, title="Correction Details"):
    detail = discord.ui.TextInput(label="Anything specific? (optional)", required=False, max_length=500, style=discord.TextStyle.paragraph)

    def __init__(self, cog: "Match", match_id: int, host_player_id: int, reason: str):
        super().__init__()
        self.cog = cog
        self.match_id = match_id
        self.host_player_id = host_player_id
        self.reason = reason

    async def on_submit(self, interaction: discord.Interaction):
        issue = await adb.create_match_issue(self.match_id, self.host_player_id, self.reason, self.detail.value or None)
        match = await adb.get_match(self.match_id)

        intake_channel = self.cog.bot.get_channel(config.ISSUE_INTAKE_CHANNEL_ID) if config.ISSUE_INTAKE_CHANNEL_ID else None
        if intake_channel:
            try:
                embed = discord.Embed(
                    title=f"Match {match['match_id']} — host-filed correction request",
                    description=self.detail.value or "(no additional detail provided)",
                    color=discord.Color.orange(),
                )
                embed.add_field(name="Reason", value=self.reason)
                embed.add_field(name="Issue ID", value=str(issue["id"]))
                await intake_channel.send(embed=embed, view=IssueResolveView(self.cog, issue["id"]))
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            "Thanks for flagging it — sent to admin review, we'll notify you once it's resolved.", ephemeral=True
        )


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

    @app_commands.command(name="correction-result", description="Host: flag a problem with this match's result before or after approval")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)")
    @app_commands.checks.cooldown(1, config.CORRECTION_COMMAND_COOLDOWN_SECONDS, key=lambda i: (i.guild_id, i.channel_id))
    async def correction_result(self, interaction: discord.Interaction, match_id: str):
        match = await adb.get_match_by_code(match_id)
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if not player or match.get("room_code_shared_by") != player["id"]:
            await interaction.response.send_message("Only the Match Host can file a correction request for this match.", ephemeral=True)
            return
        if match["status"] not in ("pending_verification", "awaiting_review", "completed"):
            await interaction.response.send_message("This match doesn't have a submitted result yet — nothing to correct.", ephemeral=True)
            return

        # Case B: host already approved (status == completed) — shorter,
        # "approved by mistake" framing rather than the full reason set.
        already_approved = match["status"] == "completed"
        await interaction.response.send_message(
            "What's the issue?" if not already_approved else "Since this was already approved — what happened?",
            view=CorrectionReasonView(self, match["id"], player["id"], already_approved),
            ephemeral=True,
        )

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

    async def _route_to_review(self, match: dict, player_id: int | None, reason: str, technical_detail: str) -> None:
        """Every failure path funnels through here: match status flips to
        awaiting_review, the full technical detail goes to the intake
        channel (for admins) as a match_issues row, and the player only
        ever sees a short, reassuring message — never the raw reasons
        list. reason must be one of match_issues' allowed reason values."""
        await adb.update_match(match["id"], {"status": "awaiting_review"})
        issue = await adb.create_match_issue(match["id"], player_id or match.get("room_code_shared_by"), reason, technical_detail)
        intake_channel = self.bot.get_channel(config.ISSUE_INTAKE_CHANNEL_ID) if config.ISSUE_INTAKE_CHANNEL_ID else None
        if intake_channel:
            try:
                await intake_channel.send(
                    embed=discord.Embed(
                        title=f"Match {match['match_id']} — needs review",
                        description=_truncate_for_discord("", [technical_detail]),
                        color=discord.Color.orange(),
                    ).add_field(name="Reason", value=reason).add_field(name="Issue ID", value=str(issue["id"])),
                    view=IssueResolveView(self, issue["id"]),
                )
            except discord.HTTPException:
                pass

    @staticmethod
    def _friendly_review_message() -> str:
        return (
            "Thanks for uploading — we hit a snag reading one of the scoreboards, so this has been "
            "sent to admin review. No action needed on your end; we'll ping you once it's sorted and "
            "the leaderboard's updated. Appreciate the patience! 🙏"
        )

    async def _finish_resolving_issue(self, interaction: discord.Interaction, issue_id: int,
                                       original_message: discord.Message, note: str | None) -> None:
        admin_player = await adb.get_player_by_discord_id(interaction.user.id)
        issue = await adb.resolve_match_issue(issue_id, admin_player["id"] if admin_player else None, note)
        reporter = await adb.get_players_by_ids([issue["reported_by"]])
        reporter_discord_id = reporter[0]["discord_id"] if reporter else None

        # Edit the intake message in place rather than deleting it, so the
        # channel stays a readable history of what came in and what happened.
        try:
            resolved_embed = original_message.embeds[0]
            resolved_embed.color = discord.Color.green()
            resolved_embed.add_field(name="Status", value=f"✅ Resolved by {interaction.user.mention}" + (f" — {note}" if note else ""))
            await original_message.edit(embed=resolved_embed, view=None)
        except (discord.HTTPException, IndexError):
            pass

        outbound_channel = self.bot.get_channel(config.ISSUE_RESOLVED_CHANNEL_ID) if config.ISSUE_RESOLVED_CHANNEL_ID else None
        if outbound_channel:
            mention = f"<@{reporter_discord_id}>" if reporter_discord_id else "player"
            try:
                await outbound_channel.send(
                    f"✅ {mention} — your match report's been reviewed and sorted. Leaderboard's up to date. Thanks for flagging it!"
                )
            except discord.HTTPException:
                pass

        await interaction.response.send_message("Marked resolved.", ephemeral=True)

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
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if match.get("status") != "awaiting_result":
            if match["status"] in ("pending_verification", "awaiting_review", "completed"):
                await interaction.response.send_message(
                    "This match's results were already submitted. If something looks wrong, "
                    "contact an admin rather than resubmitting.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "This match isn't ready for scoreboard submission yet — make sure the room code has been shared first.",
                    ephemeral=True,
                )
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
            await self._route_to_review(match, player["id"] if player else None, "result_issue", "match has no valid three-map announcement (map_pool missing or incomplete)")
            await interaction.response.send_message(self._friendly_review_message(), ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        payloads = await asyncio.gather(*(attachment.read() for attachment in attachments))
        try:
            extractions = await asyncio.gather(*(
                asyncio.to_thread(vision_extraction.extract_scoreboard, image_bytes, attachment.content_type or "image/png")
                for image_bytes, attachment in zip(payloads, attachments)
            ))
        except Exception as exc:
            await self._route_to_review(match, player["id"] if player else None, "vision_failure", f"OCR/extraction raised an exception: {exc}")
            await interaction.followup.send(self._friendly_review_message(), ephemeral=True)
            return

        match_players = await adb.get_match_players(match["id"])
        ordered_pairs, info_notes = self._reorder_pairs_by_map(maps, list(zip(extractions, attachments)))
        ordered_extractions = [pair[0] for pair in ordered_pairs]

        # Preserve the raw OCR audit record for every submitted screenshot,
        # even when one of them cannot safely be accepted. Uses the
        # reordered pairs so the stored round_number always matches what
        # _prepare_rounds actually used for MMR — otherwise a reordered
        # submission's audit trail would silently disagree with its own
        # MMR calculation.
        await asyncio.gather(*(
            adb.upsert_match_screenshot(match["id"], number, attachment.url, player["id"], extraction,
                                         extraction.get("ocr_confidence"))
            for number, (extraction, attachment) in enumerate(ordered_pairs, start=1)
        ))

        round_data, review_reasons, _ = self._prepare_rounds(match_players, maps, ordered_extractions)

        if review_reasons:
            await self._route_to_review(match, player["id"] if player else None, "vision_failure", _truncate_for_discord("Validation failed: ", review_reasons))
            await interaction.followup.send(self._friendly_review_message(), ephemeral=True)
            return

        # Each valid round is individually queryable immediately. These are
        # provisional records only: the approval RPC is the sole place that
        # can ever mutate players.mmr.
        #
        # NOTE: results rows carry "discord_id" for the verification embed's
        # @mentions (ro3_verification_card below), but match_round_results
        # has no such column — confirmed live via a 400 PGRST204 error when
        # this wasn't stripped first. Strip it only for the DB payload; the
        # embed still gets the full row with discord_id intact via round_data.
        await asyncio.gather(*(
            adb.replace_match_round_results(
                match["id"], item["round_number"],
                [{k: v for k, v in row.items() if k != "discord_id"} for row in item["results"]],
            )
            for item in round_data
        ))

        validations = await asyncio.gather(*(
            validation.validate_submission(match["id"], extraction)
            for extraction in ordered_extractions
        ))
        flags = {pid: issues for result in validations for pid, issues in result["flags"].items()}
        if flags:
            players = {item["id"]: item for item in await adb.get_players_by_ids(list(flags))}
            summary_parts = [f"{players.get(pid, {}).get('ign', pid)}: {', '.join(issues)}" for pid, issues in flags.items()]
            await self._route_to_review(match, player["id"] if player else None, "vision_failure", _truncate_for_discord("Stat validation flagged: ", summary_parts))
            await interaction.followup.send(self._friendly_review_message(), ephemeral=True)
            return

        deadline = (discord.utils.utcnow() + timedelta(seconds=config.APPROVAL_TIMEOUT_SECONDS)).isoformat()
        await adb.update_match(match["id"], {"status": "pending_verification", "approval_deadline": deadline})

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
        note_suffix = f" ({'; '.join(info_notes)})" if info_notes else ""
        await interaction.followup.send(
            f"Submitted. Check {approval_channel.mention} to approve once you've verified the rounds.{note_suffix}",
            ephemeral=True,
        )

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
    def _reorder_pairs_by_map(maps: list[str], pairs: list[tuple[dict, "discord.Attachment"]]) -> tuple[list[tuple[dict, "discord.Attachment"]], list[str]]:
        """Hosts upload 3 screenshots in whatever order they have the files
        open, not necessarily the announced round order. Since each
        extraction already carries its own detected map name, match each
        (extraction, attachment) pair to the round whose announced map it
        resolves to, rather than trusting attachment slot position.

        Falls back to the original (positional) order — with no info note
        — whenever map-based matching can't be done confidently: a map
        that doesn't resolve to anything in the announced pool, two
        screenshots resolving to the same map, or an announced map with no
        matching screenshot at all. In those cases the existing per-round
        map-mismatch check in _prepare_rounds will still catch and report
        the problem — this function only handles the *good* case of
        "right maps, wrong order" transparently.
        """
        resolved = [localization.resolve_map_name(str(ex.get("map") or "")) for ex, _ in pairs]
        announced_upper = [m.upper() for m in maps]

        if len(set(resolved)) != len(resolved) or any(r is None for r in resolved):
            return pairs, []
        if set(resolved) != set(announced_upper):
            return pairs, []

        by_map = dict(zip(resolved, pairs))
        reordered = [by_map[m] for m in announced_upper]
        if reordered == pairs:
            return pairs, []
        return reordered, ["screenshots were uploaded out of order — matched to rounds by detected map name instead"]

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

    async def _run_post_approval_cleanup(self, guild: discord.Guild | None, match: dict) -> None:
        """Shared by the manual Approve button and the auto-approve sweep.
        Mirrors admin-scrap-match's pattern: VCs die immediately, text
        channel gets a 1hr grace window via the existing cleanup sweep
        (schedule_match_cleanup), same as an abandoned match, just without
        changing status off "completed"."""
        if not guild:
            return
        for vc_field in ("voice_channel_a_id", "voice_channel_b_id"):
            vc_id = match.get(vc_field)
            if not vc_id:
                continue
            vc = guild.get_channel(int(vc_id))
            if vc:
                try:
                    await vc.delete(reason="Match approved and completed")
                except discord.HTTPException:
                    pass

        cleanup_at = (discord.utils.utcnow() + timedelta(seconds=config.MATCH_CHANNEL_CLEANUP_DELAY_SECONDS)).isoformat()
        await adb.schedule_match_cleanup(match["id"], cleanup_at)

        text_channel_id = match.get("text_channel_id")
        text_channel = guild.get_channel(int(text_channel_id)) if text_channel_id else None
        if text_channel:
            try:
                await text_channel.send(
                    "🏆 **GG — result's locked in.** MMR is updated, this channel closes in about an hour. "
                    "Head back to the queue whenever you're ready for the next one."
                )
            except discord.HTTPException:
                pass

    async def _do_approve(self, guild: discord.Guild | None, match_id: int, approved_by_id: int) -> tuple[bool, str]:
        """The one real approval path — used by the manual Approve button,
        /admin-force-approve, and the auto-approve sweep. Returns
        (success, message). The open-issue check happens here, right
        before the RPC call, not earlier — filing a correction after a
        sweep has already listed a match as "overdue" but before this
        actually runs still correctly blocks it, since this is the last
        check before anything is committed."""
        if await adb.has_open_issue(match_id):
            return False, "This match has an open correction request — approval is blocked until it's resolved."
        try:
            await adb.approve_ro3_match(match_id, approved_by_id)
        except Exception as exc:
            return False, f"Approval could not be committed safely: {exc}"

        match = await adb.get_match(match_id)
        await self._run_post_approval_cleanup(guild, match)
        return True, "approved"

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
        success, message = await self._do_approve(interaction.guild, match_id, player["id"])
        if not success:
            await interaction.followup.send(message, ephemeral=True)
            return
        match = await adb.get_match(match_id)
        match_players, round_results = await asyncio.gather(adb.get_match_players(match_id), adb.get_match_round_results(match_id))
        await interaction.followup.send(embed=ro3_result_card(match, match_players, round_results, match.get("map_pool") or []))

    @tasks.loop(seconds=config.APPROVAL_SWEEP_INTERVAL_SECONDS)
    async def approval_sweep(self):
        """DB-backed, not an in-memory per-match timer — deadline lives on
        matches.approval_deadline, so a bot restart mid-window doesn't lose
        track of anything, same reasoning as queue.py's cleanup_sweep. One
        query covers however many matches happen to be overdue at once —
        cost doesn't scale with concurrent match count."""
        now_iso = discord.utils.utcnow().isoformat()
        try:
            overdue = await adb.get_overdue_pending_matches(now_iso)
        except Exception:
            logger.exception("approval_sweep: get_overdue_pending_matches failed")
            return

        guild = self.bot.get_guild(config.GUILD_ID)
        for match in overdue:
            # Re-check has_open_issue right here (inside _do_approve), not
            # just at query time — a correction filed between the query
            # above and this call still correctly blocks approval.
            success, _ = await self._do_approve(guild, match["id"], match.get("room_code_shared_by"))
            if not success:
                continue  # blocked by an open issue, or the RPC itself rejected it — try again next sweep

            text_channel_id = match.get("text_channel_id")
            channel = guild.get_channel(int(text_channel_id)) if guild and text_channel_id else None
            if channel:
                try:
                    await channel.send(
                        "⏱️ **Auto-approved** — host didn't confirm within the review window, so this result "
                        "went through automatically. Flag anything wrong with `/correction-result`."
                    )
                except discord.HTTPException:
                    pass

            approval_channel = self.bot.get_channel(config.RESULT_APPROVAL_CHANNEL_ID) if config.RESULT_APPROVAL_CHANNEL_ID else None
            if approval_channel:
                try:
                    await approval_channel.send(
                        f"⏱️ Match **{match['match_id']}** auto-approved — host didn't review within "
                        f"{config.APPROVAL_TIMEOUT_SECONDS // 60} min. Worth a look if this keeps happening for the same host."
                    )
                except discord.HTTPException:
                    pass

    @approval_sweep.before_loop
    async def before_approval_sweep(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    cog = Match(bot)
    await bot.add_cog(cog)
    bot.add_view(SubmissionPanelView(cog))
    bot.add_dynamic_items(IssueResolveButton)
    cog.approval_sweep.start()