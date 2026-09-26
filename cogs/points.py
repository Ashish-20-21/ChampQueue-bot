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
    t = config.SHIELD_TIERS
    em = discord.Embed(
        title="⚡ Shield Station",
        description=(
            "Every match is a swing — one bad round and the grind you put in resets. "
            "A **Shield** ends that risk: **+10 SP on every win, 0 SP lost on every loss** — "
            "while everyone else is still stuck on the standard +5/-3. Your MMR stays completely "
            "untouched, this is a Season Points play only.\n\n"
            f"🛡️ **{t['normal']['label']}** — {t['normal']['duration_hours'] // 24} days · "
            f"+{t['normal']['win_sp']} SP/win · 0 SP on loss\n"
            f"🛡️🛡️ **{t['2x_normal']['label']}** — {t['2x_normal']['duration_hours'] // 24} days · "
            f"same +{t['2x_normal']['win_sp']} SP/win · 0 on loss as Normal, just twice the runway, bundle price\n"
            f"🛡️✨ **{t['premium']['label']}** — {t['premium']['duration_hours'] // 24} days · "
            f"+{t['premium']['day1_win_sp']} SP/win on day 1, then +{t['premium']['win_sp']} SP/win "
            f"for the remaining {t['premium']['duration_hours'] // 24 - 1} days\n\n"
            "**One active shield at a time. Once purchased, it's final — no refunds, no reversals.**\n"
            "Tap **Info** below for the full breakdown before you buy."
        ),
        color=0x5865F2,
    )
    em.set_footer(text="Shield activates the moment it's confirmed • protection length depends on tier")
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
        # Rank prefix removed for non-medal rows (2026-09): only 🥇🥈🥉
        # mark position now, ranks 4+ just list in order with no
        # `NN.` prefix — saves ~4-5 chars/row (backticks + padding +
        # digits + period) across up to 50 rows in the embed
        # description.
        #
        # <@discord_id> mention also dropped (2026-09): IGN alone is
        # the clean/readable label — the mention added @username noise
        # without adding info most viewers needed. discord_id is still
        # pulled from the row (kept in the loop, just unused in the
        # line below) in case a future need reintroduces it — cheap to
        # keep the variable, no reason to touch the RPC/row shape for
        # a display-only change.
        medal = medals.get(rank, "")
        ign = row["ign"]
        pts = row["points"]
        discord_id = row["discord_id"]

        prize_tag = ""
        if is_locked and row.get("payout_rupees"):
            prize_tag = f" — **₹{row['payout_rupees']}**"

        prefix = f"{medal} " if medal else ""
        lines.append(f"{prefix}**{ign}** — **{pts}** pts{prize_tag}")

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
    tier = shield.get("tier")
    tier_cfg = config.SHIELD_TIERS.get(tier, {})
    tier_label = tier_cfg.get("label", tier or "?")
    duration_hours = tier_cfg.get("duration_hours")
    duration_txt = f"{duration_hours}-hour ({duration_hours // 24}-day)" if duration_hours else "?"
    em = discord.Embed(
        title="🛡️ Shield Grant — Awaiting HOD Confirmation",
        description=(
            f"**Player:** {player_ign} (<@{player_discord_id}>)\n"
            f"**Tier:** {tier_label}\n"
            f"**Payment:** ₹{rupees} (cash)\n"
            f"**Initiated by:** <@{admin_discord_id}>\n"
            f"**Shield ID:** `{shield['id']}`\n\n"
            "Confirm that payment has been verified. The shield activates "
            f"the moment you click **Confirm** — the {duration_txt} window starts then."
        ),
        color=0xFFA500,
    )
    em.set_footer(text="Two-person approval required · Self-confirmation blocked")
    return em


def _pool_unlocked_embed(season_name: str) -> discord.Embed:
    """Fired once, the first time any player crosses
    SEASON_POOL_UNLOCK_THRESHOLD this season — an awareness ping, not
    an end-of-season announcement. The race for 1st/2nd/3rd keeps
    going until the deadline."""
    em = discord.Embed(
        title="🏆 Pool Unlocked!",
        description=(
            f"A player has crossed **{config.SEASON_POOL_UNLOCK_THRESHOLD} SP** in **{season_name}** — "
            f"the full **₹{config.PRIZE_1ST + config.PRIZE_2ND + config.PRIZE_3RD}** prize pool is now "
            "locked in and will be paid out in full.\n\n"
            "**The season isn't over.** Whoever holds 1st/2nd/3rd place at the end still wins those spots — "
            "the race is very much still on."
        ),
        color=0xFFD700,
    )
    return em


def _season_end_embed(rows: list[dict], season_name: str, pool_unlocked: bool) -> discord.Embed:
    """The announcement embed when the season locks at its deadline.

    No single "winner crossed a threshold" framing anymore — the
    season ends on the date, and whoever holds rank 1/2/3 at that
    moment gets paid, using whichever formula pool_unlocked selects."""
    winner = next((r for r in rows if r.get("locked_rank") == 1), None)
    second = next((r for r in rows if r.get("locked_rank") == 2), None)
    third = next((r for r in rows if r.get("locked_rank") == 3), None)

    if pool_unlocked:
        formula_note = "Pool was unlocked this season — payouts are the fixed ₹700/₹500/₹300 by final rank."
    else:
        formula_note = (
            f"Pool was never unlocked (nobody reached {config.SEASON_POOL_UNLOCK_THRESHOLD} SP) — "
            f"payouts are SP÷{config.POINTS_TO_RUPEE} for each of the top 3, uncapped."
        )

    desc = f"**{season_name}** has reached its end date. Final standings are locked.\n\n"
    if winner:
        desc += f"🥇 **1st Place:** {winner['ign']} — **₹{winner.get('payout_rupees', 0)}** ({winner['points']} SP)\n"
    if second:
        desc += f"🥈 **2nd Place:** {second['ign']} — **₹{second.get('payout_rupees', 0)}** ({second['points']} SP)\n"
    if third:
        desc += f"🥉 **3rd Place:** {third['ign']} — **₹{third.get('payout_rupees', 0)}** ({third['points']} SP)\n"

    desc += f"\n{formula_note}\n\n**All Season Points are now frozen.** MMR and matchmaking continue as normal."

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

        normal_cost = config.SHIELD_TIERS["normal"]["credits_cost"]
        normal_r = config.SHIELD_TIERS["normal"]["boost_rupees"]
        twox_r = config.SHIELD_TIERS["2x_normal"]["boost_rupees"]
        premium_r = config.SHIELD_TIERS["premium"]["boost_rupees"]

        menu_view = ShieldMenuView(player["id"], season_id, current_points)
        msg = await interaction.followup.send(
            "**⚡ How do you want to get your shield?**\n\n"
            f"🎯 **Use Credits** — Normal Shield only, costs **{normal_cost} SP** from your balance "
            f"(you have **{current_points}** SP)\n"
            f"💰 **Boost** — pay with cash for any shield tier: "
            f"₹{normal_r} Normal · ₹{twox_r} 2x Normal · ₹{premium_r} Premium\n\n"
            "Protection and perks vary by shield — pick Boost to see the breakdown.",
            view=menu_view,
            ephemeral=True,
            wait=True,
        )
        menu_view.message = msg


def _shield_full_info_embed() -> discord.Embed:
    """The exhaustive reference shown by the Info button — distinct
    from _shield_info_embed() (the inviting channel-post version):
    this one exists to answer "which tiers can I buy with credits,
    which need cash, and exactly what do I get" with nothing left
    implicit, since that's the whole point of a dedicated Info
    button rather than making players re-read the channel post."""
    t = config.SHIELD_TIERS
    normal, twox, premium = t["normal"], t["2x_normal"], t["premium"]
    em = discord.Embed(
        title="🛡️ Shield — Full Breakdown",
        description=(
            "Baseline (no shield): **+5 SP** on a win, **-3 SP** on a loss.\n"
            "Every shield below replaces that with a flat **0 SP on any loss** — "
            "the only differences between tiers are the win bonus and how long it lasts.\n"
        ),
        color=0x5865F2,
    )
    em.add_field(
        name=f"🛡️ {normal['label']}",
        value=(
            f"**{normal['duration_hours'] // 24} days** · **+{normal['win_sp']} SP**/win · 0 on loss\n"
            f"🎯 Use Credits: **{normal['credits_cost']} SP** · 💰 Boost: **₹{normal['boost_rupees']}**"
        ),
        inline=False,
    )
    em.add_field(
        name=f"🛡️🛡️ {twox['label']}",
        value=(
            f"**{twox['duration_hours'] // 24} days** · identical **+{twox['win_sp']} SP**/win · 0 on loss "
            f"as Normal — just twice the days.\n"
            f"💰 Boost only: **₹{twox['boost_rupees']}** (credits can't buy this tier)"
        ),
        inline=False,
    )
    em.add_field(
        name=f"🛡️✨ {premium['label']}",
        value=(
            f"**{premium['duration_hours'] // 24} days** · **Day 1: +{premium['day1_win_sp']} SP**/win, "
            f"then **+{premium['win_sp']} SP**/win for the remaining "
            f"{premium['duration_hours'] // 24 - 1} days · 0 on loss throughout.\n"
            f"💰 Boost only: **₹{premium['boost_rupees']}** (credits can't buy this tier)"
        ),
        inline=False,
    )
    em.add_field(
        name="Rules that apply to every tier",
        value=(
            "• One active shield at a time — you can't stack or queue a second one.\n"
            "• Once purchased, it's final: no refunds, no reversals, no cancellations.\n"
            "• MMR is never affected — this only changes Season Points."
        ),
        inline=False,
    )
    em.set_footer(text="Use Credits = instant, spends your own SP · Boost = cash via an admin, needs HOD confirmation")
    return em


class ShieldInfoButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"shield:info"):
    """Secondary button next to Shield — posts the full tier
    breakdown on demand (_shield_full_info_embed) without spending
    or committing to anything. Persistent across restarts via
    DynamicItem, same pattern as ShieldPurchaseButton."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(
            label="Info",
            style=discord.ButtonStyle.secondary,
            emoji="ℹ️",
            custom_id="shield:info",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                              match: "re.Match[str]") -> "ShieldInfoButton":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=_shield_full_info_embed(), ephemeral=True)


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

        # Use Credits is Normal-Shield-only — 2x_normal and premium
        # have no credits_cost in config.SHIELD_TIERS, Boost-only.
        cost = config.SHIELD_TIERS["normal"]["credits_cost"]

        if self.current_points < cost:
            await interaction.followup.send(
                f"You have **{self.current_points} SP** but need **{cost}**.\n\n"
                "You can still get a Normal Shield through **Boost** instead — click the shield button "
                "again and choose **Boost** for the cash options.",
                ephemeral=True,
            )
            return

        confirm_view = ShieldCreditsConfirmView(self.player_id, self.season_id, self.current_points)
        duration_days = config.SHIELD_TIERS["normal"]["duration_hours"] // 24
        msg = await interaction.followup.send(
            "**⚠️ Confirm credit purchase — this is irreversible**\n\n"
            f"This will deduct **{cost} SP** from your balance "
            f"(**{self.current_points}** → **{self.current_points - cost}** SP) for a **Normal Shield**.\n"
            f"Your shield will be active for **{duration_days} days** starting now — "
            f"**+{config.SHIELD_TIERS['normal']['win_sp']} SP** per win, **0 SP** on any loss.\n\n"
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

        t = config.SHIELD_TIERS
        tier_view = ShieldBoostTierView(self.player_id, self.season_id)
        msg = await interaction.followup.send(
            "**⚡ Boost Your Season Points**\n\n"
            "A Boost is a paid Shield.\n\n"
            f"**₹{t['normal']['boost_rupees']} — Normal Shield:** {t['normal']['duration_hours'] // 24} days. "
            f"+{t['normal']['win_sp']} SP on every win, 0 lost on any loss.\n"
            f"**₹{t['2x_normal']['boost_rupees']} — 2x Normal Shield:** {t['2x_normal']['duration_hours'] // 24} "
            f"days of the exact same +{t['2x_normal']['win_sp']} SP/win · 0-on-loss protection as Normal — "
            "just double the days, at a discount vs. buying two Normal Shields back to back.\n"
            f"**₹{t['premium']['boost_rupees']} — Premium Shield:** Day 1 wins pay "
            f"+{t['premium']['day1_win_sp']} SP. The remaining {t['premium']['duration_hours'] // 24 - 1} days "
            f"continue at +{t['premium']['win_sp']} SP per win, 0 on loss.\n\n"
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

        cost = config.SHIELD_TIERS["normal"]["credits_cost"]
        try:
            shield = await with_retry(
                adb.create_shield_points_path,
                self.player_id, self.season_id, "normal"
            )
        except ValueError as exc:
            # purchase_shield_with_credits raises for: insufficient
            # balance (may have changed since this screen opened),
            # already has an active shield, or season points locked.
            # db.py's wrapper preserves the original message — show it
            # directly rather than a generic failure, it's already
            # written to be player-facing.
            await interaction.followup.send(
                f"Purchase failed: {exc}. Please try again.",
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
        duration_days = config.SHIELD_TIERS["normal"]["duration_hours"] // 24
        # Ephemeral confirmation to the buyer only — keeps their exact
        # new balance private, unlike the public post below.
        await interaction.followup.send(
            f"🛡️ **Normal Shield activated!** You're protected until <t:{ends_ts}:F> (<t:{ends_ts}:R>).\n"
            f"Points deducted: **-{cost} SP** "
            f"(new balance: **{self.current_points - cost}** SP).",
            ephemeral=True,
        )

        # Public announcement in the shield channel — so other players
        # can see who's shielded and with what, same visibility the
        # cash/Boost path already gets via HODApprovalView.confirm()
        # below. The credits path had no equivalent post before this.
        if config.SHIELD_CHANNEL_ID:
            ch = interaction.client.get_channel(config.SHIELD_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(
                        f"🛡️ <@{interaction.user.id}> just activated a **Normal Shield** "
                        f"({duration_days} days, credits) — protected until <t:{ends_ts}:F>."
                    )
                except discord.HTTPException:
                    pass

        # Post to HOD approval channel (team visibility)
        await _post_to_hod_channel(
            interaction.client, self.season_id,
            f"🛡️ **Shield purchased** by <@{interaction.user.id}> "
            f"(credits path, Normal Shield, -{cost} SP) — "
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
    """Normal / 2x Normal / Premium tier selection — leads to the
    consent screen. All three are cash-only (no credits path) —
    consistent with config.SHIELD_TIERS, where only 'normal' has a
    credits_cost."""

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

    @discord.ui.button(label="₹30 Normal", style=discord.ButtonStyle.primary)
    async def tier_normal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="₹30 Normal")
        await interaction.response.edit_message(view=self)
        await self._show_consent(interaction, tier="normal")

    @discord.ui.button(label="₹50 2x Normal", style=discord.ButtonStyle.primary)
    async def tier_2x(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="₹50 2x Normal")
        await interaction.response.edit_message(view=self)
        await self._show_consent(interaction, tier="2x_normal")

    @discord.ui.button(label="₹60 Premium", style=discord.ButtonStyle.primary)
    async def tier_premium(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="₹60 Premium")
        await interaction.response.edit_message(view=self)
        await self._show_consent(interaction, tier="premium")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        _freeze_view(self, picked_label="Cancel")
        await interaction.response.edit_message(
            content="Boost selection cancelled.", view=self
        )

    async def _show_consent(self, interaction: discord.Interaction, tier: str) -> None:
        tier_cfg = config.SHIELD_TIERS[tier]
        rupees = tier_cfg["boost_rupees"]
        duration_days = tier_cfg["duration_hours"] // 24

        # Tier-specific SP mechanics line — Premium's day-1 rate is a
        # genuinely different mechanic from Normal/2x's flat rate, so
        # this isn't just a number swap, the wording itself needs to
        # differ. All three explicitly state "no negative SP" since
        # that's the actual value being sold here and shouldn't be
        # left implicit on a consent screen.
        if tier == "premium":
            mechanics_line = (
                f"• **Day 1:** every win gives **+{tier_cfg['day1_win_sp']} SP** — losses cost **0 SP**.\n"
                f"• **Days 2–{duration_days}** (the remaining {duration_days - 1} days): every win gives "
                f"**+{tier_cfg['win_sp']} SP** — losses still cost **0 SP**.\n"
                f"• **No negative SP at any point during the {duration_days} days.**\n"
            )
        elif tier == "2x_normal":
            mechanics_line = (
                f"• Identical protection to the Normal Shield — **+{tier_cfg['win_sp']} SP** on every win, "
                "**0 SP** on every loss — just running for twice as many days.\n"
                "• **Losses cost 0 SP the entire time — no negative SP at any point.**\n"
            )
        else:
            mechanics_line = (
                f"• Every win gives **+{tier_cfg['win_sp']} SP**, every day, for the full {duration_days} days.\n"
                "• **Losses cost 0 SP the entire time — no negative SP at any point.**\n"
            )

        consent_view = ShieldConsentView(self.player_id, self.season_id, tier, rupees)
        msg = await interaction.followup.send(
            f"**Confirm before you proceed — ₹{rupees} {tier_cfg['label']}**\n\n"
            "Here's exactly what happens:\n"
            f"• You're requesting a **{duration_days}-day {tier_cfg['label']}** for **₹{rupees}**.\n"
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

    def __init__(self, player_id: int, season_id: int, tier: str, rupees: int) -> None:
        super().__init__(timeout=300)
        self.player_id = player_id
        self.season_id = season_id
        self.tier = tier  # 'normal' / '2x_normal' / 'premium' — stored directly, no prefix needed
        self.rupees = rupees
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

        try:
            consent = await with_retry(
                adb.create_shield_consent,
                self.player_id, self.season_id, self.tier, self.rupees
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
        tier_label = config.SHIELD_TIERS[self.tier]["label"]
        duration_days = config.SHIELD_TIERS[self.tier]["duration_hours"] // 24

        await _post_to_hod_channel(
            interaction.client, self.season_id,
            f"🛡️ **New Boost consent** — <@{interaction.user.id}> "
            f"has agreed to the **₹{self.rupees} {tier_label}** ({duration_days}-day shield).\n"
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
    _locked_until: float = 0.0  # monotonic-clock deadline; shared across every render (class-level)

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
        # 2026-09-26: shared 60s lock, same concept as the region
        # leaderboard fix (cogs/stats.py) and RegionQueueView's Join
        # button (cogs/queue.py, the Sep 23 429-storm fix). This button
        # already deferred first (no 10062 bug here — it was built after
        # that lesson was learned), so this is purely a call-reduction
        # layer: reused_button is a fresh discord.ui.View instance each
        # render (see the bottom of this method), so the lock has to live
        # on the CLASS, not self, to survive across renders/instances.
        if PointsLeaderboardReloadButton._locked_until and                 asyncio.get_event_loop().time() < PointsLeaderboardReloadButton._locked_until:
            await interaction.response.defer()  # locked — silent
            return

        # commands.CooldownMapping expects something message-shaped
        # (reads .author.id for BucketType.user) — a raw Interaction
        # has .user, not .author. Same shim LeaderboardView.reload_callback
        # already uses in cogs/stats.py. Kept alongside the new shared
        # lock above: this still stops ONE user re-triggering it solo;
        # the shared lock additionally stops a SECOND, DIFFERENT user
        # from doing the same within that same 60s window.
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

        PointsLeaderboardReloadButton._locked_until = asyncio.get_event_loop().time() + 60
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

        # Lazy deadline-lock + pool-unlock check (migration_036) — the
        # SAME piggyback pattern as expire_shields() above, riding this
        # already-rate-limited click instead of a scheduled job. Also
        # runs from match approval (cogs/match.py) — see
        # run_season_lazy_checks' docstring for why both call sites
        # exist. Best-effort: a failure here never blocks the render.
        try:
            spawn_background(
                run_season_lazy_checks(interaction.client, season["id"]),
                error_label=f"run_season_lazy_checks for season_id={season['id']} (leaderboard reload)",
            )
        except Exception:
            logger.exception("Failed to spawn run_season_lazy_checks during leaderboard reload (non-fatal)")

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

        try:
            confirmed = await with_retry(adb.confirm_shield, self.shield_id, str(interaction.user.id))
        except ValueError as exc:
            # Duplicate-grant guard tripped (see db.py's docstring) —
            # surface it plainly so the HOD knows to Reject this card
            # instead of retrying Confirm.
            await interaction.followup.send(f"❌ Cannot confirm: {exc}", ephemeral=True)
            return
        if not confirmed:
            await interaction.followup.send("Failed to confirm — the request may have been handled already.", ephemeral=True)
            return

        # Notify the player
        player = await adb.get_player_by_id(shield["player_id"])
        ends_ts = _iso_to_ts(confirmed.get("shield_ends_at", ""))
        tier_label = config.SHIELD_TIERS.get(shield.get("tier"), {}).get("label", shield.get("tier", "Shield"))

        # Update the HOD card
        em = discord.Embed(
            title="🛡️ Shield Grant — CONFIRMED",
            description=(
                f"**Player:** {player['ign']} (<@{player['discord_id']}>)\n"
                f"**Tier:** {tier_label}\n"
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
            f"({tier_label}, cash path, ₹{shield.get('cost_rupees') or '?'}) — "
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
                        f"🛡️ <@{player['discord_id']}> — your **{tier_label}** is now **active**! "
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
        self.bot.add_dynamic_items(ShieldPurchaseButton, ShieldInfoButton, PointsLeaderboardReloadButton)

    # ── Admin: post the shield panel ─────────────────────────
    @app_commands.command(name="shield-post", description="Post the shield purchase panel in this channel")
    @app_commands.check(lambda i: is_admin(i))
    async def shield_post(self, interaction: discord.Interaction) -> None:
        view = discord.ui.View(timeout=None)
        view.add_item(ShieldPurchaseButton())
        view.add_item(ShieldInfoButton())
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

_background_tasks: set[asyncio.Task] = set()


def spawn_background(coro, error_label: str) -> None:
    """Fire-and-forget a coroutine WITHOUT the caller waiting for it —
    used specifically for run_season_lazy_checks (below), whose own
    result is genuinely cosmetic: a missed run here is caught
    automatically by the very next match approval or leaderboard
    reload, both of which call this exact same lazy check again. There
    is no correctness reason to make the approving host, or someone
    just reloading the leaderboard, sit through ~3 sequential DB round
    trips (~400ms+, confirmed from live logs) for a check whose
    failure changes nothing about what they were actually doing.

    Two things a naive `asyncio.create_task(coro)` would get wrong,
    both handled here:
    1. asyncio only holds a WEAK reference to a task it isn't
       otherwise tracking — a task with no strong reference anywhere
       can be silently garbage-collected mid-execution (documented
       asyncio behavior, not theoretical). _background_tasks holds a
       real reference until the task's own done-callback removes it,
       so it always runs to completion.
    2. An exception inside a task nobody ever awaits doesn't propagate
       anywhere useful — it becomes an "exception was never retrieved"
       warning at garbage-collection time, easy to miss entirely. The
       inner wrapper here catches and logs it explicitly instead, same
       visibility as if it had been awaited.

    Deliberately NOT a general pattern for this codebase — every other
    background-ish call site elsewhere still uses asyncio.gather(...,
    return_exceptions=True), which keeps the caller's own reliability
    guarantee (nothing can be silently dropped by a bot restart
    mid-task). This one is a narrow, deliberate exception, specific to
    a check that already self-heals on its own next trigger — not a
    template for other hot-path work."""
    async def _wrapped() -> None:
        try:
            await coro
        except Exception:
            logger.exception("Background task failed: %s", error_label)

    task = asyncio.ensure_future(_wrapped())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def run_season_lazy_checks(bot: commands.Bot, season_id: int) -> None:
    """The lazy-lock/lazy-unlock entry point (migration_036) — called
    from exactly two places, deliberately: cogs/match.py right after a
    match is approved, and PointsLeaderboardReloadButton's callback
    above. No scheduled job exists or is needed; between these two
    triggers the season locks itself within minutes of its deadline in
    practice, worst case whenever someone next opens the leaderboard —
    same "piggyback an already-frequent action" pattern this file
    already uses for expire_shields().

    Both call sites fire this via spawn_background(), not a direct
    await — see that function's docstring for why waiting on this
    specific check was pure added latency with no correctness benefit
    (confirmed live: ~400ms+ added to every single match approval,
    2026-09-16).
    Two independent lazy checks live here:
      1. Pool-unlock — did any player just cross
         SEASON_POOL_UNLOCK_THRESHOLD? Doesn't end anything, just
         flips a flag and announces once.
      2. Deadline lock — has the season's end_date passed? THE only
         thing that actually freezes points and assigns final ranks.

    Each check's underlying DB function (check_and_unlock_pool /
    check_and_lock_season_by_deadline) only returns True for the ONE
    call that actually performed the state change — so under
    concurrent triggers (a match approval and a leaderboard click
    landing at the same moment), only one of them proceeds to
    announce. The mark_*_announced() guards below are a second,
    independent layer of the same protection, specifically to fix
    ChampQueue_Audit_2026-09-13.md §6.3 ("season-end announcement
    repeats on every subsequent match") — that bug existed because the
    old version of this check had no announced-guard at all and fired
    on every call once the season was locked, not just the first."""
    season = await adb.get_active_season()
    if not season or season["id"] != season_id:
        return
    season_name = season.get("name", "Season")

    # ── 1. Pool-unlock ──
    try:
        just_unlocked = await with_retry(adb.check_and_unlock_pool, season_id)
    except Exception:
        logger.exception("check_and_unlock_pool failed for season_id=%s", season_id)
        just_unlocked = False

    if just_unlocked:
        try:
            should_announce = await with_retry(adb.mark_pool_unlock_announced, season_id)
        except Exception:
            logger.exception("mark_pool_unlock_announced failed for season_id=%s", season_id)
            should_announce = False
        if should_announce:
            em = _pool_unlocked_embed(season_name)
            if config.SHIELD_CHANNEL_ID:
                ch = bot.get_channel(config.SHIELD_CHANNEL_ID)
                if ch:
                    try:
                        await ch.send(content="@everyone", embed=em)
                    except discord.HTTPException:
                        pass

    # ── 2. Deadline lock ──
    try:
        just_locked = await with_retry(adb.check_and_lock_season_by_deadline, season_id)
    except Exception:
        logger.exception("check_and_lock_season_by_deadline failed for season_id=%s", season_id)
        just_locked = False

    if not just_locked:
        return

    try:
        should_announce = await with_retry(adb.mark_season_end_announced, season_id)
    except Exception:
        logger.exception("mark_season_end_announced failed for season_id=%s", season_id)
        return
    if not should_announce:
        return

    rows = await with_retry(adb.season_points_leaderboard, season_id)
    if not rows:
        return
    winner = next((r for r in rows if r.get("locked_rank") == 1), None)
    if not winner:
        return

    pool_unlocked = bool(season.get("sp_pool_unlocked"))
    em = _season_end_embed(rows, season_name, pool_unlocked)

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

    # Post to shield channel — public announcement
    if config.SHIELD_CHANNEL_ID:
        ch = bot.get_channel(config.SHIELD_CHANNEL_ID)
        if ch:
            try:
                await ch.send(
                    f"🏆 **{season_name} has ended!** <@{winner['discord_id']}> takes 1st place. "
                    "Full results above."
                )
            except discord.HTTPException:
                pass

    # Post to HOD approval channel — tagging HOD role for awareness
    role_mentions = _role_mentions(config.HOD_ROLE_IDS, config.ADMIN_ROLE_IDS)
    await _post_to_hod_channel(
        bot, season_id,
        f"🏆 **SEASON ENDED** (deadline reached) — <@{winner['discord_id']}> holds 1st place.\n"
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