"""Stand-ins for Discord and the database.

FakeInteraction records every Discord call the code makes, in order, in
`.calls`. That list is what the tests count. It never talks to Discord.
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

    async def send_message(self, *a, **kw):
        self._o._hit("send_message")


class _FakeFollowup:
    def __init__(self, owner: "FakeInteraction"):
        self._o = owner

    async def send(self, *a, **kw):
        self._o._hit("followup")


class FakeInteraction:
    """Looks enough like discord.Interaction for the vote callback.

    fail_on: names of calls that should raise a Discord 429, e.g. {"edit"}.
    """

    def __init__(self, user_id: int, fail_on: set[str] | None = None):
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse(self)
        self.followup = _FakeFollowup(self)
        self.calls: list[str] = []
        self._fail_on = fail_on or set()

    def _hit(self, name: str):
        self.calls.append(name)
        if name in self._fail_on:
            raise _make_429()

    async def edit_original_response(self, **kw):
        self._hit("edit")


def _make_429() -> discord.errors.HTTPException:
    class _Resp:
        status = 429
        reason = "Too Many Requests"

    return discord.errors.HTTPException(_Resp(), "Rate limit reached for webhook")


class FakeDB:
    """Replaces the parts of `adb` the vote callback uses.

    players: {discord_id: {"id": db_id, "ign": name}}
    Counts reads and bulk writes so tests can assert on them.
    """

    def __init__(self, players: dict[int, dict]):
        self.players = players
        self.reads = 0
        self.bulk_writes: list[list[dict]] = []

    async def get_player_by_discord_id(self, discord_id):
        self.reads += 1
        return self.players.get(int(discord_id))

    async def cast_skill_votes_bulk(self, votes):
        self.bulk_writes.append(list(votes))
