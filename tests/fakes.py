"""Stand-ins for Discord and the database.

FakeInteraction records every Discord call the code makes, in order, in
`.calls`, and the text of any message sent in `.messages`. That is what the
tests count and read. It never talks to Discord.
"""
from __future__ import annotations

import discord


class _FakeResponse:
    def __init__(self, owner: "FakeInteraction"):
        self._o = owner

    async def defer(self, **kw):
        self._o._hit("defer")

    async def edit_message(self, **kw):
        self._o._hit("edit_message")
        self._o.edit_view = kw.get("view")

    async def send_message(self, content=None, **kw):
        self._o._hit("send_message")
        self._o.messages.append(content)


class _FakeFollowup:
    def __init__(self, owner: "FakeInteraction"):
        self._o = owner

    async def send(self, content=None, **kw):
        self._o._hit("followup")
        self._o.messages.append(content)


class FakeInteraction:
    """Looks enough like discord.Interaction for the vote callback.

    fail_on: names of calls that should raise a Discord 429, e.g. {"edit_message"}.
    """

    def __init__(self, user_id: int, fail_on: set[str] | None = None):
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse(self)
        self.followup = _FakeFollowup(self)
        self.calls: list[str] = []
        self.messages: list[str] = []
        self.edit_view = None
        self._fail_on = fail_on or set()

    def _hit(self, name: str):
        self.calls.append(name)
        if name in self._fail_on:
            raise _make_429()

    async def edit_original_response(self, **kw):
        # The new vote code must NOT use this. Recorded so a test can catch it.
        self._hit("edit_original_response")


def _make_429() -> discord.errors.HTTPException:
    class _Resp:
        status = 429
        reason = "Too Many Requests"

    return discord.errors.HTTPException(_Resp(), "Rate limit reached for webhook")


class FakeDB:
    """Replaces the parts of `adb` the vote code may touch.

    A click must NOT read the database any more, so any read raises. Only the
    bulk write (the flush) is allowed, and it is recorded.
    """

    def __init__(self):
        self.bulk_writes: list[list[dict]] = []

    async def get_player_by_discord_id(self, discord_id):
        raise AssertionError("DB read inside the vote click path — the roster should be used instead")

    async def cast_skill_votes_bulk(self, votes):
        self.bulk_writes.append(list(votes))