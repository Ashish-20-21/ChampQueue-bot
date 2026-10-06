"""Mocked end-to-end test of the admin crown-pick flow (no Discord, no DB).

Scenario: crown hidden on Team A's screenshot. An admin opens the crown
picker, chooses position 2, and the match must go to host approval with the
+5 on A's row 2 — stored pick on matches.crown_override, raw OCR untouched.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs import match as m
from tests.test_impact_crown import SUMMIT, _extraction, _roster


def _hidden_a():
    return [(t, p, i, k, d, a, s, imp, False if t == "A" else c) for (t, p, i, k, d, a, s, imp, c) in SUMMIT]


@pytest.fixture
def env(monkeypatch):
    rows = _hidden_a()
    raw = _extraction(rows)
    state = {"match": {"id": 7, "match_id": "CQ-7", "status": "awaiting_review", "map_pool": ["SUMMIT"],
                       "text_channel_id": None, "queue_key": "q", "crown_override": None}}

    async def get_match(_id):
        return dict(state["match"])

    async def update_match(_id, payload):
        state["match"].update(payload)

    fake = MagicMock()
    fake.get_match = AsyncMock(side_effect=get_match)
    fake.update_match = AsyncMock(side_effect=update_match)
    fake.get_match_screenshot = AsyncMock(return_value={"raw_extraction": raw, "image_url": "https://img"})
    fake.get_match_players = AsyncMock(return_value=_roster(rows))
    fake.replace_match_round_data = AsyncMock()
    monkeypatch.setattr(m, "adb", fake)
    monkeypatch.setattr(m, "_post_match_status", AsyncMock())
    monkeypatch.setattr(m.config, "RESULT_APPROVAL_CHANNEL_ID", 99, raising=False)

    approval = MagicMock(send=AsyncMock())
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=approval)
    cog = m.Match.__new__(m.Match)
    cog.bot = bot
    cog._recompute_career_stats_bulk = AsyncMock()
    cog._notify_afk_leaver = AsyncMock()

    inter = MagicMock()
    inter.user.id = 555
    inter.user.mention = "<@555>"
    inter.followup.send = AsyncMock()
    inter.message.embeds = []
    return cog, inter, fake, state, approval, raw


def test_admin_pick_sends_match_to_host_approval(env):
    cog, inter, fake, state, approval, raw = env
    raw_before = [p["has_crown"] for p in raw["players"]]
    asyncio.run(cog._set_crown_override(inter, 7, "A", 2))

    assert state["match"]["crown_override"]["A"]["position"] == 2
    assert state["match"]["crown_override"]["A"]["by"] == "555"
    assert state["match"]["status"] == "pending_verification"
    rows = fake.replace_match_round_data.call_args.args[2]
    a = sorted((r for r in rows if r["team"] == "A"), key=lambda r: r["position"])
    assert [r["mmr_delta"] for r in a] == [-3, 1, -6, -8, -9]
    assert [r["bonus_5"] for r in a] == [False, True, False, False, False]
    assert all(r["bonus_5"] == r["is_crown"] for r in rows)
    assert approval.send.await_count == 1                       # verification card posted
    assert [p["has_crown"] for p in raw["players"]] == raw_before  # raw OCR untouched


def test_position_not_on_scoreboard_is_refused(env):
    cog, inter, fake, state, approval, raw = env
    raw["players"] = [p for p in raw["players"] if not (p["team"] == "A" and p["position"] == 5)]
    asyncio.run(cog._set_crown_override(inter, 7, "A", 5))
    assert state["match"]["crown_override"] is None
    assert "isn't on Team A" in inter.followup.send.call_args.args[0]


def test_one_team_set_other_still_needed(env):
    cog, inter, fake, state, approval, raw = env
    for p in raw["players"]:
        p["has_crown"] = False                                  # both crowns hidden
    asyncio.run(cog._set_crown_override(inter, 7, "A", 2))
    assert "Still needed: Team B" in inter.followup.send.call_args.args[0]
    assert state["match"]["status"] == "awaiting_review"
    asyncio.run(cog._set_crown_override(inter, 7, "B", 1))
    assert state["match"]["status"] == "pending_verification"
    assert set(state["match"]["crown_override"]) == {"A", "B"}


def test_not_awaiting_review_is_refused(env):
    cog, inter, fake, state, approval, raw = env
    state["match"]["status"] = "completed"
    asyncio.run(cog._set_crown_override(inter, 7, "A", 2))
    assert "no longer awaiting review" in inter.followup.send.call_args.args[0]
    fake.update_match.assert_not_awaited()
