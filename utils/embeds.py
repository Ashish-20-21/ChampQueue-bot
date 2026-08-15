import discord

from services import mmr_engine


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


def player_stats_card(player: dict, weekly: dict[str, dict]) -> discord.Embed:
    """The renamed, locked-field-set /player-stats card (P6, confirmed
    2026-07-19). Always sent ephemeral by the calling command — see
    cogs/stats.py. Fields, exact locked order: Name, Rank, Region, MMR
    (+peak), MVPs, KD, Avg hill/obj time, Total assists, Total matches,
    Record (W-L). Badges are computed live from weekly_leaders(), not
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

    Layout: 2 fields per row, FORCED via an invisible zero-width spacer
    field after every pair. Discord's client packs inline fields
    greedily based on available render width, not on add_field() call
    order — three short fields (e.g. Region/MMR/MVPs) will happily share
    one row on a wide screen even if they were added as separate pairs
    in code. Confirmed live 2026-07-19: the "2 per row" fix in the
    previous version still rendered as 3-then-3 on a real Discord
    client. A spacer field with a zero-width-space value and no name
    forces a hard row break after each real pair, which is the only
    reliable way to control this without going non-inline (which would
    stack everything in one column instead)."""
    rank, _ = mmr_engine.derive_rank(player["mmr"])
    embed = discord.Embed(
        title=f"{player['ign']} — {rank}",
        color=discord.Color.gold(),
    )

    def _pair(name1, value1, name2, value2):
        embed.add_field(name=name1, value=value1, inline=True)
        embed.add_field(name=name2, value=value2, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # forces row break

    _pair("Region", player.get("region", "—"), "MMR", f"{player['mmr']} (peak {player['peak_mmr']})")

    deaths = player["avg_deaths"] or 0
    kd = round(player["avg_kills"] / deaths, 2) if deaths else float(player["avg_kills"])
    _pair("MVPs", str(player["mvp_count"]), "KD", f"{kd:.2f}")

    _pair("Avg obj time", f"{player['avg_hill_time']}s", "Total assists", str(player["total_assists"]))

    total = player["total_matches"]
    wr = f"{(player['wins'] / (player['wins'] + player['losses']) * 100):.1f}%" if (player['wins'] + player['losses']) else "—"
    _pair("Total matches", str(total), "Record (rounds)", f"{player['wins']}W - {player['losses']}L ({wr})")

    badges = _badge_lines(player["id"], weekly)
    if badges:
        embed.add_field(name="This week", value="\n".join(badges), inline=False)
    return embed


def rank_progress_card(player: dict, tier: str) -> discord.Embed:
    """/rank-progress card (2026-08). Two deliberate product calls, not
    simplifications:

    1. Distance shown is always to the *immediately next* tier, never the
       raw gap to peak/top tier — a player at 190 MMR sees "19 MMR to
       Elite2", not a discouraging "1811 to Legendary2". See
       mmr_engine.next_tier_progress's docstring.
    2. The full ladder is shown so the player has (near-term progress) +
       (whole-ladder context) in one place, current tier marked with ▶.
       Ladder is intentionally NOT MMR-annotated per rung beyond the
       player's own row — showing every tier's floor value competes with
       the single distance-to-next number for attention; the ladder's job
       here is "where am I", not "here's the full band table" (that's what
       the leaderboard/derive_rank docstring is for).

    `tier` is passed in (not re-derived here) since the caller already
    calls derive_rank once for the title bar — avoids a second identical
    call for what's cosmetically the same value."""
    embed = discord.Embed(
        title=f"{player['ign']} — {tier}",
        description=f"{player['mmr']} MMR",
        color=discord.Color.blue(),
    )

    progress = mmr_engine.next_tier_progress(player["mmr"])
    if progress:
        next_tier, remaining = progress
        embed.add_field(
            name="Next rank",
            value=f"**{remaining} MMR** to {next_tier}",
            inline=False,
        )
    else:
        embed.add_field(name="Next rank", value="You're at the top tier — Titans.", inline=False)

    ladder_lines = []
    for _floor, ladder_tier in mmr_engine.tier_ladder():
        marker = "▶ " if ladder_tier == tier else "\u2003"  # em-space to align non-current rows
        ladder_lines.append(f"{marker}{ladder_tier}")
    # Ladder is stored lowest-first in mmr_engine; display highest-first so
    # "climbing" reads top-to-bottom the way a leaderboard does.
    embed.add_field(name="Rank ladder", value="\n".join(reversed(ladder_lines)), inline=False)

    embed.set_footer(text=f"Peak: {player['peak_rank']} at {player['peak_mmr']} MMR")
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
            "**MMR values are proposed** — nothing is applied until Approve is clicked."
        ),
        color=discord.Color.gold(),
    )
    results = round_data[0]["results"] if round_data else []

    players = sorted(extraction.get("players", []), key=lambda p: (p.get("team"), p.get("position", 9)))
    team_lines = {"A": [], "B": []}
    for p in players:
        ign = str(p.get("ign") or "?")
        kda = f"{p.get('kills', '?')}/{p.get('deaths', '?')}/{p.get('assists', '?')}"
        impact = p.get("impact")
        impact_str = str(impact) if impact is not None else "—"
        mvp = "  MVP" if p.get("is_mvp") else ""
        team_lines.setdefault(p.get("team"), []).append(
            f"{p.get('position', '?')}  {ign:<16.16} {kda:<10} {impact_str:>4}{mvp}"
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
    if results:
        team_a_deltas = "/".join(f"{r['mmr_delta']:+d}" for r in sorted(
            (r for r in results if r["team"] == "A"), key=lambda r: r["position"]))
        team_b_deltas = "/".join(f"{r['mmr_delta']:+d}" for r in sorted(
            (r for r in results if r["team"] == "B"), key=lambda r: r["position"]))
        mmr_line = f"\n*MMR (proposed): A {team_a_deltas}  ·  B {team_b_deltas}*"

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
        value=block + mmr_line + afk_line,
        inline=False,
    )
    return embed