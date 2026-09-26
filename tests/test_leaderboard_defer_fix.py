"""LeaderboardView (cogs/stats.py) — 2026-09-26 fixes.

1. _render() called the DB BEFORE acking the interaction — live traceback
   (Sep 26 00:11) confirmed this hit discord.errors.NotFound 10062
   "Unknown interaction" whenever region_leaderboard() was slow. Fixed by
   deferring first.
2. self.page was never clamped against how many pages actually exist —
   only the DISPLAYED text was clamped internally, so repeated Next
   clicks kept incrementing self.page past the real last page. Live
   report: "page 3/1" on a 1-page, 42-player board. Fixed by clamping
   self.page itself inside _render, using the same players list it
   already fetched (no extra DB call).
"""
import pytest

from cogs import stats


class FakeResponse:
    def __init__(self, rec):
        self.rec = rec
    async def defer(self, **kw):
        self.rec.append("defer")
    async def edit_message(self, **kw):
        self.rec.append("edit_message")  # the old, buggy call — must not be used


class FakeInter:
    def __init__(self):
        self.rec = []
        self.response = FakeResponse(self.rec)
    async def edit_original_response(self, **kw):
        self.rec.append("edit_original_response")


class FakeAdb:
    def __init__(self, n_players=0):
        self.calls = 0
        self.n_players = n_players
    async def region_leaderboard(self):
        self.calls += 1
        return [{"ign": f"p{i}", "mmr": 200, "current_rank": "Elite"} for i in range(self.n_players)]


# ---------------- defer-first ----------------

async def test_render_acks_before_touching_the_db(monkeypatch):
    fake = FakeAdb()
    monkeypatch.setattr(stats, "adb", fake)
    view = stats.LeaderboardView()
    inter = FakeInter()
    await view._render(inter)
    assert inter.rec[0] == "defer"
    assert "edit_message" not in inter.rec
    assert fake.calls == 1


def test_render_source_defers_strictly_before_the_db_call():
    import inspect
    src = inspect.getsource(stats.LeaderboardView._render)
    defer_pos = src.index("response.defer()")
    db_pos = src.index("adb.region_leaderboard()")
    assert defer_pos < db_pos, "DB call happens before the ack — reintroduces the Sep 26 10062 bug"


# ---------------- page clamping (the "page 3/1" bug) ----------------

async def test_next_click_past_the_last_page_clamps_instead_of_climbing(monkeypatch):
    """42 players, page size 50 -> exactly 1 real page. Live bug: clicking
    Next repeatedly still incremented self.page (2, 3, 4...) forever."""
    monkeypatch.setattr(stats, "adb", FakeAdb(n_players=42))
    view = stats.LeaderboardView()
    inter = FakeInter()
    for _ in range(5):                       # spam Next 5 times
        view.page += 1
        await view._render(inter)
    assert view.page == 0, "self.page climbed past the last real page (0-indexed)"


async def test_footer_shows_the_clamped_page_not_the_raw_click_count(monkeypatch):
    """Reproduces the exact live report: 42 players (1 real page), page
    driven up to 4 by stray clicks -> footer must read 'page 1/1', not
    'page 3/1' (or worse, 'page 5/1')."""
    monkeypatch.setattr(stats, "adb", FakeAdb(n_players=42))
    view = stats.LeaderboardView()
    view.page = 4  # simulates several stray Next clicks already happened

    captured = {}
    orig_edit = FakeInter.edit_original_response
    async def spying_edit(self, **kw):
        captured["embed"] = kw.get("embed")
        await orig_edit(self, **kw)
    inter = FakeInter()
    inter.edit_original_response = spying_edit.__get__(inter)

    await view._render(inter)

    assert view.page == 0
    footer = captured["embed"].footer.text
    assert "page 1/1" in footer, footer
    assert "page 3/1" not in footer and "page 5/1" not in footer


async def test_total_pages_uses_ceil_division_not_floor(monkeypatch):
    """51 players, page size 50 -> 2 real pages (a partial 2nd page with
    just 1 player). Floor division would wrongly compute 1 page and clamp
    self.page back to 0, making that 51st player permanently unreachable."""
    monkeypatch.setattr(stats, "adb", FakeAdb(n_players=51))
    view = stats.LeaderboardView()
    view.page = 1  # the 2nd page, where player #51 actually lives
    inter = FakeInter()
    await view._render(inter)
    assert view.page == 1, "clamped away from the real 2nd page — division bug"
