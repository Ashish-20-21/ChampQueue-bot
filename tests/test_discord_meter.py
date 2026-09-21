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
