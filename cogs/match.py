from __future__ import annotations

import asyncio
import difflib
import re
from collections import Counter
from datetime import timedelta

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

import config
import logging
from database.db import adb, with_retry
from services import localization, mmr_engine, validation, vision_extraction
from utils.embeds import verification_card
from utils.permissions import admin_only, is_admin

logger = logging.getLogger(__name__)


_INTEGER_FIELDS = ("position", "kills", "deaths", "assists", "score")

def normalize_match_code(raw: str) -> str:
    """Accepts whatever a user actually types for a match code and
    normalizes it to the canonical CQ-XXXX format (uppercase, hyphen,
    digits only) before it's used in a DB lookup. Added 2026-07-29 —
    found live: users frequently forgot the CQ- prefix, used lowercase,
    or dropped the hyphen ("1324", "cq1324", "cq-1324"), got an exact-
    match failure with no clear next step, and had to re-attempt the
    whole upload. Rather than pre-filling a hint (Discord slash-command
    string options don't support a default value, only modals do —
    see MatchSubmitModal below), this normalizes on the receiving end so
    ANY reasonable variant of the code works on the first try, regardless
    of entry point (modal, /match-submit, /match-correction all funnel
    through this). Strips everything except letters/digits, then
    re-assembles as CQ-<digits> — so "cq1324", "1324", " CQ-1324 ",
    "Cq1324" all normalize to "CQ-1324". If the result doesn't look like
    a valid code (no digits at all), returns the cleaned-but-unprefixed
    input as-is so get_match_by_code's "not found" error still fires
    with a sensible value rather than masking a genuinely malformed
    input as a lookup miss."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if cleaned.startswith("CQ"):
        cleaned = cleaned[2:]
    digits = re.sub(r"[^0-9]", "", cleaned)
    if not digits:
        return raw.strip()  # nothing digit-like — let the "not found" path handle it
    return f"CQ-{digits}"


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
_DISCORD_EMBED_DESCRIPTION_LIMIT = 4096


def _truncate_for_discord(prefix: str, parts: list[str], sep: str = "; ", limit: int = _DISCORD_MESSAGE_LIMIT) -> str:
    """Join `parts` onto `prefix`, trimming to stay under `limit`. Cuts
    whole parts (never mid-sentence) and appends a "+N more" note so
    admins know the list was cut, not truncated silently.

    Default limit (2000) is Discord's plain-message cap — right for
    direct chat sends. Pass limit=_DISCORD_EMBED_DESCRIPTION_LIMIT (4096)
    when populating an embed description instead; using the wrong one
    was a real bug found live 2026-07-19 — a single long technical_detail
    string, evaluated against the 2000 message limit while living inside
    an embed, couldn't fit as a whole "part" and silently fell back to
    nothing but the "+1 more" placeholder, hiding the entire detail an
    admin actually needed."""
    text = prefix
    included = 0
    for part in parts:
        candidate = text + (sep if included else "") + part
        if len(candidate) > limit - 40:  # headroom for the "+N more" suffix
            break
        text = candidate
        included += 1
    remaining = len(parts) - included
    if remaining > 0:
        text += f" (+{remaining} more — see admin review panel for full detail)"
    return text


class HostApprovalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"host_approve:(?P<match_id>[0-9]+)"):
    """Fix 2026-07-29: was a plain View button with timeout=3600 and no
    custom_id — died after 1hr in-memory OR instantly on any bot restart,
    since it was never registered via bot.add_view(). Discord kept
    rendering the button (component state persists on Discord's side)
    but nothing was listening, producing a silent client-side "didn't
    respond in time" with zero server-side log trace.

    Same DynamicItem fix as IssueResolveButton above: custom_id embeds
    match_id and gets regex-matched, so this stays clickable indefinitely
    regardless of how long the bot has been running or how many restarts
    happened in between. Registered once, generically, in setup() below —
    no per-match bot.add_view() call needed, and no re-registration on
    restart needed either."""

    def __init__(self, match_id: int):
        super().__init__(
            discord.ui.Button(label="Approve Result", style=discord.ButtonStyle.success, custom_id=f"host_approve:{match_id}")
        )
        self.match_id = match_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: "re.Match[str]"):
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Match")
        await cog.approve_result(interaction, self.match_id)


class HostApprovalView(discord.ui.View):
    """Thin wrapper so call sites can keep doing view=HostApprovalView(cog, match_id)
    without needing to know about DynamicItem internals — same pattern as
    IssueResolveView below. cog param kept for call-site compatibility
    (unused internally now; the DynamicItem resolves its own cog via
    interaction.client.get_cog at callback time)."""

    def __init__(self, cog: "Match", match_id: int):
        super().__init__(timeout=None)
        self.add_item(HostApprovalButton(match_id))


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
    see Match.start_submission below for why this two-step exists.

    2026-07-29: default="CQ-" pre-fills the prefix so the user's cursor
    lands right after it and they only type the number — found live that
    users frequently forgot the prefix or typed it lowercase/without the
    hyphen. Whatever they end up submitting is also normalized via
    normalize_match_code() as a second layer, so even if they clear the
    default and type something else entirely (e.g. just "1324"), it still
    resolves correctly instead of failing on an exact-format mismatch."""

    match_id_input = discord.ui.TextInput(
        label="Match ID",
        placeholder="e.g. CQ-0001",
        default="CQ-",
        required=True,
        max_length=16,
    )

    def __init__(self, cog: "Match"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.start_submission(interaction, normalize_match_code(self.match_id_input.value))


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
        # Unified 2026-07-29: was region-aware (checked against either
        # region's upload channel, since this button has no match_id yet
        # and can't resolve a specific match's region). Now there's only
        # ONE upload channel for all 4 queues, so this is a plain
        # single-value check — no per-match resolution needed at all,
        # here or in match_submit() below.
        if config.RESULT_UPLOAD_CHANNEL_ID and interaction.channel_id != config.RESULT_UPLOAD_CHANNEL_ID:
            await interaction.response.send_message(
                "Match results can only be submitted in the result-upload channel.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(MatchSubmitModal(self.cog))


class Match(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        localization.load_map_translations()

    def _in_upload_channel(self, interaction: discord.Interaction) -> bool:
        # Fail open (not block) if RESULT_UPLOAD_CHANNEL_ID isn't set, so a
        # missing config value doesn't brick the whole command — matches
        # the AFK_CHANNEL_ID / MATCH_LOG_CHANNEL_ID no-op pattern. Unified
        # 2026-07-29: was per-region (took a region str, looked it up in
        # RESULT_UPLOAD_CHANNEL_IDS). Now a single fixed channel for all
        # 4 queues — no per-match region lookup needed.
        if not config.RESULT_UPLOAD_CHANNEL_ID:
            return True
        return interaction.channel_id == config.RESULT_UPLOAD_CHANNEL_ID

    @app_commands.command(name="correction-result", description="Host: flag a problem with this match's result before or after approval")
    @app_commands.describe(match_id="Just the number is fine (e.g. 1234 or CQ-1234)")
    @app_commands.checks.cooldown(1, config.CORRECTION_COMMAND_COOLDOWN_SECONDS, key=lambda i: (i.guild_id, i.channel_id))
    async def correction_result(self, interaction: discord.Interaction, match_id: str):
        match = await adb.get_match_by_code(normalize_match_code(match_id))
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
        # Unified 2026-07-29 — was per-region (took a region str). One
        # approval channel for all 4 queues now, same reasoning as
        # _in_upload_channel above.
        if not config.RESULT_APPROVAL_CHANNEL_ID:
            return None
        return self.bot.get_channel(config.RESULT_APPROVAL_CHANNEL_ID)

    @app_commands.command(name="match-submit-post", description="Post the persistent match-results submission panel in this channel")
    @admin_only()
    async def match_submit_post(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Submit Match Results",
                description="Match Host: click below after the match is played.",
                color=discord.Color.blurple(),
            ),
            view=SubmissionPanelView(self),
        )

    # REMOVED 2026-07-29: /match-roomcode was a pre-/rc prototype that
    # never got wired to _post_match_log (cogs/queue.py) — using it left
    # room_code + status correctly set in the DB, but silently produced
    # NO match-log channel entry, unlike /rc which does both in one write.
    # It also required the host to type the match_id by hand instead of
    # reading it off the channel name, so a typo failed with a generic
    # "Match not found" instead of ever reaching the log-post step.
    # /rc (cogs/queue.py) is the supported command going forward —
    # confirmed on CQ-2063: /rc posted the match-log entry correctly.
    # Kept here commented out (not deleted) for traceability; safe to
    # delete outright once confirmed nothing in prod still calls
    # /match-roomcode.
    #
    # @app_commands.command(name="match-roomcode", description="Share the in-game room code for a match")
    # async def match_roomcode(self, interaction: discord.Interaction, match_id: str, code: str):
    #     match = await adb.get_match_by_code(match_id)
    #     player = await adb.get_player_by_discord_id(interaction.user.id)
    #     if not match or match["status"] != "awaiting_room":
    #         await interaction.response.send_message("Match not found or not awaiting a room code.", ephemeral=True)
    #         return
    #     if not player or match.get("room_code_shared_by") != player["id"]:
    #         await interaction.response.send_message("Only the Match Host can share the room code.", ephemeral=True)
    #         return
    #     await adb.update_match(match["id"], {"room_code": code, "status": "awaiting_result"})
    #     await interaction.response.send_message(f"Room code for **{match_id}** set. Play all three rounds, then upload the scoreboards.")

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
                        description=_truncate_for_discord("", [technical_detail], limit=_DISCORD_EMBED_DESCRIPTION_LIMIT),
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
        # Defer FIRST, before any DB/Discord work — this function does
        # several sequential awaits (DB writes, a message edit, an
        # outbound send) that can easily eat the ~3s initial-response
        # window. Found live 2026-07-18: calling response.send_message
        # only at the end raised "Unknown interaction" (404) because the
        # token had already expired by the time we got there. Same
        # defer-first pattern already used correctly in match_submit.
        await interaction.response.defer(ephemeral=True, thinking=True)

        admin_player = await with_retry(adb.get_player_by_discord_id, interaction.user.id)
        issue = await with_retry(adb.resolve_match_issue, issue_id, admin_player["id"] if admin_player else None, note)
        reporter = await with_retry(adb.get_players_by_ids, [issue["reported_by"]])
        reporter_discord_id = reporter[0]["discord_id"] if reporter else None

        # Edit the intake message in place rather than deleting it, so the
        # channel stays a readable history of what came in and what happened.
        # The resolve above (line 367) already succeeded — that's the real
        # state change. This edit is cosmetic; if it fails, log it instead
        # of silently swallowing the failure (found live 2026-07-20 — this
        # was a bare `except: pass`, unlike every other write-then-visual-
        # update spot in the codebase, which logs a warning so a stale-
        # looking intake message can be correlated back to a real cause
        # instead of looking like an unexplained UI glitch).
        match_label = "your match"  # fallback if the embed is ever missing/malformed
        try:
            resolved_embed = original_message.embeds[0]
            # The intake embed's title always starts "Match CQ-XXXX — ..."
            # (see line 245/417 where it's created) — pull the code back
            # out of it rather than a fresh adb.get_match() round-trip;
            # this embed is already fetched and about to be edited anyway,
            # so re-querying the DB for data already sitting in memory
            # would be pure waste. split(" ", 2) survives either of the
            # two title variants ("needs review" / "host-filed correction
            # request") since both share the same "Match CQ-XXXX — " prefix.
            if resolved_embed.title and resolved_embed.title.startswith("Match "):
                match_label = f"**{resolved_embed.title.split(' ', 2)[1]}**"
            resolved_embed.color = discord.Color.green()
            resolved_embed.add_field(name="Status", value=f"✅ Resolved by {interaction.user.mention}" + (f" — {note}" if note else ""))
            await original_message.edit(embed=resolved_embed, view=None)
        except (discord.HTTPException, IndexError) as e:
            logger.warning(
                "resolve_match_issue: failed to update intake message for issue_id=%s (resolution already saved): %s",
                issue_id, e,
            )

        outbound_channel = self.bot.get_channel(config.ISSUE_RESOLVED_CHANNEL_ID) if config.ISSUE_RESOLVED_CHANNEL_ID else None
        if outbound_channel:
            mention = f"<@{reporter_discord_id}>" if reporter_discord_id else "player"
            # More specific than the old generic "reviewed and sorted":
            # names the actual match, and surfaces the admin's note (if
            # one was left) so the player knows WHAT was fixed, not just
            # that something was.
            note_suffix = f" — {note}" if note else ""
            try:
                await outbound_channel.send(
                    f"✅ {mention} — {match_label} has been reviewed and resolved{note_suffix}. "
                    f"The leaderboard is up to date. Thanks for the report!"
                )
            except discord.HTTPException:
                pass

        await interaction.followup.send("Marked resolved.", ephemeral=True)

    @app_commands.command(name="match-submit", description="Host upload of the match scoreboard screenshot")
    @app_commands.describe(match_id="Just the number is fine (e.g. 1234 or CQ-1234)", screenshot="Match scoreboard")
    async def match_submit(self, interaction: discord.Interaction, match_id: str,
                           screenshot: discord.Attachment):
        # Unified 2026-07-29: was region-aware (had to fetch the match
        # first to know which region's channel was correct). Now there's
        # one upload channel for all 4 queues, so the channel check no
        # longer depends on match state at all — kept after the match
        # fetch anyway since "match not found" should still win as the
        # more specific error when both are wrong.
        match_id = normalize_match_code(match_id)
        match = await adb.get_match_by_code(match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        if not self._in_upload_channel(interaction):
            channel_mention = f"<#{config.RESULT_UPLOAD_CHANNEL_ID}>" if config.RESULT_UPLOAD_CHANNEL_ID else "the result-upload channel"
            await interaction.response.send_message(
                f"Match results can only be submitted in {channel_mention}.", ephemeral=True
            )
            return

        player = await adb.get_player_by_discord_id(interaction.user.id)
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

        # Unified 2026-07-29: admin-upload-on-host's-behalf exception. If
        # the room code sharer can't upload themselves (afk, crashed,
        # phone died, whatever), an admin (or HOD — same ADMIN_ROLE_IDS
        # set, see utils/permissions.py) can upload for them instead.
        # is_admin() is checked FIRST as an explicit bypass — the
        # host-identity check below is completely skipped for admins,
        # not weakened, so this never accidentally loosens who counts as
        # "the host" for a non-admin uploader.
        uploader_is_admin = is_admin(interaction)
        if not uploader_is_admin:
            if not player or match.get("room_code_shared_by") != player["id"]:
                await interaction.response.send_message("Only the Match Host can upload scoreboards.", ephemeral=True)
                return

        attachments = (screenshot,)
        for attachment in attachments:
            if not (attachment.content_type or "").startswith("image/"):
                await interaction.response.send_message("The upload must be an image file.", ephemeral=True)
                return
            if attachment.size > config.MAX_SCOREBOARD_UPLOAD_BYTES:
                await interaction.response.send_message("Each image must be within the configured upload limit.", ephemeral=True)
                return

        maps = match.get("map_pool") or []
        if len(maps) != 1:
            await self._route_to_review(match, player["id"] if player else None, "result_issue", "match has no valid map announcement (map_pool missing or incomplete)")
            await interaction.response.send_message(self._friendly_review_message(), ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            # Unified 2026-07-29: uploaded_by on match_screenshots now
            # stores the Discord user ID of whoever actually clicked
            # submit — the host's ID in the normal case, or the admin's
            # ID when the admin-upload exception above was used. This is
            # deliberately interaction.user.id, NOT player["id"] — an
            # admin uploading on the host's behalf may have no players
            # row at all, and match_screenshots.uploaded_by no longer has
            # an FK to players(id) (see migration_010), so there's no
            # reason to force it through the player lookup anymore.
            await self._submit_body(interaction, match, player, maps, attachments, interaction.user.id)
        except Exception as exc:
            # Safety net for anything NOT already caught by the specific
            # try/excepts inside _submit_body (OCR failure, validation
            # failure, etc.) — an unhandled crash here should never leave
            # the player staring at "thinking..." forever with silence.
            # Found live 2026-07-18: a return-value mismatch in
            # _prepare_round crashed match_submit with zero notification
            # to anyone, player or admin.
            logger.exception("match_submit: unhandled exception for match_id=%s", match_id)
            try:
                await self._route_to_review(match, player["id"] if player else None, "vision_failure",
                                             f"Unhandled exception in match_submit: {exc!r}")
            except Exception:
                logger.exception("match_submit: even _route_to_review failed while handling the original exception")
            await interaction.followup.send(
                "Something went wrong on our end processing this submission — it's been flagged for admin "
                "review automatically. Sorry about that, we'll sort it out.", ephemeral=True
            )

    async def _submit_body(self, interaction: discord.Interaction, match: dict, player: dict | None,
                            maps: list[str], attachments: tuple[discord.Attachment, ...],
                            uploader_discord_id: int) -> None:
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

        match_players = await with_retry(adb.get_match_players, match["id"])
        # RO1 (2026-08): _reorder_pairs_by_map deleted - nothing to
        # reorder with a single screenshot. ordered_pairs is just the
        # one (extraction, attachment) pair, kept as a list so the
        # gather/enumerate calls below stay unchanged in shape.
        ordered_pairs = list(zip(extractions, attachments))
        ordered_extractions = [pair[0] for pair in ordered_pairs]

        # Preserve the raw OCR audit record for the submitted screenshot.
        await asyncio.gather(*(
            with_retry(adb.upsert_match_screenshot, match["id"], number, attachment.url, uploader_discord_id, extraction,
                        extraction.get("ocr_confidence"))
            for number, (extraction, attachment) in enumerate(ordered_pairs, start=1)
        ))

        round_data, review_reasons = self._prepare_round(match_players, maps[0], ordered_extractions[0])
        round_data = [round_data]

        # Reform 2026-07-29: previously any non-empty review_reasons caused
        # an early return here, before either DB write below ever ran —
        # meaning a SINGLE bad round (e.g. one map-name misread) discarded
        # every other round's fully-valid data too. Confirmed live: a real
        # match with one bad round and two clean rounds wrote zero rows to
        # match_player_stats/match_round_results. Fix: write whichever
        # rounds _prepare_round marked "clean" (see its docstring) FIRST,
        # unconditionally, then still route to review below if needed —
        # the bad round(s) simply contribute nothing until corrected, the
        # good round(s) are no longer held hostage by them. MMR is
        # UNCHANGED: mmr_delta values are written here same as before, but
        # they still cannot affect players.mmr until approve_match
        # actually commits — this write is the same "provisional record"
        # it always was, just no longer gated on the WHOLE match validating.
        #
        # NOTE: results rows carry "discord_id" for the verification embed's
        # @mentions (verification_card below), but match_round_results
        # has no such column — confirmed live via a 400 PGRST204 error when
        # this wasn't stripped first. Strip it only for the DB payload; the
        # embed still gets the full row with discord_id intact via round_data.
        _ROUND_RESULT_FIELDS = ("player_id", "position", "is_mvp", "mmr_delta", "team")
        _PLAYER_STAT_FIELDS = ("player_id", "kills", "deaths", "assists", "damage", "hill_time", "impact", "score")
        clean_rounds = [item for item in round_data if item["clean"]]
        if clean_rounds:
            await asyncio.gather(*(
                with_retry(
                    adb.replace_match_round_results,
                    match["id"], item["round_number"],
                    [{k: v for k, v in row.items() if k in _ROUND_RESULT_FIELDS} for row in item["results"]],
                )
                for item in clean_rounds
            ))
            # P6: raw per-round stats, written from the same round_data that
            # was already assembled above — no re-extraction, no second OCR
            # pass. See migration_006_p6_stats_and_ranks.sql.
            await asyncio.gather(*(
                with_retry(
                    adb.replace_match_player_stats,
                    match["id"], item["round_number"],
                    [{k: v for k, v in row.items() if k in _PLAYER_STAT_FIELDS} for row in item["results"]],
                )
                for item in clean_rounds
            ))
            # Same fire-and-forget pattern as _do_approve's post-approval
            # recompute — a partial (clean-rounds-only) write should still
            # surface on /player-stats right away rather than waiting for
            # the whole match to eventually clear review. Never blocks or
            # fails the submission itself.
            recompute_results = await asyncio.gather(
                *(with_retry(adb.recompute_player_career_stats, mp["player_id"]) for mp in match_players),
                return_exceptions=True,
            )
            for mp, result in zip(match_players, recompute_results):
                if isinstance(result, Exception):
                    logger.exception(
                        "recompute_player_career_stats (provisional, partial-clean-rounds) failed for "
                        "player_id=%s after match_id=%s submission", mp["player_id"], match["id"], exc_info=result,
                    )

            # AFK notice — fires once per submission if _prepare_round
            # synthesized a leaver's row (see the "afk" key added there).
            # Purely informational, does not block anything below; the
            # match proceeds through the normal verification/approval
            # flow exactly as if all 10 rows had come from OCR.
            for item in clean_rounds:
                for row in item["results"]:
                    if row.get("afk"):
                        ign = next((mp["players"]["ign"] for mp in match_players if mp["player_id"] == row["player_id"]), "unknown player")
                        await self._notify_afk_leaver(match, row, ign)

        if review_reasons:
            screenshot_links = "\n".join(pair[1].url for pair in ordered_pairs)
            technical_detail = "Validation failed: " + "; ".join(review_reasons) + f"\n\nScreenshot:\n{screenshot_links}"
            await self._route_to_review(match, player["id"] if player else None, "vision_failure", technical_detail)
            await interaction.followup.send(self._friendly_review_message(), ephemeral=True)
            return

        validations = await asyncio.gather(*(
            validation.validate_submission(match["id"], extraction)
            for extraction in ordered_extractions
        ))
        flags = {pid: issues for result in validations for pid, issues in result["flags"].items()}
        if flags:
            players = {item["id"]: item for item in await with_retry(adb.get_players_by_ids, list(flags))}
            summary_parts = [f"{players.get(pid, {}).get('ign', pid)}: {', '.join(issues)}" for pid, issues in flags.items()]
            await self._route_to_review(match, player["id"] if player else None, "vision_failure", "Stat validation flagged: " + "; ".join(summary_parts))
            await interaction.followup.send(self._friendly_review_message(), ephemeral=True)
            return

        deadline = (discord.utils.utcnow() + timedelta(seconds=config.APPROVAL_TIMEOUT_SECONDS)).isoformat()
        await with_retry(adb.update_match, match["id"], {"status": "pending_verification", "approval_deadline": deadline})

        # Reform 2026-07-29: career stats (K/D, matches played, avg damage,
        # etc.) are now visible on /player-stats as soon as OCR passes and
        # a match reaches pending_verification — not gated on host/sweep
        # approval anymore. See migration_011_provisional_stats.sql for
        # the read-path change this depends on. MMR/rank are UNCHANGED —
        # still only committed by approve_match inside _do_approve().
        # Same fire-and-forget pattern as that call site: a recompute
        # failure here must never block or fail the submission itself.
        # (This runs again here even though the clean-rounds block above
        # may have already recomputed once — harmless, same idempotent
        # full-aggregate function, just cheap redundancy on the all-clean
        # happy path rather than added complexity to skip it.)
        recompute_results = await asyncio.gather(
            *(with_retry(adb.recompute_player_career_stats, mp["player_id"]) for mp in match_players),
            return_exceptions=True,
        )
        for mp, result in zip(match_players, recompute_results):
            if isinstance(result, Exception):
                logger.exception(
                    "recompute_player_career_stats (provisional, pending_verification) failed for "
                    "player_id=%s after match_id=%s submission", mp["player_id"], match["id"], exc_info=result,
                )

        # Channel lock: the verification card always posts in the
        # configured approval channel, never wherever /match-submit
        # happened to run.
        approval_channel = await self._approval_channel()
        if approval_channel is None:
            # Fail safe rather than fail silent — the match is validly at
            # pending_verification in the DB, but nobody can see the card
            # to approve it until this env var is set. Tell the uploader.
            await interaction.followup.send(
                "Scoreboards accepted, but the approval channel isn't configured — "
                "an admin needs to set RESULT_APPROVAL_CHANNEL_ID before this match can be approved.",
                ephemeral=True,
            )
            return
        await approval_channel.send(embed=verification_card(match, round_data, ordered_extractions[0], maps[0]), view=HostApprovalView(self, match["id"]))
        await interaction.followup.send(
            f"Submitted. Check {approval_channel.mention} to approve once you've verified the result.",
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
            await interaction.response.send_message("Match not found or not awaiting its scoreboard.", ephemeral=True)
            return
        if not player or match.get("room_code_shared_by") != player["id"]:
            await interaction.response.send_message("Only the Match Host can upload the scoreboard.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Match **{match_id}** confirmed. Now run `/match-submit match_id:{match_id}` "
            f"in this channel and attach the scoreboard screenshot to that command.",
            ephemeral=True,
        )

    @staticmethod
    def _fuzzy_lookup(ign: str, roster: dict) -> tuple[dict | None, str | None]:
        """Shared fuzzy-match + ambiguity-tiebreak core, used by both the
        raw-string pass and the stripped-parenthetical pass in
        _resolve_ign so the two share identical collision-safety logic —
        a fuzzy match is only accepted when exactly one roster IGN is
        decisively closer than every other candidate; two IGNs close
        enough that a one-character OCR slip could mean either one are
        refused, not guessed.

        Returns (match_player_or_None, ambiguity_note_or_None), same
        contract as _resolve_ign itself.
        """
        candidates = difflib.get_close_matches(ign, roster.keys(), n=3, cutoff=0.75)
        if not candidates:
            return None, None
        if len(candidates) == 1:
            return roster[candidates[0]], None

        scores = [(c, difflib.SequenceMatcher(None, ign, c).ratio()) for c in candidates]
        scores.sort(key=lambda item: item[1], reverse=True)
        best_ign, best_score = scores[0]
        runner_ign, runner_score = scores[1]
        if best_score - runner_score >= 0.15:
            return roster[best_ign], None

        display = ", ".join(roster[c]["players"]["ign"] for c, _ in scores[:2])
        return None, f"ambiguous — could be {display}"

    @staticmethod
    def _resolve_ign(raw_ign: str, roster: dict) -> tuple[dict | None, str | None]:
        """Look up an OCR-read IGN against this match's 10-player roster.

        Four-step sequence, each step only reached if every step before it
        came back with a clean miss (never overrides an ambiguity refusal):

        1. Exact match (case-insensitive) on the raw OCR string — the
           overwhelming common case, zero risk.
        2. Fuzzy match on the raw string (see _fuzzy_lookup) — catches
           ordinary OCR misreads (e.g. "Ézio." vs "Ezío.").
        3. Strip a "(...)" suffix, if present, and retry as an EXACT match
           on the remainder. CQ Mobile appends a parenthetical after some
           players' names on the scoreboard — a short/lowercase echo of
           their own IGN shown when the full name gets truncated (e.g.
           "RVL.Eiji(eiji)", "CÖNÑÖR(Con...)"). This is UI chrome, not part
           of the IGN, and OCR reads it verbatim per its prompt ("string,
           exactly as shown"). Never reads what was inside the parens —
           only ever discards it and matches on the part before "(".
        4. Strip the same "(...)" suffix and retry with a full fuzzy pass
           (_fuzzy_lookup again) on the remainder — catches the combined
           case where a player's name has BOTH the parenthetical AND an
           ordinary OCR character slip in the base name (e.g. OCR misreads
           "RVL.Eiji(eiji)" as "RVL.Eijl(eiji)"), which step 3's exact-only
           check can't catch on its own. Same collision-safety tiebreak as
           step 2, just run a second time on the stripped string.

        Steps 3 and 4 are a genuine last resort — they only run once
        BOTH step 1 and step 2 already missed against the raw string —
        so nothing about today's exact/fuzzy behavior changes for the
        overwhelming majority of IGNs that don't contain "(" at all.

        Returns (match_player_or_None, ambiguity_note_or_None). The note
        is set whenever a step deliberately declined an ambiguous fuzzy
        match (step 2 or step 4), so the caller can produce a "did you
        mean X or Y?" message instead of a bare "unknown IGN".
        """
        ign = raw_ign.strip().lower()

        # Step 1: exact match on the raw string.
        exact = roster.get(ign)
        if exact:
            return exact, None

        # Step 2: fuzzy match on the raw string.
        result, note = Match._fuzzy_lookup(ign, roster)
        if result or note:
            return result, note

        # Both steps 1 and 2 came back a clean miss (no match, no
        # ambiguity note) — only now do we consider stripping a
        # parenthetical, and only if one is actually present.
        if "(" not in ign:
            return None, None
        stripped = ign.split("(", 1)[0].strip()
        if not stripped:
            return None, None

        # Step 3: exact match on the stripped string.
        stripped_exact = roster.get(stripped)
        if stripped_exact:
            return stripped_exact, None

        # Step 4: fuzzy match on the stripped string — same collision
        # safety as step 2, just applied to the parenthetical-free name.
        return Match._fuzzy_lookup(stripped, roster)

    async def _notify_afk_leaver(self, match: dict, leaver_row: dict, leaver_ign: str) -> None:
        """Informational only — does NOT create a match_issues row and
        does NOT touch match status, unlike _route_to_review. The match
        this belongs to has already been written as a normal 10-row
        clean submission (see the AFK branch in _prepare_round) and
        proceeds through the ordinary verification/approval flow
        untouched. This just flags the synthesized row to admins so
        they know to check in with the player and, if the reason is
        valid, correct the MMR by hand via the existing /admin-adjust-mmr
        command — no new admin command, no blocking behavior."""
        intake_channel = self.bot.get_channel(config.ISSUE_INTAKE_CHANNEL_ID) if config.ISSUE_INTAKE_CHANNEL_ID else None
        if not intake_channel:
            return
        try:
            await intake_channel.send(
                embed=discord.Embed(
                    title=f"Match {match['match_id']} — AFK detected",
                    description=(
                        f"**{leaver_ign}** was missing from the submitted scoreboard and was "
                        f"auto-assigned a last-place loss ({leaver_row['mmr_delta']:+d} MMR) for "
                        f"this match. This did not block approval.\n\n"
                        f"If the player has a valid reason, adjust their MMR with "
                        f"`/admin-adjust-mmr` — no action needed otherwise."
                    ),
                    color=discord.Color.orange(),
                )
            )
        except discord.HTTPException:
            pass

    @staticmethod
    def _prepare_round(match_players: list[dict], announced_map: str, extraction: dict) -> tuple[dict, list[str]]:
        """RO1 (2026-08): de-looped from the original _prepare_rounds,
        which processed 3 rounds via enumerate(zip(maps, extractions)).
        Same validation logic per round, just run once instead of
        looped — team/winner resolution (OCR-grouping-based, not the
        static match_players.team) is UNCHANGED, see the comment below.
        round_number is hardcoded to 1 (schema still allows 1-3, kept
        for parity with historical RO3 rows — see migration_015_ro1.sql).
        Reason strings no longer carry a "round N:" prefix — with only
        one round, the prefix disambiguated nothing and just added
        noise to review messages.
        """
        roster = {mp["players"]["ign"].strip().lower(): mp for mp in match_players}
        reasons: list[str] = []
        resolved_map = localization.resolve_map_name(str(extraction.get("map") or ""))
        if resolved_map != announced_map.upper():
            raw_map = extraction.get("map")
            reasons.append(
                f"map mismatch — announced **{announced_map}**, "
                f"screenshot read as {raw_map!r}" +
                (f" (resolved to {resolved_map}, still doesn't match)" if resolved_map else " (not recognized by the map translation table at all)")
            )
        score = str(extraction.get("final_score") or "")
        score_match = _SCORE_RE.fullmatch(score)
        if not score_match or score_match.group(1) == score_match.group(2):
            reasons.append("final score is unreadable")
            return {"round_number": 1, "map_name": announced_map, "final_score": score, "results": [], "clean": False}, reasons
        # Winner/loser is resolved from the OCR's own screen-position
        # grouping (row["team"], "top group = A" per the vision prompt),
        # NOT from match_players.team. match_players.team is a static
        # letter fixed once at bootstrap purely for the Discord
        # Defender/Attacker display label — it has no guaranteed
        # relationship to which physical lobby side a player actually
        # sits on in a given round. Hardpoint has no real attack/defense
        # mechanic (both teams do the same thing), so nothing is lost by
        # not enforcing that mapping: this way a genuine in-game seating
        # mix-up (whole 5-player group loaded onto the "wrong" color)
        # resolves correctly on its own, instead of failing every player
        # in the round with a false "team mismatch". Confirmed further:
        # the post-match "Match Details" screen shows each viewer's own
        # team as blue regardless of physical side (observer-relative),
        # so screen color was never a reliable signal to begin with —
        # only the true spectator view shows real Defender/Attacker
        # sides. See DECISIONS.md for the accepted tradeoff (a 1-2
        # player crossover, as opposed to a whole-group swap, is not
        # detectable by this check).
        winner = "A" if int(score_match.group(1)) > int(score_match.group(2)) else "B"
        results: list[dict] = []
        seen_players: set[int] = set()
        per_team = Counter()
        for row in extraction.get("players", []):
            mp, ambiguity = Match._resolve_ign(str(row.get("ign") or ""), roster)
            if not mp:
                if ambiguity:
                    reasons.append(f"OCR IGN {row.get('ign')!r} is {ambiguity} — needs manual confirmation")
                else:
                    reasons.append(f"unknown OCR IGN {row.get('ign')!r}")
                continue
            if mp["player_id"] in seen_players:
                reasons.append(f"duplicate OCR player {row.get('ign')}")
                continue
            round_team = row.get("team")
            if round_team not in ("A", "B"):
                reasons.append(f"unreadable team grouping for {row.get('ign')!r}")
                continue
            invalid = [field for field in _INTEGER_FIELDS if not _INTEGER_RE.fullmatch(str(row.get(field, "")))]
            if not _HILL_TIME_RE.fullmatch(str(row.get("hill_time", ""))):
                invalid.append("hill_time")
            if invalid:
                reasons.append(f"invalid OCR digit format for {row.get('ign')} ({', '.join(invalid)})")
                continue
            position = int(row["position"])
            if not 1 <= position <= 5:
                reasons.append(f"invalid position for {row.get('ign')}")
                continue
            if not isinstance(row.get("is_mvp"), bool):
                reasons.append(f"MVP flag is missing or invalid for {row.get('ign')}")
                continue
            is_mvp = row["is_mvp"]
            # damage is deliberately excluded from _INTEGER_FIELDS (see
            # module-level NOTE) — it can be legitimately absent or
            # non-numeric when a screenshot's scoreboard view doesn't
            # show a Damage column. Parse it defensively here rather
            # than assuming it already passed a digit check.
            raw_damage = str(row.get("damage", ""))
            damage_value = int(raw_damage) if _INTEGER_RE.fullmatch(raw_damage) else None
            # impact, like damage, is never validated by _INTEGER_FIELDS
            # or any regex above — parse defensively rather than assume
            # it's always a clean number.
            raw_impact = str(row.get("impact", ""))
            impact_value = float(raw_impact) if _HILL_TIME_RE.fullmatch(raw_impact) else None
            results.append({"player_id": mp["player_id"], "position": position, "is_mvp": is_mvp,
                            "mmr_delta": mmr_engine.calculate_mmr_change(position, round_team == winner, is_mvp),
                            "team": round_team, "discord_id": mp["players"]["discord_id"],
                            # Raw stats, kept alongside the MMR/position outcome so
                            # match_player_stats can be written from this same pass
                            # instead of re-deriving it later (P6 — see
                            # migration_006_p6_stats_and_ranks.sql).
                            "kills": int(row["kills"]), "deaths": int(row["deaths"]),
                            "assists": int(row["assists"]), "damage": damage_value,
                            "hill_time": float(row["hill_time"]),
                            "impact": impact_value,
                            "score": int(row["score"])})
            seen_players.add(mp["player_id"])
            per_team[round_team] += 1
        # AFK / mid-match leaver detection (2026-08). Only fires when the
        # gap is unambiguous: exactly 9 of the 10 registered players
        # resolved cleanly above (no OCR-unknown, no fuzzy-match
        # ambiguity, no duplicates, no bad team/digit/position/MVP data)
        # AND exactly one registered player has no corresponding row at
        # all. Any messier case — 2+ missing, an unresolved/ambiguous
        # OCR name, wrong per-team counts — falls straight through to
        # the existing "scoreboard does not contain one valid row for
        # every match player" review path below, unchanged. This is a
        # deliberately narrow net: a genuinely unambiguous 9/10 read is
        # common enough to be worth automating, but a messy read that
        # merely LOOKS like 9/10 (e.g. one real OCR misread on top of
        # a real leaver) must not be auto-resolved — it goes to a human.
        missing_players = [mp for mp in match_players if mp["player_id"] not in seen_players]
        if len(results) == 9 and len(missing_players) == 1 and not reasons and per_team.get(missing_players[0]["team"], 0) == 4:
            leaver = missing_players[0]
            leaver_team = leaver["team"]
            taken_positions = {row["position"] for row in results if row["team"] == leaver_team}
            leaver_position = next(p for p in range(1, 6) if p not in taken_positions)
            # Stats are honestly 0 — nothing happened for this player this
            # round. MMR is NOT 0 — calculate_mmr_change() runs exactly as
            # it would for any other losing-team player in this position,
            # so leaving is never better than playing out a loss. No new
            # MMR pathway, no new constant — same formula every other row
            # in this function uses two lines up.
            results.append({
                "player_id": leaver["player_id"], "position": leaver_position, "is_mvp": False,
                "mmr_delta": mmr_engine.calculate_mmr_change(leaver_position, leaver_team == winner, False),
                "team": leaver_team, "discord_id": leaver["players"]["discord_id"],
                "kills": 0, "deaths": 0, "assists": 0, "damage": 0, "hill_time": 0.0, "impact": 0.0, "score": 0,
                "afk": True,
            })
            seen_players.add(leaver["player_id"])
            per_team[leaver_team] += 1
        if len(results) != 10 or set(seen_players) != {mp["player_id"] for mp in match_players} or per_team != Counter({"A": 5, "B": 5}):
            reasons.append("scoreboard does not contain one valid row for every match player")
        for team in ("A", "B"):
            if sum(1 for row in results if row["team"] == team and row["is_mvp"]) != 1:
                reasons.append(f"Team {team} must have exactly one game-provided MVP")
        round_dict = {
            "round_number": 1, "map_name": announced_map, "final_score": score, "results": results,
            # Reform 2026-07-29 (RO3-era): a round is "clean" only if
            # nothing in its own checks (map, score, per-player OCR
            # fields, roster completeness, MVP count) added a reason.
            # With RO1 there's only ever one round, so this flag now
            # just means "did the whole submission validate cleanly" —
            # match_submit still uses it to decide whether to write
            # match_player_stats/match_round_results. MMR is UNCHANGED
            # by this — mmr_delta still only gets committed by
            # approve_match, which still requires full manual/auto
            # approval regardless of this flag.
            "clean": len(reasons) == 0,
        }
        return round_dict, reasons

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
            await adb.approve_match(match_id, approved_by_id)
        except Exception as exc:
            return False, f"Approval could not be committed safely: {exc}"

        # P6: career-stat recompute, one call per player in this match.
        # Deliberately AFTER the MMR commit above and wrapped so a
        # recompute failure never rolls back or blocks an approval that
        # has already landed — MMR is the authoritative, already-committed
        # outcome; career stats (record/KD/avg damage/etc.) are a
        # best-effort derived view and can be caught up later (e.g. by
        # re-running recompute_player_career_stats for the affected
        # player) without needing to touch matches or MMR at all.
        match_players = await adb.get_match_players(match_id)
        results = await asyncio.gather(
            *(with_retry(adb.recompute_player_career_stats, mp["player_id"]) for mp in match_players),
            return_exceptions=True,
        )
        for mp, result in zip(match_players, results):
            if isinstance(result, Exception):
                logger.exception(
                    "recompute_player_career_stats failed for player_id=%s after match_id=%s approval",
                    mp["player_id"], match_id, exc_info=result,
                )

        match = await adb.get_match(match_id)
        await self._run_post_approval_cleanup(guild, match)
        return True, "approved"

    async def approve_result(self, interaction: discord.Interaction, match_id: int):
        # Unified 2026-07-29: was region-aware (fetched the match first
        # to know which region's channel to check against). One approval
        # channel for all 4 queues now, so this is a plain fixed check —
        # still fetching the match first since "no longer awaiting
        # approval" should win as the more specific error either way.
        match = await adb.get_match(match_id)
        if not match:
            await interaction.response.send_message("This result is no longer awaiting host approval.", ephemeral=True)
            return

        if config.RESULT_APPROVAL_CHANNEL_ID and interaction.channel_id != config.RESULT_APPROVAL_CHANNEL_ID:
            await interaction.response.send_message(
                "This result can only be approved in the result-approval channel.", ephemeral=True
            )
            return
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if match.get("status") != "pending_verification":
            await interaction.response.send_message("This result is no longer awaiting host approval.", ephemeral=True)
            return
        if not player or match.get("room_code_shared_by") != player["id"]:
            await interaction.response.send_message("Only the Match Host can approve this result.", ephemeral=True)
            return
        # thinking=True, no ephemeral — Discord locks the ephemeral state
        # at defer time, not per-followup.send() call. The earlier fix
        # (below) only removed ephemeral=True from the success message
        # itself, but this defer was still forcing every followup on this
        # interaction private regardless — confirmed live 2026-08-08, the
        # "public" message still showed "Only you can see this". Matches
        # the same non-ephemeral defer already used correctly in
        # admin.py's force_approve for the identical public-confirmation
        # case. The failure path two lines below still explicitly passes
        # ephemeral=True on its own send() call, so it's unaffected by
        # this change and stays private either way.
        await interaction.response.defer(thinking=True)
        success, message = await self._do_approve(interaction.guild, match_id, player["id"])
        if not success:
            await interaction.followup.send(message, ephemeral=True)
            return
        # Public confirmation, same channel/audience as the auto-approve
        # sweep's message just below (admins/mods in RESULT_APPROVAL_
        # CHANNEL_ID) — deliberately reversed from the 2026-07-20 private
        # version. That change made sense for what it removed (a long
        # per-round result card); it shouldn't have also made the short
        # confirmation itself invisible to the moderators sharing this
        # channel, who have no other signal that a result just cleared.
        await interaction.followup.send(
            f"✅ Match **{match['match_id']}** approved by host — leaderboard is up to date.",
        )

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

            # Unified 2026-07-29: was per-region lookup — one approval
            # channel for all 4 queues now.
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
    bot.add_dynamic_items(HostApprovalButton)
    cog.approval_sweep.start()