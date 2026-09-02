from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from database.db import adb
from services import mmr_engine
from utils.embeds import (
    player_stats_card, comparison_embed, rank_progress_card, rank_ladder_embed,
    achievements_card, achievements_browse_embed,
)
from utils.permissions import admin_only

_PAGE_SIZE = 50  # players per leaderboard page — Discord embed description
                 # limit is 4096 chars; a real ign+rank+mmr line runs
                 # ~40-50 chars, so 50/page × ~50 chars (long-name worst
                 # case) ≈ 2500 chars, still comfortably under that.

# Fixed-width columns for the code-block leaderboard layout (2026-09).
# Plain (non-code-block) embed text has no monospace guarantee on
# Discord's mobile client, so lines of wildly different IGN length
# (e.g. "SumitCantSnipe" vs "-EaSy-") render with a visibly zigzagging
# "—" separator — cosmetic, but reported as looking rough on mobile.
# A ```code block``` is the only Discord-side way to force real column
# alignment; the cost is losing **bold**/color styling, which only
# applies inside code blocks as literal characters, not formatting.
# _IGN_COL_WIDTH chosen from real leaderboard data: "SumitCantSnipe" is
# 14 chars, the longest IGN seen so far; 16 gives a little headroom
# without ballooning line width on mobile. Longer real names get
# truncated with a trailing "…" rather than wrapping (wrapping inside a
# code block breaks alignment on the wrapped continuation line, which
# would be worse than the truncation it's meant to avoid).
_IGN_COL_WIDTH = 16


def _leaderboard_page_text_aligned(players: list[dict], page: int) -> tuple[str, int]:
    """Code-block-aligned alternative to _leaderboard_page_text — fixed-
    width IGN column so the MMR/rank portion lines up vertically on every
    row, including on mobile. Trade-off: no bold rank numbers, no color;
    the whole block renders in Discord's default monospace embed font.
    Not wired into _leaderboard_embed by default — swap the call site in
    _leaderboard_embed to use this instead of _leaderboard_page_text once
    it's confirmed to look right on an actual phone, not just in theory."""
    total_pages = max(1, -(-len(players) // _PAGE_SIZE))  # ceil div
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = players[start:start + _PAGE_SIZE]
    if not chunk:
        return "```\nNo approved players yet.\n```", total_pages

    lines = []
    for i, p in enumerate(chunk, start=1):
        rank_num = f"{start + i}."
        ign = p["ign"]
        if len(ign) > _IGN_COL_WIDTH:
            ign_display = ign[: _IGN_COL_WIDTH - 1] + "…"
        else:
            ign_display = ign
        # ljust the IGN column so everything after it — the MMR/rank
        # portion — starts at the same character position on every line,
        # regardless of how short or long this row's IGN is.
        lines.append(f"{rank_num:>3} {ign_display.ljust(_IGN_COL_WIDTH)} {p['mmr']:>4} MMR ({p['current_rank']})")
    return "```\n" + "\n".join(lines) + "\n```", total_pages


def _leaderboard_page_text(players: list[dict], page: int) -> tuple[str, int]:
    """Returns (rendered page text, total page count). Rank numbers are
    global (based on position in the full MMR-sorted roster), not reset
    per page, so page 2 correctly starts at 26, not 1."""
    total_pages = max(1, -(-len(players) // _PAGE_SIZE))  # ceil div
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = players[start:start + _PAGE_SIZE]
    if not chunk:
        return "No approved players yet.", total_pages
    lines = [f"**{start + i}.** {p['ign']} — {p['mmr']} MMR ({p['current_rank']})"
             for i, p in enumerate(chunk, start=1)]
    return "\n".join(lines), total_pages


# Toggle for testing the aligned code-block layout against the current
# one — flip this to True to render leaderboards with
# _leaderboard_page_text_aligned instead. Left as an explicit constant
# rather than an env var since this is a short-lived visual A/B check,
# not a permanent runtime setting; delete this flag (and whichever
# _leaderboard_page_text_* function loses) once a call is made.
_USE_ALIGNED_LEADERBOARD = False


def _leaderboard_embed(players: list[dict], page: int) -> discord.Embed:
    if _USE_ALIGNED_LEADERBOARD:
        text, total_pages = _leaderboard_page_text_aligned(players, page)
    else:
        text, total_pages = _leaderboard_page_text(players, page)
    embed = discord.Embed(
        title="🏆 Champion's Queue Leaderboard",
        description=text,
        color=discord.Color.purple(),
    )
    embed.set_footer(text=f"{len(players)} registered players  •  page {page + 1}/{total_pages}  •  updated on reload")
    return embed


class LeaderboardView(discord.ui.View):
    """Persistent, restart-safe (custom_id-based, re-attached via
    bot.add_view — same mechanism as RegionQueueView in queue.py). Data
    is always correct in the DB the moment a match is approved; this
    view only controls when the DISPLAYED message re-renders. Reload is
    rate-limited per-user via a CooldownMapping (the same primitive
    discord.py's own command cooldown decorator wraps internally) rather
    than a custom DB-tracked limiter — matches the "why build it when
    Discord already does it" reasoning from the P6 planning discussion.

    Unified 2026-07-29: was one instance per region (East/West), each
    with its own custom_id suffix and cooldown bucket, since the
    leaderboard used to be region-scoped. Now there's exactly ONE
    leaderboard covering all 4 queues/regions combined, so this is a
    single class-level cooldown and fixed custom_ids — no more keying
    dict needed."""

    _cooldown = commands.CooldownMapping.from_cooldown(1, 60.0, commands.BucketType.user)

    def __init__(self):
        super().__init__(timeout=None)
        self.page = 0

        self.prev_button = discord.ui.Button(
            label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="lb_prev"
        )
        self.prev_button.callback = self.prev_callback
        self.add_item(self.prev_button)

        self.reload_button = discord.ui.Button(
            label="🔄 Reload", style=discord.ButtonStyle.primary, custom_id="lb_reload"
        )
        self.reload_button.callback = self.reload_callback
        self.add_item(self.reload_button)

        self.next_button = discord.ui.Button(
            label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="lb_next"
        )
        self.next_button.callback = self.next_callback
        self.add_item(self.next_button)

    async def _render(self, interaction: discord.Interaction):
        players = await adb.region_leaderboard()
        embed = _leaderboard_embed(players, self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    async def prev_callback(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self._render(interaction)

    async def next_callback(self, interaction: discord.Interaction):
        self.page += 1  # _render/_leaderboard_page_text clamps to the real last page
        await self._render(interaction)

    async def reload_callback(self, interaction: discord.Interaction):
        # commands.CooldownMapping expects something message-shaped
        # (reads .author.id for BucketType.user) — a raw Interaction has
        # .user, not .author, so it can't be passed directly. This tiny
        # shim is cheaper and less error-prone than hand-rolling a
        # separate rate limiter.
        class _Ctx:
            author = interaction.user
        bucket = LeaderboardView._cooldown.get_bucket(_Ctx())
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await interaction.response.send_message(
                f"Leaderboard was just reloaded — try again in {retry_after:.0f}s.", ephemeral=True
            )
            return
        await self._render(interaction)


class RankProgressView(discord.ui.View):
    """One-shot view for /rank-progress's "View rank ladder" button.
    Deliberately NOT persistent (no custom_id, real timeout, no
    bot.add_view() registration) — unlike LeaderboardView above, this
    isn't a panel meant to survive a bot restart; it's a single ephemeral
    reply's follow-up interaction, gone the moment the person closes it
    or the timeout window lapses. See rank_progress_card's docstring for
    why the ladder is opt-in rather than shown immediately.

    Timeout bumped 60s -> 180s (2026-08-21, live testing): a player who
    clicked "View rank ladder" ~2 minutes after running the command hit
    a silent Discord-side "didn't respond in time" — not a bug, exactly
    the documented behavior of a non-persistent view's timeout elapsing,
    but 60s was too tight for how players actually pause before
    clicking. No logged error is possible here even in principle: once
    discord.py's internal view-timeout fires, the view is dropped from
    its dispatch table, so a late click never reaches any of this code
    at all — Discord's client shows the failure entirely client-side."""

    def __init__(self, player: dict, tier: str):
        super().__init__(timeout=180)
        self.player = player
        self.tier = tier

        self.ladder_button = discord.ui.Button(
            label="📊 View rank ladder", style=discord.ButtonStyle.secondary
        )
        self.ladder_button.callback = self.ladder_callback
        self.add_item(self.ladder_button)

    async def ladder_callback(self, interaction: discord.Interaction):
        # Button is single-use — remove it after the reveal rather than
        # leaving a now-redundant button on a message that already shows
        # the ladder (nothing to toggle back to; the near-term "next rank"
        # block stays visible above the ladder either way).
        self.clear_items()
        await interaction.response.edit_message(
            embed=rank_ladder_embed(self.player, self.tier), view=self
        )


class AchievementsBrowseView(discord.ui.View):
    """One-shot view for /achievements' "Browse All Badges" button. Same
    pattern as RankProgressView above (non-persistent, real timeout, no
    custom_id/bot.add_view() registration) — a single ephemeral reply's
    follow-up interaction, not a panel meant to survive a restart. 180s
    timeout matches RankProgressView's post-2026-08-21-feedback value
    rather than the original 60s, since the same "player pauses before
    clicking" behavior applies here too."""

    def __init__(self, player: dict, earned: list[dict], live_titles: list[dict]):
        super().__init__(timeout=180)
        self.player = player
        self.earned = earned
        self.live_titles = live_titles

        self.browse_button = discord.ui.Button(
            label="📖 Browse All Badges", style=discord.ButtonStyle.secondary
        )
        self.browse_button.callback = self.browse_callback
        self.add_item(self.browse_button)

    async def browse_callback(self, interaction: discord.Interaction):
        # Single-use, same reasoning as RankProgressView.ladder_callback —
        # nothing to toggle back to once the full catalogue is showing.
        self.clear_items()
        await interaction.response.edit_message(
            embed=achievements_browse_embed(self.player, self.earned, self.live_titles), view=self
        )


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="player-stats", description="View your (or another player's) Champion's Queue stats")
    @app_commands.describe(user="Leave blank to see your own stats, or mention someone else to see theirs")
    async def player_stats(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        player = await adb.get_player_by_discord_id(target.id)
        if not player:
            await interaction.response.send_message(f"{target.mention} isn't registered.", ephemeral=True)
            return
        weekly = await adb.weekly_leaders()
        # Only visible to the person who ran the command — locked 2026-07-19,
        # this card was public before P6 and that was a real gap, not the
        # intended behavior.
        await interaction.response.send_message(embed=player_stats_card(player, weekly), ephemeral=True)

    @app_commands.command(name="leaderboard-post", description="Post the persistent unified leaderboard panel")
    @admin_only()
    async def leaderboard_post(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        players = await adb.region_leaderboard()
        view = LeaderboardView()
        embed = _leaderboard_embed(players, 0)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send("Posted the persistent leaderboard panel.", ephemeral=True)

    @app_commands.command(name="leaderboard-refresh", description="Admin: force-refresh the leaderboard from the latest DB state")
    @admin_only()
    async def leaderboard_refresh(self, interaction: discord.Interaction):
        # Same rare-stuck-entry safety valve as queue's admin tooling —
        # re-posts a fresh panel rather than trying to locate and edit a
        # possibly-stale existing message. Old panel (if any) is left as
        # a dead message; admin can delete it manually. Mirrors the
        # "don't try to be clever about finding the old message" caution
        # from the P5 handoff around stale local state.
        await interaction.response.defer(thinking=True)
        players = await adb.region_leaderboard()
        view = LeaderboardView()
        embed = _leaderboard_embed(players, 0)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send("Force-refreshed the leaderboard with the latest data.", ephemeral=True)

    # Disabled 2026-07-30: redundant with /player-stats and the
    # leaderboard, and its strict status == "completed" filter (line
    # was `[h for h in history if h.get("matches", {}).get("status") ==
    # "completed"]`) meant it almost always returned "You need at least
    # 2 completed matches to compare" given current approval friction —
    # looked broken/stale to players even though the underlying query
    # was working as designed. Kept commented rather than deleted in
    # case this is revisited later (e.g. widened to also count
    # pending_verification/awaiting_review matches, same as this
    # session's provisional-stats work — but that would need
    # comparison_embed's MMR Change field to handle a match with no
    # mmr_change yet, since that's only written by approve_ro3_match).
    #
    # @app_commands.command(name="compare-last-match", description="Compare your latest match to the one before it")
    # async def compare_last_match(self, interaction: discord.Interaction):
    #     player = await adb.get_player_by_discord_id(interaction.user.id)
    #     if not player:
    #         await interaction.response.send_message("You're not registered.", ephemeral=True)
    #         return
    #     history = await adb.player_recent_matches(player["id"], limit=2)
    #     completed = [h for h in history if h.get("matches", {}).get("status") == "completed"]
    #     if len(completed) < 2:
    #         await interaction.response.send_message("You need at least 2 completed matches to compare.", ephemeral=True)
    #         return
    #     latest, previous = completed[0], completed[1]
    #     await interaction.response.send_message(embed=comparison_embed(player["ign"], previous, latest))

    @app_commands.command(name="rank-progress", description="See your progress toward the next rank/division")
    async def rank_progress(self, interaction: discord.Interaction):
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return
        tier, division = mmr_engine.derive_rank(player["mmr"])
        await interaction.response.send_message(
            embed=rank_progress_card(player, tier),
            view=RankProgressView(player, tier),
            ephemeral=True,
        )

    @app_commands.command(name="achievements", description="View a player's earned badges and current live titles")
    @app_commands.describe(user="Leave blank to see your own badges, or mention someone else to see theirs")
    async def achievements(self, interaction: discord.Interaction, user: discord.Member | None = None):
        # Rebuilt 2026-08-21: old version read player_achievements and
        # dumped every earned row into one flat "General" category field
        # (all seed achievements share category='general', so this
        # rendered as one long undifferentiated list — the exact
        # staleness/clutter complaint that started this redesign). Now
        # reads the same get_player_achievements() data but renders it
        # against the curated _PERMANENT_BADGES display order (see
        # utils/embeds.py), plus live_player_titles() for the
        # unstored "currently #1" titles — two clearly separated
        # sections, opt-in full catalogue via the Browse button, same
        # "don't overwhelm up front" pattern as /rank-progress.
        #
        # NOT ephemeral, unlike /player-stats — badges are meant to be
        # seen by others in a competitive environment (2026-08-21
        # discussion), same reasoning as the user param existing here in
        # the first place.
        target = user or interaction.user
        player = await adb.get_player_by_discord_id(target.id)
        if not player:
            await interaction.response.send_message(f"{target.mention} isn't registered.", ephemeral=True)
            return
        earned = await adb.get_player_achievements(player["id"])
        live_titles = await adb.live_player_titles(player["id"])
        await interaction.response.send_message(
            embed=achievements_card(player, earned, live_titles),
            view=AchievementsBrowseView(player, earned, live_titles),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
    # Restart-safety: re-attach live callback to any existing persistent
    # leaderboard panel message, same mechanism as bot.add_view for
    # RegionQueueView in queue.py. Unified 2026-07-29: was a loop over
    # _REGIONS (one view per region); now a single unified panel, one
    # view registration.
    bot.add_view(LeaderboardView())