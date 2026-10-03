"""/ign-change weekly limit is driven by config.IGN_CHANGE_LIMIT (default 2).
Fakes only: no Discord, no Supabase."""
import importlib

import pytest

import config
from cogs import admin


class Member:
    def __init__(self, uid):
        self.id = uid
        self.mention = f"<@{uid}>"


class Inter:
    def __init__(self, uid):
        self.user = Member(uid)
        self.replies = []
        self.response = type("R", (), {"send_message": self._send})()

    async def _send(self, content=None, **kw):
        self.replies.append(content)


class FakeAdb:
    def __init__(self, recent):
        self.recent, self.updated, self.logged = recent, [], []

    async def get_player_by_discord_id(self, did):
        return {"id": 1, "discord_id": str(did), "ign": "wiJd"}

    async def count_recent_ign_changes(self, pid, since):
        return self.recent

    async def update_ign(self, pid, ign):
        self.updated.append(ign)

    async def log_ign_change(self, pid, old, new, by):
        self.logged.append((old, new, by))


async def run(monkeypatch, recent, limit, admin_caller=False):
    fake = FakeAdb(recent)
    monkeypatch.setattr(admin, "adb", fake)
    monkeypatch.setattr(admin, "is_admin", lambda i: admin_caller)
    monkeypatch.setattr(config, "IGN_CHANGE_LIMIT", limit)
    inter = Inter(101)
    cog = admin.Admin.__new__(admin.Admin)
    await admin.Admin.ign_change.callback(cog, inter, "wijdpro", None)
    return inter, fake


async def test_default_limit_two_blocks_third_change(monkeypatch):
    inter, fake = await run(monkeypatch, recent=2, limit=2)
    assert fake.updated == []
    assert "twice this week" in inter.replies[0]


async def test_default_limit_two_allows_second_change(monkeypatch):
    inter, fake = await run(monkeypatch, recent=1, limit=2)
    assert fake.updated == ["wijdpro"]


async def test_raised_limit_allows_more_and_blocks_at_the_new_number(monkeypatch):
    _, fake = await run(monkeypatch, recent=2, limit=4)
    assert fake.updated == ["wijdpro"]            # 3rd change now fine
    inter, fake = await run(monkeypatch, recent=4, limit=4)
    assert fake.updated == []
    assert "4 times this week" in inter.replies[0]


async def test_admin_is_never_limited(monkeypatch):
    _, fake = await run(monkeypatch, recent=99, limit=2, admin_caller=True)
    assert fake.updated == ["wijdpro"]


@pytest.mark.parametrize("raw,expected", [
    (None, 2), ("", 2), ("4", 4), ("5", 5), ("abc", 2), ("0", 2), ("-3", 2),
])
def test_env_parsing_falls_back_to_two(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("IGN_CHANGE_LIMIT", raising=False)
    else:
        monkeypatch.setenv("IGN_CHANGE_LIMIT", raw)
    try:
        assert importlib.reload(config).IGN_CHANGE_LIMIT == expected
    finally:
        monkeypatch.delenv("IGN_CHANGE_LIMIT", raising=False)
        importlib.reload(config)
