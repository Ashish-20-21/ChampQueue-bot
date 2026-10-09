"""Badges used to appear only after a manual SQL backfill because nothing called
check_and_grant_achievements. It now runs after every approval (background) and
when someone opens /achievements."""
import asyncio

import pytest

from cogs import match, stats
from database import db as dbmod


class _Exec:
    def __init__(self, data): self.data = data
    def execute(self): return self


def test_db_binding_returns_only_newly_granted_codes():
    sent = []

    class Client:
        def rpc(self, name, params):
            sent.append((name, params))
            return _Exec([{"granted_code": "veteran"}, {"granted_code": None}, {"granted_code": "grinder"}])
    fake_db = type("D", (), {"client": Client()})()
    assert dbmod.Database.grant_player_achievements(fake_db, 7) == ["veteran", "grinder"]
    assert sent == [("check_and_grant_achievements", {"p_player_id": 7})]

    class Empty:
        def rpc(self, name, params): return _Exec(None)
    assert dbmod.Database.grant_player_achievements(type("D", (), {"client": Empty()})(), 7) == []


async def test_grant_runs_for_every_player_and_survives_failures(monkeypatch):
    calls = []

    class A:
        async def grant_player_achievements(self, pid):
            calls.append(pid)
            if pid == 2:
                raise RuntimeError("boom")
            return ["initiator"]
    monkeypatch.setattr(match, "adb", A())
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))      # no real pauses in tests
    c = match.Match.__new__(match.Match)
    await c._grant_achievements([1, 2, 3], "CQ-1")                      # must not raise
    assert calls == [1, 2, 3]                                           # player 2 failing did not stop player 3


async def test_approval_spawns_the_grant_after_the_stats_refresh(monkeypatch):
    order = []

    class A:
        async def has_open_issue(self, mid): return False
        async def approve_match(self, mid, by): order.append("approve")
        async def get_match(self, mid): return {"id": mid, "match_id": "CQ-0042"}
        async def get_match_players(self, mid): return [{"player_id": 11}, {"player_id": 12}]
        async def recompute_player_career_stats_bulk(self, ids): order.append("recompute"); return []
        async def grant_player_achievements(self, pid): order.append(f"grant:{pid}"); return []
    monkeypatch.setattr(match, "adb", A())
    c = match.Match.__new__(match.Match); c.bot = None

    async def cleanup(guild, m): pass
    c._run_post_approval_cleanup = cleanup
    assert await c._do_approve(None, 7, 1) == (True, "approved")
    for _ in range(40):                                                  # let the background task finish
        await asyncio.sleep(0.05)
        if "grant:12" in order:
            break
    assert order[:2] == ["approve", "recompute"] and order[2:] == ["grant:11", "grant:12"]


def test_achievements_command_refreshes_before_reading_badges():
    import inspect
    src = inspect.getsource(stats.Stats.achievements.callback)
    assert src.index("adb.grant_player_achievements") < src.index("adb.get_player_achievements")
