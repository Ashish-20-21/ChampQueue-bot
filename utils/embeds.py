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
