"""Slash commands added 2026-09-21: registration, /admin-update-host,
/host-replace-player. Fakes only: no Discord, no Supabase."""
import pytest

import config
from cogs import admin, match


# ---------------- registration (this would have caught the misplaced-code bug) ----------------

def test_commands_registered_in_the_right_cogs():
    a = {c.name for c in admin.Admin.__cog_app_commands__}
    m = {c.name for c in match.Match.__cog_app_commands__}
    assert "admin-update-host" in a
    assert "host-replace-player" in m
    assert "host-replace-player" not in a      # Admin cog is manage_guild-gated; hosts must not need it


# ---------------- fakes ----------------

class Member:
    def __init__(self, uid):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.display_name = f"user{uid}"


class Chan:
    def __init__(self):
        self.sent, self.perms = [], []

    async def send(self, content=None, **kw):
        self.sent.append(content)

    async def set_permissions(self, target, **kw):
        self.perms.append((target.id, kw))


class Guild:
    def __init__(self, chan):
        self.chan = chan
        self.asked = []

    def get_channel(self, cid):
        self.asked.append(cid)
        return self.chan

    def get_member(self, uid):
        assert isinstance(uid, int), "get_member needs an int (discord_id is stored as text)"
        return Member(uid)


class Inter:
    def __init__(self, uid, guild=None, order=None):
        self.user = Member(uid)
        self.guild = guild
        self.order = order if order is not None else []
        self.replies, self.followups = [], []
        self.response = type("R", (), {
            "send_message": self._send, "defer": self._defer})()
        self.followup = type("F", (), {"send": self._follow})()
        self.deferred = False

    async def _send(self, content=None, **kw):
        self.replies.append(content)

    async def _defer(self, **kw):
        self.deferred = True
        self.order.append("defer")

    async def _follow(self, content=None, **kw):
        self.followups.append(content)


def P(pid, discord_id, ign, **kw):
    return {"id": pid, "discord_id": str(discord_id), "ign": ign, "status": "approved",
            "reputation": 100, **kw}


class FakeAdb:
    """Only what the two commands touch. Records writes."""

    def __init__(self, players_by_discord, roster, match_row, claim_ok=True, elsewhere=None, queue=()):
        self.p, self.roster, self.m = players_by_discord, roster, match_row
        self.claim_ok, self.elsewhere, self.queue = claim_ok, elsewhere, list(queue)
        self.calls = []
        self.order = []

    async def get_match_by_code(self, code):
        self.order.append("db")
        return self.m
    async def get_player_by_discord_id(self, did): return self.p.get(int(did))
    async def get_match_players(self, pk): return self.roster
    async def get_active_match_for_player(self, pid, excl=None): return self.elsewhere
    async def claim_host_replacement(self, pk, used):
        self.calls.append(("claim", used)); return self.claim_ok
    async def queue_current(self): return self.queue
    async def queue_leave(self, pid): self.calls.append(("queue_leave", pid))
    async def remove_match_player(self, pk, pid): self.calls.append(("remove", pid))
    async def add_match_player(self, pk, pid, team, is_captain=False): self.calls.append(("add", pid, team))
    async def update_match(self, pk, fields): self.calls.append(("update", fields))


def roster_rows(*pairs):  # (player_id, team, ign)
    return [{"player_id": i, "team": t, "players": {"ign": n}} for i, t, n in pairs]


def world(**over):
    """Match CQ-1: host=player 1 (discord 101); old=player 2 (102); spare new=player 20 (120)."""
    players = {101: P(1, 101, "Host"), 102: P(2, 102, "Old"), 120: P(20, 120, "New"), 103: P(3, 103, "Mate")}
    roster = roster_rows((1, "A", "Host"), (2, "A", "Old"), (3, "B", "Mate"))
    m = {"id": 7, "match_id": "CQ-1", "status": "awaiting_room", "room_code_shared_by": 1,
         "text_channel_id": "555", "host_replacements_used": 0}
    m.update(over.pop("match", {}))
    return FakeAdb(over.pop("players", players), roster, m, **over)


@pytest.fixture
def cog(monkeypatch):
    monkeypatch.setattr(config, "HOST_REPLACE_LIMIT", 2)
    monkeypatch.setattr(config, "MATCH_LOG_CHANNEL_ID", None)
    monkeypatch.setattr(match, "is_admin", lambda i: False)
    c = match.Match.__new__(match.Match)
    c.bot = type("B", (), {"get_channel": lambda self, cid: None})()
    return c


async def run(cog, monkeypatch, fake, caller=101, old=102, new=120):
    monkeypatch.setattr(match, "adb", fake)
    chan = Chan()
    inter = Inter(caller, Guild(chan), fake.order)
    await match.Match.host_replace_player.callback(cog, inter, "1", Member(old), Member(new))
    return inter, chan


# ---------------- /host-replace-player ----------------

async def test_happy_path_swaps_player_and_uses_one_slot(cog, monkeypatch):
    fake = world()
    inter, chan = await run(cog, monkeypatch, fake)
    assert ("claim", 0) in fake.calls
    assert ("remove", 2) in fake.calls and ("add", 20, "A") in fake.calls      # new player inherits the team
    assert inter.followups and "→" in inter.followups[0]
    assert any("replaced by" in (s or "") for s in chan.sent)
    assert (102, {"overwrite": None}) in chan.perms                            # old player loses access (int id used)


async def test_non_host_is_refused_and_nothing_changes(cog, monkeypatch):
    fake = world()
    inter, _ = await run(cog, monkeypatch, fake, caller=103)                   # a teammate, not the host
    assert "Only the Match Host" in inter.followups[0]
    assert fake.calls == []


async def test_limit_reached_is_refused(cog, monkeypatch):
    fake = world(match={"host_replacements_used": 2})
    inter, _ = await run(cog, monkeypatch, fake)
    assert "2/2" in inter.followups[0]
    assert fake.calls == []


async def test_switch_off_with_limit_zero(cog, monkeypatch):
    monkeypatch.setattr(config, "HOST_REPLACE_LIMIT", 0)
    fake = world()
    inter, _ = await run(cog, monkeypatch, fake)
    assert "disabled" in inter.followups[0] and fake.calls == []


async def test_host_cannot_replace_self(cog, monkeypatch):
    fake = world()
    inter, _ = await run(cog, monkeypatch, fake, old=101)
    assert "replace yourself" in inter.followups[0] and fake.calls == []


async def test_new_player_not_approved_is_refused(cog, monkeypatch):
    fake = world()
    fake.p[120]["status"] = "banned"
    inter, _ = await run(cog, monkeypatch, fake)
    assert "not approved" in inter.followups[0] and fake.calls == []


async def test_new_player_low_reputation_is_refused(cog, monkeypatch):
    fake = world()
    fake.p[120]["reputation"] = -9999
    inter, _ = await run(cog, monkeypatch, fake)
    assert "can't play right now" in inter.followups[0] and fake.calls == []


async def test_new_player_already_in_another_live_match_is_refused(cog, monkeypatch):
    fake = world(elsewhere={"match_id": "CQ-9", "status": "awaiting_result"})
    inter, _ = await run(cog, monkeypatch, fake)
    assert "CQ-9" in inter.followups[0] and fake.calls == []


async def test_old_player_not_in_match_is_refused(cog, monkeypatch):
    fake = world()
    fake.p[130] = P(30, 130, "Stranger")
    inter, _ = await run(cog, monkeypatch, fake, old=130)
    assert "isn't part of match" in inter.followups[0] and fake.calls == []


async def test_finished_match_is_refused(cog, monkeypatch):
    fake = world(match={"status": "completed"})
    inter, _ = await run(cog, monkeypatch, fake)
    assert "pre-review" in inter.followups[0] and fake.calls == []


async def test_lost_race_on_the_counter_changes_nothing(cog, monkeypatch):
    fake = world(claim_ok=False)                                               # someone else took the slot
    inter, _ = await run(cog, monkeypatch, fake)
    assert "run the command again" in inter.followups[0]
    assert ("remove", 2) not in fake.calls and not any(c[0] == "add" for c in fake.calls)


async def test_new_player_is_pulled_out_of_the_queue(cog, monkeypatch):
    fake = world(queue=[{"player_id": 20}])
    await run(cog, monkeypatch, fake)
    assert ("queue_leave", 20) in fake.calls


async def test_admin_bypasses_limit_and_does_not_consume_it(cog, monkeypatch):
    monkeypatch.setattr(match, "is_admin", lambda i: True)
    fake = world(match={"host_replacements_used": 2})
    inter, _ = await run(cog, monkeypatch, fake, caller=999)
    assert not any(c[0] == "claim" for c in fake.calls)
    assert ("add", 20, "A") in fake.calls


# ---------------- /admin-update-host ----------------

@pytest.fixture
def admin_cog(monkeypatch):
    monkeypatch.setattr(config, "MATCH_LOG_CHANNEL_ID", None)
    c = admin.Admin.__new__(admin.Admin)
    c.bot = None
    return c


async def run_uh(admin_cog, monkeypatch, fake, new=102, reason=""):
    monkeypatch.setattr(admin, "adb", fake)
    chan = Chan()
    inter = Inter(900, Guild(chan), fake.order)
    await admin.Admin.update_host.callback(admin_cog, inter, "CQ-1", Member(new), reason)
    return inter, chan


async def test_update_host_happy_path(admin_cog, monkeypatch):
    fake = world()
    inter, chan = await run_uh(admin_cog, monkeypatch, fake, reason="host went AFK")
    assert ("update", {"room_code_shared_by": 2}) in fake.calls
    assert any("Host changed" in (s or "") and "host went AFK" in s for s in chan.sent)


async def test_update_host_new_host_must_be_in_match(admin_cog, monkeypatch):
    fake = world()
    fake.p[120] = P(20, 120, "New")
    inter, _ = await run_uh(admin_cog, monkeypatch, fake, new=120)
    assert "isn't part of match" in inter.followups[0] and fake.calls == []


async def test_update_host_same_host_is_refused(admin_cog, monkeypatch):
    fake = world()
    inter, _ = await run_uh(admin_cog, monkeypatch, fake, new=101)
    assert "already the host" in inter.followups[0] and fake.calls == []


async def test_update_host_finished_match_is_refused(admin_cog, monkeypatch):
    fake = world(match={"status": "abandoned"})
    inter, _ = await run_uh(admin_cog, monkeypatch, fake)
    assert "no longer be changed" in inter.followups[0] and fake.calls == []


# ---------------- defer first + match-channel-only (2026-09-21 live fixes) ----------------

async def test_host_replace_defers_before_any_db_read(cog, monkeypatch):
    fake = world()
    inter, _ = await run(cog, monkeypatch, fake)
    assert fake.order[0] == "defer" and inter.replies == []       # nothing sent the old way


async def test_host_replace_refusals_also_defer_first(cog, monkeypatch):
    fake = world()
    inter, _ = await run(cog, monkeypatch, fake, caller=103)      # non-host -> refusal
    assert fake.order[0] == "defer" and inter.replies == [] and inter.followups


async def test_host_replace_never_posts_to_match_log(cog, monkeypatch):
    monkeypatch.setattr(config, "MATCH_LOG_CHANNEL_ID", 999)
    asked = []
    cog.bot = type("B", (), {"get_channel": lambda self, cid: asked.append(cid)})()
    fake = world()
    inter, chan = await run(cog, monkeypatch, fake)
    assert 999 not in asked and any("replaced by" in (x or "") for x in chan.sent)   # match channel only


async def test_update_host_defers_before_any_db_read(admin_cog, monkeypatch):
    fake = world()
    inter, _ = await run_uh(admin_cog, monkeypatch, fake)
    assert fake.order[0] == "defer" and inter.replies == []


async def test_update_host_never_posts_to_match_log(admin_cog, monkeypatch):
    monkeypatch.setattr(config, "MATCH_LOG_CHANNEL_ID", 999)
    fake = world()
    monkeypatch.setattr(admin, "adb", fake)
    chan = Chan(); guild = Guild(chan); inter = Inter(900, guild, fake.order)
    await admin.Admin.update_host.callback(admin_cog, inter, "CQ-1", Member(102), "")
    assert 999 not in guild.asked and any("Host changed" in (x or "") for x in chan.sent)
