"""LeaderboardView._render() (cogs/stats.py) — 2026-09-26 fix.

Live traceback (Sep 26 00:11) confirmed _render() called the DB BEFORE
acking the interaction, so a slow region_leaderboard() call expired the
3s window -> discord.errors.NotFound 10062 "Unknown interaction" (same
failure class already fixed for /host-replace-player and
/admin-update-host). Fixed by deferring first. No lock/cooldown change —
that part was reverted separately as its own, unrelated design issue.
"""
import pytest

from cogs import stats


class FakeResponse:
    def __init__(self, rec):
        self.rec = rec
    async def defer(self, **kw):
        self.rec.append("defer")
    async def edit_message(self, **kw):
        self.rec.append("edit_message")  # the old, buggy call — must not be used


class FakeInter:
    def __init__(self):
        self.rec = []
        self.response = FakeResponse(self.rec)
    async def edit_original_response(self, **kw):
        self.rec.append("edit_original_response")


class FakeAdb:
    def __init__(self):
        self.calls = 0
    async def region_leaderboard(self):
        self.calls += 1
        return []


async def test_render_acks_before_touching_the_db(monkeypatch):
    fake = FakeAdb()
    monkeypatch.setattr(stats, "adb", fake)
    view = stats.LeaderboardView()
    inter = FakeInter()
    await view._render(inter)
    assert inter.rec[0] == "defer"            # ack is the very first thing
    assert "edit_message" not in inter.rec     # the buggy old call path is gone
    assert fake.calls == 1


def test_render_source_defers_strictly_before_the_db_call():
    """Static ordering guard — pins the actual live bug: response.defer()
    must appear before adb.region_leaderboard() in source, not after."""
    import inspect
    src = inspect.getsource(stats.LeaderboardView._render)
    defer_pos = src.index("response.defer()")
    db_pos = src.index("adb.region_leaderboard()")
    assert defer_pos < db_pos, "DB call happens before the ack — reintroduces the Sep 26 10062 bug"
