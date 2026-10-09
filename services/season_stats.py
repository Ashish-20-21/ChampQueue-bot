"""Season Recap and extra Hall of Fame numbers, computed in Python from tables
that already exist. Needs NO SQL migration and NO new database function, so
/admin-dispatch works as it is, whoever runs it.

Why this exists (2026-10-09): the recap used the season_recap_stats() SQL
function, which exists in two versions (bigint and integer) once migration_038
has run next to migration_026. PostgREST cannot choose between them (PGRST203),
and fixing that needed someone to run SQL by hand. Reading the rows and adding
them up here removes that dependency. Pure functions, easy to test.

Counting rules (same as migration_026 and the hof_* functions):
  * only matches with status = 'completed' count;
  * a player's rounds are folded into one row per match first, so an old
    3-round match counts once;
  * a "real" match is one that has recorded player stats.

Every extra number is best-effort: if its data is missing or odd it is simply
left out. Nothing here is allowed to take the whole dispatch down.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("champions_queue")

IST = timezone(timedelta(hours=5, minutes=30))
PAGE_SIZE = 1000                 # PostgREST returns at most 1000 rows per request
CONSISTENT_MIN_MATCHES = 20      # win rate over fewer games than this is luck, not consistency
MIN_MATCHES_FLOOR = 8            # same sample floor as migration_024
MIN_STREAK_TO_SHOW = 3           # a 2-game streak is not worth a trophy
MIN_COVERAGE = 0.9              # an extra needs data for at least 90% of the season, or it is left out
BONUS_POINTS = 5                 # the +5 MMR (MVP tag before 2026-10-06, Impact crown after)

_SCORE_RE = re.compile(r"\s*(\d+)\s*[-:]\s*(\d+)\s*")


# --------------------------------------------------------------------------
# Loading (the only part that touches the database)
# --------------------------------------------------------------------------
def _fetch_all(build, order: tuple[str, ...] = ("id",)) -> list[dict]:
    """Read every page of a query. `build` returns a FRESH query each call.
    Ordered by a unique key so pages never overlap or skip rows."""
    rows: list[dict] = []
    start = 0
    while True:
        query = build()
        for column in order:
            query = query.order(column)
        batch = query.range(start, start + PAGE_SIZE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def load_dataset(client, season_id: int) -> dict:
    """Pull the completed matches of one season plus their player stats and
    round results. Read-only; a few dozen small requests for a 900-match
    season, run once per dispatch."""
    matches = _fetch_all(lambda: (
        client.table("matches")
        .select("id, match_id, created_at, completed_at, map_pool, queue_key, final_score")
        .eq("season_id", season_id)
        .eq("status", "completed")
    ))

    def scoped(table: str, columns: str):
        return lambda: (
            client.table(table)
            .select(f"{columns}, matches!inner(season_id,status)")
            .eq("matches.season_id", season_id)
            .eq("matches.status", "completed")
        )

    stats = _fetch_all(scoped(
        "match_player_stats",
        "id, match_id, player_id, round_number, kills, deaths, assists, hill_time, impact, score",
    ))
    try:
        results = _fetch_all(scoped(
            "match_round_results",
            "id, match_id, player_id, round_number, team, is_mvp, mmr_delta, is_crown, bonus_5",
        ))
    except Exception:
        # A database that never got migration_041 has no is_crown / bonus_5.
        logger.warning("season_stats: is_crown/bonus_5 unavailable, using the older column set", exc_info=True)
        results = _fetch_all(scoped(
            "match_round_results",
            "id, match_id, player_id, round_number, team, is_mvp, mmr_delta",
        ))
    return {"season_id": season_id, "matches": matches, "stats": stats, "results": results,
            "screen_scores": _load_screen_scores(scoped)}


def _load_screen_scores(scoped) -> dict[int, str]:
    """match id -> final score as read from the scoreboard screenshot (the
    same score the host checked on the verification card). matches.final_score
    is only filled for some matches, so on its own it would pick a "closest
    finish" from a small, unrepresentative sample. Best effort: any problem
    here just means no scores, and the recap leaves those two fields out."""
    try:
        shots = _fetch_all(scoped("match_screenshots", "match_id, round_number, raw_extraction"),
                           order=("match_id", "round_number"))
    except Exception:
        logger.warning("season_stats: could not read screenshot scores", exc_info=True)
        return {}
    per_match: dict[int, list] = defaultdict(list)
    for row in shots:
        per_match[row["match_id"]].append(row.get("raw_extraction"))
    scores: dict[int, str] = {}
    for mid, raws in per_match.items():
        if len(raws) != 1:                       # old multi-round matches have no single score
            continue
        raw = raws[0]
        if isinstance(raw, str):                 # stored as a JSON string in some rows
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        if isinstance(raw, dict) and raw.get("final_score"):
            scores[mid] = str(raw["final_score"])
    return scores


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _num(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def parse_ts(value) -> datetime | None:
    """Parse the timestamps PostgREST returns ('2026-10-07T23:28:18.94+00:00',
    or with a bare '+00' offset). None when unparseable."""
    if not value:
        return None
    text = str(value).strip().replace(" ", "T", 1).replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})$", r"\1:00", text)            # '+00'  -> '+00:00'
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)     # '+0000' -> '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fmt_hour(hour: int) -> str:
    def one(h: int) -> str:
        h %= 24
        return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"
    return f"{one(hour)}–{one(hour + 1)} IST"


def _base_delta(row: dict) -> float:
    """MMR change without the +5 bonus. The bonus sits on bonus_5 since
    migration_041, and on the MVP tag before that."""
    flag = row.get("bonus_5")
    if flag is None:
        flag = row.get("is_mvp")
    return _num(row.get("mmr_delta")) - (BONUS_POINTS if flag else 0)


def _player_matches(ds: dict) -> dict[tuple[int, int], dict]:
    """One record per (player, match). Rounds of an old multi-round match are
    folded together; impact is averaged because it is a rating, not a count."""
    out: dict[tuple[int, int], dict] = {}
    impacts: dict[tuple[int, int], list[float]] = defaultdict(list)
    for r in ds["stats"]:
        key = (r["player_id"], r["match_id"])
        rec = out.setdefault(key, {"kills": 0, "deaths": 0, "assists": 0, "hill_time": 0.0, "score": 0})
        rec["kills"] += int(_num(r.get("kills")))
        rec["deaths"] += int(_num(r.get("deaths")))
        rec["assists"] += int(_num(r.get("assists")))
        rec["hill_time"] += _num(r.get("hill_time"))
        rec["score"] += int(_num(r.get("score")))
        if r.get("impact") is not None:
            impacts[key].append(_num(r["impact"]))
    for key, rec in out.items():
        rec["impact"] = (sum(impacts[key]) / len(impacts[key])) if impacts.get(key) else None
    return out


def _match_outcomes(ds: dict) -> tuple[dict[tuple[int, int], dict], bool]:
    """(player, match) -> {"team", "won", "crown"}. The winning team is the one
    whose members gained more MMR once the +5 bonus is taken out, so it does not
    depend on matches.winner_team (never filled in for RO1 matches) and is not
    fooled by a losing Impact-crown holder who ends up with a positive number.
    Second value: False when no crown data exists at all."""
    team_totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in ds["results"]:
        if r.get("team") in ("A", "B"):
            team_totals[r["match_id"]][r["team"]] += _base_delta(r)
    winners: dict[int, str | None] = {}
    for mid, totals in team_totals.items():
        a, b = totals.get("A", 0.0), totals.get("B", 0.0)
        winners[mid] = "A" if a > b else "B" if b > a else None

    # Crowns only exist for matches played since migration_041 (2026-10-06);
    # older rows have is_crown NULL. Ranking "most crowns" over a few days of a
    # season-long card would be unfair, so it needs near-full coverage.
    known = sum(1 for r in ds["results"] if r.get("is_crown") is not None)
    have_crown_data = bool(ds["results"]) and known / len(ds["results"]) >= MIN_COVERAGE
    out: dict[tuple[int, int], dict] = {}
    for r in ds["results"]:
        key = (r["player_id"], r["match_id"])
        rec = out.setdefault(key, {"team": r.get("team"), "won": None, "crown": False})
        winner = winners.get(r["match_id"])
        rec["won"] = (r.get("team") == winner) if winner else None
        if r.get("is_crown"):
            rec["crown"] = True
    return out, have_crown_data


def _pick(rows: list[dict], value_key: str, *, tie_key: str = "matches_played") -> dict | None:
    """Highest value wins; ties go to whoever needed fewer matches, then the
    lower player id, so the result never changes between two runs."""
    if not rows:
        return None
    return sorted(rows, key=lambda x: (-x[value_key], x.get(tie_key, 0), x["player_id"]))[0]


# --------------------------------------------------------------------------
# Season Recap
# --------------------------------------------------------------------------
def compute_recap(ds: dict) -> dict:
    """Season-wide numbers. Keeps the keys of the old season_recap_stats()
    result so the embed and the fallback path read the same shape, and adds
    the new ones. Player ids are returned, names are looked up by the caller."""
    stats = ds["stats"]
    matches_by_id = {m["id"]: m for m in ds["matches"]}
    stat_match_ids = {r["match_id"] for r in stats}
    real_matches = [matches_by_id[i] for i in stat_match_ids if i in matches_by_id]

    pm = _player_matches(ds)
    recap: dict = {
        "matches_played": len(real_matches),
        "rounds_played": len({(r["match_id"], r["round_number"]) for r in stats}),
        "unique_players": len({r["player_id"] for r in stats}),
        "total_kills": sum(int(_num(r.get("kills"))) for r in stats),
        "total_deaths": sum(int(_num(r.get("deaths"))) for r in stats),
        "total_assists": sum(int(_num(r.get("assists"))) for r in stats),
        "total_mvps_awarded": sum(1 for r in ds["results"] if r.get("is_mvp")),
        "total_hardpoint_hours": round(sum(_num(r.get("hill_time")) for r in stats) / 3600.0, 1),
    }

    # --- when people play (IST) -------------------------------------------
    days: Counter = Counter()
    hours: Counter = Counter()
    for m in real_matches:
        ts = parse_ts(m.get("created_at"))
        if ts:
            local = ts.astimezone(IST)
            days[(local.date(), f"{local:%b} {local.day}")] += 1
            hours[local.hour] += 1
    if days:
        (_, label), count = sorted(days.items(), key=lambda kv: (-kv[1], kv[0][0]))[0]
        recap["busiest_day"] = {"label": label, "matches": count}
    if hours:
        hour, count = sorted(hours.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        recap["peak_hour"] = {"label": _fmt_hour(hour), "matches": count}

    # --- what they play ---------------------------------------------------
    maps: Counter = Counter()
    queues: Counter = Counter()
    for m in real_matches:
        pool = m.get("map_pool")
        if isinstance(pool, list) and pool and pool[0]:
            maps[str(pool[0])] += 1
        if m.get("queue_key"):
            queues[str(m["queue_key"])] += 1
    if maps:
        name, count = sorted(maps.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        recap["top_map"] = {"name": name, "matches": count}
    if queues:
        total = sum(queues.values())
        recap["queue_split"] = [
            {"queue": q, "matches": n, "pct": round(100.0 * n / total)}   # 0 means "under 0.5%"
            for q, n in sorted(queues.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    # --- standout moments -------------------------------------------------
    if pm:
        (pid, mid), rec = sorted(pm.items(), key=lambda kv: (-kv[1]["kills"], kv[0][1], kv[0][0]))[0]
        if rec["kills"] > 0 and mid in matches_by_id:
            recap["best_single_game"] = {
                "player_id": pid, "kills": rec["kills"], "match_code": matches_by_id[mid].get("match_id"),
            }
    scored = []
    screen_scores = ds.get("screen_scores") or {}
    for m in real_matches:
        found = _SCORE_RE.fullmatch(str(m.get("final_score") or screen_scores.get(m["id"]) or ""))
        if found and int(found[1]) != int(found[2]):
            a, b = int(found[1]), int(found[2])
            scored.append((abs(a - b), m.get("match_id"), f"{a}–{b}"))
    # "Closest" and "biggest" are only honest when nearly every match has a score.
    if scored and real_matches and len(scored) / len(real_matches) >= MIN_COVERAGE:
        gap, code, shown = min(scored, key=lambda t: (t[0], str(t[1])))
        recap["closest_finish"] = {"gap": gap, "match_code": code, "score": shown}
        gap, code, shown = max(scored, key=lambda t: (t[0], str(t[1])))
        recap["biggest_blowout"] = {"gap": gap, "match_code": code, "score": shown}
    return recap


# --------------------------------------------------------------------------
# Hall of Fame extras
# --------------------------------------------------------------------------
def compute_hof_extras(ds: dict) -> dict[str, dict | None]:
    """Extra Hall of Fame categories from data we already store. Each one is
    independent: if it cannot be worked out it comes back as None and the
    embed leaves it out. Rows carry player_id; the caller adds the ign."""
    matches_by_id = {m["id"]: m for m in ds["matches"]}
    pm = _player_matches(ds)
    outcomes, have_crown_data = _match_outcomes(ds)

    per_player: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    for (pid, mid), rec in pm.items():
        if mid in matches_by_id:
            per_player[pid].append((mid, rec))

    extras: dict[str, dict | None] = {}

    def guarded(name, fn):
        try:
            extras[name] = fn()
        except Exception:
            logger.exception("season_stats: HoF extra %r failed, leaving it out", name)
            extras[name] = None

    def best_avg_assists():
        rows = []
        for p, ms in per_player.items():
            if len(ms) >= MIN_MATCHES_FLOOR:
                rows.append({"player_id": p, "matches_played": len(ms),
                             "avg_assists": round(sum(r["assists"] for _, r in ms) / len(ms), 1)})
        best = _pick(rows, "avg_assists")
        return best if best and best["avg_assists"] > 0 else None

    def best_avg_impact():
        rows = []
        for p, ms in per_player.items():
            vals = [r["impact"] for _, r in ms if r["impact"] is not None]
            if len(ms) >= MIN_MATCHES_FLOOR and len(vals) >= MIN_MATCHES_FLOOR:
                rows.append({"player_id": p, "matches_played": len(ms),
                             "avg_impact": round(sum(vals) / len(vals), 1)})
        return _pick(rows, "avg_impact")

    def hill_king():
        rows = []
        for p, ms in per_player.items():
            if len(ms) >= MIN_MATCHES_FLOOR:
                rows.append({"player_id": p, "matches_played": len(ms),
                             "avg_hill_seconds": round(sum(r["hill_time"] for _, r in ms) / len(ms))})
        best = _pick(rows, "avg_hill_seconds")
        return best if best and best["avg_hill_seconds"] > 0 else None

    def most_crowns():
        if not have_crown_data:
            return None
        rows = []
        for p, ms in per_player.items():
            crowns = sum(1 for mid, _ in ms if outcomes.get((p, mid), {}).get("crown"))
            if crowns:
                rows.append({"player_id": p, "matches_played": len(ms), "crown_count": crowns})
        return _pick(rows, "crown_count")

    def best_single_game():
        best = None
        for p, ms in per_player.items():
            for mid, r in ms:
                cand = (-r["kills"], mid, p)
                if best is None or cand < best[0]:
                    best = (cand, r["kills"], mid, p)
        if not best or best[1] <= 0:
            return None
        return {"player_id": best[3], "kills": best[1], "match_code": matches_by_id[best[2]].get("match_id")}

    def most_wins():
        rows = []
        for p, ms in per_player.items():
            known = [outcomes.get((p, mid), {}).get("won") for mid, _ in ms]
            wins = sum(1 for w in known if w is True)
            if wins:
                rows.append({"player_id": p, "matches_played": len(ms), "wins": wins})
        return _pick(rows, "wins")

    def longest_win_streak():
        def when(mid: int):
            ts = parse_ts(matches_by_id[mid].get("created_at"))
            return (ts or datetime.min.replace(tzinfo=timezone.utc), mid)
        rows = []
        for p, ms in per_player.items():
            run = longest = 0
            for mid, _ in sorted(ms, key=lambda x: when(x[0])):
                if outcomes.get((p, mid), {}).get("won") is True:
                    run += 1
                    longest = max(longest, run)
                else:
                    run = 0                      # a loss, or an unknown result, ends the run
            if longest >= MIN_STREAK_TO_SHOW:
                rows.append({"player_id": p, "matches_played": len(ms), "streak": longest})
        return _pick(rows, "streak")

    def most_consistent():
        # Same win logic as Most Wins / Win Streak (team MMR once the +5 bonus is
        # out), so the three can never contradict each other. The old SQL version
        # decided wins from the MVP tag per player and had no real floor.
        rows = []
        for p, ms in per_player.items():
            known = [outcomes.get((p, mid), {}).get("won") for mid, _ in ms]
            decided = [w for w in known if w is not None]
            if len(decided) >= CONSISTENT_MIN_MATCHES:
                wins = sum(1 for w in decided if w)
                rows.append({"player_id": p, "matches_played": len(decided), "wins": wins,
                             "win_rate_pct": round(100.0 * wins / len(decided), 1)})
        if not rows:
            return None
        return sorted(rows, key=lambda x: (-x["win_rate_pct"], -x["matches_played"], x["player_id"]))[0]

    guarded("most_consistent", most_consistent)
    guarded("best_avg_assists", best_avg_assists)
    guarded("best_avg_impact", best_avg_impact)
    guarded("hill_king", hill_king)
    guarded("most_crowns", most_crowns)
    guarded("best_single_game", best_single_game)
    guarded("most_wins", most_wins)
    guarded("longest_win_streak", longest_win_streak)
    return extras


# Same table the embed and the hall_of_fame table use: category -> number that is stored.
EXTRA_VALUE_KEYS = {
    "most_consistent": "win_rate_pct",
    "best_avg_assists": "avg_assists",
    "best_avg_impact": "avg_impact",
    "hill_king": "avg_hill_seconds",
    "most_crowns": "crown_count",
    "best_single_game": "kills",
    "most_wins": "wins",
    "longest_win_streak": "streak",
}
