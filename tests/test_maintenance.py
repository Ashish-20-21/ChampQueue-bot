"""Tests for the nightly maintenance notice (cogs/maintenance.py).

No real Discord or filesystem outside tmp_path."""
import json
import time

import pytest

import config
from cogs import maintenance as m


class FakeMessage:
    def __init__(self, mid):
        self.id = mid


class FakePartialMessage:
    def __init__(self, store, mid, fail):
        self.store, self.mid, self.fail = store, mid, fail

    async def edit(self, content):
        if self.fail:
            raise RuntimeError("edit failed")
        self.store.append((self.mid, content))


class FakePartialMessageable:
    def __init__(self, store, fail):
        self.store, self.fail = store, fail

    def get_partial_message(self, mid):
        return FakePartialMessage(self.store, mid, self.fail)


class FakeChannel:
    def __init__(self, cid=555, fail=False):
        self.id, self.fail, self.sent = cid, fail, []

    async def send(self, content, **kw):
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(content)
        return FakeMessage(900 + len(self.sent))


class FakeBot:
    def __init__(self, channel=None, edit_fails=False):
        self.channel = channel
        self.edits = []
        self.edit_fails = edit_fails

    def get_channel(self, cid):
        return self.channel

    async def fetch_channel(self, cid):
        raise RuntimeError("not found")

    def get_partial_messageable(self, cid):
        return FakePartialMessageable(self.edits, self.edit_fails)


@pytest.fixture(autouse=True)
def _no_botlog_no_db(monkeypatch):
    monkeypatch.setattr(config, "BOTLOG_CHANNEL_ID", None)

    async def fake_queue_current(*a, **kw):
        return [1, 2, 3]

    async def fake_with_retry(fn, *a, **kw):
        return await fake_queue_current()

    monkeypatch.setattr(m, "with_retry", fake_with_retry)


def make(tmp_path, bot, enabled=True, channel_id=555):
    return m.Maintenance(bot, enabled=enabled, channel_id=channel_id, marker_path=tmp_path / "data" / "planned_restart.json")


def put_marker(cog, age_seconds=0):
    cog.marker_path.parent.mkdir(parents=True, exist_ok=True)
    cog.marker_path.write_text(json.dumps({"channel_id": 555, "message_id": 901, "posted_ts": time.time() - age_seconds}))


async def test_notice_posts_and_writes_marker(tmp_path):
    ch = FakeChannel()
    cog = make(tmp_path, FakeBot(ch))
    await cog._do_notice()
    assert ch.sent == [m.NAP_TEXT]
    data = json.loads(cog.marker_path.read_text())
    assert data["channel_id"] == 555 and data["message_id"] == 901


async def test_notice_send_failure_leaves_no_marker(tmp_path):
    cog = make(tmp_path, FakeBot(FakeChannel(fail=True)))
    await cog._do_notice()
    assert not cog.marker_path.exists()


async def test_notice_channel_missing_does_nothing(tmp_path):
    cog = make(tmp_path, FakeBot(None))
    await cog._do_notice()
    assert not cog.marker_path.exists()


async def test_boot_with_fresh_marker_edits_to_back_and_clears(tmp_path):
    bot = FakeBot(FakeChannel())
    cog = make(tmp_path, bot)
    put_marker(cog, age_seconds=90)
    await cog._do_boot()
    assert bot.edits == [(901, m.BACK_TEXT)]
    assert not cog.marker_path.exists()


async def test_boot_without_marker_is_silent(tmp_path):
    bot = FakeBot(FakeChannel())
    cog = make(tmp_path, bot)
    await cog._do_boot()
    assert bot.edits == []


async def test_boot_runs_only_once_per_process(tmp_path):
    bot = FakeBot(FakeChannel())
    cog = make(tmp_path, bot)
    put_marker(cog)
    await cog._do_boot()
    put_marker(cog)  # a later gateway reconnect fires on_ready again
    await cog._do_boot()
    assert len(bot.edits) == 1


async def test_boot_with_stale_marker_clears_without_editing(tmp_path):
    bot = FakeBot(FakeChannel())
    cog = make(tmp_path, bot)
    put_marker(cog, age_seconds=m.MARKER_MAX_AGE_SECONDS + 60)
    await cog._do_boot()
    assert bot.edits == []
    assert not cog.marker_path.exists()


async def test_boot_edit_failure_does_not_raise_and_marker_still_cleared(tmp_path):
    bot = FakeBot(FakeChannel(), edit_fails=True)
    cog = make(tmp_path, bot)
    put_marker(cog)
    await cog._do_boot()
    assert not cog.marker_path.exists()


async def test_check_marker_present_means_restart_skipped(tmp_path):
    bot = FakeBot(FakeChannel())
    cog = make(tmp_path, bot)
    put_marker(cog, age_seconds=480)
    await cog._do_check()
    assert bot.edits == [(901, m.SKIPPED_TEXT)]
    assert not cog.marker_path.exists()


async def test_check_without_marker_does_nothing(tmp_path):
    bot = FakeBot(FakeChannel())
    cog = make(tmp_path, bot)
    await cog._do_check()
    assert bot.edits == []


async def test_corrupt_marker_is_removed_not_fatal(tmp_path):
    cog = make(tmp_path, FakeBot(FakeChannel()))
    cog.marker_path.parent.mkdir(parents=True)
    cog.marker_path.write_text("{not json")
    assert cog._read_marker() is None
    assert not cog.marker_path.exists()


async def test_disabled_without_channel_or_switch(tmp_path):
    assert make(tmp_path, FakeBot(), enabled=False).enabled is False
    assert make(tmp_path, FakeBot(), enabled=True, channel_id=None).enabled is False


async def test_on_ready_ignored_when_disabled(tmp_path):
    bot = FakeBot(FakeChannel())
    cog = make(tmp_path, bot, enabled=False)
    put_marker(cog)
    await cog.on_ready()
    assert bot.edits == [] and cog.marker_path.exists()


def test_check_time_is_eight_minutes_after_notice(monkeypatch):
    monkeypatch.setattr(config, "MAINTENANCE_NOTICE_HHMM", (5, 59))
    assert (m._notice_time().hour, m._notice_time().minute) == (5, 59)
    assert (m._check_time().hour, m._check_time().minute) == (6, 7)
    monkeypatch.setattr(config, "MAINTENANCE_NOTICE_HHMM", (23, 55))
    assert (m._check_time().hour, m._check_time().minute) == (0, 3)


@pytest.mark.parametrize("raw,expected", [("05:59", (5, 59)), (" 6:07 ", (6, 7)), ("25:00", (5, 59)), ("abc", (5, 59)), ("", (5, 59))])
def test_parse_hhmm(raw, expected):
    assert config._parse_hhmm(raw) == expected
