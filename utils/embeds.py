import discord


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
    embed = discord.Embed(
        title=f"{player['ign']} — {player['current_rank']} {player['current_division']}",
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


def leaderboard_embed(players: list[dict], metric_label: str = "MMR") -> discord.Embed:
    embed = discord.Embed(title=f"🏆 Champion's Queue Leaderboard — {metric_label}", color=discord.Color.purple())
    lines = []
    for i, p in enumerate(players, start=1):
        lines.append(f"**{i}.** {p['ign']} — {p['mmr']} MMR ({p['current_rank']} {p['current_division']})")
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


def ro3_verification_card(match: dict, round_data: list[dict], extractions: list[dict], maps: list[str]) -> discord.Embed:
    """Host-facing verification card. Shows the actual per-round stats
    (K/D/A, Impact, MVP) a host can visually compare against their own
    screenshot — not MMR deltas as the primary content. MMR moves to a
    compact one-line summary at the bottom of each round instead, since
    a bare list of +N MMR values gives the host nothing to verify against;
    the raw stats are what catches an OCR misread. See DECISIONS.md
    (2026-07-18 planning session) for why this replaced the earlier
    MMR-only version."""
    embed = discord.Embed(
        title=f"Match {match['match_id']} — RO3 Verification",
        description=(
            "Review all three rounds against your own screenshots. Only the Match Host can approve. "
            "**MMR values are proposed** — nothing is applied until Approve is clicked."
        ),
        color=discord.Color.gold(),
    )
    results_by_round = {item["round_number"]: item["results"] for item in round_data}

    for round_number, (announced_map, extraction) in enumerate(zip(maps, extractions), start=1):
        results = results_by_round.get(round_number, [])
        delta_by_ign = {row["ign"].strip().lower(): row for row in results}

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

        block = f"Team A\n```\n{chr(10).join(team_lines.get('A', [])) or '(no readable rows)'}\n```\n" \
                f"Team B\n```\n{chr(10).join(team_lines.get('B', [])) or '(no readable rows)'}\n```"

        mmr_line = ""
        if results:
            team_a_deltas = "/".join(f"{r['mmr_delta']:+d}" for r in sorted(
                (r for r in results if r["team"] == "A"), key=lambda r: r["position"]))
            team_b_deltas = "/".join(f"{r['mmr_delta']:+d}" for r in sorted(
                (r for r in results if r["team"] == "B"), key=lambda r: r["position"]))
            mmr_line = f"\n*MMR (proposed): A {team_a_deltas}  ·  B {team_b_deltas}*"

        final_score = extraction.get("final_score") or "—"
        embed.add_field(
            name=f"Round {round_number} — {announced_map} ({final_score})",
            value=block + mmr_line,
            inline=False,
        )
    return embed


def ro3_result_card(match: dict, match_players: list[dict], round_results: list[dict], maps: list[str]) -> discord.Embed:
    """Final result with visible per-round MMR components and match totals."""
    embed = discord.Embed(title=f"Match {match['match_id']} — Result", color=discord.Color.green())
    by_player: dict[int, list[dict]] = {}
    for row in round_results:
        by_player.setdefault(row["player_id"], []).append(row)
    names = {mp["player_id"]: mp["players"]["ign"] for mp in match_players}
    for player_id, rows in sorted(by_player.items(), key=lambda item: names.get(item[0], "")):
        lines = []
        for row in sorted(rows, key=lambda item: item["round_number"]):
            bonus = " (+5 MVP)" if row.get("is_mvp") else ""
            lines.append(f"{maps[row['round_number'] - 1]} — {row['mmr_delta']:+d}{bonus}")
        total = sum(row["mmr_delta"] for row in rows)
        embed.add_field(name=names.get(player_id, f"Player {player_id}"), value="\n".join(lines) + f"\n**Total: {total:+d} MMR**", inline=False)
    return embed