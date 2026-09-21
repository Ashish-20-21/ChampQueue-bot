"""SkillVoteView: call counts and rules AFTER the roster + one-call change.

Before (Step A baseline): accepted vote = defer + edit (2 Discord calls) + 1 DB
read; rejected = defer + followup (2 calls) + 1 DB read; failed edit = 3 calls.
Now: accepted = 1 call (edit_message), rejected = 1 call (send_message),
failed edit = 1 call, and ZERO DB reads (FakeDB raises if one happens).
"""
import asyncio
import logging

import pytest

import config
from cogs import queue
from tests.fakes import FakeDB, FakeInteraction

TEAM = {1, 2, 3, 4, 5}                                   # db ids on this team
# discord id 101..105 = team players 1..5 ; 106..110 = other team (not in roster)
ROSTER = {100 + i: {"id": i, "ign": f"P{i}"} for i in range(1, 6)}


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(queue, "adb", fake)
    return fake


def make_view():
    return queue.SkillVoteView(match_id=1, team="A", team_player_ids=set(TEAM), roster=dict(ROSTER))


async def click(view, skill_index, discord_id, **kw):
    """Press skill button #skill_index as discord user discord_id."""
    button = view.children[skill_index]
    inter = FakeInteraction(discord_id, **kw)
    await button.callback(inter)
    # Visible with:  python -m pytest -v -s
    note = f" (forced 429 on: {sorted(kw['fail_on'])})" if kw.get("fail_on") else ""
    print(f"\n  click by discord {discord_id} on skill #{skill_index}{note}"
          f" -> Discord calls: {inter.calls}")
    return inter


# ---------------- call counts ----------------

async def test_accepted_vote_is_one_call_and_no_db_read(db):
    view = make_view()
    inter = await click(view, 0, 101)
    assert inter.calls == ["edit_message"]          # 1 Discord call (was 2)
    assert inter.edit_view is view                   # it repainted the shared panel
    assert view.children[0].disabled is True
    assert "P1" in view.children[0].label            # shows who picked it


async def test_wrong_team_click_is_one_private_message(db):
    view = make_view()
    inter = await click(view, 0, 106)               # not in this team's roster
    assert inter.calls == ["send_message"]
    assert "isn't your team" in inter.messages[0]
    assert view.children[0].disabled is False


async def test_already_voted_is_one_message_that_names_the_skill(db):
    view = make_view()
    await click(view, 0, 101)
    inter = await click(view, 1, 101)               # same player, different skill
    assert inter.calls == ["send_message"]
    assert config.OPERATOR_SKILLS[0] in inter.messages[0]   # tells them what they picked
    assert view.children[1].disabled is False


async def test_clicking_own_picked_skill_again_says_already_picked_not_taken(db):
    view = make_view()
    await click(view, 0, 101)
    inter = await click(view, 0, 101)               # same player, SAME skill
    assert inter.calls == ["send_message"]
    assert "already picked" in inter.messages[0]
    assert "teammate" not in inter.messages[0]


async def test_taken_skill_is_one_message(db):
    view = make_view()
    await click(view, 0, 101)
    inter = await click(view, 0, 102)               # teammate, same skill
    assert inter.calls == ["send_message"]
    assert "teammate" in inter.messages[0]


async def test_full_team_is_five_calls_and_no_db_reads(db, monkeypatch):
    monkeypatch.setattr(config, "STORE_SKILL_VOTES", True)
    view = make_view()
    total = 0
    for i in range(5):
        inter = await click(view, i, 101 + i)
        total += len(inter.calls)
    assert total == 5                                # was 10
    assert len(db.bulk_writes) == 1                  # FakeDB would have raised on any read


# ---------------- failure mode ----------------

async def test_failed_reply_still_counts_vote_and_adds_no_extra_call(db, caplog):
    view = make_view()
    with caplog.at_level(logging.WARNING, logger="champions_queue"):
        inter = await click(view, 0, 101, fail_on={"edit_message"})
    assert inter.calls == ["edit_message"]           # no fallback follow-up, no retry (was 3 calls)
    assert view.children[0].disabled is True         # vote locked in memory
    assert 1 in view.voted_player_ids
    assert len([r for r in caplog.records if "ack dropped" in r.getMessage()]) == 1   # ONE warning line


async def test_failed_reply_vote_is_still_saved_at_five(db, monkeypatch):
    monkeypatch.setattr(config, "STORE_SKILL_VOTES", True)
    view = make_view()
    await click(view, 0, 101, fail_on={"edit_message"})    # this player's repaint fails
    for i in range(1, 5):
        await click(view, i, 101 + i)
    assert len(db.bulk_writes) == 1
    assert len(db.bulk_writes[0]) == 5                      # all 5, including the failed-repaint one


async def test_failed_player_is_told_their_pick_on_next_click(db):
    view = make_view()
    await click(view, 0, 101, fail_on={"edit_message"})
    inter = await click(view, 3, 101)                       # clicks again, other skill
    assert inter.calls == ["send_message"]
    assert config.OPERATOR_SKILLS[0] in inter.messages[0]


async def test_next_accepted_vote_repaints_missed_pick(db):
    view = make_view()
    await click(view, 0, 101, fail_on={"edit_message"})     # panel not repainted for P1
    inter = await click(view, 1, 102)                       # teammate votes
    assert inter.edit_view is view
    assert view.children[0].disabled is True                # the repaint carries P1's lock too
    assert "P1" in view.children[0].label


# ---------------- rules that must never break ----------------

async def test_first_click_wins_when_two_click_at_once(db):
    view = make_view()
    a = FakeInteraction(101)
    b = FakeInteraction(102)
    await asyncio.gather(view.children[0].callback(a), view.children[0].callback(b))
    assert sorted([a.calls[0], b.calls[0]]) == ["edit_message", "send_message"]
    assert len(view.taken_skills) == 1


async def test_vote_path_never_uses_defer_or_edit_original_response(db):
    view = make_view()
    seen = []
    for i in range(5):
        seen += (await click(view, i, 101 + i)).calls
    seen += (await click(view, 5, 106)).calls
    assert "defer" not in seen and "edit_original_response" not in seen and "followup" not in seen


async def test_fifth_vote_flushes_once_with_five_votes(db, monkeypatch):
    monkeypatch.setattr(config, "STORE_SKILL_VOTES", True)
    view = make_view()
    for i in range(5):
        await click(view, i, 101 + i)
    assert len(db.bulk_writes) == 1
    assert {v["player_id"] for v in db.bulk_writes[0]} == TEAM
    assert all(v["match_id"] == 1 and v["team"] == "A" for v in db.bulk_writes[0])


async def test_timeout_flushes_partial_votes(db, monkeypatch):
    monkeypatch.setattr(config, "STORE_SKILL_VOTES", True)
    view = make_view()
    await click(view, 0, 101)
    await click(view, 1, 102)
    assert db.bulk_writes == []                      # not 5/5 yet
    await view.on_timeout()
    assert len(db.bulk_writes) == 1 and len(db.bulk_writes[0]) == 2


# ---------------- STORE_SKILL_VOTES switch ----------------

async def test_switch_off_locks_buttons_but_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(config, "STORE_SKILL_VOTES", False)
    view = make_view()
    for i in range(5):
        inter = await click(view, i, 101 + i)
        assert inter.calls == ["edit_message"]
    assert all(view.children[i].disabled for i in range(5))
    assert db.bulk_writes == []
    await view.on_timeout()
    assert db.bulk_writes == []