"""+result text trigger and the shared result pipeline (2026-09-28).

Fakes only: no Discord, no Supabase, no OCR. Each test counts the Discord
calls the code makes, because keeping that number small is the point of the
reply policy (ignore non-hosts, ignore repeats, one notice per mistake).
"""
import asyncio
import importlib
import time

import discord
import pytest

import config
import switches
from cogs import match


# ---------------- fakes ----------------

def _429():
    class _Resp:
        status = 429
        reason = "Too Many Requests"
    return discord.errors.HTTPException(_Resp(), "rate limited")


class Msg:
    """A message the bot posted; records edits."""
    def __init__(self, chan, content):
        self.chan, self.content, self.edits = chan, content, []

    async def edit(self, content=None, **kw):
        self.chan.calls.append("edit")
        if "edit" in self.chan.fail_on:
            raise _429()
        self.edits.append(content)
        self.content = content


class Chan:
    def __init__(self, name="cq-0042", fail_on=()):
        self.name, self.fail_on = name, set(fail_on)
        self.calls, self.sent, self.files, self.msgs = [], [], [], []

    async def send(self, content=None, file=None, **kw):
        self.calls.append("send")
        if "send" in self.fail_on:
            raise _429()
        self.sent.append(content)
        if file is not None:
            self.files.append(file)
        m = Msg(self, content)
        self.msgs.append(m)
        return m


class Author:
    def __init__(self, uid, bot=False):
        self.id, self.bot, self.display_name = uid, bot, f"user{uid}"


class Att:
    def __init__(self, content_type="image/png", size=1000, filename="sb.png"):
        self.content_type, self.size, self.filename = content_type, size, filename
        self.url = f"https://cdn.example/{filename}"

    async def read(self):
        return b"\x89PNG-bytes"


class Message:
    def __init__(self, author, chan, content="+result", attachments=None):
        self.author, self.channel, self.content = author, chan, content
        self.attachments = [Att()] if attachments is None else attachments


HOST, OTHER, ADMIN = 101, 102, 900


class FakeAdb:
    def __init__(self, status="awaiting_result", map_pool=("Summit",)):
        self.match = {"id": 7, "match_id": "CQ-0042", "status": status,
                      "room_code_shared_by": 1, "map_pool": list(map_pool), "text_channel_id": "555"}
        self.players = {HOST: {"id": 1, "ign": "Ravi"}, OTHER: {"id": 2, "ign": "Mate"}}
        self.reads = 0

    async def get_match_by_code(self, code):
        self.reads += 1
        return dict(self.match) if code == "CQ-0042" else None

    async def get_player_by_discord_id(self, did):
        self.reads += 1
        return self.players.get(int(did))


@pytest.fixture
def cog(monkeypatch):
    monkeypatch.setattr(switches, "RESULT_TEXT_TRIGGER", True)
    monkeypatch.setattr(switches, "RESULT_SLASH_COMMAND", True)
    monkeypatch.setattr(config, "RESULT_SS_CHANNEL_ID", None)
    monkeypatch.setattr(match, "is_admin_user", lambda u: getattr(u, "id", None) == ADMIN)
    c = match.Match.__new__(match.Match)
    c.bot = type("B", (), {"get_channel": lambda self, cid: None})()
    c.runs = []

    async def fake_run(reply, m, player, maps, attachments, uid, name, *, safety_net_text):
        c.runs.append((m["match_id"], uid, attachments))
        await reply.send("Submitted. Check #approval to approve once you've verified the result.")
    c._run_submission = fake_run
    return c


def use(monkeypatch, fake):
    monkeypatch.setattr(match, "adb", fake)
    return fake


# ---------------- who gets ignored (zero Discord calls) ----------------

async def test_non_host_is_ignored_with_zero_calls(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    chan = Chan()
    await cog.on_message(Message(Author(OTHER), chan))
    assert chan.calls == [] and cog.runs == []


async def test_bot_and_non_match_channel_and_other_words_are_ignored(cog, monkeypatch):
    fake = use(monkeypatch, FakeAdb())
    chan, lobby = Chan(), Chan(name="general")
    await cog.on_message(Message(Author(HOST, bot=True), chan))
    await cog.on_message(Message(Author(HOST), lobby))
    await cog.on_message(Message(Author(HOST), chan, content="+results"))
    await cog.on_message(Message(Author(HOST), chan, content="gg +result"))
    assert chan.calls == [] and lobby.calls == [] and fake.reads == 0


async def test_already_submitted_is_silent(cog, monkeypatch):
    for status in ("pending_verification", "awaiting_review", "completed"):
        use(monkeypatch, FakeAdb(status=status))
        chan = Chan()
        await cog.on_message(Message(Author(HOST), chan))
        assert chan.calls == [], status
    assert cog.runs == []


# ---------------- the happy path ----------------

async def test_host_result_is_one_message_then_one_edit(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    chan = Chan()
    await cog.on_message(Message(Author(HOST), chan))
    assert chan.calls == ["send", "edit"]                       # processing msg, then edited into the outcome
    assert "processing" in chan.sent[0] and "5 minutes" in chan.sent[0]
    assert cog.runs == [("CQ-0042", HOST, cog.runs[0][2])]


async def test_admin_can_submit_for_the_host(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    chan = Chan()
    await cog.on_message(Message(Author(ADMIN), chan))
    assert len(cog.runs) == 1 and cog.runs[0][1] == ADMIN


async def test_only_the_first_image_is_used(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    first, second = Att(filename="a.png"), Att(filename="b.png")
    await cog.on_message(Message(Author(HOST), Chan(), attachments=[first, second]))
    assert cog.runs[0][2] == (first,)


# ---------------- one notice per mistake, then silence ----------------

async def test_missing_image_notice_is_sent_once(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    chan = Chan()
    for _ in range(3):
        await cog.on_message(Message(Author(HOST), chan, attachments=[]))
    assert chan.calls == ["send"] and "same" in chan.sent[0]
    assert cog.runs == []


async def test_bad_file_and_too_big_are_one_notice(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    chan = Chan()
    await cog.on_message(Message(Author(HOST), chan, attachments=[Att(content_type="application/pdf")]))
    await cog.on_message(Message(Author(HOST), chan,
                                 attachments=[Att(size=config.MAX_SCOREBOARD_UPLOAD_BYTES + 1)]))
    assert chan.calls == ["send"] and cog.runs == []


async def test_room_code_not_shared_notice_once(cog, monkeypatch):
    use(monkeypatch, FakeAdb(status="awaiting_room"))
    chan = Chan()
    await cog.on_message(Message(Author(HOST), chan))
    await cog.on_message(Message(Author(HOST), chan))
    assert chan.calls == ["send"] and "+rc" in chan.sent[0]


async def test_text_switch_off_tells_host_once_and_does_nothing(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    monkeypatch.setattr(switches, "RESULT_TEXT_TRIGGER", False)
    chan = Chan()
    await cog.on_message(Message(Author(HOST), chan))
    await cog.on_message(Message(Author(HOST), chan))
    assert chan.calls == ["send"] and "/match-submit" in chan.sent[0] and cog.runs == []


async def test_text_switch_off_still_silent_for_non_hosts(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    monkeypatch.setattr(switches, "RESULT_TEXT_TRIGGER", False)
    chan = Chan()
    await cog.on_message(Message(Author(OTHER), chan))
    assert chan.calls == []


# ---------------- in-flight guard ----------------

async def test_repeat_while_processing_is_ignored(cog, monkeypatch):
    fake = use(monkeypatch, FakeAdb())
    gate = asyncio.Event()

    async def slow_run(reply, m, *a, **kw):
        cog.runs.append(m["match_id"])
        await gate.wait()
        await reply.send("done")
    cog._run_submission = slow_run

    chan = Chan()
    first = asyncio.create_task(cog.on_message(Message(Author(HOST), chan)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    reads_before = fake.reads
    await cog.on_message(Message(Author(HOST), chan))          # repeat while first is running
    assert fake.reads == reads_before                          # ignored before any DB read
    gate.set()
    await first
    assert cog.runs == ["CQ-0042"] and chan.calls == ["send", "edit"]


async def test_slot_is_released_after_the_run(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    await cog.on_message(Message(Author(HOST), Chan()))
    await cog.on_message(Message(Author(HOST), Chan()))        # e.g. after /admin-reset-match
    assert len(cog.runs) == 2


async def test_slot_is_released_even_when_the_run_crashes(cog, monkeypatch):
    use(monkeypatch, FakeAdb())

    async def boom(*a, **kw):
        raise RuntimeError("x")
    cog._run_submission = boom
    await cog.on_message(Message(Author(HOST), Chan()))        # must not raise out of the listener
    assert cog._claim_result_slot("CQ-0042") is True


async def test_stale_slot_does_not_lock_the_host_out(cog, monkeypatch):
    use(monkeypatch, FakeAdb())
    inflight, _, _ = cog._result_state()
    inflight["CQ-0042"] = time.monotonic() - config.RESULT_INFLIGHT_STALE_SECONDS - 1
    await cog.on_message(Message(Author(HOST), Chan()))
    assert len(cog.runs) == 1


async def test_fresh_slot_blocks_a_second_claim(cog):
    assert cog._claim_result_slot("CQ-0042") is True
    assert cog._claim_result_slot("CQ-0042") is False
    assert cog._claim_result_slot("CQ-0043") is True           # other matches unaffected


# ---------------- reply object ----------------

async def test_reply_survives_a_429_on_edit():
    chan = Chan(fail_on={"edit"})
    status = await chan.send("processing")
    reply = match._MessageReply(chan, status)
    await reply.send("done")                                   # must not raise
    assert chan.calls == ["send", "edit"]                      # no retry, no extra send


async def test_reply_falls_back_to_one_send_if_processing_message_failed():
    chan = Chan()
    reply = match._MessageReply(chan, None)
    await reply.send("done")
    await reply.send("done again")
    assert chan.calls == ["send", "edit"]                      # second outcome edits the fallback message


async def test_processing_message_429_still_runs_and_answers(cog, monkeypatch):
    use(monkeypatch, FakeAdb())

    class FlakyChan(Chan):
        async def send(self, content=None, **kw):
            if not self.calls:
                self.calls.append("send")
                raise _429()
            return await super().send(content, **kw)
    chan = FlakyChan()
    await cog.on_message(Message(Author(HOST), chan))
    assert len(cog.runs) == 1 and chan.calls == ["send", "send"]


async def test_no_map_goes_to_review_with_friendly_message(cog, monkeypatch):
    use(monkeypatch, FakeAdb(map_pool=()))
    routed = []

    async def fake_route(m, pid, reason, detail):
        routed.append(reason)
    cog._route_to_review = fake_route
    chan = Chan()
    await cog.on_message(Message(Author(HOST), chan))
    assert routed == ["result_issue"] and cog.runs == []
    assert chan.calls == ["send", "edit"]
    assert "processing" in chan.sent[0]                        # first shown text
    assert "snag" in chan.msgs[0].content                      # then edited into the friendly review line


# ---------------- IGN branch: no duplicate "battling" line ----------------

class FakeIgnAdb:
    def __init__(self):
        self.updates = []

    async def update_match(self, pk, fields):
        self.updates.append(fields)


async def _run_ign(monkeypatch, reply, match_chan):
    fake = FakeIgnAdb()
    monkeypatch.setattr(match, "adb", fake)
    monkeypatch.setattr(config, "ISSUE_INTAKE_CHANNEL_ID", None)
    c = match.Match.__new__(match.Match)
    c.bot = type("B", (), {"get_channel": lambda self, cid: match_chan})()
    m = {"id": 7, "match_id": "CQ-0042", "text_channel_id": "555"}
    await c._route_to_ign_confirmation(reply, m, [], [{"ocr_ign": "x"}], [{"player_id": 1}], "u")
    return fake


async def test_ign_branch_for_result_says_battling_once_via_the_edit(monkeypatch):
    chan = Chan()
    status = await chan.send("processing")
    reply = match._MessageReply(chan, status)
    fake = await _run_ign(monkeypatch, reply, chan)
    assert chan.calls == ["send", "edit"]                      # no second post in the match channel
    assert "battling special characters" in status.content
    assert fake.updates == [{"status": "awaiting_review"}]


async def test_ign_branch_for_slash_is_unchanged(monkeypatch):
    chan, followups = Chan(), []

    class SlashReply:
        in_match_channel = False
        async def send(self, text):
            followups.append(text)
    await _run_ign(monkeypatch, SlashReply(), chan)
    assert chan.calls == ["send"] and "battling" in chan.sent[0]   # match channel still told
    assert followups and "snag" in followups[0]                     # uploader gets the usual message


# ---------------- screenshot forward ----------------

async def test_forward_posts_a_real_file_with_the_match_code(monkeypatch):
    ss = Chan(name="result-ss")
    monkeypatch.setattr(config, "RESULT_SS_CHANNEL_ID", 42)
    c = match.Match.__new__(match.Match)
    c.bot = type("B", (), {"get_channel": lambda self, cid: ss if cid == 42 else None})()
    c._forward_result_screenshot({"match_id": "CQ-0042"}, b"img", Att(), "Ravi")
    _, _, tasks_ = c._result_state()
    await asyncio.gather(*tasks_)
    assert ss.calls == ["send"] and len(ss.files) == 1
    assert "CQ-0042" in ss.sent[0] and "Ravi" in ss.sent[0]


async def test_forward_failure_is_swallowed(monkeypatch):
    ss = Chan(name="result-ss", fail_on={"send"})
    monkeypatch.setattr(config, "RESULT_SS_CHANNEL_ID", 42)
    c = match.Match.__new__(match.Match)
    c.bot = type("B", (), {"get_channel": lambda self, cid: ss})()
    c._forward_result_screenshot({"match_id": "CQ-0042"}, b"img", Att(), "Ravi")
    _, _, tasks_ = c._result_state()
    await asyncio.gather(*tasks_)                              # would raise if not swallowed
    assert ss.calls == ["send"]                                # exactly one attempt, no retry


async def test_forward_off_when_channel_unset(monkeypatch):
    monkeypatch.setattr(config, "RESULT_SS_CHANNEL_ID", None)
    c = match.Match.__new__(match.Match)
    c.bot = None                                               # would crash if touched
    c._forward_result_screenshot({"match_id": "CQ-0042"}, b"img", Att(), "Ravi")
    _, _, tasks_ = c._result_state()
    assert not tasks_


# ---------------- approval card: exactly one retry ----------------

async def _run_submit_body(monkeypatch, approval_chan):
    class A:
        def __init__(self):
            self.updates = []
        async def get_match_players(self, pk): return [{"player_id": 1, "team": "A", "players": {"ign": "Ravi"}}]
        async def upsert_match_screenshot(self, *a, **kw): return None
        async def recompute_player_career_stats(self, pid): return None
        async def update_match(self, pk, fields): self.updates.append(fields)
    fake = A()
    monkeypatch.setattr(match, "adb", fake)
    monkeypatch.setattr(config, "RESULT_SS_CHANNEL_ID", None)
    monkeypatch.setattr(match.vision_extraction, "extract_scoreboard", lambda b, ct: {"players": []})
    monkeypatch.setattr(match.Match, "_prepare_round",
                        staticmethod(lambda mp, mapn, ex, *a, **kw: ({"clean": False, "round_number": 1, "results": []}, [], [], False)))

    async def no_flags(mid, ex, *a, **kw): return {"flags": {}}
    monkeypatch.setattr(match.validation, "validate_submission", no_flags)
    monkeypatch.setattr(match, "verification_card", lambda *a, **kw: discord.Embed())
    monkeypatch.setattr(match, "HostApprovalView", lambda *a, **kw: None)

    async def quiet(*a, **kw): return None
    monkeypatch.setattr(match, "_post_match_status", quiet)
    monkeypatch.setattr(match.incident_log, "post", quiet)
    monkeypatch.setattr(match.asyncio, "sleep", quiet)

    c = match.Match.__new__(match.Match)
    c.bot = None

    async def appr(): return approval_chan
    c._approval_channel = appr
    said = []

    class R:
        in_match_channel = True
        async def send(self, text): said.append(text)
    m = {"id": 7, "match_id": "CQ-0042", "queue_key": "EU_AF"}
    await c._submit_body(R(), m, {"id": 1}, ["Summit"], (Att(),), HOST, "Ravi")
    return said, fake


class ApprChan(Chan):
    def __init__(self, failures):
        super().__init__(name="approval")
        self.failures = failures
        self.mention = "#approval"

    async def send(self, content=None, **kw):
        self.calls.append("send")
        if self.failures > 0:
            self.failures -= 1
            raise _429()
        return Msg(self, content)


async def test_approval_card_retries_once_then_succeeds(monkeypatch):
    chan = ApprChan(failures=1)
    said, fake = await _run_submit_body(monkeypatch, chan)
    assert chan.calls == ["send", "send"]
    assert said and said[-1].startswith("Submitted.")
    assert fake.updates[-1]["status"] == "pending_verification"


async def test_approval_card_gives_up_after_two_and_tells_host(monkeypatch):
    chan = ApprChan(failures=5)
    said, _ = await _run_submit_body(monkeypatch, chan)
    assert chan.calls == ["send", "send"]                      # never a third attempt
    assert "couldn't post the approval card" in said[-1]


async def test_approval_card_first_try_is_one_call(monkeypatch):
    chan = ApprChan(failures=0)
    said, _ = await _run_submit_body(monkeypatch, chan)
    assert chan.calls == ["send"]


# ---------------- slash command switch ----------------

async def test_slash_switch_off_points_to_result_and_touches_nothing(monkeypatch):
    monkeypatch.setattr(switches, "RESULT_SLASH_COMMAND", False)

    class NoDb:
        def __getattr__(self, n):
            raise AssertionError("DB touched while /match-submit is switched off")
    monkeypatch.setattr(match, "adb", NoDb())
    replies = []

    class I:
        response = type("R", (), {"send_message": lambda self, t=None, **kw: _rec(replies, t)})()
    c = match.Match.__new__(match.Match)
    await match.Match.match_submit.callback(c, I(), "42", Att())
    assert len(replies) == 1 and "+result" in replies[0]


async def _rec(lst, t):
    lst.append(t)


# ---------------- switch file guard ----------------

def test_both_switches_off_turns_both_back_on(monkeypatch):
    monkeypatch.setenv("RESULT_TEXT_TRIGGER", "false")
    monkeypatch.setenv("RESULT_SLASH_COMMAND", "false")
    try:
        mod = importlib.reload(switches)
        assert mod.RESULT_TEXT_TRIGGER is True and mod.RESULT_SLASH_COMMAND is True
    finally:
        monkeypatch.delenv("RESULT_TEXT_TRIGGER")
        monkeypatch.delenv("RESULT_SLASH_COMMAND")
        importlib.reload(switches)


def test_one_switch_off_is_respected(monkeypatch):
    monkeypatch.setenv("RESULT_SLASH_COMMAND", "false")
    try:
        mod = importlib.reload(switches)
        assert mod.RESULT_TEXT_TRIGGER is True and mod.RESULT_SLASH_COMMAND is False
    finally:
        monkeypatch.delenv("RESULT_SLASH_COMMAND")
        importlib.reload(switches)
