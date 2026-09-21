"""Step A: pin down how many Discord calls / DB reads ONE vote click makes
TODAY. These are the 'before' numbers. After the one-call change we edit
the expected values here and the tests prove the improvement.
"""
import pytest

import config
from cogs import queue
from tests.fakes import FakeDB, FakeInteraction

TEAM = {1, 2, 3, 4, 5}                       # db ids on this team
PLAYERS = {100 + i: {"id": i, "ign": f"P{i}"} for i in range(1, 11)}  # discord 101..110
SKILLS = config.OPERATOR_SKILLS


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(PLAYERS)
    monkeypatch.setattr(queue, "adb", fake)
    return fake


def make_view():
    return queue.SkillVoteView(match_id=1, team="defender", team_player_ids=set(TEAM))


async def click(view, skill_index, discord_id, **kw):
    """Press the skill button #skill_index as discord user discord_id."""
    button = view.children[skill_index]
    inter = FakeInteraction(discord_id, **kw)
    await button.callback(inter)
    # Visible with:  python -m pytest -v -s
    note = f" (forced 429 on: {sorted(kw['fail_on'])})" if kw.get("fail_on") else ""
    print(f"\n  click by discord {discord_id} on skill #{skill_index}{note}"
          f" -> Discord calls: {inter.calls}")
    return inter


async def test_accepted_vote_is_two_discord_calls_and_one_db_read(db):
    view = make_view()
    inter = await click(view, 0, 101)          # player id 1, on the team
    assert inter.calls == ["defer", "edit"]     # 2 Discord calls
    assert db.reads == 1                        # 1 DB read
    assert view.children[0].disabled is True    # skill locked


async def test_wrong_team_click_is_defer_plus_followup(db):
    view = make_view()
    inter = await click(view, 0, 106)          # player id 6, NOT on the team
    assert inter.calls == ["defer", "followup"]
    assert db.reads == 1
    assert view.children[0].disabled is False   # nothing locked


async def test_already_voted_click_is_defer_plus_followup(db):
    view = make_view()
    await click(view, 0, 101)
    inter = await click(view, 1, 101)          # same player, second skill
    assert inter.calls == ["defer", "followup"]
    assert view.children[1].disabled is False


async def test_taken_skill_click_is_defer_plus_followup(db):
    view = make_view()
    await click(view, 0, 101)
    inter = await click(view, 0, 102)          # teammate, same skill
    assert inter.calls == ["defer", "followup"]


async def test_failed_edit_adds_a_third_fallback_call(db):
    view = make_view()
    inter = await click(view, 0, 101, fail_on={"edit"})
    assert inter.calls == ["defer", "edit", "followup"]   # 3 calls
    assert view.children[0].disabled is True               # vote still counted


async def test_fifth_vote_flushes_once(db):
    view = make_view()
    for i in range(5):
        await click(view, i, 101 + i)
    assert len(db.bulk_writes) == 1
    assert len(db.bulk_writes[0]) == 5


async def test_full_team_totals(db):
    """5 accepted votes = 10 Discord calls, 5 DB reads (per team)."""
    view = make_view()
    total = 0
    for i in range(5):
        inter = await click(view, i, 101 + i)
        total += len(inter.calls)
    assert total == 10
    assert db.reads == 5