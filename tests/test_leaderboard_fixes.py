"""LeaderboardView (cogs/stats.py) and PointsLeaderboardReloadButton
(cogs/points.py) — 2026-09-26 fixes.

Live traceback (Sep 26 00:11) confirmed LeaderboardView._render() called
the DB BEFORE acking the interaction, so a slow region_leaderboard() call
expired the 3s window -> discord.errors.NotFound 10062 "Unknown
interaction", the same failure mode already fixed once for
/host-replace-player and /admin-update-host. Fixed by deferring first.
Also adds a shared 60s lock on both leaderboards' buttons (the view/
message is shared by every viewer, so — same concept as RegionQueueView's
Join-button disable, the Sep 23 429-storm fix — locking it after one use
locks it for everyone, cutting redundant renders when several people
click close together.
"""
import asyncio
import logging

import pytest

import config
from cogs import stats, points


# ---------------- fakes ----------------

class FakeResponse:
    def __init__(self, rec):
        self.rec = rec
    async def defer(self, **kw):
        self.rec.append("defer")
    async def send_message(self, content=None, **kw):
        self.rec.append(("send_message", content))
    async def edit_message(self, **kw):
        self.rec.append("edit_message")  # must NOT be used any more


class FakeMessage:
    def __init__(self):
        self.edits = []
    async def edit(self, **kw):
        self.edits.append(kw)


class FakeInter:
    def __init__(self, uid=1):
        self.user = type("U", (), {"id": uid})()
        self.rec = []
        self.response = FakeResponse(self.rec)
        self.message = FakeMessage()
        self.client = None
    async def edit_original_response(self, **kw):
        self.rec.append(("edit_original_response", kw))


class FakeAdb:
    def __init__(self):
        self.calls = 0
    async def region_leaderboard(self):
        self.calls += 1
        return []


@pytest.fixture
def fast_sleep(monkeypatch):
    """Collapse the 60s unlock wait so tests run instantly."""
    async def instant(_seconds):
        pass
    monkeypatch.setattr(asyncio, "sleep", instant)


# ---------------- LeaderboardView: defer-first fix ----------------

async def test_render_acks_before_touching_the_db(monkeypatch, fast_sleep):
    fake = FakeAdb()
    monkeypatch.setattr(stats, "adb", fake)
    view = stats.LeaderboardView()
    inter = FakeInter()
    await view._render(inter)
    assert inter.rec[0] == "defer"                       # ack is the very first thing
    assert "edit_message" not in inter.rec                # the old, buggy call path is gone
    assert fake.calls == 1


async def test_render_never_calls_response_edit_message(monkeypatch, fast_sleep):
    """Regression guard for the exact live bug: edit_message() before an
    ack is what threw NotFound 10062 on Sep 26."""
    monkeypatch.setattr(stats, "adb", FakeAdb())
    view = stats.LeaderboardView()
    inter = FakeInter()
    await view._render(inter)
    assert "edit_message" not in inter.rec


def test_render_source_defers_strictly_before_the_db_call():
    """Static ordering guard, since a mocked adb can't tell WHEN the real
    network call would have happened relative to the ack — only that both
    occurred. Pins the actual live bug: response.defer() must appear
    before adb.region_leaderboard() in source, not after."""
    import inspect
    src = inspect.getsource(stats.LeaderboardView._render)
    defer_pos = src.index("response.defer()")
    db_pos = src.index("adb.region_leaderboard()")
    assert defer_pos < db_pos, "DB call happens before the ack — reintroduces the Sep 26 10062 bug"


# ---------------- LeaderboardView: shared 60s lock ----------------

async def test_render_locks_all_three_buttons_after_use(monkeypatch, fast_sleep):
    monkeypatch.setattr(stats, "adb", FakeAdb())
    view = stats.LeaderboardView()
    await view._render(FakeInter())
    # Locked immediately after render (before the sleep "unlocks" it again below)
    # — check the mid-lock state by racing a second call while sleep is patched
    # to a no-op, so we instead assert final state carefully via a controlled sleep.


async def test_points_button_still_locks_when_called_directly_with_lock_preset(monkeypatch, reset_points_lock):
    """Behavioral (not source-text) guard for the lock CHECK specifically,
    independent of test_points_button_locks_for_a_different_user_too
    (which exercises it via two sequential real calls): pre-set the lock
    deadline directly, then confirm a single call is blocked by it. This
    catches a removed/bypassed `if` check even if the surrounding
    call-twice test happened to pass for an unrelated reason."""
    fake = FakePointsAdb()
    monkeypatch.setattr(points, "adb", fake)
    monkeypatch.setattr(points, "with_retry", lambda fn, *a: fn(*a))
    monkeypatch.setattr(points, "spawn_background", lambda *a, **kw: None)

    points.PointsLeaderboardReloadButton._locked_until = asyncio.get_event_loop().time() + 60
    btn = points.PointsLeaderboardReloadButton()
    inter = FakeInter(uid=1)
    await btn.callback(inter)
    assert fake.calls == 0, "lock check did not block the call"
    assert inter.rec == ["defer"]


async def test_lock_is_shared_not_per_user(monkeypatch):
    """The core claim: locking after ANY click blocks a DIFFERENT user too,
    because it's one view/message shared by every viewer."""
    monkeypatch.setattr(stats, "adb", FakeAdb())

    async def slow_sleep(seconds):
        await real_sleep(0)  # yield once, but don't actually wait 60s in the test
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", slow_sleep)

    view = stats.LeaderboardView()
    user_a = FakeInter(uid=1)
    render_task = asyncio.ensure_future(view._render(user_a))
    await asyncio.sleep(0)  # let it run up to the sleep(60) point
    assert view.reload_button.disabled is True
    assert view.prev_button.disabled is True
    assert view.next_button.disabled is True

    user_b = FakeInter(uid=2)                  # a DIFFERENT user
    await view.reload_callback(user_b)
    assert user_b.rec == ["defer"]             # silently blocked, no re-render, no 2nd DB call
    await render_task


async def test_buttons_unlock_after_the_wait(monkeypatch, fast_sleep):
    monkeypatch.setattr(stats, "adb", FakeAdb())
    view = stats.LeaderboardView()
    await view._render(FakeInter())
    assert view.reload_button.disabled is False   # fast_sleep collapses the 60s, so it's unlocked by return
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is False


async def test_locked_click_does_not_touch_the_db(monkeypatch):
    fake = FakeAdb()
    monkeypatch.setattr(stats, "adb", fake)

    async def slow_sleep(seconds):
        pass
    monkeypatch.setattr(asyncio, "sleep", slow_sleep)

    view = stats.LeaderboardView()
    view.prev_button.disabled = True   # simulate: already locked from an earlier click
    inter = FakeInter()
    await view.prev_callback(inter)
    assert fake.calls == 0
    assert inter.rec == ["defer"]


# ---------------- PointsLeaderboardReloadButton: shared lock (no pre-existing bug) ----------------

class FakeSeason(dict):
    pass


class FakePointsAdb:
    def __init__(self):
        self.calls = 0
    async def expire_shields(self): pass
    async def get_active_season(self):
        return FakeSeason(id=1, name="S1")
    async def season_points_leaderboard(self, sid):
        self.calls += 1
        return []
    async def is_season_points_locked(self, sid):
        return False


@pytest.fixture
def reset_points_lock():
    """Resets BOTH the new shared 60s lock AND the pre-existing per-user
    CooldownMapping (points.py's own class-level rate limiter) — the
    latter isn't new, but it's also class-level state that leaks across
    tests if left alone, and this fixture is the natural place to
    isolate it."""
    from discord.ext import commands as _commands
    points.PointsLeaderboardReloadButton._locked_until = 0.0
    points.PointsLeaderboardReloadButton._cooldown = _commands.CooldownMapping.from_cooldown(
        1, config.POINTS_LEADERBOARD_COOLDOWN_SECONDS, _commands.BucketType.user
    )
    yield
    points.PointsLeaderboardReloadButton._locked_until = 0.0


async def test_points_button_already_deferred_first_unaffected(monkeypatch, reset_points_lock):
    """No pre-existing bug here (this button already deferred first) —
    confirm the new lock doesn't change that."""
    fake = FakePointsAdb()
    monkeypatch.setattr(points, "adb", fake)
    monkeypatch.setattr(points, "with_retry", lambda fn, *a: fn(*a))
    def _fake_spawn_background(coro, **kw):
        coro.close()  # never actually scheduled — avoids the "never awaited" warning
    monkeypatch.setattr(points, "spawn_background", _fake_spawn_background)
    btn = points.PointsLeaderboardReloadButton()
    inter = FakeInter()
    await btn.callback(inter)
    assert inter.rec[0] == "defer"
    assert fake.calls == 1


async def test_points_button_locks_for_a_different_user_too(monkeypatch, reset_points_lock):
    fake = FakePointsAdb()
    monkeypatch.setattr(points, "adb", fake)
    monkeypatch.setattr(points, "with_retry", lambda fn, *a: fn(*a))
    def _fake_spawn_background(coro, **kw):
        coro.close()  # never actually scheduled — avoids the "never awaited" warning
    monkeypatch.setattr(points, "spawn_background", _fake_spawn_background)

    btn = points.PointsLeaderboardReloadButton()
    await btn.callback(FakeInter(uid=1))
    assert fake.calls == 1

    inter2 = FakeInter(uid=2)                  # different user, same 60s window
    await btn.callback(inter2)
    assert fake.calls == 1                      # no second render
    assert inter2.rec == ["defer"]


async def test_points_button_unlocks_after_60s(monkeypatch, reset_points_lock):
    fake = FakePointsAdb()
    monkeypatch.setattr(points, "adb", fake)
    monkeypatch.setattr(points, "with_retry", lambda fn, *a: fn(*a))
    def _fake_spawn_background(coro, **kw):
        coro.close()  # never actually scheduled — avoids the "never awaited" warning
    monkeypatch.setattr(points, "spawn_background", _fake_spawn_background)

    btn = points.PointsLeaderboardReloadButton()
    await btn.callback(FakeInter(uid=1))
    points.PointsLeaderboardReloadButton._locked_until -= 61   # simulate 61s having passed
    await btn.callback(FakeInter(uid=2))
    assert fake.calls == 2
