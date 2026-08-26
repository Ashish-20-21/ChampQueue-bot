"""
Matchmaking service: turns 10 queued players into two balanced 5-player
teams, and decides whether we're still in the "bootstrap" (random) phase
or the analysis-driven phase.

Bootstrap cutover rule (see config.BOOTSTRAP_MATCH_THRESHOLD /
BOOTSTRAP_MIN_ELIGIBLE_POOL): calendar time is not a reliable proxy for
"enough data exists", so we gate on match counts instead of days.
A given match runs in analysis mode only if ALL 10 players in that
queue pop have already reached the match threshold; otherwise it's a
bootstrap (random) match, and it still counts toward every player's
threshold progress.

Analysis-mode split (2026-08): exhaustive evaluation of all C(10,5)=252
possible 5-player splits, scored on a composite metric (MMR-dominant).
Wired up 2026-08 — previously balance_teams() computed a snake-draft
split every match but cogs/queue.py discarded the result and used
even-odd join-order instead (see DECISIONS.md "Team split: join-order
vs MMR-balanced" — flagged there as "a real gap, not intentional").
Historical replay against 145 real match pops confirmed even-odd
produced a mean team-MMR gap of ~116-262 depending on measurement
method (see session notes); the true optimal split for those same
pops averaged a 3-10 point gap. Exhaustive search over 252 splits is
sub-millisecond (benchmarked ~0.39ms), completely negligible against
the >1s of Discord API calls (channel/VC creation) that follow team
formation in cogs/queue.py's _start_match_flow.

Randomization (2026-08): rather than always picking the single
mathematically-best split, we collect every split within
config.TEAM_SPLIT_EPSILON composite-points of the true optimum and
pick uniformly at random among them. This exists because a fully
deterministic split means the same 10 players (a common occurrence in
a small community that queues together repeatedly) would get the
identical team lineup every time — swapping "random, sometimes
lopsided" for "predictable, always the same two crews" is its own
complaint waiting to happen. Every candidate in the pool is
near-optimal by construction, so this never trades away real balance
for variety. Epsilon=10 was chosen by replaying it against all 145
real historical match pops: every single pop produced at least 2
candidate splits (never a de-facto single option), median 8 candidates
per pop, and the randomly-sampled diff stayed at a 7-14 point median/
mean — negligible against this project's MMR scale (players observed
ranging roughly 0-990 composite).

Uncertainty discount (2026-08): a player who just crossed
BOOTSTRAP_MATCH_THRESHOLD has far less real signal behind their stats
than a 90+ match veteran, but _composite_score() alone can't tell the
difference — it takes every player's numbers at face value regardless
of sample size. Same problem every competitive matchmaker has to
solve (Rainbow Six Siege, OpenSkill, TrueSkill all track a skill
estimate AND a separate confidence/uncertainty value). Solved here via
_uncertainty_discount(): a per-player deduction from their composite
score, based purely on total_matches, that shrinks as they play more
— never based on whether their individual stats "look consistent",
since an earlier version of this (flagging players whose MMR and KD
disagreed with each other) ended up penalizing the server's most
active veteran hardest, precisely because a large sample size makes a
real skill gap between two metrics look bigger than a small sample's
noise does. That's backwards — more data should mean more trust.
Match-count-only avoids that trap by construction. Calibrated against
197 real historical match pops (base=50, half_life=20): smallest
average-balance cost of every combination tested (+0.53 pts over the
no-discount baseline of 6.87), while still giving a 10-match player a
~40 point discount and a 97-match veteran only ~21 — proportionate at
both ends, not a cliff and not a token gesture.
"""

from __future__ import annotations
import asyncio
import itertools
import random
from typing import Any

import config
from database.db import db, adb


async def is_bootstrap_match(player_ids: list[int]) -> bool:
    """A match runs in random/bootstrap mode unless every player already
    has enough completed matches AND the overall graduated pool is large
    enough for analysis to be meaningful."""
    graduated = [
        pid for pid in player_ids
        if await adb.player_completed_match_count(pid) >= config.BOOTSTRAP_MATCH_THRESHOLD
    ]
    if len(graduated) < len(player_ids):
        return True  # someone in this pop hasn't graduated yet
    # Everyone in this pop has graduated — but also require a healthy
    # overall pool so early "analysis" isn't based on 10 people total.
    pool_res = await asyncio.to_thread(
        lambda: db.client.table("players").select("id", count="exact").eq("status", "approved").execute()
    )
    return (pool_res.count or 0) < config.BOOTSTRAP_MIN_ELIGIBLE_POOL


def _composite_score(player: dict) -> float:
    """Single composite score used ONLY for balancing — not the same
    thing as MMR, though MMR is the dominant input (~75% of the score
    for a typical player, by design — the secondary signals exist to
    differentiate players whose MMR is similar but whose actual play
    isn't, and to break ties, not to override MMR).

    Formula locked 2026-08 after reviewing real player data (267
    players, MMR range 0-990 composite): mmr + win_rate*100 +
    kd_ratio*40 + avg_hill_time*1.5 + mvp_rate*60.
    """
    mmr = player.get("mmr", 200)  # matches players.mmr's default (200 as of 2026-07-30 global-transition reset, see migration_012)
    total = player.get("total_matches", 0)
    win_rate = (player.get("wins", 0) / total) if total > 0 else 0.0
    avg_kills = float(player.get("avg_kills", 0) or 0)
    avg_deaths = float(player.get("avg_deaths", 0) or 0)
    kd_ratio = avg_kills / avg_deaths if avg_deaths > 0 else avg_kills  # avoid div/0; a 0-death player's KD is just their kill count
    mvp_rate = (player.get("mvp_count", 0) / total) if total > 0 else 0.0
    avg_hill_time = float(player.get("avg_hill_time", 0) or 0)
    return mmr + (win_rate * 100) + (kd_ratio * 40) + (avg_hill_time * 1.5) + (mvp_rate * 60)


def _uncertainty_discount(player: dict) -> float:
    """How much to discount a player's composite score before
    balancing, based purely on how many matches they've played — not
    on whether their stats "look right." A newer graduated player's
    numbers are less proven than a veteran's, so they get a bigger
    discount; this shrinks toward zero as match count grows, never
    fully reaching it.

    Deliberately does NOT look at whether a player's individual stats
    agree with each other (an earlier version of this tried exactly
    that — flagging players whose MMR and KD disagreed — but it ended
    up penalizing high-performing veterans the hardest, since a large
    sample size makes a real skill gap between two metrics look like
    a bigger "disagreement" than a small sample's noise does. That's
    backwards: more data should mean MORE trust, not less. Match-count
    alone avoids that trap entirely — a 97-match player gets one of
    the smallest discounts in the whole pool, by construction, no
    matter how unusual their stats look.)

    Calibration: see config.TEAM_SIGMA_BASE / TEAM_SIGMA_HALF_LIFE.
    """
    total = player.get("total_matches", 0)
    return config.TEAM_SIGMA_BASE / ((total / config.TEAM_SIGMA_HALF_LIFE) + 1) ** 0.5


def balance_teams(queued_players: list[dict], bootstrap: bool) -> dict[str, Any]:
    """
    queued_players: list of player dicts (must include id, mmr, wins,
    total_matches, avg_kills, avg_deaths, avg_hill_time, mvp_count).
    Returns {"team_a": [...], "team_b": [...]}

    Bootstrap: random shuffle — noisy/insufficient data shouldn't be
    trusted for balancing (see module docstring + DECISIONS.md).

    Analysis mode: exhaustive C(10,5)=252-split search on
    (composite score - uncertainty discount), epsilon-bounded
    randomization among near-optimal splits (see module docstring for
    the epsilon=10 and sigma calibration). Which of the two resulting
    groups becomes Defender (team_a) vs Attacker (team_b) is a coin
    flip — nothing about the split computation itself should create a
    systematic side bias.
    """
    assert len(queued_players) == config.QUEUE_SIZE, "matchmaking requires exactly 10 players"

    players = list(queued_players)

    if bootstrap:
        random.shuffle(players)
        team_a = players[:config.TEAM_SIZE]
        team_b = players[config.TEAM_SIZE:]
    else:
        scores = [_composite_score(p) - _uncertainty_discount(p) for p in players]
        best_diff = float("inf")
        all_splits: list[tuple[float, tuple[int, ...], tuple[int, ...]]] = []
        for combo in itertools.combinations(range(10), 5):
            rest = tuple(i for i in range(10) if i not in combo)
            sum_a = sum(scores[i] for i in combo)
            sum_b = sum(scores[i] for i in rest)
            diff = abs(sum_a - sum_b)
            all_splits.append((diff, combo, rest))
            if diff < best_diff:
                best_diff = diff

        near_optimal = [s for s in all_splits if s[0] <= best_diff + config.TEAM_SPLIT_EPSILON]
        _, group_1_idx, group_2_idx = random.choice(near_optimal)

        group_1 = [players[i] for i in group_1_idx]
        group_2 = [players[i] for i in group_2_idx]
        if random.random() < 0.5:
            team_a, team_b = group_1, group_2
        else:
            team_a, team_b = group_2, group_1

    return {
        "team_a": team_a,
        "team_b": team_b,
    }


async def pick_map_candidates(team_a_ids: list[int], team_b_ids: list[int], bootstrap: bool,
                               n: int = 1) -> list[str]:
    """
    Pick n candidate maps for the vote. In bootstrap mode: pure random.

    RO1 (2026-08): default n changed from 3 to 1 - Global now plays a
    single Hardpoint round per match, not three. The call site in
    queue.py's _start_match_flow() passes n=1 explicitly either way,
    but the default is kept consistent with actual usage rather than
    left pointing at the old RO3 value.

    Analysis mode is TEMPORARILY DISABLED (falls back to the same random
    pick as bootstrap) — found live 2026-07-20, crashing every match once
    a roster crosses BOOTSTRAP_MATCH_THRESHOLD: this queried matches.map
    and matches.winner_team, both pre-RO3 columns that don't exist on the
    live schema anymore (maps live in matches.map_pool as an array now;
    there's no single-match winner_team since RO3 counts wins/losses at
    the round level — see recompute_player_career_stats in
    migration_006_p6_full.sql for the correct current pattern). This
    function was apparently never actually exercised until a real roster
    crossed the bootstrap threshold for the first time tonight.

    Real fix (not done here — this is a stop-the-bleeding fix, not a
    rebuild) needs rewriting the balance heuristic against
    match_round_results + matches.map_pool instead of the dead columns.
    Flagged as real follow-up work, not silently deferred — random
    selection is a safe, correct fallback in the meantime (bootstrap
    mode already proves random is an acceptable map-pick strategy), just
    not the smarter balanced pick that was originally intended here.
    """
    return random.sample(config.HARDPOINT_MAPS, k=min(n, len(config.HARDPOINT_MAPS)))