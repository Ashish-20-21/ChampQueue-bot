"""Defer-first on the player-facing slash commands (2026-10-03).

Why: prod logs showed the first DB call of a click sometimes taking ~4 s
after an idle gap. These commands used to do their DB work BEFORE replying,
so Discord's 3 s deadline passed and the player got "Unknown interaction"
(NotFound 10062). Same bug class as the Sep 26 leaderboard fix (see
test_leaderboard_defer_fix.py).

Commands covered: /player-stats /cs-stats /rank-progress /achievements
/whoami /register /queue-status /afk /report /match-submit

Two kinds of test:
  * behaviour: run the real command body against recording fakes and check
    the ORDER of Discord/DB calls, and that nothing answers twice.
  * source guard: for every covered command, the ack must come before the
    first DB call and no `response.send_message` may follow the ack (after
    a defer, send_message raises InteractionResponded in production).
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

import switches
from cogs import match, queue, registration, stats


# ───────────────────────── recording fakes ─────────────────────────

class _Response:
    def __init__(self, ev):
        self.ev = ev

    async def defer(self, **kw):
        self.ev.append(("defer", kw))

    async def send_message(self, content=None, **kw):
        self.ev.append(("send_message", kw))


class _Followup:
    def __init__(self, ev):
        self.ev = ev

    async def send(self, content=None, **kw):
        self.ev.append(("followup", {"content": content, **kw}))


class Inter:
    def __init__(self, ev, user_id=42, channel=None):
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>", display_name="tester")
        self.response = _Response(ev)
        self.followup = _Followup(ev)
        self.channel = channel
        self.guild = None


class Adb:
    """Every method call is recorded as ("db", name) in the SAME list as the
    Discord calls, so ordering between the two is directly assertable."""

    def __init__(self, ev, **returns):
        self.ev = ev
        self.returns = returns

    def __getattr__(self, name):
        async def call(*a, **kw):
            self.ev.append(("db", name))
            r = self.returns.get(name)
            return r(*a, **kw) if callable(r) else r
        return call


def kinds(ev):
    return [e[0] for e in ev]


def assert_acked_before_db_and_never_double_answers(ev):
    k = kinds(ev)
    assert k[0] == "defer", f"first call must be the ack, got {k}"
    assert "send_message" not in k, f"send_message after a defer raises in prod: {k}"
    assert "followup" in k, f"the player never got an answer: {k}"


# ───────────────────────── behaviour: stats.py ─────────────────────────

async def test_player_stats_acks_first_ephemeral_and_answers_via_followup(monkeypatch):
    ev = []
    monkeypatch.setattr(stats, "adb", Adb(ev, get_player_by_discord_id={"id": 1}, weekly_leaders=[]))
    monkeypatch.setattr(stats, "player_stats_card", lambda p, w: "EMBED")
    await stats.Stats.player_stats.callback(stats.Stats(None), Inter(ev), None)
    assert_acked_before_db_and_never_double_answers(ev)
    assert ev[0][1].get("ephemeral") is True


async def test_player_stats_unregistered_still_single_answer(monkeypatch):
    ev = []
    monkeypatch.setattr(stats, "adb", Adb(ev, get_player_by_discord_id=None))
    await stats.Stats.player_stats.callback(stats.Stats(None), Inter(ev), None)
    assert_acked_before_db_and_never_double_answers(ev)
    assert kinds(ev).count("followup") == 1


async def test_cs_stats_no_active_season_path(monkeypatch):
    ev = []
    monkeypatch.setattr(stats, "adb", Adb(ev, get_player_by_discord_id={"id": 1}, get_active_season=None))
    await stats.Stats.cs_stats.callback(stats.Stats(None), Inter(ev), None)
    assert_acked_before_db_and_never_double_answers(ev)
    assert ev[0][1].get("ephemeral") is True


# ───────────────────────── behaviour: registration.py ─────────────────────────

async def test_register_bad_uid_still_replies_instantly_without_db(monkeypatch):
    """Pure-CPU validation must keep its single fast reply: no defer, no DB."""
    ev = []
    monkeypatch.setattr(registration, "adb", Adb(ev))
    await registration.Registration.register.callback(
        registration.Registration(None), Inter(ev), "123", "ign", "INDIA_ME", None
    )
    assert kinds(ev) == ["send_message"]


async def test_register_happy_path_acks_before_every_db_call(monkeypatch):
    ev = []
    monkeypatch.setattr(registration, "adb", Adb(
        ev, get_player_by_discord_id=None, get_player_by_uid=None, create_player={"id": 9},
    ))
    await registration.Registration.register.callback(
        registration.Registration(None), Inter(ev), "1" * 19, "ign", "INDIA_ME", None
    )
    assert_acked_before_db_and_never_double_answers(ev)
    assert ev[0][1].get("ephemeral") is True
    assert ("db", "approve_player") in ev


async def test_register_already_registered_single_followup(monkeypatch):
    ev = []
    monkeypatch.setattr(registration, "adb", Adb(
        ev, get_player_by_discord_id={"ign": "x", "status": "approved"},
    ))
    await registration.Registration.register.callback(
        registration.Registration(None), Inter(ev), "1" * 19, "ign", "INDIA_ME", None
    )
    assert_acked_before_db_and_never_double_answers(ev)
    assert kinds(ev).count("followup") == 1
    assert ("db", "create_player") not in ev


async def test_whoami_acks_first(monkeypatch):
    ev = []
    monkeypatch.setattr(registration, "adb", Adb(ev, get_player_by_discord_id=None))
    await registration.Registration.whoami.callback(registration.Registration(None), Inter(ev))
    assert_acked_before_db_and_never_double_answers(ev)


# ───────────────────────── behaviour: queue.py ─────────────────────────

async def test_queue_status_defer_is_public_because_the_reply_is_public(monkeypatch):
    """ephemeral can't change after the ack. /queue-status was always a public
    message, so its defer must NOT be ephemeral."""
    ev = []
    monkeypatch.setattr(queue, "adb", Adb(ev, queue_current=[{"players": {"ign": "a"}}]))
    q = SimpleNamespace(name="EU", value="EU_AF")
    await queue.Queue.queue_status.callback(queue.Queue.__new__(queue.Queue), Inter(ev), q)
    assert_acked_before_db_and_never_double_answers(ev)
    assert not ev[0][1].get("ephemeral"), "public reply must not be deferred ephemeral"
    assert ev[-1][1]["content"].startswith("**EU Queue (1/10):**")


def _match_channel():
    ch = MagicMock(spec=discord.TextChannel)
    ch.name = "cq-0001"
    return ch


async def test_report_cheap_rejections_keep_the_instant_reply(monkeypatch):
    ev = []
    monkeypatch.setattr(queue, "adb", Adb(ev))
    cog = queue.Queue.__new__(queue.Queue)
    inter = Inter(ev, user_id=5, channel=_match_channel())
    me = SimpleNamespace(id=5, bot=False, mention="<@5>")
    await cog._submit_match_report(inter, me, "x", kind="report")
    assert kinds(ev) == ["send_message"], "self-report must still be a single instant reply, no DB"


async def test_report_acks_before_its_three_db_calls(monkeypatch):
    ev = []
    monkeypatch.setattr(queue, "adb", Adb(ev, get_player_by_discord_id=None))
    cog = queue.Queue.__new__(queue.Queue)
    inter = Inter(ev, user_id=5, channel=_match_channel())
    other = SimpleNamespace(id=6, bot=False, mention="<@6>")
    await cog._submit_match_report(inter, other, "x", kind="afk")
    assert_acked_before_db_and_never_double_answers(ev)
    assert ev[0][1].get("ephemeral") is True


# ───────────────────────── behaviour: match.py /match-submit ─────────────────────────

async def test_match_submit_switch_off_is_still_one_instant_reply(monkeypatch):
    ev = []
    monkeypatch.setattr(switches, "RESULT_SLASH_COMMAND", False)
    monkeypatch.setattr(match, "adb", Adb(ev))
    await match.Match.match_submit.callback(match.Match.__new__(match.Match), Inter(ev), "1", object())
    assert kinds(ev) == ["send_message"]


async def test_match_submit_acks_before_the_first_db_call(monkeypatch):
    ev = []
    monkeypatch.setattr(switches, "RESULT_SLASH_COMMAND", True)
    monkeypatch.setattr(match, "adb", Adb(ev, get_match_by_code=None))
    await match.Match.match_submit.callback(match.Match.__new__(match.Match), Inter(ev), "1", object())
    assert_acked_before_db_and_never_double_answers(ev)
    assert ev[0][1] == {"thinking": True, "ephemeral": True}
    assert ev[-1][1]["content"] == "Match not found."


# ───────────────────────── source guard (all covered commands) ─────────────────────────

COVERED = {
    "player-stats": stats.Stats.player_stats.callback,
    "cs-stats": stats.Stats.cs_stats.callback,
    "rank-progress": stats.Stats.rank_progress.callback,
    "achievements": stats.Stats.achievements.callback,
    "whoami": registration.Registration.whoami.callback,
    "register": registration.Registration.register.callback,
    "queue-status": queue.Queue.queue_status.callback,
    "afk/report pipeline": queue.Queue._submit_match_report,
    "match-submit": match.Match.match_submit.callback,
}


def _await_lines(fn):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    db, defer, send = [], [], []
    for n in ast.walk(tree):
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
            name = ast.unparse(n.value.func)
            if name.startswith("adb."):
                db.append(n.lineno)
            elif name.endswith("response.defer"):
                defer.append(n.lineno)
            elif name.endswith("response.send_message"):
                send.append(n.lineno)
    return db, defer, send


@pytest.mark.parametrize("name", sorted(COVERED))
def test_defer_precedes_first_db_call_and_no_send_message_after_it(name):
    db, defer, send = _await_lines(COVERED[name])
    assert defer, f"{name}: no response.defer()"
    assert len(defer) == 1, f"{name}: deferring twice raises InteractionResponded"
    assert db, f"{name}: expected DB calls"
    assert min(defer) < min(db), f"{name}: DB call before the ack reintroduces the 10062 bug"
    late = [ln for ln in send if ln > defer[0]]
    assert not late, f"{name}: response.send_message after the defer (lines {late}) raises in prod"
