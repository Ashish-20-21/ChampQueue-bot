import discord

from services import mmr_engine
from typing import Optional


def season_recap_embed(season: dict, stats: dict, ai_tokens_used: str | None = None) -> discord.Embed:
    """Decorative season-wide stat showcase — fires before Hall of Fame,
    "how big was this season" framing rather than per-player winners.
    Numbers come straight from season_recap_stats() (migration_026);
    this function only formats, never computes.

    ai_tokens_used is the one exception — there's no token-usage
    tracking anywhere in the schema or codebase (confirmed: nothing in
    vision_extraction.py or elsewhere persists per-call token counts),
    so this isn't derived from a DB query like every other field here.
    It's an optional pre-formatted string supplied by whoever triggers
    the recap, sourced from the OpenAI dashboard directly. Omit the
    field entirely if not provided, rather than showing a fake/zero
    value."""
    season_label = season.get("code") or season.get("name") or "Season"
    start = (season.get("start_date") or "")[:10]  # YYYY-MM-DD, no time
    embed = discord.Embed(
        title=f"📊 Season Recap — {season_label}",
        description=f"From **{start}** to today — here's what the community built together. 🔥",
        color=discord.Color.from_rgb(255, 140, 0),
    )
    embed.add_field(name="🎮 Matches Played", value=f"**{stats['matches_played']:,}**", inline=True)
    embed.add_field(name="🔄 Rounds Played", value=f"**{stats['rounds_played']:,}**", inline=True)
    embed.add_field(name="👥 Players", value=f"**{stats['unique_players']:,}**", inline=True)
    embed.add_field(name="🔫 Total Kills", value=f"**{stats['total_kills']:,}**", inline=True)
    embed.add_field(name="💀 Total Deaths", value=f"**{stats['total_deaths']:,}**", inline=True)
    embed.add_field(name="⭐ MVPs Awarded", value=f"**{stats['total_mvps_awarded']:,}**", inline=True)
    embed.add_field(name="⏱️ Hours of Hardpoint", value=f"**{stats['total_hardpoint_hours']:,}**", inline=True)
    if ai_tokens_used:
        embed.add_field(name="🤖 AI Tokens Processed", value=f"**{ai_tokens_used}**", inline=True)
    embed.set_footer(text="Every kill, every clutch, every close call — this was Season 1. 🏆")
    return embed


def hall_of_fame_embed(season: dict, winners: dict[str, Optional[dict]]) -> discord.Embed:
    """Season-end Hall of Fame card, one field per category. `winners` maps
    category key -> the row dict from the matching db.hof_* call (or None
    if the >=8-match floor excluded everyone for that category — shown as
    'Not enough matches this season' rather than silently omitting the
    field, so it's visibly a real season-data outcome, not a bug).

    Category value/units differ per category (win %, mmr/match, raw kill
    count, K/D ratio, etc.) — each branch below formats its own row rather
    than trying to force one generic formatter across incompatible units."""
    season_label = season.get("code") or season.get("name") or "Season"
    embed = discord.Embed(
        title=f"🏆 Hall of Fame — {season_label}",
        description="Top performers from the season that was.",
        color=discord.Color.gold(),
    )

    def _line(row: Optional[dict], stat: str) -> str:
        if not row:
            return "*Not enough matches this season*"
        return f"**{row['ign']}** — {stat}"

    mc = winners.get("most_consistent")
    embed.add_field(name="🎯 Most Consistent", value=_line(mc, f"{mc['win_rate_pct']}% win rate ({mc['matches_played']} matches)" if mc else ""), inline=False)

    fc = winners.get("fastest_climber")
    embed.add_field(name="📈 Fastest Climber", value=_line(fc, f"+{fc['mmr_per_match']} MMR/match ({fc['mmr_gained']} total)" if fc else ""), inline=False)

    hk = winners.get("highest_total_kills")
    embed.add_field(name="🔫 Highest Kills", value=_line(hk, f"{hk['total_kills']} kills ({hk['matches_played']} matches)" if hk else ""), inline=False)

    bak = winners.get("best_avg_kills")
    embed.add_field(name="💥 Best Avg Kills", value=_line(bak, f"{bak['avg_kills']} kills/match" if bak else ""), inline=False)

    bad = winners.get("best_avg_deaths")
    embed.add_field(name="🛡️ Best Avg Deaths", value=_line(bad, f"{bad['avg_deaths']} deaths/match (fewest)" if bad else ""), inline=False)

    mv = winners.get("most_mvps")
    embed.add_field(name="⭐ Most MVPs", value=_line(mv, f"{mv['mvp_count']} MVPs ({mv['matches_played']} matches)" if mv else ""), inline=False)

    mp = winners.get("most_matches_played")
    embed.add_field(name="🎮 Most Matches Played", value=_line(mp, f"{mp['matches_played']} matches" if mp else ""), inline=False)

    kd = winners.get("best_kd")
    embed.add_field(name="⚔️ Best K/D", value=_line(kd, f"{kd['kd_ratio']} K/D ({kd['total_kills']}/{kd['total_deaths']})" if kd else ""), inline=False)

    hm = winners.get("highest_mmr")
    embed.add_field(name="👑 Highest Rank/MMR", value=_line(hm, f"{hm['mmr']} MMR ({hm['current_rank']})" if hm else ""), inline=False)

    embed.set_footer(text="Congratulations to everyone who competed this season! 🎉")
    return embed


def result_card(match: dict, match_players: list[dict]) -> discord.Embed:
    winner = match.get("winner_team")
    color = discord.Color.green() if winner else discord.Color.blurple()
    embed = discord.Embed(
        title=f"Match {match['match_id']} — Result",
        description=f"**Map:** {match.get('map', 'N/A')}   |   **Score:** {match.get('final_score', 'N/A')}",
        color=color,
    )
    for team in ("A", "B"):
        lines = []
        for mp in sorted([m for m in match_players if m["team"] == team], key=lambda m: -(m.get("score") or 0)):
            ign = mp["players"]["ign"]
            mvp_tag = " 👑" if mp.get("is_mvp") else ""
            change = mp.get("mmr_change") or 0
            sign = "+" if change >= 0 else ""
            lines.append(
                f"**{ign}**{mvp_tag} — {mp.get('kills', 0)}/{mp.get('deaths', 0)} "
                f"| DMG {mp.get('damage', 0)} | Hill {mp.get('hill_time', 0)}s | MMR {sign}{change}"
            )
        label = f"Team {team}" + (" 🏆" if winner == team else "")
        embed.add_field(name=label, value="\n".join(lines) or "—", inline=False)
    return embed


def profile_card(player: dict, achievements: list[dict]) -> discord.Embed:
    # NOTE: zero live callers (confirmed via repo-wide grep, 2026-08-15) —
    # /player-stats uses player_stats_card below instead. Kept for now as
    # a possible future admin/profile-lookup command; current_division
    # removed from the title since that column was dropped from the
    # players table (migration_016) — it was always '' in every live
    # writer anyway, so this is a no-op visually if this ever gets wired up.
    embed = discord.Embed(
        title=f"{player['ign']} — {player['current_rank']}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="MMR", value=f"{player['mmr']} (peak {player['peak_mmr']})", inline=True)
    embed.add_field(name="Reputation", value=str(player["reputation"]), inline=True)
    embed.add_field(name="Region", value=player.get("region", "—"), inline=True)

    total = player["total_matches"]
    wr = f"{(player['wins'] / total * 100):.1f}%" if total else "—"
    embed.add_field(name="Record", value=f"{player['wins']}W - {player['losses']}L ({wr})", inline=True)
    embed.add_field(name="Avg KD", value=f"{player['avg_kills']}/{player['avg_deaths']}", inline=True)
    embed.add_field(name="MVPs", value=str(player["mvp_count"]), inline=True)

    embed.add_field(name="Avg Damage", value=str(player["avg_damage"]), inline=True)
    embed.add_field(name="Avg Hill Time", value=f"{player['avg_hill_time']}s", inline=True)
    embed.add_field(name="Total Matches", value=str(total), inline=True)

    if achievements:
        names = ", ".join(a["achievements"]["name"] for a in achievements[:8])
        more = f" (+{len(achievements) - 8} more)" if len(achievements) > 8 else ""
        embed.add_field(name="Achievements", value=names + more, inline=False)
    return embed


# P6: badge display names + which weekly_leaders() category key each one
# reads. Ordered by "impressiveness" — badge_lines() below shows the
# first 1-2 a player actually holds, so this order is what gets shown
# when someone holds several at once.
_WEEKLY_BADGES = (
    ("most_mvp", "🏆 Most MVP this week"),
    ("top_kills", "🎯 Most kills this week"),
    ("top_obj", "🚩 Most objective time this week"),
    ("top_impact", "⚡ Highest impact this week"),
    ("most_matches", "🎮 Most matches played this week"),
)


def _badge_lines(player_id: int, weekly: dict[str, dict], cap: int = 2) -> list[str]:
    """Returns up to `cap` badge labels this player currently holds,
    highest-priority first (see _WEEKLY_BADGES order). A player can only
    ever show 1-2 badges even if they top every category, by design —
    keeps the card from getting cluttered as more categories are added
    later."""
    held = [label for key, label in _WEEKLY_BADGES if weekly.get(key, {}).get("player_id") == player_id]
    return held[:cap]


# Permanent career-badge metadata (2026-08-21 design session, calibrated
# against real prod stat distributions — see migration_020's own
# comments for the calibration data). Ordered as displayed: kill ladder,
# MVP ladder, match-count ladder, KD, rank ladder, streaks. Every code
# here must exist in the `achievements` table (migration_020) — this is
# purely display metadata (icon + short mobile-friendly description),
# NOT the source of truth for who has earned what. That's always
# player_achievements, read fresh via get_player_achievements().
_PERMANENT_BADGES = (
    ("first_blood",      "🩸", "First Blood",   "250 career kills"),
    ("kill_slayer",      "🔫", "Kill Slayer",    "750 career kills"),
    ("death_dealer",     "💀", "Death Dealer",   "1500 career kills"),
    ("sharpshooter",     "🎯", "Sharpshooter",   "3000 career kills"),
    ("mvp_king",         "🏅", "MVP King",       "20 career MVPs"),
    ("mvp_legend",       "👑", "MVP Legend",     "30 career MVPs"),
    ("initiator",        "🌱", "Initiator",      "Played your first match"),
    ("pacekeeper",       "🥾", "Pacekeeper",     "10 matches played"),
    ("grinder",          "⚙️", "Grinder",        "30 matches played"),
    ("veteran",          "🎖️", "Veteran",        "50 matches played"),
    ("centurion",        "💯", "Centurion",      "100+ matches played"),
    ("positive_kd",      "📈", "Positive KD",    "Career KD above 1.0"),
    ("rank_pro",         "🥈", "PRO",            "Reached PRO1 rank"),
    ("rank_master",      "🥇", "Master",         "Reached Master1 rank"),
    ("rank_grandmaster", "💎", "Grandmaster",    "Reached Grandmaster1 rank"),
    ("rank_legendary",   "🌟", "Legendary",      "Reached Legendary1 rank"),
    ("rank_titan",       "🛡️", "Titan",          "Reached Titans rank"),
    # Hidden 2026-08-21 (post-launch check): confirmed via direct query
    # that ZERO players have ever earned these 3 -- because nothing,
    # old (dead services/stats_engine.py) or new
    # (check_and_grant_achievements, migration_020), has ever actually
    # implemented streak-detection logic. Threshold badges above check
    # a single stored cumulative value ("total_matches >= 50"); a streak
    # needs ordered, sequential match history ("were the last N matches
    # in a row all wins/MVPs/positive-KD") plus a real design decision
    # on what resets a streak and when that reset gets detected — a
    # meaningfully different, harder feature than what's built so far.
    # Commented out (not deleted) so the roadmap is visible in the code
    # itself: uncomment these 3 lines once the actual streak-checking
    # function exists and is wired into check_and_grant_achievements().
    # The `achievements` table rows themselves are untouched — DB state
    # doesn't need to change for this, only what /achievements displays.
    # ("win_streak_10",    "🔥", "10 Win Streak",  "10 wins in a row"),
    # ("mvp_streak",       "🎯", "MVP Streak",     "3 MVPs in a row"),
    # ("positive_kd_streak","📈","Positive KD Streak", "Positive KD across 5 matches"),
)
_PERMANENT_BADGE_LOOKUP = {code: (icon, name, desc) for code, icon, name, desc in _PERMANENT_BADGES}

# Seasonal participation badges (2026-09) — distinct from _PERMANENT_BADGES
# above on purpose: every permanent badge is a lifetime-career threshold
# ("3000 career kills"), earned once and never tied to a specific season.
# A "played in Season 1" badge is a different kind of thing — a
# participation marker for one closed season — and mixing it into the
# same "🏅 Earned" list would misleadingly present "played S1" next to
# "3000 career kills" as if they were the same category of achievement.
# Gets its own section below instead. New seasons add a new row here
# (code convention: "played_s{N}") — nothing else about this code needs
# to change per season.
_SEASONAL_BADGES = (
    ("played_s1", "🎮", "Urising Season 1", "Played in Season 1"),
)
_SEASONAL_BADGE_LOOKUP = {code: (icon, name, desc) for code, icon, name, desc in _SEASONAL_BADGES}


# Live titles (2026-08-21) — NOT stored, computed fresh every call via
# the live_player_titles() SQL function (migration_020). title_name
# strings here must match the fixed literals that function returns
# EXACTLY (it always returns the same constant string per code, never a
# dynamic one) — kept as plain metadata here, not re-derived from the
# RPC result, so the browse view can render every title's name even for
# a player who holds none of them.
_LIVE_TITLES = (
    ("top_of_ladder",     "👑", "Top of the Ladder",       "Currently #1 on the leaderboard"),
    ("top_10",             "🏆", "Top 10",                  "Currently ranked #2–#10"),
    ("top_50",             "⚡", "Top 50",                  "Currently ranked #11–#50"),
    ("most_mvps_ever",     "🎯", "Most MVPs Ever",          "Highest career MVP count"),
    ("most_matches_ever",  "⚔️", "Most Matches Played",     "Highest career match count"),
    ("highest_kd_ever",    "💥", "Highest KD",              "Highest career KD"),
)
_LIVE_TITLE_LOOKUP = {code: (icon, name, desc) for code, icon, name, desc in _LIVE_TITLES}


def achievements_card(player: dict, earned: list[dict], live_titles: list[dict]) -> discord.Embed:
    """Main /achievements card — earned permanent badges + current live
    titles only, NOT the full browsable list (that's achievements_browse_
    embed below, opt-in via a button, same "don't overwhelm up front"
    pattern as rank_progress_card's ladder reveal). `earned` is
    get_player_achievements()'s raw return (list of {"achievements":
    {...}} rows); `live_titles` is live_player_titles()'s raw RPC return
    (list of {"title_code": ..., "title_name": ...} rows).

    Badges/titles with no metadata entry in _PERMANENT_BADGE_LOOKUP /
    _LIVE_TITLE_META (e.g. an old dead-pipeline code like 'hill_king'
    that somehow has a row) are skipped rather than crashing or showing
    a blank line — this card only ever renders codes it has real
    icon/name data for."""
    embed = discord.Embed(title=f"{player['ign']} — Badges", color=discord.Color.gold())

    earned_codes = [e["achievements"]["code"] for e in earned]
    permanent_lines = [
        f"{icon} **{name}**"
        for code, icon, name, _desc in _PERMANENT_BADGES
        if code in earned_codes
    ]
    embed.add_field(
        name="🏅 Earned",
        value="\n".join(permanent_lines) if permanent_lines else "*No badges earned yet — play a match to get started!*",
        inline=False,
    )

    # Seasonal participation badges — separate field from career-threshold
    # badges above (see _SEASONAL_BADGES' comment for why). Only shown at
    # all if the player has at least one, so a brand-new player's card
    # isn't padded with an empty section.
    seasonal_lines = [
        f"{icon} **{name}**"
        for code, icon, name, _desc in _SEASONAL_BADGES
        if code in earned_codes
    ]
    if seasonal_lines:
        embed.add_field(name="🗓️ Seasons Played", value="\n".join(seasonal_lines), inline=False)

    live_lines = [
        f"{_LIVE_TITLE_LOOKUP[t['title_code']][0]} **{_LIVE_TITLE_LOOKUP[t['title_code']][1]}**"
        for t in live_titles
        if t["title_code"] in _LIVE_TITLE_LOOKUP
    ]
    if live_lines:
        embed.add_field(name="👑 Live Titles (can be snatched)", value="\n".join(live_lines), inline=False)

    return embed


def achievements_browse_embed(player: dict, earned: list[dict], live_titles: list[dict]) -> discord.Embed:
    """Full badge catalogue reveal for /achievements' "Browse All Badges"
    button — every permanent badge and live title that exists, with a
    ✅/⬜ earned indicator and a short mobile-friendly description on
    each, split into the same two sections as achievements_card. See
    2026-08-21 mockup discussion for the exact layout this follows."""
    embed = discord.Embed(title=f"All Badges — {player['ign']}", color=discord.Color.gold())

    earned_codes = {e["achievements"]["code"] for e in earned}
    permanent_lines = []
    for code, icon, name, desc in _PERMANENT_BADGES:
        mark = "✅" if code in earned_codes else "⬜"
        permanent_lines.append(f"{mark} {icon} **{name}**\n{desc}")
    embed.add_field(name="🏅 PERMANENT (earn once)", value="\n\n".join(permanent_lines), inline=False)

    held_titles = {t["title_code"] for t in live_titles}
    live_lines = []
    for code, icon, name, desc in _LIVE_TITLES:
        mark = "✅" if code in held_titles else "⬜"
        live_lines.append(f"{mark} {icon} **{name}**\n{desc}")
    embed.add_field(name="👑 LIVE TITLES (can be snatched)", value="\n\n".join(live_lines), inline=False)

    return embed


def player_stats_card(player: dict, weekly: dict[str, dict]) -> discord.Embed:
    """The renamed, locked-field-set /player-stats card (P6, confirmed
    2026-07-19). Always sent ephemeral by the calling command — see
    cogs/stats.py. Fields, exact locked order: Name, Rank, Region, MMR
    (+peak), MVPs, KD, Avg hill/obj time, Avg kills, Total matches,
    Battles (win %). Badges are computed live from weekly_leaders(), not
    stored — see migration_007.

    Rank is derived from mmr_engine.derive_rank(player['mmr']), NOT read
    from player['current_rank']. Found live 2026-07-19: current_rank is
    only ever written by approve_ro3_match at Approve time, so any player
    whose current mmr didn't get there via a real approval (test seeding,
    manual DB edits, leftover values from before a tier-band change) can
    have a stored current_rank that disagrees with what their mmr number
    actually maps to. Same bug, same fix as region_leaderboard() in
    migration_007 — that one derives it in SQL since it's a set-based
    query; here it's a single row, so the existing Python function is
    the simpler fix, no SQL duplication needed.

    Layout (revised 2026-08-20, mobile UI feedback): plain inline
    add_field() pairs, no forced-row-break spacer. The old version
    inserted a zero-width spacer field after every pair specifically to
    force a hard 2-per-row break on desktop (Discord packs inline fields
    by available render width, not add_field() call order — see the
    prior version of this docstring). That trick worked on desktop but
    actively hurt mobile: mobile already renders inline fields 1-per-row
    on its own narrow width, so every spacer added a wasted blank row
    between pairs, roughly doubling the card's scroll length for no
    benefit. Removing the spacer lets Discord's native inline packing
    handle both cases correctly — desktop still gets a clean 2-3-per-row
    layout from the available width, mobile stacks tightly with no dead
    space. Field order/pairing in the code is unchanged; only the
    forced-break spacer is gone."""
    rank, _ = mmr_engine.derive_rank(player["mmr"])
    embed = discord.Embed(
        title=f"{player['ign']} — {rank}",
        color=discord.Color.gold(),
    )

    def _pair(name1, value1, name2, value2):
        embed.add_field(name=name1, value=value1, inline=True)
        embed.add_field(name=name2, value=value2, inline=True)

    _pair("Region", player.get("region", "—"), "MMR", f"{player['mmr']} (peak {player['peak_mmr']})")

    deaths = player["avg_deaths"] or 0
    kd = round(player["avg_kills"] / deaths, 2) if deaths else float(player["avg_kills"])
    _pair("MVPs", str(player["mvp_count"]), "KD", f"{kd:.2f}")

    # avg_kills swapped in for total_assists (2026-08-20 UI feedback) —
    # total_assists was felt to be a less useful at-a-glance number than
    # a per-match kill average. avg_kills is a stored column already
    # computed by recompute_player_career_stats() as
    # total_kills / total_rounds, guarded there by a
    # "case when v_total_rounds > 0" check — so a player with matches
    # but no counted rounds yet (abandoned match, incomplete result
    # entry, etc.) already safely reads 0 here rather than dividing by
    # zero. No new null/zero handling needed on this side.
    _pair("Avg obj time", f"{player['avg_hill_time']}s", "Avg kills", f"{player['avg_kills']:.2f}")

    total = player["total_matches"]
    wr = f"{(player['wins'] / (player['wins'] + player['losses']) * 100):.1f}%" if (player['wins'] + player['losses']) else "—"
    # Renamed 2026-08-20: "Record (rounds)" -> "Battles (win %)" (UI
    # wording feedback). Value format unchanged (still "WW - LL (xx%)").
    _pair("Total matches", str(total), "Battles (win %)", f"{player['wins']}W - {player['losses']}L ({wr})")

    badges = _badge_lines(player["id"], weekly)
    if badges:
        embed.add_field(name="This week", value="\n".join(badges), inline=False)
    return embed


def cs_stats_card(player: dict, season: dict, season_stats: dict | None) -> discord.Embed:
    """Current-season equivalent of player_stats_card (added for
    /cs-stats, 2026-09-11). Same field order/formulas — MVPs, KD, Avg
    obj time, Avg kills, Total matches, Battles (win %) — but every
    number comes from adb.current_season_stats() (SQL RPC, migration_034
    — see database/db.py) instead of the player row's all-time columns.
    No precomputed per-season stat table exists for these fields (unlike
    season_points), so this is computed fresh on every call, same as the
    hof_* Hall of Fame reads.

    peak_mmr is deliberately omitted — it's a lifetime value, not
    season-scoped, and showing it here would misrepresent it as this
    season's peak. Region/current-MMR are also left off for the same
    reason: this card exists specifically to show season-only truth,
    not a repeat of the all-time card with a different title.

    season_stats is None when the player has zero completed matches this
    season (see current_season_stats' docstring) — shown as an explicit
    empty state rather than a card full of zeros, since 0 kills/0
    matches could otherwise read as a data bug rather than "hasn't
    played yet"."""
    season_label = season.get("code") or season.get("name") or "Season"
    embed = discord.Embed(
        title=f"{player['ign']} — {season_label} Stats",
        color=discord.Color.gold(),
    )

    if not season_stats:
        embed.description = "*No completed matches yet this season.*"
        return embed

    def _pair(name1, value1, name2, value2):
        embed.add_field(name=name1, value=value1, inline=True)
        embed.add_field(name=name2, value=value2, inline=True)

    deaths = season_stats["total_deaths"] or 0
    kills = season_stats["total_kills"] or 0
    kd = round(kills / deaths, 2) if deaths else float(kills)
    _pair("MVPs", str(season_stats["mvps"]), "KD", f"{kd:.2f}")

    _pair("Avg obj time", f"{season_stats['avg_hill_time']}s", "Avg kills", f"{season_stats['avg_kills']}")

    total = season_stats["matches"]
    wl_total = season_stats["wins"] + season_stats["losses"]
    wr = f"{(season_stats['wins'] / wl_total * 100):.1f}%" if wl_total else "—"
    _pair("Total matches", str(total), "Battles (win %)", f"{season_stats['wins']}W - {season_stats['losses']}L ({wr})")

    return embed


def rank_progress_card(player: dict, tier: str) -> discord.Embed:
    """/rank-progress initial card (2026-08, revised after live design
    review). Deliberately does NOT show the full ladder up front — locked
    2026-08-15 after the first version (title/MMR/next-tier/full-ladder
    all in one embed) was flagged as demotivating: a brand-new Elite1
    player seeing all 10 tiers stacked above them reads as "how far I
    have to go", not "how close I am". The fix is sequencing, not data:
    this card shows only the near-term win (next-tier distance); the full
    ladder is opt-in via the "View rank ladder" button (see
    RankProgressView in cogs/stats.py and rank_ladder_embed below) — by
    the time a player taps that, they've already gotten the "19 MMR to
    Elite2" dopamine hit, so the same ladder reads as "here's the system"
    rather than "here's the mountain".

    Distance is always to the *immediately next* tier, never the raw gap
    to the top — see mmr_engine.next_tier_progress's docstring.

    `tier` is passed in (not re-derived here) since the caller already
    calls derive_rank once — avoids a second identical call for what's
    cosmetically the same value."""
    embed = discord.Embed(
        description=f"**{player['ign']} — {tier} - {player['mmr']} MMR**\n"
                     f"Peak: {player['peak_rank']} at {player['peak_mmr']} MMR",
        color=discord.Color.blue(),
    )

    progress = mmr_engine.next_tier_progress(player["mmr"])
    if progress:
        next_tier, remaining = progress
        embed.add_field(name="🔼 Next rank", value=f"{remaining} MMR to {next_tier}", inline=False)
    else:
        embed.add_field(name="🔼 Next rank", value="You're at the top tier — Titans.", inline=False)

    return embed


def rank_ladder_embed(player: dict, tier: str) -> discord.Embed:
    """The full-ladder reveal shown after tapping "View rank ladder" on
    the /rank-progress card. Same header/next-rank block as
    rank_progress_card (so the message reads as one continuous card, not
    a jarring swap) plus the full ladder, current tier marked ▶ with a
    "you are here" note. See rank_progress_card's docstring for why this
    is opt-in rather than shown up front."""
    embed = rank_progress_card(player, tier)

    ladder_lines = []
    for _floor, ladder_tier in mmr_engine.tier_ladder():
        if ladder_tier == tier:
            ladder_lines.append(f"▶ {ladder_tier}   ← you are here")
        else:
            ladder_lines.append(f"\u2003{ladder_tier}")
    # Ladder is stored lowest-first in mmr_engine; display highest-first so
    # "climbing" reads top-to-bottom the way a leaderboard does.
    embed.add_field(name="📍 Your path", value="\n".join(reversed(ladder_lines)), inline=False)

    return embed


def leaderboard_embed(players: list[dict], metric_label: str = "MMR") -> discord.Embed:
    # NOTE: zero live callers (confirmed via repo-wide grep, 2026-08-15) —
    # the actually-used leaderboard render is _leaderboard_embed() inside
    # cogs/stats.py, a separate function. Kept for now; current_division
    # removed from the line format since that column was dropped from the
    # players table (migration_016).
    embed = discord.Embed(title=f"🏆 Champion's Queue Leaderboard — {metric_label}", color=discord.Color.purple())
    lines = []
    for i, p in enumerate(players, start=1):
        lines.append(f"**{i}.** {p['ign']} — {p['mmr']} MMR ({p['current_rank']})")
    embed.description = "\n".join(lines) or "No ranked players yet."
    return embed


def comparison_embed(ign: str, previous: dict, latest: dict) -> discord.Embed:
    embed = discord.Embed(title=f"{ign} — Last Match vs Previous", color=discord.Color.teal())

    def row(field, fmt=lambda x: x):
        prev_v = previous.get(field)
        new_v = latest.get(field)
        return f"{fmt(prev_v)} → {fmt(new_v)}"

    embed.add_field(name="Kills", value=row("kills"), inline=True)
    embed.add_field(name="Deaths", value=row("deaths"), inline=True)
    prev_kd = (previous.get("kills") or 0) / max(previous.get("deaths") or 1, 1)
    new_kd = (latest.get("kills") or 0) / max(latest.get("deaths") or 1, 1)
    embed.add_field(name="KD", value=f"{prev_kd:.2f} → {new_kd:.2f}", inline=True)
    embed.add_field(name="Damage", value=row("damage"), inline=True)
    embed.add_field(name="Hill Time", value=row("hill_time"), inline=True)
    embed.add_field(name="MMR Change", value=row("mmr_change"), inline=True)
    return embed


def verification_card(match: dict, round_data: list[dict], extraction: dict, map_name: str) -> discord.Embed:
    """Host-facing verification card. Shows the actual stats (K/D/A,
    Impact, MVP) a host can visually compare against their own
    screenshot — not MMR deltas as the primary content. MMR moves to a
    compact one-line summary at the bottom instead, since a bare list
    of +N MMR values gives the host nothing to verify against; the raw
    stats are what catches an OCR misread. See DECISIONS.md
    (2026-07-18 planning session) for why this replaced the earlier
    MMR-only version.

    RO1 (2026-08): de-looped from the original 3-round ro3_verification_card
    — one round, one screenshot, one field instead of a 3-round loop.
    Signature changed from (round_data, extractions: list, maps: list)
    to (round_data, extraction: single dict, map_name: single str) to
    match. round_data is still the list _prepare_round returns (one
    item, but kept as a list since callers/round_data shape elsewhere
    in match.py still expect list-of-dicts)."""
    embed = discord.Embed(
        title=f"Match {match['match_id']} — Verification",
        description=(
            "Review the round against your own screenshot. Only the Match Host can approve. "
            "**MMR and SP values are proposed** — nothing is applied until Approve is clicked."
        ),
        color=discord.Color.gold(),
    )
    results = round_data[0]["results"] if round_data else []

        # position may be None for admin-match-card's dash-placeholder rows
    # (roster players with no round_results row at all — see
    # cogs/admin.py's match_card). Sort those last within their team
    # rather than crashing on a None-vs-int comparison (found live
    # 2026-08-19: TypeError, '<' not supported between str and int, from
    # an earlier version of match_card that used "—" as the position
    # value instead of None).
    players = sorted(extraction.get("players", []), key=lambda p: (p.get("team") or "", p.get("position") if p.get("position") is not None else 9))
    team_lines = {"A": [], "B": []}
    for p in players:
        ign = str(p.get("ign") or "?")
        position_str = str(p["position"]) if p.get("position") is not None else "—"
        kills = p.get("kills")
        deaths = p.get("deaths")
        assists = p.get("assists")
        kda = f"{kills if kills is not None else '—'}/{deaths if deaths is not None else '—'}/{assists if assists is not None else '—'}"
        impact = p.get("impact")
        impact_str = str(impact) if impact is not None else "—"
        mvp = "  MVP" if p.get("is_mvp") else ""
        team_lines.setdefault(p.get("team"), []).append(
            f"{position_str}  {ign:<16.16} {kda:<10} {impact_str:>4}{mvp}"
        )

    # AFK / mid-match leaver (2026-08, nice-to-have): a synthesized row
    # from _prepare_round's AFK branch has no corresponding entry in
    # extraction["players"] at all — OCR never saw that player, since
    # they weren't on the scoreboard. Without this, the row would just
    # be silently absent from the block above rather than shown as
    # what it is, which could read as a missing/broken card rather
    # than an intentional AFK auto-assignment. verification_card has no
    # roster/IGN lookup available (only match/round_data/extraction/
    # map_name are passed in), so this uses the row's own discord_id
    # (already set by _prepare_round) for a @mention instead.
    for r in results:
        if r.get("afk"):
            team_lines.setdefault(r["team"], []).append(
                f"{r['position']}  {'(AFK — left)':<16.16} {'—/—/—':<10}    —"
            )

    block = f"Team A\n```\n{chr(10).join(team_lines.get('A', [])) or '(no readable rows)'}\n```\n" \
            f"Team B\n```\n{chr(10).join(team_lines.get('B', [])) or '(no readable rows)'}\n```"

    mmr_line = ""
    sp_line = ""
    if results:
        team_a_deltas = "/".join(f"{r['mmr_delta']:+d}" for r in sorted(
            (r for r in results if r["team"] == "A"), key=lambda r: r["position"]))
        team_b_deltas = "/".join(f"{r['mmr_delta']:+d}" for r in sorted(
            (r for r in results if r["team"] == "B"), key=lambda r: r["position"]))
        mmr_line = f"\n*MMR (proposed): A {team_a_deltas}  ·  B {team_b_deltas}*"

        # SP (Season Points) preview — same fixed rule as
        # update_season_points_for_match() in migration_029: win/loss
        # derived from mmr_delta with the MVP bonus stripped first
        # (the same signal recompute_player_career_stats trusts), no
        # per-player breakdown since every winner gets the same +5 and
        # every loser gets the same -3 (no MVP bonus on points) — a
        # single team-level number is the whole story, unlike MMR
        # which varies per position. This is a preview only, same as
        # MMR (proposed) above — nothing is written until Approve.
        def _team_won(team_results: list[dict]) -> bool:
            # Majority of a team's rows will agree on win/loss (they're
            # on the same side), just check the first row's signal.
            r0 = team_results[0]
            base_delta = r0["mmr_delta"] - (5 if r0.get("is_mvp") else 0)
            return base_delta > 0

        team_a_results = [r for r in results if r["team"] == "A" and not r.get("afk")]
        team_b_results = [r for r in results if r["team"] == "B" and not r.get("afk")]
        if team_a_results and team_b_results:
            a_sp = "+5" if _team_won(team_a_results) else "-3"
            b_sp = "+5" if _team_won(team_b_results) else "-3"
            sp_line = f"\n*SP (proposed): A {a_sp}  ·  B {b_sp}*"

    # Mentions don't render inside code fences (where the "(AFK — left
    # match)" placeholder line above lives), so the actual @mention is
    # appended here instead, outside the block, one line per AFK row.
    afk_rows = [r for r in results if r.get("afk")]
    afk_line = ""
    if afk_rows:
        mentions = "  ".join(f"<@{r['discord_id']}>" for r in afk_rows)
        afk_line = f"\n⚠️ Auto-assigned AFK: {mentions}"

    final_score = extraction.get("final_score") or "—"
    embed.add_field(
        name=f"{map_name} ({final_score})",
        value=block + mmr_line + sp_line + afk_line,
        inline=False,
    )
    return embed


def ign_confirmation_embed(
    match: dict,
    match_players: list[dict],
    ign_failures: list[dict],
    unmatched: list[dict],
    screenshot_url: str,
) -> discord.Embed:
    """Rich embed for the IGN confirmation flow. Shows:
    1. What happened (how many IGNs failed)
    2. The OCR-read IGNs that couldn't be resolved
    3. The unmatched roster players (numbered, for the N≥2 modal)
    4. The full roster for context
    5. The screenshot as embed image

    For N=1 the mapping is unambiguous and shown explicitly.
    For N≥2 the numbered unmatched list is what the admin references
    when typing roster numbers in the modal."""
    n = len(ign_failures)
    embed = discord.Embed(
        title=f"Match {match['match_id']} — IGN Confirmation Needed",
        description=(
            f"OCR read the scoreboard but **{n}** player name{'s' if n > 1 else ''} "
            f"couldn't be matched to the roster. All other data (stats, teams, "
            f"score) validated fine — this is an IGN-reading issue only."
        ),
        color=discord.Color.gold(),
    )

    # Unresolved OCR reads
    ocr_lines = "\n".join(f"• `{f.get('ocr_ign', '?')}`" for f in ign_failures)
    embed.add_field(
        name="🔍 OCR Could Not Resolve",
        value=ocr_lines,
        inline=True,
    )

    # Unmatched roster players (numbered for modal reference)
    unmatched_lines = "\n".join(
        f"**{i+1}.** {mp['players']['ign']}  <@{mp['players']['discord_id']}>"
        for i, mp in enumerate(unmatched)
    )
    embed.add_field(
        name="❓ Unmatched Roster Players",
        value=unmatched_lines or "(none)",
        inline=True,
    )

    # For N=1, show the explicit proposed mapping
    if n == 1:
        embed.add_field(
            name="📋 Proposed Mapping",
            value=(
                f"`{ign_failures[0].get('ocr_ign', '?')}` → "
                f"**{unmatched[0]['players']['ign']}** "
                f"<@{unmatched[0]['players']['discord_id']}>"
            ),
            inline=False,
        )

    # Full roster for context (Team Defender / Attacker)
    team_a = [mp for mp in match_players if mp.get("team") == "A"]
    team_b = [mp for mp in match_players if mp.get("team") == "B"]
    roster_a = ", ".join(mp["players"]["ign"] for mp in team_a)
    roster_b = ", ".join(mp["players"]["ign"] for mp in team_b)
    embed.add_field(
        name="🛡️ Team Defender",
        value=roster_a or "(empty)",
        inline=True,
    )
    embed.add_field(
        name="⚔️ Team Attacker",
        value=roster_b or "(empty)",
        inline=True,
    )

    # Screenshot for visual verification
    if screenshot_url:
        embed.set_image(url=screenshot_url)

    if n == 1:
        embed.set_footer(text="Click Confirm if this player is visible in the screenshot.")
    else:
        embed.set_footer(text="Click Map IGNs and enter the roster number for each OCR name.")

    return embed