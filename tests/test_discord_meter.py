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
