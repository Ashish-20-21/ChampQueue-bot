"""Content and embed builders for the interactive /post-info guide.

Pure functions: no Discord I/O, no database, no network. Every page is built from
static text in memory, so showing a page costs nothing but a few microseconds of CPU.

Anything that can change in production (limits, channel ids) is read from config AT
RENDER TIME, so a page can never contradict the bot's real settings: change
HOST_REPLACE_LIMIT in the environment and the guide follows after a restart.

Pages are addressed by a short key that travels inside each button's custom_id
(see cogs/info.py), which is why the guide needs no per-user state at all.
"""
from __future__ import annotations

import discord

import config

GOLD = 0xF5B301
GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB

HOME = "home"
STEP_KEYS = ("s1", "s2", "s3", "s4", "s5", "s6", "s7")
ORDER = STEP_KEYS + ("mmr", "rules", "cmds")          # the order of Previous / Next
PAGE_KEYS = frozenset(ORDER) | {HOME}

# key -> (button label, emoji, button colour)
BUTTONS = {
    "s1": ("Verify", "🆔", "primary"),
    "s2": ("Register", "📝", "primary"),
    "s3": ("Queue", "🎮", "primary"),
    "s4": ("Match", "🏁", "primary"),
    "s5": ("Room", "🔑", "primary"),
    "s6": ("Result", "📸", "primary"),
    "s7": ("Approve", "✅", "primary"),
    "mmr": ("MMR & points", "📊", "success"),
    "rules": ("Rules", "📜", "danger"),
    "cmds": ("Commands", "📖", "secondary"),
}


# ── small helpers ────────────────────────────────────────────────────────────
def _ch(channel_id, fallback: str) -> str:
    """A clickable #mention when the channel id is configured, else a plain name."""
    return f"<#{channel_id}>" if channel_id else f"**{fallback}**"


def _span(seconds: int) -> str:
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} hour" + ("" if h == 1 else "s")
    m = max(1, round(seconds / 60))
    return f"{m} minute" + ("" if m == 1 else "s")


def _footer(embed: discord.Embed, key: str) -> discord.Embed:
    if key == HOME:
        embed.set_footer(text="ChampQueue guide · tap a button · only you see the answer")
    else:
        embed.set_footer(text=f"ChampQueue guide · page {ORDER.index(key) + 1} of {len(ORDER)}")
    return embed


# ── pages ────────────────────────────────────────────────────────────────────
def _home() -> discord.Embed:
    e = discord.Embed(
        title="🏆 ChampQueue — how it works",
        description=(
            "Ranked Hardpoint scrims for CODM players.\n"
            "**Tap a button** to read the details. The answer is private to you, "
            "and the buttons never expire.\n\n"
            "**The flow in 7 small steps**\n"
            "`1` 🆔 **Verify** — send your profile screenshot\n"
            "`2` 📝 **Register** — `/register` with your UID\n"
            "`3` 🎮 **Queue** — press *Join Queue*, wait for 10 players\n"
            "`4` 🏁 **Match** — the host starts it, teams and map are set\n"
            "`5` 🔑 **Room** — the host shares the room code\n"
            "`6` 📸 **Result** — the host uploads the scoreboard\n"
            "`7` ✅ **Approve** — MMR and points update\n\n"
            "**Also**\n"
            "📊 **MMR & points** · 📜 **Rules** · 📖 **Commands**"
        ),
        color=GOLD,
    )
    return _footer(e, HOME)


def _s1() -> discord.Embed:
    e = discord.Embed(
        title="🆔 Step 1 — Get verified",
        description=(
            f"Start here. Upload a **screenshot of your in-game profile** in {_ch(config.INFO_VERIFY_CHANNEL_ID, 'the verify channel')}.\n\n"
            "Once you are verified, continue with **Step 2 — Register**."
        ),
        color=GOLD,
    )
    return _footer(e, "s1")


def _s2() -> discord.Embed:
    e = discord.Embed(
        title="📝 Step 2 — Register",
        description=(
            f"Run `/register` **once** in {_ch(config.INFO_REGISTER_CHANNEL_ID, 'the register channel')}.\n\n"
            "**You will enter**\n"
            "▸ your **19-digit COD UID**, exactly as shown in-game\n"
            "▸ your current **IGN**\n"
            "▸ your **region** (EU/AF, NA/Latam, India/ME or Japan)\n"
            "▸ *Organization* (optional)\n\n"
            "A valid UID is approved instantly. A wrong UID saves nothing — just run `/register` again. "
            "Check your status any time with `/whoami`."
        ),
        color=GOLD,
    )
    e.add_field(
        name="Renamed in-game?",
        value=(f"Use `/ign-change` (up to **{config.IGN_CHANGE_LIMIT}** times per week). "
               "Your stats follow your UID, so you lose nothing."),
        inline=False,
    )
    return _footer(e, "s2")


def _s3() -> discord.Embed:
    e = discord.Embed(
        title="🎮 Step 3 — Join the queue",
        description=(
            f"Go to {_ch(config.INFO_QUEUE_CHANNEL_ID, 'the queue channel')} and press **Join Queue**.\n\n"
            "▸ Every queue feeds **one global pool**: one MMR, one rank ladder, one leaderboard.\n"
            "▸ The **India/ME-only** queue needs the India/ME region.\n"
            "▸ Changed your mind? Press **Leave Queue**.\n"
            "▸ At **10 / 10** a **Start Match** button appears.\n\n"
            "See who is waiting with `/queue-status`."
        ),
        color=GOLD,
    )
    return _footer(e, "s3")


def _s4() -> discord.Embed:
    e = discord.Embed(
        title="🏁 Step 4 — The match starts",
        description=(
            "Whoever presses **Start Match** becomes the **Host** and runs the match until it is approved.\n\n"
            "**The bot does this for you**\n"
            "▸ splits the 10 players into **2 teams of 5** (Defender and Attacker)\n"
            "▸ picks the **map**\n"
            "▸ opens a **private match channel** with the host, the teams, the map and the operator-skill panels\n\n"
            "Teams are balanced by skill once all 10 players have **10+ matches**; before that they are random."
        ),
        color=GOLD,
    )
    e.add_field(
        name="Your job",
        value="Tap **one operator skill** on your own team's panel. First tap wins — teammates can't take the same one.",
        inline=False,
    )
    e.add_field(name="Bad map?", value="The host can reroll it with `/host-roll-map`.", inline=False)
    return _footer(e, "s4")


def _s5() -> discord.Embed:
    e = discord.Embed(
        title="🔑 Step 5 — Room code",
        description=(
            "**Host only.** Create the room in-game and share the code in your match channel:\n"
            "```\n+rc241905\n```"
            "(or use `/rc`). Typed it wrong? Fix it with `+urc<code>`.\n\n"
            "Then everybody joins and plays **one Hardpoint round** on the announced map."
        ),
        color=GOLD,
    )
    e.add_field(
        name="Problems?",
        value=(f"▸ A player is missing: the host swaps in a sub with `/host-replace-player` "
               f"(up to **{config.HOST_REPLACE_LIMIT}** per match).\n"
               "▸ Someone went AFK: anyone can run `/afk @player`."),
        inline=False,
    )
    return _footer(e, "s5")


def _s6() -> discord.Embed:
    min_side = getattr(config, "CROWN_MIN_IMAGE_SIDE", 0)
    tips = (
        "▸ show the **full scoreboard**: both teams, all rows, the **Impact** column and the **👑 crown**\n"
        "▸ nothing covering it (loading bar, notification)\n"
        "▸ upload the **original screenshot**, not a tiny copy"
    )
    if min_side:
        tips += f" — images smaller than **{min_side}px** need staff to confirm the crown"
    e = discord.Embed(
        title="📸 Step 6 — Upload the result",
        description=(
            "**Host only.** After the game, upload **one scoreboard screenshot**: type `+result` in your "
            "match channel with the image attached (or use `/match-submit`).\n\n"
            "**For a clean read**\n" + tips
        ),
        color=GOLD,
    )
    e.add_field(
        name="What happens next",
        value=("The bot reads the screenshot. If a name or the crown is unclear, staff get a quick confirm "
               "prompt — nothing for you to do. Result looks wrong? The host runs `/correction-result`."),
        inline=False,
    )
    return _footer(e, "s6")


def _s7() -> discord.Embed:
    minutes = max(1, config.APPROVAL_TIMEOUT_SECONDS // 60)
    e = discord.Embed(
        title="✅ Step 7 — Approve and rewards",
        description=(
            f"The bot posts a **verification card** in {_ch(config.RESULT_APPROVAL_CHANNEL_ID, 'the result-approval channel')}: "
            "both teams' stats, the proposed **MMR** and **Season Points**, and who holds the **👑 Impact crown**. "
            "Nothing is applied yet.\n\n"
            f"The host presses **Approve** — or it **auto-approves after {minutes} minutes**.\n\n"
            "Then your **MMR, rank and Season Points update together** and the leaderboard refreshes in "
            f"{_ch(config.INFO_LEADERBOARD_CHANNEL_ID, 'the leaderboard channel')}.\n\n"
            "The match channel is deleted about 15 minutes later."
        ),
        color=GOLD,
    )
    return _footer(e, "s7")


def _mmr() -> discord.Embed:
    e = discord.Embed(
        title="📊 MMR & Points",
        description=(
            "Your MMR change depends on your **position on your team's scoreboard** and the **result**.\n"
            "```\n"
            "Position    1st   2nd   3rd   4th   5th\n"
            "Win         +9    +8    +6    +4    +3\n"
            "Loss        -3    -4    -6    -8    -9\n"
            "```"
        ),
        color=GREEN,
    )
    e.add_field(
        name="👑 Impact crown: +5",
        value=("The player holding the **crown icon** next to the Impact number gets **+5 on top** — "
               "in **each team**, in a win **or** a loss, from **any position**.\n"
               "Impact rewards **objective play** as well as kills, so play the point."),
        inline=False,
    )
    e.add_field(name="Good to know", value="MMR never goes below **0**.", inline=False)
    e.add_field(
        name="Ranks",
        value=("Elite1 `0` · Elite2 `201` · PRO1 `401` · PRO2 `601` · Master1 `801` · Master2 `1001` · "
               "Grandmaster1 `1201` · Grandmaster2 `1401` · Legendary1 `1601` · Legendary2 `1801` · Titans `2001+`"),
        inline=False,
    )
    e.add_field(
        name="Season Points (SP)",
        value=("**+5** for a win, **−3** for a loss per approved match, never below 0. "
               f"Shields change how a match scores for a limited time — see {_ch(config.SHIELD_CHANNEL_ID, 'the shield channel')}."),
        inline=False,
    )
    return _footer(e, "mmr")


def _rules() -> discord.Embed:
    e = discord.Embed(
        title="📜 Rules & Penalties",
        description="All players must strictly follow the **CODM World Championship Rules 2026**.",
        color=RED,
    )
    e.add_field(
        name="⚔️ In-game rule violations",
        value="Immediate point deduction, **no prior warning**. Exceptions only at staff discretion for minor, well-explained infractions.",
        inline=False,
    )
    e.add_field(
        name="🚪 Match abandonment",
        value=("Disconnecting or leaving mid-match results in a point deduction. A valid reason may reduce it to a "
               "warning. Frequent leavers face a **matchmaking ban**."),
        inline=False,
    )
    e.add_field(
        name="🚫 Leaving mid-match",
        value="Strictly prohibited. Report the player immediately with valid proof (screenshot or video) so the report can be processed.",
        inline=False,
    )
    e.add_field(
        name="⚠️ Penalty",
        value=("**−10 points.** For each of the cases below you get **1 warning**; from the **2nd time**, MMR and SP "
               "are deducted by **10**:\n"
               "1. a player is reported AFK\n"
               "2. a player posts fake or modified results\n"
               "3. a player violates server rules or misbehaves, even in game — you may get a **server ban**"),
        inline=False,
    )
    return _footer(e, "rules")


def _cmds() -> discord.Embed:
    report_limit = config.REPORT_COOLDOWN_USES
    window = _span(config.REPORT_COOLDOWN_WINDOW_SECONDS)
    e = discord.Embed(
        title="📖 Commands",
        description="Everything you can use. Commands marked **host** are for the match host only.",
        color=BLUE,
    )
    e.add_field(
        name="▶ Getting started",
        value=(f"`/register` — sign up with your 19-digit COD UID and IGN ({_ch(config.INFO_REGISTER_CHANNEL_ID, 'register')})\n"
               "`/whoami` — check your registration status"),
        inline=False,
    )
    e.add_field(
        name="▶ Queue & matches",
        value=(f"Queue panel buttons: **Join Queue** / **Leave Queue** / **Start Match** (appears when 10 players are in) — "
               f"{_ch(config.INFO_QUEUE_CHANNEL_ID, 'queue')}\n"
               "`/queue-status` — who is in the queue"),
        inline=False,
    )
    e.add_field(
        name="▶ Your stats",
        value=("`/player-stats` — overall stats (yours or another player's)\n"
               "`/cs-stats` — stats for the current season\n"
               "`/rank-progress` — how close you are to your next rank\n"
               "`/achievements` — your badges and current titles\n"
               "`/compare-last-match` — your latest match vs the one before\n"
               f"Use them in {_ch(config.INFO_STATS_CHANNEL_ID, 'the stats channel')}"),
        inline=False,
    )
    e.add_field(
        name="▶ Change your IGN",
        value=(f"`/ign-change` — change your in-game name, up to **{config.IGN_CHANGE_LIMIT}** times per week "
               f"(need more? tag an admin or mod) — {_ch(config.IGN_CHANGE_CHANNEL_ID, 'ign-change')}"),
        inline=False,
    )
    e.add_field(
        name="▶ Problem with a player? (inside your match channel only)",
        value=("`/afk` — report someone who is AFK or not responding\n"
               "`/report` — report anything else (wrong operator, cheating, toxicity) and say what happened in your own words\n"
               f"Limit: **{report_limit}** reports per **{window}** in total (`/afk` and `/report` share it)."),
        inline=False,
    )
    e.add_field(
        name="▶ Match host only",
        value=("`+rc<code>` — share the room code (or `/rc`)\n"
               "`+urc<code>` — fix a wrong room code\n"
               "`+result` — upload the result screenshot in your match channel (or `/match-submit`)\n"
               "`/host-roll-map` — reroll the map\n"
               f"`/host-replace-player` — replace an unresponsive player (**{config.HOST_REPLACE_LIMIT}** uses per match)\n"
               "`/correction-result` — flag a problem with the result"),
        inline=False,
    )
    e.add_field(
        name="▶ Shield station",
        value=f"Buttons for the shield breakdown — {_ch(config.SHIELD_CHANNEL_ID, 'shield')}",
        inline=False,
    )
    return _footer(e, "cmds")


_BUILDERS = {HOME: _home, "s1": _s1, "s2": _s2, "s3": _s3, "s4": _s4, "s5": _s5, "s6": _s6, "s7": _s7,
             "mmr": _mmr, "rules": _rules, "cmds": _cmds}


def build_embed(key: str) -> discord.Embed:
    """The embed for a page key. An unknown key (a button from an older version
    of the guide) falls back to the home page instead of failing."""
    return _BUILDERS.get(key, _home)()


def neighbours(key: str) -> tuple[str | None, str | None]:
    """(previous, next) page keys in reading order; None at either end."""
    if key not in ORDER:
        return None, None
    i = ORDER.index(key)
    return (ORDER[i - 1] if i > 0 else None, ORDER[i + 1] if i < len(ORDER) - 1 else None)
