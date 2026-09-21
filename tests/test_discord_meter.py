"""Tests for the per-minute Discord call meter."""
import pytest
from utils import discord_meter


@pytest.fixture(autouse=True)
def clean():
    discord_meter.reset()
    yield
    discord_meter.reset()


def test_record_and_snapshot():
    discord_meter._record("POST", "/interactions/123/abc/callback", "ok")
    discord_meter._record("POST", "/interactions/123/abc/callback", "ok")
    discord_meter._record("POST", "/interactions/456/def/callback", "429")
    snap = discord_meter.snapshot()
    # paths simplified: digits/tokens → x
    ok_key = [k for k in snap if k[3] == "ok"]
    err_key = [k for k in snap if k[3] == "429"]
    assert len(ok_key) == 1 and snap[ok_key[0]] == 2
    assert len(err_key) == 1 and snap[err_key[0]] == 1


def test_simplify_path():
    assert discord_meter._simplify_path("/channels/123456/messages") == "/channels/x/messages"
    assert discord_meter._simplify_path("/interactions/999/abcdef123456789012345/callback") == "/interactions/x/x/callback"


def test_record_never_raises():
    # Even with garbage input, _record must not raise.
    discord_meter._record(None, None, None)  # type: ignore
    # No assertion needed — survival is the test.


async def test_flush_logs_one_line(caplog):
    import logging
    # _record uses _minute_key() internally, so we insert under a known key directly.
    prev_minute = "2000-01-01T00:00"
    discord_meter._counts[(prev_minute, "POST", "/channels/x/messages", "ok")] = 1
    discord_meter._counts[(prev_minute, "POST", "/channels/x/messages", "429")] = 1
    # Set last flush to that minute so next flush sees a rollover.
    discord_meter._last_flush_minute = prev_minute
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        await discord_meter._flush_if_new_minute()
    meter_lines = [r for r in caplog.records if "DISCORD_METER" in r.getMessage()]
    assert len(meter_lines) == 1
    msg = meter_lines[0].getMessage()
    assert "ok=1" in msg and "429=1" in msg


# ---------------- callback kinds, catch-up flush, timer, wrapper ----------------

def test_callback_kind_names_the_response_type():
    cb = "/interactions/{webhook_id}/{webhook_token}/callback"
    assert discord_meter._callback_kind(cb, {"payload": {"type": 7}}) == "edit_message"
    assert discord_meter._callback_kind(cb, {"payload": {"type": 6}}) == "defer_update"
    assert discord_meter._callback_kind(cb, {"payload": {"type": 5}}) == "defer_reply"
    assert discord_meter._callback_kind(cb, {"payload": {"type": 4}}) == "send_message"
    # payload sent as multipart (when files are attached)
    mp = {"multipart": [{"name": "payload_json", "value": '{"type": 4}'}]}
    assert discord_meter._callback_kind(cb, mp) == "send_message"


def test_callback_kind_is_empty_for_other_routes_and_garbage():
    assert discord_meter._callback_kind("/webhooks/{webhook_id}/{webhook_token}", {"payload": {"type": 7}}) == ""
    assert discord_meter._callback_kind("/interactions/x/y/callback", {"payload": "junk"}) == ""
    assert discord_meter._callback_kind(None, None) == ""          # must never raise


def test_record_keeps_kinds_apart():
    cb = "/interactions/{webhook_id}/{webhook_token}/callback"      # what discord.py really passes
    discord_meter._record("POST", cb, "ok", "edit_message")
    discord_meter._record("POST", cb, "ok", "defer_update")
    labels = sorted(k[2] for k in discord_meter.snapshot())
    assert labels == [cb + " [defer_update]", cb + " [edit_message]"]


async def test_flush_catches_up_every_finished_minute(caplog):
    import logging
    for minute in ("2000-01-01T00:00", "2000-01-01T00:05"):
        discord_meter._counts[(minute, "POST", "/channels/x/messages", "ok")] = 1
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        await discord_meter._flush_if_new_minute()
    lines = [r.getMessage() for r in caplog.records if "DISCORD_METER" in r.getMessage()]
    assert len(lines) == 2 and "minute=2000-01-01T00:00" in lines[0] and "minute=2000-01-01T00:05" in lines[1]
    assert discord_meter.snapshot() == {}


async def test_timer_prints_a_finished_minute_without_any_new_call(caplog):
    import asyncio, logging
    discord_meter._counts[("2000-01-01T00:00", "POST", "/channels/x/messages", "ok")] = 3
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        discord_meter._start_ticker(interval=0.01)
        await asyncio.sleep(0.1)
        discord_meter._stop_ticker()
    assert any("DISCORD_METER" in r.getMessage() and "ok=3" in r.getMessage() for r in caplog.records)


class _Route:
    method, path = "POST", "/interactions/{webhook_id}/{webhook_token}/callback"


async def test_webhook_wrapper_is_transparent_and_labels_the_call():
    async def original(self, route, session, **kw):
        return "RESULT"
    wrapped = discord_meter._wrap_webhook_request(original)
    assert await wrapped(None, _Route(), None, payload={"type": 7}) == "RESULT"      # returns what the original returns
    assert any("[edit_message]" in k[2] and k[3] == "ok" for k in discord_meter.snapshot())


async def test_webhook_wrapper_records_429_and_reraises():
    class Boom(Exception):
        response = type("R", (), {"status": 429})()
    async def original(self, route, session, **kw):
        raise Boom()
    wrapped = discord_meter._wrap_webhook_request(original)
    with pytest.raises(Boom):
        await wrapped(None, _Route(), None, payload={"type": 6})
    assert any("[defer_update]" in k[2] and k[3] == "429" for k in discord_meter.snapshot())


# ---------------- per-second peaks + 429 forensics (2026-09-22) ----------------

BASE = 946684800          # 2000-01-01T00:00:00Z


def test_record_counts_per_second_by_category():
    for _ in range(3):
        discord_meter._record("POST", "/x", "ok", "", "reply", BASE + 5.2)
    discord_meter._record("POST", "/y", "ok", "", "chan", BASE + 5.9)
    assert discord_meter._sec_hits[("reply", BASE + 5)] == 3
    assert discord_meter._sec_hits[("chan", BASE + 5)] == 1


async def test_minute_line_carries_the_busiest_second_and_five_seconds(caplog):
    import logging
    discord_meter._counts[("2000-01-01T00:00", "POST", "/x", "ok")] = 12
    for sec, n in ((1, 4), (2, 3), (10, 5)):
        discord_meter._sec_hits[("reply", BASE + sec)] = n
    discord_meter._sec_hits[("chan", BASE + 3)] = 2
    with caplog.at_level(logging.INFO, logger="champions_queue"):
        await discord_meter._flush_if_new_minute()
    line = [r.getMessage() for r in caplog.records if "DISCORD_METER" in r.getMessage()][0]
    assert "peak1s_reply=5" in line and "peak5s_reply=7" in line      # 4+3 in seconds 1-2 beats the lone 5
    assert "peak1s_chan=2" in line and "peak5s_chan=2" in line
    assert discord_meter._sec_hits == {}                              # consumed


class _Resp429:
    status = 429

    def __init__(self, headers):
        self.headers = headers


class _Boom(Exception):
    code = 0
    text = "Rate limit reached for webhook"

    def __init__(self, headers=None):
        self.response = _Resp429(headers if headers is not None else {})


class _RouteWithSecret(_Route):
    url = "https://discord.com/api/v10/interactions/1/SECRETTOKEN123/callback"


async def test_429_writes_one_forensic_line_with_headers_and_recent_burst(caplog):
    import logging, time
    headers = {"X-RateLimit-Scope": "shared", "X-RateLimit-Limit": "5",
               "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.5"}
    for _ in range(4):                                   # a burst just before the failing call
        discord_meter._record("POST", "/webhooks/{webhook_id}/{webhook_token}", "ok", "", "reply", time.time())
    async def original(self, route, session, **kw):
        raise _Boom(headers)
    wrapped = discord_meter._wrap_webhook_request(original)
    with caplog.at_level(logging.WARNING, logger="champions_queue"):
        with pytest.raises(_Boom):
            await wrapped(None, _RouteWithSecret(), None, payload={"type": 6})
    lines = [r.getMessage() for r in caplog.records if "DISCORD_429" in r.getMessage()]
    assert len(lines) == 1
    msg = lines[0]
    assert "[defer_update]" in msg and "cat=reply" in msg
    assert "shared" in msg and "Reset-After" in msg and "Rate limit reached for webhook" in msg
    assert "recent_5s=5" in msg                          # 4 earlier calls + this one
    assert "SECRETTOKEN123" not in caplog.text           # only the route template is ever logged


async def test_429_lines_are_capped_per_minute(caplog, monkeypatch):
    import logging
    monkeypatch.setattr(discord_meter, "_now", lambda: BASE + 0.5)      # freeze the clock: one minute
    monkeypatch.setattr(discord_meter, "_MAX_429_LINES_PER_MINUTE", 3)
    async def original(self, route, session, **kw):
        raise _Boom()
    wrapped = discord_meter._wrap_webhook_request(original)
    with caplog.at_level(logging.WARNING, logger="champions_queue"):
        for _ in range(8):
            with pytest.raises(_Boom):
                await wrapped(None, _Route(), None, payload={"type": 4})
    assert len([r for r in caplog.records if "DISCORD_429" in r.getMessage()]) == 3
    assert sum(v for k, v in discord_meter.snapshot().items() if k[3] == "429") == 8   # all still counted


async def test_forensics_survive_a_missing_or_odd_response():
    class Odd(Exception):
        response = None
        text = None
    async def original(self, route, session, **kw):
        raise Odd()
    wrapped = discord_meter._wrap_webhook_request(original)
    with pytest.raises(Odd):
        await wrapped(None, _Route(), None, payload={"type": 4})            # no crash inside the meter


async def test_meter_can_never_change_the_outcome_of_a_call(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("metering bug")
    monkeypatch.setattr(discord_meter, "_record", broken)
    async def ok_call(self, route, session, **kw):
        return "RESULT"
    async def failing_call(self, route, session, **kw):
        raise _Boom()
    assert await discord_meter._wrap_webhook_request(ok_call)(None, _Route(), None, payload={"type": 4}) == "RESULT"
    with pytest.raises(_Boom):                                              # the ORIGINAL error, not the meter's
        await discord_meter._wrap_webhook_request(failing_call)(None, _Route(), None, payload={"type": 4})
