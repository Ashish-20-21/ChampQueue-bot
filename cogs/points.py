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
        title="🛡️ Point Shield",
        description=(
            "Protect your season points from match losses for **48 hours**.\n\n"
            "While a shield is active, losses cost you **0 points** instead of the usual **-3**. "
            "Wins still give the normal **+5**. Your MMR is completely unaffected.\n\n"
            "**How to get one:**\n"
            "• **With points:** costs **500 points** — tap the button below.\n"
            f"• **With cash:** costs **₹{config.SHIELD_COST_RUPEES}** — raise a ticket with an admin, "
            "they'll handle it from there."
        ),
        color=0x5865F2,  # Discord blurple
    )
    em.set_footer(text="One active shield at a time · Shield starts the moment it's activated")
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
    em = discord.Embed(
        title="🛡️ Shield Grant — Awaiting HOD Confirmation",
        description=(
            f"**Player:** {player_ign} (<@{player_discord_id}>)\n"
            f"**Payment:** ₹{config.SHIELD_COST_RUPEES} (cash)\n"
            f"**Initiated by:** <@{admin_discord_id}>\n"
            f"**Shield ID:** `{shield['id']}`\n\n"
            "Confirm that payment has been verified. The shield activates "
            "the moment you click **Confirm** — the 48-hour window starts then."
        ),
        color=0xFFA500,
    )
    em.set_footer(text="Two-person approval required · Self-confirmation blocked")
    return em


def _season_end_embed(winner_ign: str, winner_discord_id: str, winner_points: int,
                       second: dict | None, third: dict | None,
                       season_name: str) -> discord.Embed:
    """The announcement embed when someone crosses 2500."""
    desc = (
        f"🎉 **{winner_ign}** (<@{winner_discord_id}>) has crossed "
        f"**{config.SEASON_END_THRESHOLD}** points with **{winner_points}** pts!\n\n"
        f"🥇 **1st Place:** {winner_ign} — **₹{config.PRIZE_1ST}**\n"
    )
    if second:
        desc += f"🥈 **2nd Place:** {second['ign']} — **₹{second.get('payout_rupees', 0)}** ({second['points']} pts)\n"
    if third:
        desc += f"🥉 **3rd Place:** {third['ign']} — **₹{third.get('payout_rupees', 0)}** ({third['points']} pts)\n"

    desc += "\n**All season points are now frozen.** MMR and matchmaking continue as normal."

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
    """Self-serve shield purchase with points. Persistent across restarts.
    Takes no per-instance parameters, so from_custom_id just re-creates
    a fresh instance — same DynamicItem pattern as HostApprovalButton /
    IssueResolveButton in cogs/match.py."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(
            label="Buy Shield (500 pts)",
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
            await interaction.followup.send("You need to be registered to buy a shield.", ephemeral=True)
            return

        # Check: no existing active shield
        existing = await adb.get_active_shield(player["id"], season_id)
        if existing:
            ends = existing.get("shield_ends_at", "")
            await interaction.followup.send(
                f"You already have an active shield (expires <t:{_iso_to_ts(ends)}:R>). "
                "One shield at a time.",
                ephemeral=True,
            )
            return

        # Check: season not locked
        locked = await adb.is_season_points_locked(season_id)
        if locked:
            await interaction.followup.send("Season points are locked — shields are no longer available.", ephemeral=True)
            return

        # Check balance
        sp = await adb.get_season_points(player["id"], season_id)
        current_points = sp["points"] if sp else 0
        if current_points < config.SHIELD_COST_POINTS:
            await interaction.followup.send(
                f"You have **{current_points}** points but need **{config.SHIELD_COST_POINTS}**. "
                f"You can top up by purchasing with cash (₹{config.SHIELD_COST_RUPEES}) — "
                "raise a ticket with an admin to get started.",
                ephemeral=True,
            )
            return

        # Confirmation step
        confirm_view = ShieldConfirmView(player["id"], season_id, current_points)
        await interaction.followup.send(
            f"**Confirm shield purchase?**\n\n"
            f"This will deduct **{config.SHIELD_COST_POINTS} points** from your balance "
            f"({current_points} → {current_points - config.SHIELD_COST_POINTS}).\n"
            f"Your shield will be active for **{config.SHIELD_DURATION_HOURS} hours** starting now.\n\n"
            "During this time, losses will cost **0 points** instead of -3.",
            view=confirm_view,
            ephemeral=True,
        )


class ShieldConfirmView(discord.ui.View):
    """Ephemeral confirmation before deducting points."""

    def __init__(self, player_id: int, season_id: int, current_points: int) -> None:
        super().__init__(timeout=60)
        self.player_id = player_id
        self.season_id = season_id
        self.current_points = current_points

    @discord.ui.button(label="Confirm Purchase", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        self.stop()

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
        except Exception as exc:
            logger.exception("Shield purchase failed for player_id=%s", self.player_id)
            await interaction.followup.send(
                "Something went wrong processing your shield. Please try again or contact an admin.",
                ephemeral=True,
            )
            return

        ends_ts = _iso_to_ts(shield.get("shield_ends_at", ""))
        await interaction.followup.send(
            f"🛡️ **Shield activated!** You're protected until <t:{ends_ts}:F> (<t:{ends_ts}:R>).\n"
            f"Points deducted: **-{config.SHIELD_COST_POINTS}** "
            f"(new balance: **{self.current_points - config.SHIELD_COST_POINTS}**).",
            ephemeral=True,
        )

        # Post to shield channel audit log
        await _post_shield_audit(
            interaction.client, self.season_id,
            f"🛡️ **Shield purchased** by <@{interaction.user.id}> "
            f"(points path, -{config.SHIELD_COST_POINTS} pts) — "
            f"active until <t:{ends_ts}:F>"
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message("Shield purchase cancelled.", ephemeral=True)
        self.stop()


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
        await _post_shield_audit(
            interaction.client, shield["season_id"],
            f"🛡️ **Shield granted** to <@{player['discord_id']}> "
            f"(cash path, ₹{config.SHIELD_COST_RUPEES}) — "
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
        await _post_shield_audit(
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
    """Convert an ISO datetime string to a Unix timestamp for Discord formatting."""
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return 0


async def _post_shield_audit(bot: commands.Bot, season_id: int, message: str) -> None:
    """Post an audit line to the shield channel."""
    if not config.SHIELD_CHANNEL_ID:
        return
    ch = bot.get_channel(config.SHIELD_CHANNEL_ID)
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