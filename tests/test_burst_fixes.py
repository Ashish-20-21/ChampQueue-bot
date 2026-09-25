"""Burst-1 no-op reply drop + burst-2 batching (2026-09-22).

Fakes only — no Discord, no Supabase. These pin the two behaviours that
must hold in prod: rejected join/leave clicks send NO followup (just the
defer), the 'queue full' reply is KEPT, and match start uses one bulk
insert + one bulk completed-count query instead of ten of each.
"""
import pytest

import config
from cogs import queue as qmod
from services import matchmaking


# ---------------- fakes ----------------

class Resp:
    def __init__(self, rec):
        self.rec = rec
    async def defer(self, **kw):
        self.rec.append("defer")

class Followup:
    def __init__(self, rec):
        self.rec = rec
    async def send(self, content=None, **kw):
        self.rec.append(("followup", content))

class Msg:
    async def edit(self, **kw): pass

class Inter:
    def __init__(self, uid):
        self.user = type("U", (), {"id": uid})()
        self.rec = []
        self.response = Resp(self.rec)
        self.followup = Followup(self.rec)
        self.message = Msg()
    async def edit_original_response(self, **kw):
        self.rec.append("edit_original")


def followups(inter):
    return [c for item in inter.rec if isinstance(item, tuple) and item[0] == "followup" for c in [item[1]]]


# ---------------- burst-1: no-op reply drop ----------------

class JoinAdb:
    """Player exists+approved+eligible; queue_join returns None = already in."""
    def __init__(self, already_in, queue):
        self.already_in, self.queue = already_in, queue
    async def get_player_by_discord_id(self, did):
        return {"id": 1, "ign": "P1", "status": "approved", "reputation": 100}
    async def queue_current(self, queue_key=None):
        return self.queue
    async def queue_join(self, pid, qk):
        return None if self.already_in else {"id": 99}


@pytest.fixture
def cog():
    c = qmod.Queue.__new__(qmod.Queue)
    import collections, asyncio
    c._locks = collections.defaultdict(asyncio.Lock)
    return c


async def make_cog_call(cog, monkeypatch, adb, handler, uid=1):
    monkeypatch.setattr(qmod, "adb", adb)
    monkeypatch.setattr(qmod.reputation, "is_queue_eligible", lambda p: (True, ""))
    monkeypatch.setattr(qmod, "_click_gate", lambda uid, act: True)   # bypass debounce
    view = qmod.RegionQueueView("INDIA_ME", cog)
    inter = Inter(uid)
    await handler(cog, inter, "INDIA_ME", view)
    return inter


async def test_already_in_queue_sends_no_followup(cog, monkeypatch):
    adb = JoinAdb(already_in=True, queue=[{"player_id": 2, "players": {}}])
    inter = await make_cog_call(cog, monkeypatch, adb, qmod.Queue.handle_join)
    assert "defer" in inter.rec
    assert followups(inter) == []                     # the dropped no-op


async def test_queue_full_reply_is_kept(cog, monkeypatch):
    full = [{"player_id": i, "players": {}} for i in range(10)]
    adb = JoinAdb(already_in=False, queue=full)
    inter = await make_cog_call(cog, monkeypatch, adb, qmod.Queue.handle_join)
    assert any("full" in (c or "").lower() for c in followups(inter))   # meaningful reply stays


class LeaveAdb:
    def __init__(self, queue):
        self.queue = queue
        self.left = False
    async def get_player_by_discord_id(self, did):
        return {"id": 1, "ign": "P1", "status": "approved"}
    async def queue_current(self, queue_key=None):
        return self.queue
    async def queue_leave(self, pid):
        self.left = True


async def test_not_in_queue_sends_no_followup(cog, monkeypatch):
    adb = LeaveAdb(queue=[{"player_id": 2, "players": {}}])   # player 1 not present
    monkeypatch.setattr(qmod, "adb", adb)
    monkeypatch.setattr(qmod, "_click_gate", lambda uid, act: True)
    view = qmod.RegionQueueView("INDIA_ME", cog)
    inter = Inter(1)
    await qmod.Queue.handle_leave(cog, inter, "INDIA_ME", view)
    assert "defer" in inter.rec
    assert followups(inter) == []
    assert adb.left is False                           # nothing removed, correctly


# ---------------- burst-2: batching ----------------

async def test_bootstrap_uses_one_bulk_count_query(monkeypatch):
    calls = {"bulk": 0, "single": 0}
    async def bulk(pids):
        calls["bulk"] += 1
        return {pid: 99 for pid in pids}              # everyone graduated
    async def single(pid):
        calls["single"] += 1
        return 99
    fake = type("A", (), {"player_completed_counts": staticmethod(bulk),
                          "player_completed_match_count": staticmethod(single)})()
    monkeypatch.setattr(matchmaking, "adb", fake)
    # pool check path: force the "graduated" branch to also need the pool query,
    # but we only assert the per-player calls here.
    import asyncio
    monkeypatch.setattr(matchmaking, "db", type("D", (), {"client": None})())
    async def fake_pool(*a, **k):
        return type("R", (), {"count": 9999})()
    monkeypatch.setattr(asyncio, "to_thread", fake_pool)
    monkeypatch.setattr(config, "BOOTSTRAP_MATCH_THRESHOLD", 1)
    monkeypatch.setattr(config, "BOOTSTRAP_MIN_ELIGIBLE_POOL", 1)
    await matchmaking.is_bootstrap_match(list(range(1, 11)))
    assert calls["bulk"] == 1 and calls["single"] == 0     # one bulk query, zero per-player


def test_bulk_insert_payload_is_idempotent_upsert(monkeypatch):
    from database.db import Database
    captured = {}
    class Chain:
        def upsert(self, payload, **kw):
            captured["payload"] = payload; captured["kw"] = kw; return self
        def execute(self):
            return type("R", (), {"data": captured["payload"]})()
    class Client:
        def table(self, name):
            captured["table"] = name; return Chain()
    db = Database.__new__(Database)
    db.client = Client()
    rows = [{"player_id": 5, "team": "A", "is_captain": False},
            {"player_id": 6, "team": "B", "is_captain": False}]
    out = db.add_match_players_bulk(42, rows)
    assert captured["table"] == "match_players"
    assert captured["kw"].get("ignore_duplicates") is True          # idempotent
    assert captured["kw"].get("on_conflict") == "match_id,player_id"
    assert all(r["match_id"] == 42 for r in captured["payload"])     # match id stamped on every row
    assert len(out) == 2


def test_bulk_completed_counts_defaults_absent_players_to_zero(monkeypatch):
    from database.db import Database
    class Chain:
        def select(self, *a, **k): return self
        def in_(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self):
            return type("R", (), {"data": [{"player_id": 5}, {"player_id": 5}]})()
    class Client:
        def table(self, name): return Chain()
    db = Database.__new__(Database)
    db.client = Client()
    counts = db.player_completed_counts([5, 6, 7])
    assert counts == {5: 2, 6: 0, 7: 0}              # 5 has two, others default to zero


def test_bulk_completed_counts_empty_input():
    from database.db import Database
    assert Database.player_completed_counts(Database.__new__(Database), []) == {}


# ---------------- host tag merged into one message (2026-09-23) ----------------

def test_host_tag_sent_only_once_in_match_start_source():
    """Regression guard: 'is the Match Host' text must appear in exactly
    ONE text_channel.send(...) call in _start_match_flow, not two — the
    2026-09-22 log showed the host mention posted twice (a standalone
    'is the Match Host.' message, then again in the room-code instructions),
    costing an extra Discord call per match for duplicate information."""
    import re
    src = open("cogs/queue.py", encoding="utf-8").read()
    start = src.index("async def _start_match_flow")
    end = src.index("\n    async def ", start + 10)
    body = src[start:end]
    # Strip comment lines so the count reflects real code, not commentary
    # that happens to mention the phrase (this test's own history bit us once).
    code_only = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    # Match each text_channel.send(...) call as a block (they can be multi-line).
    calls = re.findall(r"text_channel\.send\([^)]*(?:\([^)]*\)[^)]*)*\)", code_only, re.S)
    calls_with_tag = [c for c in calls if "is the Match Host" in c]
    assert len(calls_with_tag) == 1, calls_with_tag
    assert 'text_channel.send(f"{host_mention} is the Match Host.")' not in code_only


# ---------------- Sep 23 429-storm fix: disable Join at 10/10 + cooldown (2026-09-25) ----------------
# Root cause (from live DISCORD_METER/DISCORD_429 forensics): 100% of 193
# rate-limit hits over 13+ hours traced to one 4-minute burst of repeat
# Join clicks on an already-full queue. Two independent defenses tested here.

async def test_join_button_disabled_when_queue_hits_ten(cog):
    view = qmod.RegionQueueView("INDIA_ME", cog)
    full = [{"player_id": i} for i in range(10)]
    await view.update_view_state(full)
    assert view.join_button.disabled is True
    assert view.start_match_button in view.children


async def test_join_button_re_enabled_when_queue_drops_below_ten(cog):
    view = qmod.RegionQueueView("INDIA_ME", cog)
    await view.update_view_state([{"player_id": i} for i in range(10)])
    assert view.join_button.disabled is True
    await view.update_view_state([{"player_id": i} for i in range(9)])   # someone left
    assert view.join_button.disabled is False
    assert view.start_match_button not in view.children


async def test_leave_button_never_disabled_by_queue_state(cog):
    view = qmod.RegionQueueView("INDIA_ME", cog)
    await view.update_view_state([{"player_id": i} for i in range(10)])
    assert view.leave_button.disabled is False    # only Join is gated


async def test_queue_full_reply_sent_once_then_silent_on_repeat(cog, monkeypatch):
    qmod._queue_full_last.clear()   # isolate from other tests / real clock
    full = [{"player_id": i, "players": {}} for i in range(10)]
    adb = JoinAdb(already_in=False, queue=full)

    inter1 = await make_cog_call(cog, monkeypatch, adb, qmod.Queue.handle_join, uid=1)
    assert any("full" in (c or "").lower() for c in followups(inter1))   # 1st click: told

    inter2 = await make_cog_call(cog, monkeypatch, adb, qmod.Queue.handle_join, uid=1)
    assert followups(inter2) == []                                       # 2nd click: silent


async def test_queue_full_reply_still_reaches_a_different_player(cog, monkeypatch):
    """The cooldown is per-player — it must never silence a genuinely new
    player just because someone else was recently told."""
    qmod._queue_full_last.clear()
    full = [{"player_id": i, "players": {}} for i in range(10)]
    adb = JoinAdb(already_in=False, queue=full)

    await make_cog_call(cog, monkeypatch, adb, qmod.Queue.handle_join, uid=1)
    inter_other = await make_cog_call(cog, monkeypatch, adb, qmod.Queue.handle_join, uid=2)
    assert any("full" in (c or "").lower() for c in followups(inter_other))


def test_queue_full_gate_fails_open_on_internal_error(monkeypatch):
    """Same fail-open contract as _click_gate: a bug in the gate itself
    must never block a real player's first notice."""
    monkeypatch.setattr(qmod, "_queue_full_last", None)   # force an exception inside the gate
    assert qmod._queue_full_gate(999) is True


def test_click_debounce_is_three_seconds_not_two_or_five():
    """Pins the exact tuning decision (2026-09-25): 2s was too short (didn't
    stop the Sep 23 storm), 5s was rejected as too laggy-feeling for a
    genuine double-tap. Guards against either direction drifting back."""
    assert qmod._CLICK_COOLDOWN_SECONDS == 3.0
