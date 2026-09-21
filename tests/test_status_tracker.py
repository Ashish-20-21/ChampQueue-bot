"""Tests for the match status tracker (_post_match_status)."""
import logging
import pytest
import config
from cogs.match import _post_match_status


class FakeChannel:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, content):
        if self.fail:
            raise Exception("Discord send failed")
        self.sent.append(content)


class FakeBot:
    def __init__(self, channel=None):
        self._channel = channel

    def get_channel(self, cid):
        return self._channel


async def test_posts_when_channel_configured(monkeypatch):
    ch = FakeChannel()
    bot = FakeBot(ch)
    monkeypatch.setattr(config, "MATCH_STATUS_CHANNEL_ID", 12345)
    await _post_match_status(bot, "CQ-1234", "INDIA_ME", "result submitted, awaiting host approval")
    assert len(ch.sent) == 1
    assert "CQ-1234" in ch.sent[0]
    assert "INDIA/ME" in ch.sent[0]


async def test_does_nothing_when_channel_not_configured(monkeypatch):
    monkeypatch.setattr(config, "MATCH_STATUS_CHANNEL_ID", None)
    # Should not raise even with a bad bot
    await _post_match_status(None, "CQ-1234", "INDIA_ME", "test")


async def test_swallows_send_failure(monkeypatch, caplog):
    ch = FakeChannel(fail=True)
    bot = FakeBot(ch)
    monkeypatch.setattr(config, "MATCH_STATUS_CHANNEL_ID", 12345)
    with caplog.at_level(logging.WARNING):
        await _post_match_status(bot, "CQ-1234", "INDIA_ME", "test")
    # Must not raise, and should log a warning
    assert any("swallowed" in r.getMessage() for r in caplog.records)


async def test_does_nothing_when_channel_not_found(monkeypatch):
    bot = FakeBot(None)  # get_channel returns None
    monkeypatch.setattr(config, "MATCH_STATUS_CHANNEL_ID", 99999)
    await _post_match_status(bot, "CQ-1234", "INDIA_ME", "test")
    # No error, just silently returns
