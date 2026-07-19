from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from database.db import adb
from services import mmr_engine
from utils.embeds import player_stats_card, comparison_embed
from utils.permissions import admin_only

_REGIONS = ("East", "West")
_PAGE_SIZE = 25  # players per leaderboard page — Discord embed description
                 # limit is 4096 chars; a real ign+rank+mmr line runs
                 # ~40-50 chars, so 25/page stays comfortably under that
                 # even for long names, without needing per-name length math.


def _leaderboard_page_text(players: list[dict], page: int) -> tuple[str, int]:
    """Returns (rendered page text, total page count). Rank numbers are
    global (based on position in the full MMR-sorted roster), not reset
    per page, so page 2 correctly starts at 26, not 1."""
    total_pages = max(1, -(-len(players) // _PAGE_SIZE))  # ceil div
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = players[start:start + _PAGE_SIZE]
    if not chunk:
        return "No approved players in this region yet.", total_pages
    lines = [f"**{start + i}.** {p['ign']} — {p['mmr']} MMR ({p['current_rank']})"
             for i, p in enumerate(chunk, start=1)]
    return "\n".join(lines), total_pages


def _leaderboard_embed(region: str, players: list[dict], page: int) -> discord.Embed:
    text, total_pages = _leaderboard_page_text(players, page)
    embed = discord.Embed(
        title=f"🏆 {region} Leaderboard",
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
    One shared mapping per region (class-level) so the limit survives
    across view instances created by different reload/refresh calls."""

    _cooldowns: dict[str, commands.CooldownMapping] = {}

    def __init__(self, region: str):
        super().__init__(timeout=None)
        self.region = region
        self.page = 0
        if region not in LeaderboardView._cooldowns:
            LeaderboardView._cooldowns[region] = commands.CooldownMapping.from_cooldown(
                1, 60.0, commands.BucketType.user
            )

        self.prev_button = discord.ui.Button(
            label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id=f"lb_prev_{region}"
        )
        self.prev_button.callback = self.prev_callback
        self.add_item(self.prev_button)

        self.reload_button = discord.ui.Button(
            label="🔄 Reload", style=discord.ButtonStyle.primary, custom_id=f"lb_reload_{region}"
        )
        self.reload_button.callback = self.reload_callback
        self.add_item(self.reload_button)

        self.next_button = discord.ui.Button(
            label="Next ▶", style=discord.ButtonStyle.secondary, custom_id=f"lb_next_{region}"
        )
        self.next_button.callback = self.next_callback
        self.add_item(self.next_button)

    async def _render(self, interaction: discord.Interaction):
        players = await adb.region_leaderboard(self.region)
        embed = _leaderboard_embed(self.region, players, self.page)
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
        bucket = LeaderboardView._cooldowns[self.region].get_bucket(_Ctx())
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await interaction.response.send_message(
                f"Leaderboard was just reloaded — try again in {retry_after:.0f}s.", ephemeral=True
            )
            return
        await self._render(interaction)


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
        weekly = await adb.weekly_leaders(player["region"])
        # Only visible to the person who ran the command — locked 2026-07-19,
        # this card was public before P6 and that was a real gap, not the
        # intended behavior.
        await interaction.response.send_message(embed=player_stats_card(player, weekly), ephemeral=True)

    @app_commands.command(name="leaderboard-post", description="Post the persistent region leaderboard panel")
    @app_commands.describe(region="The competitive region (East, West)")
    @admin_only()
    async def leaderboard_post(self, interaction: discord.Interaction, region: str):
        region_norm = region.capitalize()
        if region_norm not in _REGIONS:
            await interaction.response.send_message("Invalid region. Please specify either 'East' or 'West'.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        players = await adb.region_leaderboard(region_norm)
        view = LeaderboardView(region_norm)
        embed = _leaderboard_embed(region_norm, players, 0)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send(f"Posted the persistent **{region_norm}** leaderboard panel.", ephemeral=True)

    @app_commands.command(name="leaderboard-refresh", description="Admin: force-refresh a region's leaderboard from the latest DB state")
    @app_commands.describe(region="The competitive region (East, West)")
    @admin_only()
    async def leaderboard_refresh(self, interaction: discord.Interaction, region: str):
        # Same rare-stuck-entry safety valve as queue's admin tooling —
        # re-posts a fresh panel rather than trying to locate and edit a
        # possibly-stale existing message. Old panel (if any) is left as
        # a dead message; admin can delete it manually. Mirrors the
        # "don't try to be clever about finding the old message" caution
        # from the P5 handoff around stale local state.
        region_norm = region.capitalize()
        if region_norm not in _REGIONS:
            await interaction.response.send_message("Invalid region. Please specify either 'East' or 'West'.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        players = await adb.region_leaderboard(region_norm)
        view = LeaderboardView(region_norm)
        embed = _leaderboard_embed(region_norm, players, 0)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send(f"Force-refreshed **{region_norm}** leaderboard with the latest data.", ephemeral=True)

    @app_commands.command(name="compare-last-match", description="Compare your latest match to the one before it")
    async def compare_last_match(self, interaction: discord.Interaction):
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return
        history = await adb.player_recent_matches(player["id"], limit=2)
        completed = [h for h in history if h.get("matches", {}).get("status") == "completed"]
        if len(completed) < 2:
            await interaction.response.send_message("You need at least 2 completed matches to compare.", ephemeral=True)
            return
        latest, previous = completed[0], completed[1]
        await interaction.response.send_message(embed=comparison_embed(player["ign"], previous, latest))

    @app_commands.command(name="rank-progress", description="See your progress toward the next rank/division")
    async def rank_progress(self, interaction: discord.Interaction):
        player = await adb.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.send_message("You're not registered.", ephemeral=True)
            return
        tier, division = mmr_engine.derive_rank(player["mmr"])
        await interaction.response.send_message(
            f"**{player['ign']}** — {tier} {division} — {player['mmr']} MMR\n"
            f"(Peak: {player['peak_rank']} at {player['peak_mmr']} MMR)"
        )

    @app_commands.command(name="achievements", description="View your earned achievements")
    async def achievements(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        player = await adb.get_player_by_discord_id(target.id)
        if not player:
            await interaction.response.send_message(f"{target.mention} isn't registered.", ephemeral=True)
            return
        earned = await adb.get_player_achievements(player["id"])
        if not earned:
            await interaction.response.send_message(f"{player['ign']} hasn't earned any achievements yet.")
            return
        by_category: dict[str, list[str]] = {}
        for e in earned:
            cat = e["achievements"]["category"]
            by_category.setdefault(cat, []).append(e["achievements"]["name"])
        embed = discord.Embed(title=f"{player['ign']} — Achievements", color=discord.Color.gold())
        for cat, names in by_category.items():
            embed.add_field(name=cat.title(), value="\n".join(names), inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
    # Restart-safety: re-attach live callbacks to any existing persistent
    # leaderboard panel messages, same mechanism as bot.add_view for
    # RegionQueueView in queue.py.
    for region in _REGIONS:
        bot.add_view(LeaderboardView(region))
