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


def ro3_verification_card(match: dict, round_data: list[dict]) -> discord.Embed:
    """Host-facing verification card: one independently readable block/map."""
    embed = discord.Embed(
        title=f"Match {match['match_id']} — RO3 Verification",
        description=(
            "Review all three rounds. Only the Match Host can approve this result. "
            "**MMR values below are proposed** — nothing is applied to anyone's actual "
            "MMR or the leaderboard until Approve is clicked."
        ),
        color=discord.Color.gold(),
    )
    for round_info in sorted(round_data, key=lambda item: item["round_number"]):
        lines = []
        for row in sorted(round_info["results"], key=lambda item: (item["team"], item["position"])):
            mvp = " MVP" if row.get("is_mvp") else ""
            delta = row["mmr_delta"]
            lines.append(f"Team {row['team']} #{row['position']} — <@{row['discord_id']}>: {delta:+d} MMR{mvp}")
        embed.add_field(
            name=f"Round {round_info['round_number']} — {round_info['map_name']} ({round_info['final_score']})",
            value="\n".join(lines) or "No readable player rows.",
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