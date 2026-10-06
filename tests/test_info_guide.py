"""The interactive /post-info guide: content limits, buttons, navigation and the command.
No Discord and no network: interactions are mocks."""
import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from cogs import info
from utils import info_pages as pages


def _run(coro):
    return asyncio.run(coro)


def _embed_size(e: discord.Embed) -> int:
    n = len(e.title or "") + len(e.description or "") + len((e.footer.text if e.footer else "") or "")
    return n + sum(len(f.name) + len(f.value) for f in e.fields)


# ------------------------------------------------------------------ content
@pytest.mark.parametrize("key", sorted(pages.PAGE_KEYS))
def test_every_page_fits_discords_embed_limits(key):
    e = pages.build_embed(key)
    assert e.title and (e.description or e.fields)
    assert len(e.description or "") <= 4096 and len(e.title) <= 256
    assert all(len(f.name) <= 256 and len(f.value) <= 1024 for f in e.fields) and len(e.fields) <= 25
    assert _embed_size(e) <= 6000


def test_unknown_page_falls_back_to_home():
    assert pages.build_embed("nope").title == pages.build_embed(pages.HOME).title


def test_reading_order_and_neighbours():
    assert pages.ORDER[:7] == pages.STEP_KEYS and set(pages.BUTTONS) == set(pages.ORDER)
    assert pages.neighbours("s1") == (None, "s2")
    assert pages.neighbours("cmds") == ("rules", None)
    assert pages.neighbours("s4") == ("s3", "s5")


def test_numbers_come_from_config_not_from_text(monkeypatch):
    monkeypatch.setattr(config, "HOST_REPLACE_LIMIT", 7)
    monkeypatch.setattr(config, "IGN_CHANGE_LIMIT", 9)
    monkeypatch.setattr(config, "REPORT_COOLDOWN_USES", 4)
    monkeypatch.setattr(config, "APPROVAL_TIMEOUT_SECONDS", 600)
    cmds = " ".join(f.value for f in pages.build_embed("cmds").fields)
    assert "**7** uses per match" in cmds and "**9** times per week" in cmds and "**4** reports per **1 hour**" in cmds
    assert "10 minutes" in pages.build_embed("s7").description


def test_channel_mention_when_configured_plain_name_otherwise(monkeypatch):
    monkeypatch.setattr(config, "INFO_VERIFY_CHANNEL_ID", None)
    assert "the verify channel" in pages.build_embed("s1").description
    monkeypatch.setattr(config, "INFO_VERIFY_CHANNEL_ID", 123456)
    assert "<#123456>" in pages.build_embed("s1").description


def test_the_crown_rule_is_explained():
    e = pages.build_embed("mmr")
    crown = next(f.value for f in e.fields if "crown" in f.name.lower())
    assert "+5" in crown and "each team" in crown and "any position" in crown and "win" in crown and "loss" in crown


def test_a_bad_channel_env_never_crashes_config(monkeypatch):
    monkeypatch.setenv("INFO_X", "<channel id>")
    assert config._optional_channel_id("INFO_X") is None
    monkeypatch.setenv("INFO_X", " 42 ")
    assert config._optional_channel_id("INFO_X") == 42


# ------------------------------------------------------------------ buttons
def _items(view):
    return [c for c in view.children]


def _check_view(view):
    ids = [c.item.custom_id for c in _items(view)]
    assert len(ids) == len(set(ids)), "custom_ids must be unique inside one message"
    assert len(_items(view)) <= 25
    rows = {}
    for c in _items(view):
        rows.setdefault(c.item.row, []).append(c)
    assert all(len(r) <= 5 for r in rows.values())
    pattern = {"info_open": info.InfoOpenButton.__discord_ui_compiled_template__,
               "info_go": info.InfoGoButton.__discord_ui_compiled_template__}
    for cid in ids:
        assert len(cid) <= 100
        assert pattern[cid.split(":")[0]].fullmatch(cid), cid


def test_public_hub_has_ten_buttons_in_two_rows():
    async def go():
        v = info.build_view("open", pages.HOME)
        _check_view(v)
        assert len(v.children) == 10 and {c.item.row for c in v.children} == {0, 1}
        assert all(c.item.custom_id.startswith("info_open:") for c in v.children)
    _run(go())


def test_private_menu_uses_go_buttons_and_pages_get_prev_menu_next():
    async def go():
        menu = info.build_view("go", pages.HOME)
        _check_view(menu)
        assert all(c.item.custom_id.startswith("info_go:") for c in menu.children)
        mid = info.build_view("go", "s4")
        _check_view(mid)
        assert [c.item.custom_id for c in mid.children] == ["info_go:s3", "info_go:home", "info_go:s5"]
        assert not any(c.item.disabled for c in mid.children)
        first, last = info.build_view("go", "s1"), info.build_view("go", "cmds")
        _check_view(first), _check_view(last)
        assert first.children[0].item.disabled and not first.children[2].item.disabled
        assert last.children[2].item.disabled and not last.children[0].item.disabled
    _run(go())


# ------------------------------------------------------------------ taps
def _interaction():
    i = MagicMock()
    i.response.send_message = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def test_first_tap_sends_one_private_message_and_does_nothing_else():
    async def go():
        i = _interaction()
        await info.InfoOpenButton("s3").callback(i)
        i.response.send_message.assert_awaited_once()
        kw = i.response.send_message.call_args.kwargs
        assert kw["ephemeral"] is True and "Step 3" in kw["embed"].title
        i.response.defer.assert_not_awaited()
        i.followup.send.assert_not_awaited()
        i.response.edit_message.assert_not_awaited()
    _run(go())


def test_later_taps_edit_the_same_private_message():
    async def go():
        i = _interaction()
        await info.InfoGoButton("rules").callback(i)
        i.response.edit_message.assert_awaited_once()
        assert "Rules" in i.response.edit_message.call_args.kwargs["embed"].title
        i.response.send_message.assert_not_awaited()
        i.response.defer.assert_not_awaited()
    _run(go())


def test_stale_button_opens_the_menu_and_an_expired_tap_never_crashes():
    async def go():
        i = _interaction()
        await info.InfoGoButton("removed_page").callback(i)
        assert "how it works" in i.response.edit_message.call_args.kwargs["embed"].title
        gone = _interaction()
        gone.response.edit_message.side_effect = discord.NotFound(MagicMock(status=404), "Unknown interaction")
        await info.InfoGoButton("s1").callback(gone)          # must not raise
        broken = _interaction()
        broken.response.send_message.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
        await info.InfoOpenButton("s1").callback(broken)      # must not raise
    _run(go())


def test_rebuilding_a_button_from_its_custom_id_after_a_restart():
    async def go():
        m = info.InfoGoButton.__discord_ui_compiled_template__.fullmatch("info_go:s6")
        item = await info.InfoGoButton.from_custom_id(MagicMock(), MagicMock(), m)
        assert item.page == "s6" and item.item.custom_id == "info_go:s6"
    _run(go())


# ------------------------------------------------------------------ /post-info
def _cog():
    bot = MagicMock()
    bot.user.id = 999
    bot.add_dynamic_items = MagicMock()
    return info.InfoCog(bot)


async def _post(cog, **kw):
    i = _interaction()
    i.channel.send = AsyncMock(return_value=MagicMock(jump_url="https://discord.com/channels/1/2/3"))
    await info.InfoCog.post_info.callback(cog, i, **kw)
    return i


def test_post_info_posts_once_with_defer_and_registers_buttons():
    async def go():
        cog = _cog()
        cog.bot.add_dynamic_items.assert_called_once_with(info.InfoOpenButton, info.InfoGoButton)
        i = await _post(cog)
        i.response.defer.assert_awaited_once()
        i.channel.send.assert_awaited_once()
        sent = i.channel.send.call_args.kwargs
        assert sent["embed"].title.startswith("🏆") and len(sent["view"].children) == 10
        assert "posted" in i.followup.send.call_args.args[0]
    _run(go())


def test_post_info_can_update_its_own_message_but_not_someone_elses():
    async def go():
        cog = _cog()
        mine = MagicMock(); mine.author.id = 999; mine.edit = AsyncMock(); mine.jump_url = "u"
        chan = MagicMock(); chan.fetch_message = AsyncMock(return_value=mine)
        i = await _post(cog, channel=chan, message_id="123")
        mine.edit.assert_awaited_once()
        assert "updated" in i.followup.send.call_args.args[0]
        theirs = MagicMock(); theirs.author.id = 1; theirs.edit = AsyncMock()
        chan.fetch_message = AsyncMock(return_value=theirs)
        i2 = await _post(cog, channel=chan, message_id="123")
        theirs.edit.assert_not_awaited()
        assert "wasn't posted by me" in i2.followup.send.call_args.args[0]
        i3 = await _post(cog, channel=chan, message_id="abc")
        assert "can't find" in i3.followup.send.call_args.args[0]
    _run(go())


def test_post_info_reports_missing_permissions_instead_of_crashing():
    async def go():
        cog = _cog()
        i = _interaction()
        i.channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
        await info.InfoCog.post_info.callback(cog, i)
        assert "permission" in i.followup.send.call_args.args[0]
    _run(go())
