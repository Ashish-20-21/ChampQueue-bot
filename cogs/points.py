"""Season Points — shield channel, points leaderboard, shield purchase.

This cog owns:
  - The persistent Shield Channel panel (buy-with-points self-serve button)
  - The Points Leaderboard channel (persistent message + reload button,
    per-user 60s cooldown same as the region leaderboard)
  - The HOD approval card for cash-path shield grants (Confirm/Reject)
  - Season-end lock/announcement check, called from match.py after approval

It does NOT own:
  - /admin-grant-shield (lives in admin.py)
  - /admin-recompute-points (lives in admin.py)
  - The approve_match() points hook (that's SQL-level, inside the RPC)
  - Per-match point-change display — that's now the "SP (proposed)" line
    on the pre-approval verification card (utils/embeds.py), not a
    separate post here. There used to be a standalone post-approval
    summary; removed since two point-related messages in one channel
    (verification card + a second points card) was one too many.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands, ui
from discord.ext import commands

import config
from database.db import adb, with_retry
from utils import embeds as embed_utils
from utils.permissions import is_admin
from utils import incident_log

logger = logging.getLogger("champions_queue")


# ======================================================================
# EMBEDS
# ======================================================================

def _shield_info_embed() -> discord.Embed:
    """The informational embed shown in the shield channel."""
    em = discord.Embed(
        title="⚡ Shield Station",
        description=(
            "Think you're climbing fast? Protect your grind.\n\n"
            "A **Shield** blocks the **-3 SP penalty** on losses for a full **7 days**. "
            "Wins still give you the normal **+5 SP**. Your MMR stays completely untouched — "
            "this is a Season Points play only.\n\n"
            "**One active shield at a time. Once purchased, it's final — no refunds, no reversals.**"
        ),
        color=0x5865F2,
    )
    em.set_footer(text="Shield activates the moment it's confirmed • 7 days, no exceptions")
    return em


def _points_leaderboard_embed(rows: list[dict], season_name: str, is_locked: bool) -> discord.Embed:
    """Build the points leaderboard embed from season_points_leaderboard() rows."""
    if not rows:
        em = discord.Embed(
            title=f"🏆 Season Points — {season_name}",
            description="No points recorded yet. Play matches to earn points!",
            color=0xF1C40F,
        )
        return em

    # Medals for top 3
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    lines = []
    for row in rows[:config.POINTS_LEADERBOARD_PAGE_SIZE]:
        rank = row["rank"]
        medal = medals.get(rank, f"`{rank:>2}.`")
        ign = row["ign"]
        pts = row["points"]
        discord_id = row["discord_id"]

        prize_tag = ""
        if is_locked and row.get("payout_rupees"):
            prize_tag = f" — **₹{row['payout_rupees']}**"

        lines.append(f"{medal} **{ign}** (<@{discord_id}>) — **{pts}** pts{prize_tag}")

    status = "🔒 **SEASON LOCKED** — prize positions final" if is_locked else "🟢 Season active"

    em = discord.Embed(
        title=f"🏆 Season Points — {season_name}",
        description=f"{status}\n\n" + "\n".join(lines),
        color=0xE74C3C if is_locked else 0xF1C40F,
    )
    em.set_footer(text=f"Top {min(len(rows), config.POINTS_LEADERBOARD_PAGE_SIZE)} · Win = +5 · Loss = -3")
    return em


def _shield_hod_approval_embed(shield: dict, player_ign: str, player_discord_id: str,
                                admin_discord_id: str) -> discord.Embed:
    """The HOD confirmation card for a cash-path shield grant."""
    rupees = shield.get("cost_rupees") or "?"
    em = discord.Embed(
        title="🛡️ Shield Grant — Awaiting HOD Confirmation",
        description=(
            f"**Player:** {player_ign} (<@{player_discord_id}>)\n"
            f"**Payment:** ₹{rupees} (cash)\n"
            f"**Initiated by:** <@{admin_discord_id}>\n"
            f"**Shield ID:** `{shield['id']}`\n\n"
            "Confirm that payment has been verified. The shield activates "
            f"the moment you click **Confirm** — the {config.SHIELD_DURATION_HOURS}-hour "
            f"({config.SHIELD_DURATION_HOURS // 24}-day) window starts then."
        ),
        color=0xFFA500,
    )
    em.set_footer(text="Two-person approval required · Self-confirmation blocked")
    return em


def _season_end_embed(winner_ign: str, winner_discord_id: str, winner_points: int,
                       second: dict | None, third: dict | None,
                       season_name: str) -> discord.Embed:
    """The announcement embed when someone crosses 3500."""
    desc = (
        f"🏆 **{winner_ign}** (<@{winner_discord_id}>) has crossed "
        f"**{config.SEASON_END_THRESHOLD}** SP with **{winner_points}** points "
        f"and is declared the **Conqueror of {season_name}**!\n\n"
        f"🥇 **1st Place:** {winner_ign} — **₹{config.PRIZE_1ST}**\n"
    )
    if second:
        desc += f"🥈 **2nd Place:** {second['ign']} — **₹{second.get('payout_rupees', 0)}** ({second['points']} SP)\n"
    if third:
        desc += f"🥉 **3rd Place:** {third['ign']} — **₹{third.get('payout_rupees', 0)}** ({third['points']} SP)\n"

    desc += "\n**All Season Points are now frozen.** MMR and matchmaking continue as normal."

    em = discord.Embed(
        title=f"🏆 Season Ended — {season_name}",
        description=desc,
        color=0xFFD700,
    )
    return em


# ======================================================================
# PERSISTENT VIEWS
# ======================================================================

class ShieldPurchaseButton(discord.ui.DynamicItem[discord.ui.Button],
                           template=r"shield:buy_points"):
    """Main shield button — opens the 3-option menu (Use Credits / Boost / Cancel).
    Persistent across restarts via DynamicItem."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(
            label="Shield",
            style=discord.ButtonStyle.primary,
            emoji="🛡️",
            custom_id="shield:buy_points",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                              match: "re.Match[str]") -> "ShieldPurchaseButton":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        season = await adb.get_active_season()
        if not season:
            await interaction.followup.send("No active season right now.", ephemeral=True)
            return

        season_id = season["id"]
        player = await adb.get_player_by_discord_id(str(interaction.user.id))
        if not player:
            await interaction.followup.send("You need to be registered first.", ephemeral=True)
            return

        existing = await adb.get_active_shield(player["id"], season_id)
        if existing:
            ends = existing.get("shield_ends_at", "")
            await interaction.followup.send(
                f"You already have an active shield (expires <t:{_iso_to_ts(ends)}:R>). "
                "One shield at a time.",
                ephemeral=True,
            )
            return

        locked = await adb.is_season_points_locked(season_id)
        if locked:
            await interaction.followup.send("Season points are locked — shields are no longer available.", ephemeral=True)
            return

        sp = await adb.get_season_points(player["id"], season_id)
        current_points = sp["points"] if sp else 0

        menu_view = ShieldMenuView(player["id"], season_id, current_points)
        msg = await interaction.followup.send(
            "**⚡ How do you want to get your shield?**\n\n"
            f"🎯 **Use Credits** — costs **{config.SHIELD_COST_POINTS} SP** from your balance "
            f"(you have **{current_points}** SP)\n"
            "💰 **Boost** — pay with cash (₹100 or ₹200) for a paid shield\n\n"
            f"Both give you **{config.SHIELD_DURATION_HOURS // 24} days** of loss protection.",
            view=menu_view,
            ephemeral=True,
            wait=True,
        )
        menu_view.message = msg


def _role_mentions(*role_id_sets: set[int]) -> str:
    """Build a mention string across multiple role-ID sets, deduped.
    HOD_ROLE_IDS and ADMIN_ROLE_IDS can legitimately overlap by design
    (an HOD role is often also given admin power so they can act when
    no separate admin is around) — without dedup, an overlapping role
    gets @mentioned twice in the same message."""
    seen: set[int] = set()
    for role_ids in role_id_sets:
        seen |= role_ids
    return " ".join(f"<@&{rid}>" for rid in seen)


def _freeze_view(view: discord.ui.View, picked_label: str | None = None) -> None:
    """Disable every button on a completed/expired step's view so it
    visibly shows it's no longer actionable, instead of sitting there
    looking clickable while silently dead underneath (the "ChampQueue
    didn't respond in time" symptom a stale-but-visible button gives).
    If picked_label is given, that specific button's label gets a
    checkmark prefix so the message also shows which choice was made.
    Mutates in place — caller still needs to push the updated view via
    edit_message()/edit_original_response()/message.edit()."""
    for child in view.children:
        if isinstance(child, discord.ui.Button):
            child.disabled = True
            if picked_label is not None and child.label == picked_label:
                child.label = f"✓ {child.label}"


class ShieldMenuView(discord.ui.View):
    """The 3-option menu: Use Credits / Boost / Cancel."""

    def __init__(self, player_id: int, season_id: int, current_points: int) -> None:
        super().__init__(timeout=300)  # 5 minutes
        self.player_id = player_id
        self.season_id = season_id
        self.current_points = current_points
        self.message: discord.WebhookMessage | discord.Message | None = None

    async def on_timeout(self) -> None:
        # Natural 5-minute expiry — disable in place instead of leaving
        # dead-but-visible buttons on an ephemeral message nobody will
        # ever see updated otherwise.
        _freeze_view(self)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Use Credits", style=discord.ButtonStyle.success, emoji="🎯")
    async def use_credits(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Use Credits")
        await interaction.response.edit_message(view=self)

        if self.current_points < config.SHIELD_COST_POINTS:
            await interaction.followup.send(
                f"You have **{self.current_points} SP** but need **{config.SHIELD_COST_POINTS}**.\n\n"
                "You can get a shield through a **Boost** instead — click the shield button again "
                "and choose **Boost** to see the cash options.",
                ephemeral=True,
            )
            return

        confirm_view = ShieldCreditsConfirmView(self.player_id, self.season_id, self.current_points)
        msg = await interaction.followup.send(
            "**⚠️ Confirm credit purchase — this is irreversible**\n\n"
            f"This will deduct **{config.SHIELD_COST_POINTS} SP** from your balance "
            f"(**{self.current_points}** → **{self.current_points - config.SHIELD_COST_POINTS}** SP).\n"
            f"Your shield will be active for **{config.SHIELD_DURATION_HOURS // 24} days** starting now.\n\n"
            "Once purchased, this cannot be undone, refunded, or reversed. "
            "No exceptions will be made for credit-based purchases.",
            view=confirm_view,
            ephemeral=True,
            wait=True,
        )
        confirm_view.message = msg

    @discord.ui.button(label="Boost", style=discord.ButtonStyle.primary, emoji="💰")
    async def boost(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Boost")
        await interaction.response.edit_message(view=self)

        tier_view = ShieldBoostTierView(self.player_id, self.season_id)
        msg = await interaction.followup.send(
            "**⚡ Boost Your Season Points**\n\n"
            "A Boost is a paid Shield — **7 days** of protection where your losses cost you nothing.\n\n"
            f"**₹{config.SHIELD_BOOST_100_RUPEES} Boost** — {config.SHIELD_BOOST_100_POINTS} SP value\n"
            f"**₹{config.SHIELD_BOOST_200_RUPEES} Boost** — {config.SHIELD_BOOST_200_POINTS} SP value\n\n"
            "Payment is handled by an admin after you raise a ticket — "
            "nothing is charged automatically. You'll confirm exactly what you're agreeing to on the next screen.",
            view=tier_view,
            ephemeral=True,
            wait=True,
        )
        tier_view.message = msg

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_menu(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Cancel")
        await interaction.response.edit_message(
            content="No worries — shield menu closed.", view=self
        )


class ShieldCreditsConfirmView(discord.ui.View):
    """Irreversible confirmation before deducting points."""

    def __init__(self, player_id: int, season_id: int, current_points: int) -> None:
        super().__init__(timeout=300)
        self.player_id = player_id
        self.season_id = season_id
        self.current_points = current_points
        self.message: discord.WebhookMessage | discord.Message | None = None

    async def on_timeout(self) -> None:
        _freeze_view(self)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Confirm Purchase", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Confirm Purchase")
        await interaction.response.edit_message(view=self)

        try:
            shield = await with_retry(
                adb.create_shield_points_path,
                self.player_id, self.season_id, config.SHIELD_COST_POINTS
            )
        except ValueError:
            await interaction.followup.send(
                "Purchase failed — your points balance may have changed. Please try again.",
                ephemeral=True,
            )
            return
        except Exception:
            logger.exception("Shield purchase failed for player_id=%s", self.player_id)
            await interaction.followup.send(
                "Something went wrong processing your shield. Please try again or contact an admin.",
                ephemeral=True,
            )
            return

        ends_ts = _iso_to_ts(shield.get("shield_ends_at", ""))
        # Ephemeral confirmation to the buyer only
        await interaction.followup.send(
            f"🛡️ **Shield activated!** You're protected until <t:{ends_ts}:F> (<t:{ends_ts}:R>).\n"
            f"Points deducted: **-{config.SHIELD_COST_POINTS} SP** "
            f"(new balance: **{self.current_points - config.SHIELD_COST_POINTS}** SP).",
            ephemeral=True,
        )

        # Post to HOD approval channel (team visibility)
        await _post_to_hod_channel(
            interaction.client, self.season_id,
            f"🛡️ **Shield purchased** by <@{interaction.user.id}> "
            f"(credits path, -{config.SHIELD_COST_POINTS} SP) — "
            f"active until <t:{ends_ts}:F>"
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Cancel")
        await interaction.response.edit_message(
            content="Shield purchase cancelled.", view=self
        )


class ShieldBoostTierView(discord.ui.View):
    """₹100 / ₹200 tier selection — leads to the consent screen."""

    def __init__(self, player_id: int, season_id: int) -> None:
        super().__init__(timeout=300)
        self.player_id = player_id
        self.season_id = season_id
        self.message: discord.WebhookMessage | discord.Message | None = None

    async def on_timeout(self) -> None:
        _freeze_view(self)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="₹100 Boost", style=discord.ButtonStyle.primary)
    async def tier_100(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="₹100 Boost")
        await interaction.response.edit_message(view=self)
        await self._show_consent(interaction, tier=100)

    @discord.ui.button(label="₹200 Boost", style=discord.ButtonStyle.primary)
    async def tier_200(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="₹200 Boost")
        await interaction.response.edit_message(view=self)
        await self._show_consent(interaction, tier=200)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Cancel")
        await interaction.response.edit_message(
            content="Boost selection cancelled.", view=self
        )

    async def _show_consent(self, interaction: discord.Interaction, tier: int) -> None:
        rupees = config.SHIELD_BOOST_200_RUPEES if tier == 200 else config.SHIELD_BOOST_100_RUPEES
        sp_value = config.SHIELD_BOOST_200_POINTS if tier == 200 else config.SHIELD_BOOST_100_POINTS

        # Tier-specific SP mechanics line — the ₹200 tier's day-1
        # boost is a genuinely different mechanic from the ₹100 tier's
        # flat rate, so this isn't just a number swap, the wording
        # itself needs to differ. Both tiers explicitly state "no
        # negative SP" since that's the actual value being sold here
        # and shouldn't be left implicit on a consent screen.
        if tier == 200:
            mechanics_line = (
                "• **Day 1:** every win gives **+50 SP** (10x boost) — losses cost **0 SP**.\n"
                "• **Days 2–7:** every win gives the normal **+5 SP** — losses still cost **0 SP**.\n"
                "• **No negative SP at any point during the 7 days.**\n"
            )
        else:
            mechanics_line = (
                "• Every win gives the normal **+5 SP**, every day, for all 7 days.\n"
                "• **Losses cost 0 SP the entire time — no negative SP at any point.**\n"
            )

        consent_view = ShieldConsentView(self.player_id, self.season_id, tier, rupees, sp_value)
        msg = await interaction.followup.send(
            f"**Confirm before you proceed — ₹{rupees} Boost**\n\n"
            "Here's exactly what happens:\n"
            f"• You're requesting a **7-day ({config.SHIELD_DURATION_HOURS} hour) shield** for **₹{rupees}**.\n"
            f"{mechanics_line}"
            f"• Payment is made directly to an admin via <#{config.SUPPORT_CHANNEL_ID}> — "
            "ChampQueue never handles your money.\n"
            "• Once an HOD confirms your payment, the shield activates immediately "
            "and **cannot be cancelled, refunded, or reversed**.\n"
            "• If you don't actually complete payment after this, no shield will be granted — "
            "this step only records that you understood the terms.\n\n"
            "By clicking **I Agree**, you confirm you understand the above "
            f"and intend to proceed to <#{config.SUPPORT_CHANNEL_ID}> to arrange payment.",
            view=consent_view,
            ephemeral=True,
            wait=True,
        )
        consent_view.message = msg


class ShieldConsentView(discord.ui.View):
    """Final consent gate — writes to DB and notifies HOD channel."""

    def __init__(self, player_id: int, season_id: int, tier: int,
                 rupees: int, sp_value: int) -> None:
        super().__init__(timeout=300)
        self.player_id = player_id
        self.season_id = season_id
        self.tier = tier
        self.rupees = rupees
        self.sp_value = sp_value
        self.message: discord.WebhookMessage | discord.Message | None = None

    async def on_timeout(self) -> None:
        _freeze_view(self)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="I Agree — Go to #support", style=discord.ButtonStyle.danger, emoji="✅")
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="I Agree — Go to #support")
        await interaction.response.edit_message(view=self)

        tier_label = f"boost_{self.tier}"
        try:
            consent = await with_retry(
                adb.create_shield_consent,
                self.player_id, self.season_id, tier_label, self.rupees
            )
        except Exception:
            logger.exception("Shield consent record failed for player_id=%s", self.player_id)
            await interaction.followup.send(
                "Something went wrong recording your consent. Please try again or contact an admin.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ **Consent recorded** (Shield ID: `{consent['id']}`).\n\n"
            f"Now head to <#{config.SUPPORT_CHANNEL_ID}> to raise a ticket and arrange your "
            f"**₹{self.rupees}** payment. Once an admin verifies it, your shield will be activated by an HOD.\n\n"
            "If you have any questions, ask in the ticket.",
            ephemeral=True,
        )

        # Post to HOD approval channel — tagging both HOD and admin roles
        role_mentions = _role_mentions(config.HOD_ROLE_IDS, config.ADMIN_ROLE_IDS)

        await _post_to_hod_channel(
            interaction.client, self.season_id,
            f"🛡️ **New Boost consent** — <@{interaction.user.id}> "
            f"has agreed to the **₹{self.rupees} Boost** ({self.sp_value} SP, 7-day shield).\n"
            f"Consent ID: `{consent['id']}` · Awaiting payment via <#{config.SUPPORT_CHANNEL_ID}>.\n\n"
            f"{role_mentions}"
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Cancel")
        await interaction.response.edit_message(
            content="Boost consent cancelled — nothing was recorded.", view=self
        )


class PointsLeaderboardReloadButton(discord.ui.DynamicItem[discord.ui.Button],
                                     template=r"points_lb:reload"):
    """Persistent reload button for the points leaderboard. No
    per-instance parameters, same reconstruction pattern as
    ShieldPurchaseButton above.

    Rate-limited per-user via the same CooldownMapping primitive
    LeaderboardView (cogs/stats.py) already uses for the region
    leaderboard — same reasoning: Discord/discord.py already has a
    correct rate limiter, no need to hand-roll a DB-tracked one."""

    _cooldown = commands.CooldownMapping.from_cooldown(
        1, config.POINTS_LEADERBOARD_COOLDOWN_SECONDS, commands.BucketType.user
    )

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(
            label="Reload",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            custom_id="points_lb:reload",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                              match: "re.Match[str]") -> "PointsLeaderboardReloadButton":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        # commands.CooldownMapping expects something message-shaped
        # (reads .author.id for BucketType.user) — a raw Interaction
        # has .user, not .author. Same shim LeaderboardView.reload_callback
        # already uses in cogs/stats.py.
        class _Ctx:
            author = interaction.user
        bucket = PointsLeaderboardReloadButton._cooldown.get_bucket(_Ctx())
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await interaction.response.send_message(
                f"Leaderboard was just reloaded — try again in {retry_after:.0f}s.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Lazy status sweep: point_shields.status only ever gets set
        # once (on creation/confirmation) and nothing else updates it
        # as time passes — has_active_shield()/get_active_shield() are
        # correct regardless since they check shield_ends_at directly,
        # but the status column itself can sit on 'active' long after
        # a shield has actually expired, which reads wrong for anyone
        # eyeballing point_shields directly (exactly the audit-trail
        # use case that table exists for). Rather than a background
        # sweep task, ride this already-rate-limited button click —
        # cheap, no new infrastructure, keeps status honest whenever
        # anyone actually looks at the leaderboard. Best-effort: never
        # blocks the render if it fails.
        try:
            await with_retry(adb.expire_shields)
        except Exception:
            logger.exception("expire_shields() sweep failed during leaderboard reload (non-fatal)")

        season = await adb.get_active_season()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return

        rows = await with_retry(adb.season_points_leaderboard, season["id"])
        locked = await adb.is_season_points_locked(season["id"])
        em = _points_leaderboard_embed(rows, season.get("name", "Season"), locked)

        view = discord.ui.View(timeout=None)
        view.add_item(PointsLeaderboardReloadButton())

        try:
            await interaction.message.edit(embed=em, view=view)
        except discord.HTTPException:
            pass


class HODApprovalView(discord.ui.View):
    """Confirm/Reject buttons sent to the HOD approval channel for
    cash-path shield grants. NOT persistent — lives only as long as
    the pending request does. If the bot restarts before HOD acts,
    the admin re-runs /admin-grant-shield (same as re-running any
    other interrupted command)."""

    def __init__(self, shield_id: int, initiated_by: str) -> None:
        super().__init__(timeout=None)  # no timeout — waits until HOD acts
        self.shield_id = shield_id
        self.initiated_by = initiated_by

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # HOD role check
        if not _has_hod_role(interaction.user):
            await interaction.response.send_message("Only HOD members can confirm shield grants.", ephemeral=True)
            return

        # Two-person check
        if str(interaction.user.id) == self.initiated_by:
            await interaction.response.send_message(
                "You initiated this grant — a **different** HOD member must confirm it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        shield = await adb.get_shield_by_id(self.shield_id)
        if not shield or shield["status"] != "pending_hod_confirmation":
            await interaction.followup.send("This shield request is no longer pending.", ephemeral=True)
            return

        confirmed = await with_retry(adb.confirm_shield, self.shield_id, str(interaction.user.id))
        if not confirmed:
            await interaction.followup.send("Failed to confirm — the request may have been handled already.", ephemeral=True)
            return

        # Notify the player
        player = await adb.get_player_by_id(shield["player_id"])
        ends_ts = _iso_to_ts(confirmed.get("shield_ends_at", ""))

        # Update the HOD card
        em = discord.Embed(
            title="🛡️ Shield Grant — CONFIRMED",
            description=(
                f"**Player:** {player['ign']} (<@{player['discord_id']}>)\n"
                f"**Confirmed by:** <@{interaction.user.id}>\n"
                f"**Active until:** <t:{ends_ts}:F>"
            ),
            color=0x2ECC71,
        )
        await interaction.message.edit(embed=em, view=None)

        # Post audit log to shield channel
        await _post_to_hod_channel(
            interaction.client, shield["season_id"],
            f"🛡️ **Shield granted** to <@{player['discord_id']}> "
            f"(cash path, ₹{shield.get('cost_rupees') or '?'}) — "
            f"initiated by <@{self.initiated_by}>, "
            f"confirmed by <@{interaction.user.id}> — "
            f"active until <t:{ends_ts}:F>"
        )

        # DM-in-channel to the player (in shield channel, not actual DM)
        if config.SHIELD_CHANNEL_ID:
            ch = interaction.client.get_channel(config.SHIELD_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(
                        f"🛡️ <@{player['discord_id']}> — your shield is now **active**! "
                        f"Protected until <t:{ends_ts}:F> (<t:{ends_ts}:R>)."
                    )
                except discord.HTTPException:
                    pass

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _has_hod_role(interaction.user):
            await interaction.response.send_message("Only HOD members can reject shield grants.", ephemeral=True)
            return

        if str(interaction.user.id) == self.initiated_by:
            await interaction.response.send_message(
                "You initiated this grant — a **different** HOD member must reject it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        shield = await adb.get_shield_by_id(self.shield_id)
        if not shield or shield["status"] != "pending_hod_confirmation":
            await interaction.followup.send("This shield request is no longer pending.", ephemeral=True)
            return

        await with_retry(adb.reject_shield, self.shield_id, str(interaction.user.id))

        player = await adb.get_player_by_id(shield["player_id"])
        em = discord.Embed(
            title="🛡️ Shield Grant — REJECTED",
            description=(
                f"**Player:** {player['ign']} (<@{player['discord_id']}>)\n"
                f"**Rejected by:** <@{interaction.user.id}>\n"
                f"**Reason:** Payment not verified or request denied."
            ),
            color=0xE74C3C,
        )
        await interaction.message.edit(embed=em, view=None)

        # Audit
        await _post_to_hod_channel(
            interaction.client, shield["season_id"],
            f"❌ **Shield request rejected** for <@{player['discord_id']}> "
            f"(cash path) — rejected by <@{interaction.user.id}>"
        )


# ======================================================================
# COG
# ======================================================================

class PointsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Register persistent dynamic items
        self.bot.add_dynamic_items(ShieldPurchaseButton, PointsLeaderboardReloadButton)

    # ── Admin: post the shield panel ─────────────────────────
    @app_commands.command(name="shield-post", description="Post the shield purchase panel in this channel")
    @app_commands.check(lambda i: is_admin(i))
    async def shield_post(self, interaction: discord.Interaction) -> None:
        view = discord.ui.View(timeout=None)
        view.add_item(ShieldPurchaseButton())
        await interaction.channel.send(embed=_shield_info_embed(), view=view)
        await interaction.response.send_message("Shield panel posted.", ephemeral=True)

    # ── Admin: post the points leaderboard ───────────────────
    @app_commands.command(name="points-leaderboard-post", description="Post the points leaderboard in this channel")
    @app_commands.check(lambda i: is_admin(i))
    async def points_leaderboard_post(self, interaction: discord.Interaction) -> None:
        season = await adb.get_active_season()
        if not season:
            await interaction.response.send_message("No active season.", ephemeral=True)
            return

        rows = await with_retry(adb.season_points_leaderboard, season["id"])
        locked = await adb.is_season_points_locked(season["id"])
        em = _points_leaderboard_embed(rows, season.get("name", "Season"), locked)

        view = discord.ui.View(timeout=None)
        view.add_item(PointsLeaderboardReloadButton())

        await interaction.channel.send(embed=em, view=view)
        await interaction.response.send_message("Points leaderboard posted.", ephemeral=True)


# ======================================================================
# MODULE-LEVEL HELPERS (used by other cogs too)
# ======================================================================

async def check_and_announce_season_end(
    bot: commands.Bot,
    season_id: int,
) -> None:
    """Called after points update — checks if the season just locked
    and announces the results."""
    locked = await adb.is_season_points_locked(season_id)
    if not locked:
        return

    season = await adb.get_active_season()
    if not season or season["id"] != season_id:
        return

    rows = await with_retry(adb.season_points_leaderboard, season_id)
    if not rows:
        return

    # Find the top 3
    winner = next((r for r in rows if r.get("locked_rank") == 1), None)
    second = next((r for r in rows if r.get("locked_rank") == 2), None)
    third = next((r for r in rows if r.get("locked_rank") == 3), None)

    if not winner:
        return

    season_name = season.get("name", "Season")
    em = _season_end_embed(
        winner["ign"], winner["discord_id"], winner["points"],
        second, third, season_name,
    )

    # Post to points leaderboard channel
    if config.POINTS_LEADERBOARD_CHANNEL_ID:
        ch = bot.get_channel(config.POINTS_LEADERBOARD_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=em)
            except discord.HTTPException:
                pass

    # Also post to match log channel (wider visibility)
    if config.MATCH_LOG_CHANNEL_ID:
        ch = bot.get_channel(config.MATCH_LOG_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=em)
            except discord.HTTPException:
                pass

    # Post to shield channel — public "Conqueror" announcement
    if config.SHIELD_CHANNEL_ID:
        ch = bot.get_channel(config.SHIELD_CHANNEL_ID)
        if ch:
            try:
                await ch.send(
                    f"🏆 **<@{winner['discord_id']}> has hit {config.SEASON_END_THRESHOLD} SP "
                    f"and is declared the Conqueror of {season_name}!**"
                )
            except discord.HTTPException:
                pass

    # Post to HOD approval channel — tagging HOD role for awareness
    role_mentions = _role_mentions(config.HOD_ROLE_IDS, config.ADMIN_ROLE_IDS)
    await _post_to_hod_channel(
        bot, season_id,
        f"🏆 **SEASON ENDED** — <@{winner['discord_id']}> crossed {config.SEASON_END_THRESHOLD} SP.\n"
        f"Points table is now **frozen**. Prize payouts pending review.\n\n"
        f"{role_mentions}"
    )


async def post_hod_approval_card(
    bot: commands.Bot,
    shield: dict,
    player: dict,
    admin_discord_id: str,
) -> bool:
    """Posts the HOD confirmation card to the HOD approval channel.
    Returns True if posted successfully."""
    if not config.HOD_APPROVAL_CHANNEL_ID:
        logger.warning("HOD_APPROVAL_CHANNEL_ID not set — cannot post shield approval card")
        return False

    ch = bot.get_channel(config.HOD_APPROVAL_CHANNEL_ID)
    if not ch:
        logger.warning("HOD approval channel %s not found", config.HOD_APPROVAL_CHANNEL_ID)
        return False

    em = _shield_hod_approval_embed(shield, player["ign"], player["discord_id"], admin_discord_id)
    view = HODApprovalView(shield["id"], admin_discord_id)

    try:
        await ch.send(embed=em, view=view)
        return True
    except discord.HTTPException:
        logger.exception("Failed to post HOD approval card for shield %s", shield["id"])
        return False


# ======================================================================
# INTERNAL HELPERS
# ======================================================================

def _has_hod_role(member: discord.Member | discord.User) -> bool:
    """Check if a member has any of the configured HOD roles."""
    if not config.HOD_ROLE_IDS:
        return False
    if not isinstance(member, discord.Member):
        return False
    return any(role.id in config.HOD_ROLE_IDS for role in member.roles)


def _iso_to_ts(iso_str: str) -> int:
    """Convert an ISO datetime string (as returned by Supabase/Postgres)
    to a Unix timestamp for Discord's <t:...> formatting.

    Falls back to 0 (renders as 1 Jan 1970 in Discord) only if truly
    unparseable — but logs when that happens, since a silent fallback
    here means a wrong date gets shown to real people without anyone
    knowing why.

    Caught live 2026-09-07: a shield-granted message showed 1970
    instead of the real 7-day expiry. Root cause confirmed: Postgres
    returned "2026-09-07 14:42:32.4923+00" — space-separated (not
    'T'), and critically a UTC offset with NO COLON ("+00" not
    "+00:00"). Python's datetime.fromisoformat() on 3.10 (this
    project's runtime) rejects colonless offsets outright — that
    became fully ISO 8601 compliant only in 3.11. The original
    version of this function had no normalization for that case and
    silently swallowed the ValueError."""
    if not iso_str:
        logger.warning("_iso_to_ts called with empty/None iso_str — will render as 1970 epoch")
        return 0
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        cleaned = cleaned.replace(" ", "T", 1)  # Postgres space-separator -> ISO 'T'
        # Normalize a colonless UTC offset ("+00" or "-05") to
        # "+00:00"/"-05:00" — Python 3.10's fromisoformat rejects the
        # colonless form even though 3.11+ accepts it.
        import re as _re
        cleaned = _re.sub(r'([+-]\d{2})$', r'\1:00', cleaned)
        dt = datetime.fromisoformat(cleaned)
        return int(dt.timestamp())
    except (ValueError, AttributeError, TypeError) as exc:
        logger.warning("_iso_to_ts failed to parse %r — falling back to 1970 epoch: %s", iso_str, exc)
        return 0


async def _post_to_hod_channel(bot: commands.Bot, season_id: int, message: str) -> None:
    """Post an audit/notification line to the HOD approval channel."""
    if not config.HOD_APPROVAL_CHANNEL_ID:
        return
    ch = bot.get_channel(config.HOD_APPROVAL_CHANNEL_ID)
    if not ch:
        return
    try:
        await ch.send(message)
    except discord.HTTPException:
        pass


# ======================================================================
# SETUP
# ======================================================================

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PointsCog(bot))