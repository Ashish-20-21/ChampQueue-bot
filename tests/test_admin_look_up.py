"""/admin-look-up: staff lookup, username -> IGN and IGN -> username.
Fakes only: no Discord, no Supabase."""
import pytest
from discord import app_commands

from cogs import admin
from database.db import Database


class Member:
    def __init__(self, uid):
        self.id = uid
        self.mention = f"<@{uid}>"


class Inter:
    def __init__(self):
        self.events, self.sent = [], []
        self.response = type("R", (), {"defer": self._defer, "send_message": self._send})()
        self.followup = type("F", (), {"send": self._follow})()

    async def _defer(self, **kw):
        self.events.append("defer")

    async def _send(self, content=None, **kw):
        self.events.append("send_message")

    async def _follow(self, content=None, **kw):
        self.events.append("db_done_followup")
        self.sent.append((content, kw))


class FakeAdb:
    def __init__(self, by_discord=None, by_ign=None, order=None, boom=False):
        self.by_discord, self.by_ign, self.order, self.boom = by_discord or {}, by_ign or [], order, boom

    async def get_player_by_discord_id(self, did):
        if self.boom:
            raise RuntimeError("db down")
        return self.by_discord.get(int(did))

    async def find_players_by_ign(self, ign):
        if self.boom:
            raise RuntimeError("db down")
        self.asked = ign
        return self.by_ign


def P(pid, did, ign, status="approved"):
    return {"id": pid, "discord_id": str(did), "ign": ign, "cod_uid": f"{pid:019d}", "status": status}


def choice(v):
    return app_commands.Choice(name=v, value=v)


async def run(monkeypatch, fake, search_by, user=None, ign=None):
    monkeypatch.setattr(admin, "adb", fake)
    inter = Inter()
    cog = admin.Admin.__new__(admin.Admin)
    await admin.Admin.look_up.callback(cog, inter, choice(search_by), user, ign)
    return inter


def test_registered_with_dropdown_and_staff_gate():
    cmd = next(c for c in admin.Admin.__cog_app_commands__ if c.name == "admin-look-up")
    assert {c.value for c in cmd._params["search_by"].choices} == {"username", "ign"}
    assert any("mod_or_admin_only" in getattr(ch, "__qualname__", "") for ch in cmd.checks)


async def test_username_to_ign(monkeypatch):
    fake = FakeAdb(by_discord={101: P(1, 101, "wijdpro")})
    inter = await run(monkeypatch, fake, "username", user=Member(101))
    text, kw = inter.sent[0]
    assert "wijdpro" in text and "<@101>" in text
    assert inter.events == ["defer", "db_done_followup"]          # ack first, never send_message after
    assert kw["ephemeral"] is True and kw["allowed_mentions"].users is False   # no pings


async def test_username_not_registered(monkeypatch):
    inter = await run(monkeypatch, FakeAdb(), "username", user=Member(5))
    assert "isn't registered" in inter.sent[0][0]


async def test_username_mode_without_user_asks_for_it(monkeypatch):
    inter = await run(monkeypatch, FakeAdb(), "username", user=None, ign="ignored")
    assert "user" in inter.sent[0][0].lower()


async def test_ign_to_username(monkeypatch):
    fake = FakeAdb(by_ign=[P(1, 101, "wiJd")])
    inter = await run(monkeypatch, fake, "ign", ign="  wijd ")
    text, kw = inter.sent[0]
    assert fake.asked == "wijd"                                    # trimmed
    assert "<@101>" in text and "wiJd" in text
    assert kw["allowed_mentions"].users is False


async def test_ign_duplicates_are_all_shown_with_a_note(monkeypatch):
    fake = FakeAdb(by_ign=[P(1, 101, "Same"), P(2, 102, "same")])
    text = (await run(monkeypatch, fake, "ign", ign="same")).sent[0][0]
    assert "<@101>" in text and "<@102>" in text and "More than one" in text


async def test_ign_more_than_five_is_capped(monkeypatch):
    fake = FakeAdb(by_ign=[P(i, 100 + i, "Same") for i in range(1, 7)])
    text = (await run(monkeypatch, fake, "ign", ign="same")).sent[0][0]
    assert text.count("<@") == 5 and "first 5" in text


async def test_ign_not_found_and_empty(monkeypatch):
    assert "No registered player" in (await run(monkeypatch, FakeAdb(), "ign", ign="nobody")).sent[0][0]
    assert "IGN" in (await run(monkeypatch, FakeAdb(), "ign", ign="   ")).sent[0][0]


@pytest.mark.parametrize("mode,kw", [("username", {"user": Member(7)}), ("ign", {"ign": "x"})])
async def test_db_failure_still_answers_after_defer(monkeypatch, mode, kw):
    inter = await run(monkeypatch, FakeAdb(boom=True), mode, **kw)
    assert inter.events == ["defer", "db_done_followup"]          # replied, did not hang or raise
    assert "try again" in inter.sent[0][0]


# ---- the DB helper: wildcards must be escaped, match must be case-insensitive (ilike) ----

class _Q:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(*a):
            self.calls.append((name, a))
            return self
        return f

    def execute(self):
        return type("Res", (), {"data": [{"ign": "x"}]})()


def test_find_players_by_ign_escapes_wildcards_and_uses_ilike():
    q = _Q()
    fake_db = type("D", (), {"client": type("C", (), {"table": lambda self, t: q})()})()
    rows = Database.find_players_by_ign(fake_db, "Pro_Pl%ayer\\x")
    assert rows == [{"ign": "x"}]
    assert ("ilike", ("ign", "Pro\\_Pl\\%ayer\\\\x")) in q.calls
