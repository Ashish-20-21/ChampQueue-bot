"""Branch 8: fewer, calmer database calls per result submission.

Fakes only. What these pin down:
  * career stats are refreshed with ONE bulk call, never one call per player
  * the useless first recompute (match still 'awaiting_result') is gone
  * a flagged match (sent to review) now DOES get its stats refreshed, after
    its status flips
  * a failure is retried once, then reported -- and never breaks the submission
  * validate_submission reuses the roster and never runs more than 3 history
    lookups at once
  * the SQL follows the house rule (FOR..LOOP + PERFORM) and never touches SP
"""
import asyncio
import re
from pathlib import Path

import discord
import pytest

import config
from cogs import match
from database import db as dbmod
from services import validation

ROSTER = [{"player_id": pid, "team": "A" if pid <= 5 else "B", "players": {"ign": f"p{pid}"}} for pid in range(1, 11)]
MATCH = {"id": 7, "match_id": "CQ-0042", "queue_key": "EU_AF"}


class Recorder:
    """Fake adb that records the order of every call."""
    def __init__(self, bulk_results=None):
        self.log, self.bulk_results = [], list(bulk_results or [])

    async def get_match_players(self, pk):
        self.log.append("get_match_players"); return ROSTER
    async def upsert_match_screenshot(self, *a, **kw): self.log.append("upsert_screenshot")
    async def replace_match_round_data(self, *a, **kw): self.log.append("replace_round_data")
    async def recompute_player_career_stats(self, pid): self.log.append(f"recompute_one:{pid}")
    async def get_players_by_ids(self, ids): return [{"id": i, "ign": f"p{i}"} for i in ids]

    async def update_match(self, pk, fields):
        self.log.append(f"update_match:{fields['status']}")

    async def recompute_player_career_stats_bulk(self, ids):
        self.log.append(f"bulk:{len(ids)}")
        nxt = self.bulk_results.pop(0) if self.bulk_results else []
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _cog(incidents):
    c = match.Match.__new__(match.Match)
    c.bot = None
    return c


@pytest.fixture
def incidents(monkeypatch):
    seen = []

    async def post(bot, **kw): seen.append(kw)
    async def nosleep(*a, **kw): return None
    monkeypatch.setattr(match.incident_log, "post", post)
    monkeypatch.setattr(match.asyncio, "sleep", nosleep)
    return seen


# ---------------- the helper ----------------

async def test_bulk_helper_is_one_call_for_all_ten_players(monkeypatch, incidents):
    fake = Recorder(); monkeypatch.setattr(match, "adb", fake)
    await _cog(incidents)._recompute_career_stats_bulk(MATCH, ROSTER, stale_note="x")
    assert fake.log == ["bulk:10"] and incidents == []


async def test_bulk_helper_retries_once_then_succeeds_quietly(monkeypatch, incidents):
    fake = Recorder([KeyError(73), []]); monkeypatch.setattr(match, "adb", fake)
    await _cog(incidents)._recompute_career_stats_bulk(MATCH, ROSTER, stale_note="x")
    assert fake.log == ["bulk:10", "bulk:10"] and incidents == []     # a blip that clears is not an incident


async def test_bulk_helper_reports_everyone_when_it_fails_twice_and_never_raises(monkeypatch, incidents):
    fake = Recorder([KeyError(73), KeyError(81), KeyError(1)]); monkeypatch.setattr(match, "adb", fake)
    await _cog(incidents)._recompute_career_stats_bulk(MATCH, ROSTER, stale_note="until approved")
    assert fake.log == ["bulk:10", "bulk:10"]                          # exactly two attempts, no third
    assert len(incidents) == 1 and incidents[0]["category"] == "MATCH_STAT_RECOMPUTE_FAIL"
    assert "all 10 players" in incidents[0]["summary"] and "MMR unaffected" in incidents[0]["summary"]


async def test_bulk_helper_names_the_players_the_database_could_not_do(monkeypatch, incidents):
    fake = Recorder([[4, 9]]); monkeypatch.setattr(match, "adb", fake)
    await _cog(incidents)._recompute_career_stats_bulk(MATCH, ROSTER, stale_note="x")
    assert fake.log == ["bulk:10"]                                     # partial failure is NOT retried blindly
    assert "[4, 9]" in incidents[0]["summary"]


async def test_bulk_helper_with_no_players_does_nothing(monkeypatch, incidents):
    fake = Recorder(); monkeypatch.setattr(match, "adb", fake)
    await _cog(incidents)._recompute_career_stats_bulk(MATCH, [], stale_note="x")
    assert fake.log == []


# ---------------- the submit pipeline ----------------

def _rows():
    return [{"player_id": pid, "position": 1, "is_mvp": False, "mmr_delta": 3, "team": "A", "kills": 1, "deaths": 1,
             "assists": 1, "damage": 1, "hill_time": 1.0, "impact": 1.0, "score": 1} for pid in range(1, 11)]


class _Att:
    content_type, size, filename, url = "image/png", 10, "sb.png", "https://cdn/x.png"
    async def read(self): return b"img"


class _Chan:
    mention = "#approval"
    def __init__(self, log): self.log = log
    async def send(self, *a, **kw): self.log.append("approval_card")


async def _run(monkeypatch, fake, *, flags):
    monkeypatch.setattr(match, "adb", fake)
    monkeypatch.setattr(config, "RESULT_SS_CHANNEL_ID", None)
    monkeypatch.setattr(match.vision_extraction, "extract_scoreboard", lambda b, ct: {"players": []})
    monkeypatch.setattr(match.Match, "_prepare_round", staticmethod(
        lambda mp, mapn, ex, *a, **kw: ({"clean": True, "round_number": 1, "results": _rows()}, [], [], False)))
    seen_kwargs = {}

    async def validate(mid, ex, *a, **kw):
        fake.log.append("validate"); seen_kwargs.update(kw)
        return {"flags": {1: ["kills=99 is 9.0 std devs"]} if flags else {}}
    monkeypatch.setattr(match.validation, "validate_submission", validate)
    monkeypatch.setattr(match, "verification_card", lambda *a, **kw: discord.Embed())
    monkeypatch.setattr(match, "HostApprovalView", lambda *a, **kw: None)

    async def quiet(*a, **kw): return None
    monkeypatch.setattr(match, "_post_match_status", quiet)
    monkeypatch.setattr(match.incident_log, "post", quiet)
    monkeypatch.setattr(match.asyncio, "sleep", quiet)

    c = match.Match.__new__(match.Match)
    c.bot = None

    async def appr(): return _Chan(fake.log)
    c._approval_channel = appr

    async def route(m, pid, kind, detail): fake.log.append("route_to_review")
    c._route_to_review = route
    said = []

    class R:
        in_match_channel = True
        async def send(self, text): said.append(text)
    await c._submit_body(R(), dict(MATCH), {"id": 1}, ["Summit"], (_Att(),), 101, "Ravi")
    return said, seen_kwargs


async def test_clean_submission_makes_exactly_one_recompute_call_in_the_right_place(monkeypatch):
    fake = Recorder()
    said, _ = await _run(monkeypatch, fake, flags=False)
    assert not [e for e in fake.log if e.startswith("recompute_one")]  # no per-player burst, ever
    assert fake.log.count("bulk:10") == 1                              # and no useless first wave either
    i = fake.log.index
    assert i("replace_round_data") < i("validate") < i("update_match:pending_verification") < i("bulk:10") < i("approval_card")
    assert said[-1].startswith("Submitted.")


async def test_submission_reuses_the_roster_it_already_loaded(monkeypatch):
    fake = Recorder()
    _, kw = await _run(monkeypatch, fake, flags=False)
    assert kw.get("match_players") == ROSTER
    assert fake.log.count("get_match_players") == 1                    # loaded once, not twice


async def test_flagged_match_gets_its_stats_refreshed_after_it_goes_to_review(monkeypatch):
    fake = Recorder()
    said, _ = await _run(monkeypatch, fake, flags=True)
    assert fake.log.count("bulk:10") == 1
    assert fake.log.index("route_to_review") < fake.log.index("bulk:10")   # status must already be awaiting_review
    assert "approval_card" not in fake.log
    assert said                                                        # the host still gets told


async def test_a_recompute_outage_never_stops_the_submission(monkeypatch):
    fake = Recorder([KeyError(73), KeyError(81)])
    said, _ = await _run(monkeypatch, fake, flags=False)
    assert "approval_card" in fake.log and said[-1].startswith("Submitted.")


# ---------------- validate_submission ----------------

def _extraction(n=10):
    return {"players": [{"ign": f"P{pid}", "kills": 5} for pid in range(1, n + 1)]}


async def test_validation_uses_the_roster_it_is_given(monkeypatch):
    fake = Recorder(); monkeypatch.setattr(validation, "adb", fake)

    async def none(pid, stats): return []
    monkeypatch.setattr(validation, "check_stat_outliers", none)
    res = await validation.validate_submission(7, _extraction(), match_players=ROSTER)
    assert fake.log == [] and res == {"auto_accept": True, "flags": {}}


async def test_validation_still_loads_the_roster_when_not_given(monkeypatch):
    fake = Recorder(); monkeypatch.setattr(validation, "adb", fake)

    async def none(pid, stats): return []
    monkeypatch.setattr(validation, "check_stat_outliers", none)
    await validation.validate_submission(7, _extraction())
    assert fake.log == ["get_match_players"]


async def test_validation_never_runs_more_than_three_lookups_at_once_and_keeps_flag_order(monkeypatch):
    monkeypatch.setattr(validation, "adb", Recorder())
    running = peak = 0

    async def slow(pid, stats):
        nonlocal running, peak
        running += 1; peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return [f"flag{pid}"] if pid in (9, 2, 5) else []
    monkeypatch.setattr(validation, "check_stat_outliers", slow)
    res = await validation.validate_submission(7, _extraction(), match_players=ROSTER)
    assert peak == validation.HISTORY_LOOKUP_CONCURRENCY == 3
    assert list(res["flags"]) == [2, 5, 9] and not res["auto_accept"]     # roster order, same as the old loop


# ---------------- db binding + SQL ----------------

def test_db_binding_sends_one_rpc_and_returns_failed_ids():
    sent = []

    class Exec:
        def __init__(self, data): self.data = data
        def execute(self): return self
    class Client:
        def rpc(self, name, params): sent.append((name, params)); return Exec([4, 9])
    fake_db = type("D", (), {"client": Client()})()
    assert dbmod._recompute_player_career_stats_bulk(fake_db, (1, 2, 3)) == [4, 9]
    assert sent == [("recompute_player_career_stats_bulk", {"p_player_ids": [1, 2, 3]})]

    class Client2:
        def rpc(self, name, params): return Exec(None)
    assert dbmod._recompute_player_career_stats_bulk(type("D", (), {"client": Client2()})(), [1]) == []


def test_bulk_sql_follows_the_house_rules_and_never_touches_season_points():
    sql = Path("database/migration_040_bulk_recompute_career_stats.sql").read_text().lower()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert re.search(r"for\s+v_pid\s+in\b.*?\bloop", code, re.S)              # FOR ... LOOP
    assert "perform recompute_player_career_stats(v_pid)" in code             # PERFORM, not `select func(id)`
    assert "exception when others" in code                                     # one bad player can't sink nine
    assert "grant execute on function recompute_player_career_stats_bulk(bigint[]) to service_role" in code
    assert "season_point" not in code                                          # the no-negative-SP floor is out of reach


# ---------------- approval (commit 3) ----------------

async def test_approval_refreshes_stats_with_one_bulk_call(monkeypatch):
    class A(Recorder):
        async def has_open_issue(self, mid): return False
        async def approve_match(self, mid, by): self.log.append("approve_match")
        async def get_match(self, mid): self.log.append("get_match"); return {"id": mid, "match_id": "CQ-0042"}   # no season_id: SP check skipped
    fake = A(); monkeypatch.setattr(match, "adb", fake)
    c = match.Match.__new__(match.Match); c.bot = None

    async def cleanup(guild, m): fake.log.append("cleanup")
    c._run_post_approval_cleanup = cleanup
    ok, msg = await c._do_approve(None, 7, 1)
    assert (ok, msg) == (True, "approved")
    assert fake.log.count("bulk:10") == 1 and not [e for e in fake.log if e.startswith("recompute_one")]
    assert fake.log.index("approve_match") < fake.log.index("bulk:10") < fake.log.index("cleanup")   # MMR first, then stats
    assert fake.log.count("get_match") == 1                                                          # moved, not added


async def test_approval_still_succeeds_when_the_stats_refresh_fails(monkeypatch, incidents):
    class A(Recorder):
        async def has_open_issue(self, mid): return False
        async def approve_match(self, mid, by): pass
        async def get_match(self, mid): return {"id": mid, "match_id": "CQ-0042"}
    fake = A([KeyError(73), KeyError(81)]); monkeypatch.setattr(match, "adb", fake)
    c = match.Match.__new__(match.Match); c.bot = None

    async def cleanup(guild, m): pass
    c._run_post_approval_cleanup = cleanup
    assert await c._do_approve(None, 7, 1) == (True, "approved")        # MMR already committed; stats are best-effort
    assert incidents and "CQ-0042" in incidents[0]["summary"]
