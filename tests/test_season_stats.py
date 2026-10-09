"""Season Recap + Hall of Fame computed in Python, no SQL migration needed.
Fakes only: no Discord, no Supabase."""
from types import SimpleNamespace

import pytest

from cogs import admin
from services import season_stats
from utils import embeds


# ---------------------------------------------------------------------------
# Synthetic season
# ---------------------------------------------------------------------------
def make_ds(specs, rounds_per_match=1, crown=True):
    """specs: list of dicts with id, code, created, winner ('A'/'B'), and
    optional map, queue, score. Players 1-5 are team A, 6-10 team B.
    Player 10 gets the most kills/assists/impact, player 3 the most hill time,
    players 1 and 6 hold the Impact crown every match."""
    matches, stats, results = [], [], []
    for sp in specs:
        matches.append({
            "id": sp["id"], "match_id": sp["code"], "created_at": sp["created"],
            "completed_at": None, "map_pool": [sp.get("map", "Summit")],
            "queue_key": sp.get("queue", "EU_AF"), "final_score": sp.get("score"),
        })
        for rnd in range(1, rounds_per_match + 1):
            for pid in range(1, 11):
                team = "A" if pid <= 5 else "B"
                stats.append({
                    "id": len(stats) + 1, "match_id": sp["id"], "player_id": pid, "round_number": rnd,
                    "kills": 10 + pid, "deaths": 5, "assists": pid,
                    "hill_time": 120 if pid == 3 else 60, "impact": 100 + pid, "score": 1000 + pid,
                })
                won = team == sp["winner"]
                is_crown = pid in (1, 6)
                row = {
                    "id": len(results) + 1, "match_id": sp["id"], "player_id": pid, "round_number": rnd,
                    "team": team, "is_mvp": pid in (1, 6),
                    # the +5 bonus is already inside mmr_delta, like the real rows
                    "mmr_delta": (10 if won else -3) + (5 if is_crown else 0),
                }
                if crown:
                    row["is_crown"] = is_crown
                    row["bonus_5"] = is_crown
                results.append(row)
    return {"season_id": 2, "matches": matches, "stats": stats, "results": results}


def eight_matches(winner_for):
    return [
        {"id": i, "code": f"CQ-{i:04d}", "created": f"2026-10-{i:02d}T10:00:00+00:00", "winner": winner_for(i)}
        for i in range(1, 9)
    ]


# ---------------------------------------------------------------------------
# compute_recap
# ---------------------------------------------------------------------------
def test_recap_totals_and_single_round_equality():
    ds = make_ds(eight_matches(lambda i: "B"))
    r = season_stats.compute_recap(ds)
    assert r["matches_played"] == 8 and r["rounds_played"] == 8      # RO1: same number
    assert r["unique_players"] == 10
    assert r["total_kills"] == 8 * sum(10 + p for p in range(1, 11))
    assert r["total_deaths"] == 8 * 5 * 10
    assert r["total_assists"] == 8 * sum(range(1, 11))
    assert r["total_hardpoint_hours"] == round(8 * (9 * 60 + 120) / 3600.0, 1)


def test_recap_multi_round_match_counts_once_but_rounds_count_all():
    ds = make_ds(eight_matches(lambda i: "A"), rounds_per_match=3)
    r = season_stats.compute_recap(ds)
    assert r["matches_played"] == 8 and r["rounds_played"] == 24


def test_recap_ignores_completed_rows_without_real_stats():
    ds = make_ds(eight_matches(lambda i: "A"))
    ds["matches"].append({"id": 99, "match_id": "CQ-0099", "created_at": "2026-10-09T10:00:00+00:00",
                          "map_pool": ["Summit"], "queue_key": "EU_AF", "final_score": None})
    assert season_stats.compute_recap(ds)["matches_played"] == 8


def test_busiest_day_and_hour_use_ist():
    specs = [
        {"id": 1, "code": "CQ-1", "created": "2026-10-01T19:00:00+00:00", "winner": "A"},   # 00:30 IST on Oct 2
        {"id": 2, "code": "CQ-2", "created": "2026-10-01T19:10:00+00:00", "winner": "A"},   # 00:40 IST on Oct 2
        {"id": 3, "code": "CQ-3", "created": "2026-10-03 10:00:00+00", "winner": "A"},      # bare +00 offset
    ]
    r = season_stats.compute_recap(make_ds(specs))
    assert r["busiest_day"] == {"label": "Oct 2", "matches": 2}
    assert r["peak_hour"] == {"label": "12 AM–1 AM IST", "matches": 2}


def test_map_and_queue_split():
    specs = [
        {"id": 1, "code": "CQ-1", "created": "2026-10-01T10:00:00+00:00", "winner": "A", "map": "Summit", "queue": "EU_AF"},
        {"id": 2, "code": "CQ-2", "created": "2026-10-02T10:00:00+00:00", "winner": "A", "map": "Summit", "queue": "INDIA_ME"},
        {"id": 3, "code": "CQ-3", "created": "2026-10-03T10:00:00+00:00", "winner": "A", "map": "Raid", "queue": "INDIA_ME"},
        {"id": 4, "code": "CQ-4", "created": "2026-10-04T10:00:00+00:00", "winner": "A", "map": "Summit", "queue": "NA_LATAM"},
    ]
    r = season_stats.compute_recap(make_ds(specs))
    assert r["top_map"] == {"name": "Summit", "matches": 3}
    assert [q["queue"] for q in r["queue_split"]] == ["INDIA_ME", "EU_AF", "NA_LATAM"]
    assert r["queue_split"][0]["pct"] == 50


def test_best_single_game_and_score_extremes():
    specs = [
        {"id": 1, "code": "CQ-1", "created": "2026-10-01T10:00:00+00:00", "winner": "A", "score": "250-249"},
        {"id": 2, "code": "CQ-2", "created": "2026-10-02T10:00:00+00:00", "winner": "A", "score": "250-60"},
        {"id": 3, "code": "CQ-3", "created": "2026-10-03T10:00:00+00:00", "winner": "A", "score": None},
    ]
    r = season_stats.compute_recap(make_ds(specs))
    assert r["best_single_game"] == {"player_id": 10, "kills": 20, "match_code": "CQ-1"}   # earliest wins a tie
    assert r["closest_finish"] == {"gap": 1, "match_code": "CQ-1", "score": "250–249"}
    assert r["biggest_blowout"] == {"gap": 190, "match_code": "CQ-2", "score": "250–60"}


def test_no_final_scores_means_no_score_fields():
    r = season_stats.compute_recap(make_ds(eight_matches(lambda i: "A")))
    assert "closest_finish" not in r and "biggest_blowout" not in r


# ---------------------------------------------------------------------------
# compute_hof_extras
# ---------------------------------------------------------------------------
def test_extras_pick_the_right_winners():
    ex = season_stats.compute_hof_extras(make_ds(eight_matches(lambda i: "B")))
    assert ex["most_assists"]["player_id"] == 10 and ex["most_assists"]["total_assists"] == 80
    assert ex["best_avg_impact"]["player_id"] == 10 and ex["best_avg_impact"]["avg_impact"] == 110.0
    assert ex["hill_king"]["player_id"] == 3
    assert ex["most_crowns"]["player_id"] == 1 and ex["most_crowns"]["crown_count"] == 8   # tie -> lowest id
    assert ex["best_single_game"]["player_id"] == 10 and ex["best_single_game"]["kills"] == 20
    assert ex["most_wins"]["player_id"] == 6 and ex["most_wins"]["wins"] == 8
    assert ex["longest_win_streak"]["player_id"] == 6 and ex["longest_win_streak"]["streak"] == 8


def test_losing_crown_holder_is_not_counted_as_a_winner():
    # a losing crown holder nets +2 (-3 base, +5 crown) but his team still lost
    ds = make_ds(eight_matches(lambda i: "B"))
    mine = [r for r in ds["results"] if r["player_id"] == 1]
    assert all(r["mmr_delta"] > 0 for r in mine)          # positive number, yet the team lost
    ex = season_stats.compute_hof_extras(ds)
    assert ex["most_wins"]["player_id"] != 1


def test_win_streak_resets_on_a_loss_and_needs_three():
    # player 6 (team B) wins matches 1-4, loses 5, wins 6-7, loses 8 -> longest 4
    winners = {1: "B", 2: "B", 3: "B", 4: "B", 5: "A", 6: "B", 7: "B", 8: "A"}
    ex = season_stats.compute_hof_extras(make_ds(eight_matches(lambda i: winners[i])))
    assert ex["longest_win_streak"]["streak"] == 4

    short = {i: ("B" if i % 2 else "A") for i in range(1, 9)}          # never 2 in a row
    assert season_stats.compute_hof_extras(make_ds(eight_matches(lambda i: short[i])))["longest_win_streak"] is None


def test_rate_category_needs_eight_matches():
    ex = season_stats.compute_hof_extras(make_ds(eight_matches(lambda i: "B")[:7]))
    assert ex["best_avg_impact"] is None
    assert ex["most_assists"] is not None            # cumulative: no floor


def test_crowns_left_out_when_the_database_has_no_crown_columns():
    ds = make_ds(eight_matches(lambda i: "B"), crown=False)
    ex = season_stats.compute_hof_extras(ds)
    assert ex["most_crowns"] is None
    assert ex["most_wins"]["player_id"] == 6         # still works from the MVP-tag bonus


def test_empty_season_gives_nothing_and_does_not_crash():
    ds = {"season_id": 2, "matches": [], "stats": [], "results": []}
    assert season_stats.compute_recap(ds)["matches_played"] == 0
    assert all(v is None for v in season_stats.compute_hof_extras(ds).values())


# ---------------------------------------------------------------------------
# load_dataset: paging and the missing-column fallback
# ---------------------------------------------------------------------------
class FakeQuery:
    def __init__(self, client, table):
        self.client, self.table_name, self.cols = client, table, ""
        self.lo, self.hi = 0, 999

    def select(self, cols, **kw):
        self.cols = cols
        return self

    def eq(self, *a):
        return self

    def order(self, *a):
        return self

    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self

    def execute(self):
        self.client.calls.append((self.table_name, self.lo))
        if self.table_name == "match_round_results" and "is_crown" in self.cols and self.client.no_crown_columns:
            raise RuntimeError("column match_round_results.is_crown does not exist")
        return SimpleNamespace(data=self.client.tables[self.table_name][self.lo:self.hi + 1])


class FakeClient:
    def __init__(self, tables, no_crown_columns=False):
        self.tables, self.no_crown_columns, self.calls = tables, no_crown_columns, []

    def table(self, name):
        return FakeQuery(self, name)


def test_loader_reads_every_page():
    tables = {"matches": [{"id": i} for i in range(2350)], "match_player_stats": [], "match_round_results": []}
    ds = season_stats.load_dataset(FakeClient(tables), 2)
    assert len(ds["matches"]) == 2350
    assert [lo for t, lo in FakeClient(tables).calls] == []                 # fresh client, untouched
    client = FakeClient(tables)
    season_stats.load_dataset(client, 2)
    assert [lo for t, lo in client.calls if t == "matches"] == [0, 1000, 2000]


def test_loader_falls_back_when_crown_columns_are_missing():
    tables = {"matches": [], "match_player_stats": [],
              "match_round_results": [{"id": 1, "match_id": 1, "player_id": 1, "team": "A", "mmr_delta": 5, "is_mvp": False}]}
    ds = season_stats.load_dataset(FakeClient(tables, no_crown_columns=True), 2)
    assert len(ds["results"]) == 1


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------
SEASON = {"code": "S2-0901", "start_date": "2026-09-02T00:00:00+00:00", "end_date": None}


def names(embed):
    return [f.name for f in embed.fields]


def test_recap_card_drops_the_duplicate_numbers_and_fixes_the_footer():
    stats = season_stats.compute_recap(make_ds(eight_matches(lambda i: "A")))
    stats["best_single_game"]["ign"] = "Kayy"
    stats["new_players"] = 12
    e = embeds.season_recap_embed(SEASON, stats, ai_tokens_used="5.2M")
    n = names(e)
    assert "🎮 Matches Played" in n
    assert "🔄 Rounds Played" not in n and "⭐ MVPs Awarded" not in n
    for wanted in ("🤝 Total Assists", "📅 Busiest Day", "🕘 Peak Hour", "🗺️ Most Played Map",
                   "🌍 Where We Queued", "💥 Best Single Game", "🆕 New Players", "🤖 AI Tokens Processed"):
        assert wanted in n, wanted
    assert "S2-0901" in e.footer.text and "Season 1" not in e.footer.text
    assert "to **today**" in e.description


def test_recap_card_shows_rounds_only_when_they_differ():
    stats = season_stats.compute_recap(make_ds(eight_matches(lambda i: "A"), rounds_per_match=3))
    assert "🔄 Rounds Played" in names(embeds.season_recap_embed(SEASON, stats))


def test_recap_card_still_works_with_the_old_sql_result_shape():
    old = {"matches_played": 847, "rounds_played": 847, "unique_players": 464, "total_kills": 1,
           "total_deaths": 2, "total_mvps_awarded": 1694, "total_hardpoint_hours": 140.2}
    n = names(embeds.season_recap_embed(SEASON, old))
    assert n == ["🎮 Matches Played", "👥 Players", "🔫 Total Kills", "💀 Total Deaths", "⏱️ Hours of Hardpoint"]


def test_recap_shows_end_date_for_a_finished_season():
    done = {**SEASON, "end_date": "2020-10-10T18:29:59+00:00"}  # long past, whatever today is
    stats = {"matches_played": 1, "rounds_played": 1, "unique_players": 1, "total_kills": 1,
             "total_deaths": 1, "total_hardpoint_hours": 0.1}
    assert "**2020-10-10**" in embeds.season_recap_embed(done, stats).description


def _legacy_winners():
    row = {"ign": "X", "matches_played": 28}
    return {
        "most_consistent": {**row, "win_rate_pct": 100.0},
        "fastest_climber": {"ign": "EXCL Lighty", "matches_played": 28, "mmr_per_match": 11.21, "mmr_gained": 314},
        "highest_total_kills": {**row, "total_kills": 5}, "best_avg_kills": {**row, "avg_kills": 1},
        "best_avg_deaths": {**row, "avg_deaths": 1}, "most_mvps": {**row, "mvp_count": 1},
        "most_matches_played": row, "best_kd": {**row, "kd_ratio": 2, "total_kills": 2, "total_deaths": 1},
        "highest_mmr": {"ign": "X", "mmr": 1, "current_rank": "Elite1"},
    }


def test_fastest_climber_line_says_what_the_numbers_are():
    e = embeds.hall_of_fame_embed(SEASON, _legacy_winners())
    line = next(f.value for f in e.fields if "Fastest Climber" in f.name)
    assert "314 MMR gained over 28 matches" in line and "(314 total)" not in line


def test_extra_hof_fields_appear_only_with_a_winner():
    winners = _legacy_winners()
    assert len(embeds.hall_of_fame_embed(SEASON, winners).fields) == 9
    winners["most_wins"] = {"player_id": 6, "ign": "Ezio", "wins": 30, "matches_played": 39}
    winners["longest_win_streak"] = {"player_id": 6, "matches_played": 39, "streak": 9}   # no ign -> skipped
    e = embeds.hall_of_fame_embed(SEASON, winners)
    assert len(e.fields) == 10
    assert next(f.value for f in e.fields if "Most Wins" in f.name) == "**Ezio** — 30 wins (39 matches)"


# ---------------------------------------------------------------------------
# The two /admin-dispatch handlers
# ---------------------------------------------------------------------------
class Channel:
    mention = "#hall-of-fame"

    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


class Guild:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        return self.channel


class FakeAdb:
    def __init__(self, ds, *, dataset_boom=False, record_boom=False, hof_boom=(), recap_rpc=None):
        self.ds, self.dataset_boom, self.record_boom = ds, dataset_boom, record_boom
        self.hof_boom, self.recap_rpc = set(hof_boom), recap_rpc
        self.recorded, self.rpc_calls = [], 0

    async def get_season_by_id(self, sid):
        return {"id": sid, **SEASON}

    async def get_season_dataset(self, sid):
        if self.dataset_boom:
            raise RuntimeError("load failed")
        return self.ds

    async def count_players_registered_since(self, since):
        return 12

    async def get_players_by_ids(self, ids):
        return [{"id": i, "ign": f"Player{i}"} for i in ids]

    async def season_recap_stats(self, sid):
        self.rpc_calls += 1
        if self.recap_rpc is None:
            raise RuntimeError("PGRST203 ambiguous")
        return self.recap_rpc

    async def record_hall_of_fame(self, sid, category, pid, value):
        if self.record_boom:
            raise RuntimeError("check constraint")
        self.recorded.append((category, pid, value))

    def __getattr__(self, name):
        if name.startswith("hof_"):
            async def fn(*a):
                if name[4:] in self.hof_boom:
                    raise RuntimeError("rpc down")
                row = {"player_id": 1, "ign": "Legacy", "matches_played": 10, "win_rate_pct": 90, "mmr_per_match": 5,
                       "mmr_gained": 50, "total_kills": 9, "avg_kills": 9, "avg_deaths": 3, "mvp_count": 4,
                       "kd_ratio": 2, "total_deaths": 4, "mmr": 1000, "current_rank": "Elite1"}
                return row
            return fn
        raise AttributeError(name)


def cog_and_inter(monkeypatch, fake, channel):
    monkeypatch.setattr(admin, "adb", fake)
    monkeypatch.setattr(admin.config, "HALL_OF_FAME_CHANNEL_ID", 123, raising=False)

    async def passthrough(fn, *a, **kw):
        return await fn(*a, **kw)
    monkeypatch.setattr(admin, "with_retry", passthrough)
    cog = admin.Admin.__new__(admin.Admin)
    return cog, SimpleNamespace(guild=Guild(channel))


@pytest.mark.asyncio
async def test_recap_handler_posts_without_any_sql_function(monkeypatch):
    fake, ch = FakeAdb(make_ds(eight_matches(lambda i: "B"))), Channel()
    cog, inter = cog_and_inter(monkeypatch, fake, ch)
    msg = await cog._dispatch_season_recap(inter, season_id=2, ai_tokens_used="5.2M")
    assert msg.startswith("✅") and fake.rpc_calls == 0
    names_ = names(ch.sent[0]["embed"])
    assert "👥 Players" in names_ and "🆕 New Players" in names_ and "🤖 AI Tokens Processed" in names_
    best = next(f.value for f in ch.sent[0]["embed"].fields if "Best Single Game" in f.name)
    assert "Player10" in best


@pytest.mark.asyncio
async def test_recap_handler_falls_back_to_the_old_function_if_python_breaks(monkeypatch):
    old = {"matches_played": 5, "rounds_played": 5, "unique_players": 9, "total_kills": 1,
           "total_deaths": 1, "total_mvps_awarded": 10, "total_hardpoint_hours": 1.0}
    fake, ch = FakeAdb(None, dataset_boom=True, recap_rpc=old), Channel()
    cog, inter = cog_and_inter(monkeypatch, fake, ch)
    assert (await cog._dispatch_season_recap(inter, season_id=2)).startswith("✅")
    assert fake.rpc_calls == 1


@pytest.mark.asyncio
async def test_recap_handler_reports_a_season_with_no_matches(monkeypatch):
    fake, ch = FakeAdb({"season_id": 2, "matches": [], "stats": [], "results": []}), Channel()
    cog, inter = cog_and_inter(monkeypatch, fake, ch)
    msg = await cog._dispatch_season_recap(inter, season_id=2)
    assert msg.startswith("❌") and not ch.sent and fake.rpc_calls == 0


@pytest.mark.asyncio
async def test_hof_handler_posts_extras_and_records_them(monkeypatch):
    fake, ch = FakeAdb(make_ds(eight_matches(lambda i: "B"))), Channel()
    cog, inter = cog_and_inter(monkeypatch, fake, ch)
    msg = await cog._dispatch_hall_of_fame(inter, season_id=2)
    assert msg.startswith("✅") and "⚠️" not in msg
    embed = ch.sent[0]["embed"]
    assert any("Most Wins" in f.name for f in embed.fields) and any("Longest Win Streak" in f.name for f in embed.fields)
    cats = {c for c, _, _ in fake.recorded}
    assert {"most_consistent", "highest_mmr", "most_wins", "hill_king", "best_single_game"} <= cats


@pytest.mark.asyncio
async def test_hof_handler_survives_a_failed_category_and_a_failed_extras_load(monkeypatch):
    fake, ch = FakeAdb(None, dataset_boom=True, hof_boom=("best_kd",)), Channel()
    cog, inter = cog_and_inter(monkeypatch, fake, ch)
    msg = await cog._dispatch_hall_of_fame(inter, season_id=2)
    assert msg.startswith("✅") and "best_kd" in msg and "extra categories" in msg
    assert len(ch.sent[0]["embed"].fields) == 9          # the original nine still post


@pytest.mark.asyncio
async def test_hof_handler_still_posts_if_saving_to_the_table_fails(monkeypatch):
    fake, ch = FakeAdb(make_ds(eight_matches(lambda i: "B")), record_boom=True), Channel()
    cog, inter = cog_and_inter(monkeypatch, fake, ch)
    msg = await cog._dispatch_hall_of_fame(inter, season_id=2)
    assert msg.startswith("✅") and "not saved" in msg and ch.sent
