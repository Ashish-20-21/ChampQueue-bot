"""Tests for the per-interaction timing meter (utils/interaction_timer.py)."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from utils import discord_meter, interaction_timer as it


def fake_interaction(custom_id="join_queue_INDIA_ME", name=None, age_ms=80, itype="component"):
    data = {"name": name} if name else {"custom_id": custom_id}
    return SimpleNamespace(
        created_at=datetime.now(timezone.utc) - timedelta(milliseconds=age_ms),
        data=data,
        type=SimpleNamespace(name=itype),
    )


def timing_lines(caplog):
    return [r.getMessage() for r in caplog.records if "INTERACTION_TIMING" in r.getMessage()]


@pytest.fixture(autouse=True)
def restore_patches():
    yield
    it.uninstall()


# ── labels ─────────────────────────────────────────────────────────

def test_normalize_action():
    assert it.normalize_action("join_queue_INDIA_ME", "component") == "join_queue_INDIA_ME"
    # random hex ids (operator-skill buttons) collapse to the fallback name
    assert it.normalize_action("a3f9c2d7e1b84c6f9a0b1c2d3e4f5a6b", "component") == "component"
    # long digit runs (match ids, user ids) don't blow up the log's variety
    assert it.normalize_action("approve_1234567", "component") == "approve_#"
    assert it.normalize_action(None, "modal") == "modal"


# ── the wrapper ────────────────────────────────────────────────────

async def test_wrapper_logs_one_line_and_returns_result(caplog):
    async def original(self, item, interaction):
        it.note_discord("defer_update", 120.0, "reply")
        it.note_db("queue_join", 300.0, 4.0)
        it.note_retry()
        it.note_discord("webhook_edit", 90.0, "reply")
        return "result"

    wrapped = it._make_wrapper(original, "component")
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        out = await wrapped(object(), object(), fake_interaction())
    assert out == "result"
    lines = timing_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "kind=component" in line and "action=join_queue_INDIA_ME" in line
    assert "db=300(n=1,pool=4,retry=1)" in line
    assert "discord=210(n=2)" in line
    assert "ack=" in line and "done=" in line and "arrive=" in line
    assert "db.queue_join=300" in line and "discord.defer_update=120" in line


async def test_wrapper_passes_exceptions_through_and_still_logs(caplog):
    async def original(self, interaction):
        raise ValueError("boom")

    wrapped = it._make_wrapper(original, "command")
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        with pytest.raises(ValueError):
            await wrapped(object(), fake_interaction(name="queue-status", itype="application_command"))
    assert len(timing_lines(caplog)) == 1


async def test_original_always_runs_even_if_timing_setup_breaks(monkeypatch):
    def broken(kind, args):
        raise RuntimeError("meter bug")
    # _start is guarded internally, but prove the wrapper survives even a raw failure there
    monkeypatch.setattr(it, "_start", lambda kind, args: None)

    async def original(self, interaction):
        return 42

    wrapped = it._make_wrapper(original, "command")
    assert await wrapped(object(), fake_interaction(name="x")) == 42


async def test_autocomplete_is_not_timed(caplog):
    async def original(self, interaction):
        return 1

    wrapped = it._make_wrapper(original, "command")
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        await wrapped(object(), fake_interaction(name="x", itype="autocomplete"))
    assert timing_lines(caplog) == []


async def test_slow_flag_and_arrive_clamp(caplog):
    # a clock that is slightly ahead of the host must not give a negative arrive
    async def original(self, interaction):
        it.note_discord("send_message", 2500.0, "reply")

    wrapped = it._make_wrapper(original, "command")
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        await wrapped(object(), fake_interaction(name="slowcmd", age_ms=-500))
    line = timing_lines(caplog)[0]
    assert "arrive=0" in line


async def test_records_after_finish_are_ignored(caplog):
    captured = {}

    async def original(self, interaction):
        captured["t"] = it.current()

    wrapped = it._make_wrapper(original, "command")
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        await wrapped(object(), fake_interaction(name="x"))
    t = captured["t"]
    assert t.closed
    # a background task finishing later must not change a closed timing
    before = t.db_n
    token = it._current.set(t)
    try:
        it.note_db("late_call", 10.0, 0.0)
    finally:
        it._current.reset(token)
    assert t.db_n == before


def test_no_context_means_no_recording():
    assert it.current() is None
    it.note_db("x", 1.0, 0.0)      # must not raise
    it.note_retry()
    it.note_discord("x", 1.0, "reply")
    it.note_lock(5.0)


# ── DB timing ──────────────────────────────────────────────────────

async def test_timed_db_returns_result_and_records():
    t = it._Timing("component", "x", 0.0)
    token = it._current.set(t)
    try:
        def work(a, b=1):
            time.sleep(0.02)
            return a + b
        assert await it.timed_db("work", work, (1,), {"b": 2}) == 3
    finally:
        it._current.reset(token)
    assert t.db_n == 1 and t.db_ms >= 15
    assert t.steps[0][0] == "db.work"


async def test_timed_db_propagates_exceptions_and_still_records():
    t = it._Timing("component", "x", 0.0)
    token = it._current.set(t)
    try:
        def boom():
            raise KeyError("bad")
        with pytest.raises(KeyError):
            await it.timed_db("boom", boom, (), {})
    finally:
        it._current.reset(token)
    assert t.db_n == 1


async def test_db_proxy_times_calls_inside_an_interaction_only():
    from database.db import _AsyncDatabaseProxy

    class FakeDb:
        def get_thing(self, x):
            return x * 2

    proxy = _AsyncDatabaseProxy(FakeDb())
    # outside an interaction: plain call, nothing recorded, same result
    assert await proxy.get_thing(2) == 4
    t = it._Timing("component", "x", 0.0)
    token = it._current.set(t)
    try:
        assert await proxy.get_thing(3) == 6
    finally:
        it._current.reset(token)
    assert t.db_n == 1 and t.steps[0][0] == "db.get_thing"


async def test_with_retry_counts_retries():
    import httpx
    from database.db import with_retry

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.RemoteProtocolError("Server disconnected")
        return "ok"

    t = it._Timing("component", "x", 0.0)
    token = it._current.set(t)
    try:
        assert await with_retry(flaky, base_delay=0.0) == "ok"
    finally:
        it._current.reset(token)
    assert t.retries == 1


# ── lock timing ────────────────────────────────────────────────────

async def test_timed_lock_records_wait_and_behaves_like_a_lock():
    lock = asyncio.Lock()
    t = it._Timing("component", "x", 0.0)
    token = it._current.set(t)

    async def holder():
        async with lock:
            await asyncio.sleep(0.05)

    try:
        h = asyncio.create_task(holder())
        await asyncio.sleep(0)            # let holder take the lock
        async with it.timed_lock(lock):
            assert lock.locked()
        await h
    finally:
        it._current.reset(token)
    assert not lock.locked()
    assert t.lock_ms >= 30


async def test_timed_lock_releases_on_error():
    lock = asyncio.Lock()
    with pytest.raises(RuntimeError):
        async with it.timed_lock(lock):
            raise RuntimeError("inside")
    assert not lock.locked()


# ── Discord meter hook ─────────────────────────────────────────────

def test_discord_meter_after_call_reports_into_the_timer():
    discord_meter.reset()
    t = it._Timing("component", "x", 0.0)
    token = it._current.set(t)
    try:
        discord_meter._after_call(
            "POST", "/interactions/{webhook_id}/{webhook_token}/callback",
            "defer_update", "reply", time.time() - 0.15, None)
        discord_meter._after_call(
            "PATCH", "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}",
            "", "reply", time.time() - 0.2, None)
    finally:
        it._current.reset(token)
        discord_meter.reset()
    assert t.disc_n == 2
    names = [n for n, _ in t.steps]
    assert names == ["discord.defer_update", "discord.webhook_edit"]
    assert t.first_ack is not None and t.last_reply >= t.first_ack


def test_timing_label():
    assert discord_meter._timing_label("POST", "/x", "edit_message", "reply") == "edit_message"
    assert discord_meter._timing_label("POST", "/webhooks/{a}/{b}", "", "reply") == "followup_send"
    assert discord_meter._timing_label("POST", "/channels/123456/messages", "", "chan") == "chan.POST channels/x/messages"


# ── install / uninstall ────────────────────────────────────────────

def test_install_patches_and_uninstall_restores():
    from discord import app_commands, ui
    originals = (ui.View._scheduled_task, ui.Modal._scheduled_task, app_commands.CommandTree._call)
    it.install()
    assert ui.View._scheduled_task is not originals[0]
    assert ui.Modal._scheduled_task is not originals[1]
    assert app_commands.CommandTree._call is not originals[2]
    it.install()   # second call is a no-op, must not double-wrap
    assert ui.View._scheduled_task.__wrapped__ is originals[0]
    it.uninstall()
    assert (ui.View._scheduled_task, ui.Modal._scheduled_task, app_commands.CommandTree._call) == originals


def test_install_respects_the_switch(monkeypatch):
    import switches
    from discord import ui
    original = ui.View._scheduled_task
    monkeypatch.setattr(switches, "INTERACTION_TIMING", False)
    it.install()
    assert ui.View._scheduled_task is original
