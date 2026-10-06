"""Nightly maintenance restart: the player-facing notice (2026-10).

What this cog does, and what it does NOT do
-------------------------------------------
The restart itself is a Katabump panel schedule (06:00 IST = 00:30 UTC). This
cog only tells players about it and checks that it happened:

  05:59 IST  post ONE "bot is taking a nap" message, remember its id in a
             small marker file, put an info line in #botlog.
  boot       after on_ready, EDIT that same message to "back online" and
             delete the marker. No new post, no fetch: one API call.
  06:07 IST  if the marker is still there the restart never happened (the
             schedule was off or misfired). Edit the message to say so, log
             it, delete the marker. If the bot died instead, nobody is
             running this check; the panel's 06:05 "Start server" task is
             the net for that, and the message stays on "nap" (accurate).

Restarts that did not follow a notice (crash, deploy, manual restart) find
no marker and stay completely silent.

Everything is behind switches.NIGHTLY_RESTART_NOTICES (default off) plus
config.MAINTENANCE_CHANNEL_ID. Every Discord/file failure is logged and
swallowed: this cog must never be the reason the bot misbehaves.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks

import config
import switches
from database.db import adb, with_retry

logger = logging.getLogger(__name__)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# How long after the notice the "did the restart happen?" check runs.
CHECK_DELAY_MINUTES = 8
# A marker older than this is stale: just remove it, don't edit anything.
MARKER_MAX_AGE_SECONDS = 2 * 60 * 60

NAP_TEXT = (
    "😴 **Bot's taking a quick nap.** Restarting in about a minute and back "
    "within ~2 minutes. You can carry on after that.\n"
    "-# If this still says *nap* after 5 more minutes, ping staff."
)
BACK_TEXT = "⚡ **Refreshed and back online.** Queues are open, queue away!"
SKIPPED_TEXT = "✅ **Maintenance skipped tonight.** The bot stayed up. Queues are open as normal."


def _notice_time() -> datetime.time:
    hh, mm = config.MAINTENANCE_NOTICE_HHMM
    return datetime.time(hour=hh, minute=mm, tzinfo=IST)


def _check_time() -> datetime.time:
    hh, mm = config.MAINTENANCE_NOTICE_HHMM
    base = datetime.datetime(2000, 1, 1, hh, mm) + datetime.timedelta(minutes=CHECK_DELAY_MINUTES)
    return datetime.time(hour=base.hour, minute=base.minute, tzinfo=IST)


def _rss_mb() -> float | None:
    """Current RSS in MB from /proc (Linux only; None elsewhere). Avoids a
    psutil dependency."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


_IMPORTED_AT = time.monotonic()


class Maintenance(commands.Cog):
    def __init__(self, bot: commands.Bot, *, enabled: bool, channel_id: int | None, marker_path: str | Path):
        self.bot = bot
        self.channel_id = channel_id
        self.marker_path = Path(marker_path)
        self.enabled = bool(enabled and channel_id)
        self._boot_handled = False
        if enabled and not channel_id:
            logger.warning("NIGHTLY_RESTART_NOTICES is on but MAINTENANCE_CHANNEL_ID is not set: notices stay off.")

    # ---------- lifecycle ----------

    async def cog_load(self):
        if self.enabled:
            self.notice_loop.start()
            self.check_loop.start()
            logger.info(
                "Maintenance notices ON: notice %s IST, restart check %s IST.",
                _notice_time().strftime("%H:%M"), _check_time().strftime("%H:%M"),
            )

    def cog_unload(self):
        self.notice_loop.cancel()
        self.check_loop.cancel()

    # ---------- marker file ----------

    def _write_marker(self, channel_id: int, message_id: int) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.marker_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "channel_id": channel_id, "message_id": message_id, "posted_ts": time.time(),
        }), encoding="utf-8")
        os.replace(tmp, self.marker_path)  # atomic: never a half-written marker

    def _read_marker(self) -> dict | None:
        try:
            data = json.loads(self.marker_path.read_text(encoding="utf-8"))
            return {
                "channel_id": int(data["channel_id"]),
                "message_id": int(data["message_id"]),
                "posted_ts": float(data["posted_ts"]),
            }
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Maintenance marker unreadable; removing it.", exc_info=True)
            self._clear_marker()
            return None

    def _clear_marker(self) -> None:
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not delete maintenance marker %s", self.marker_path, exc_info=True)

    # ---------- discord helpers (never raise) ----------

    async def _botlog(self, text: str) -> None:
        logger.info("[MAINTENANCE] %s", text)
        if not config.BOTLOG_CHANNEL_ID:
            return
        try:
            channel = self.bot.get_channel(config.BOTLOG_CHANNEL_ID)
            if channel is None:
                channel = await self.bot.fetch_channel(config.BOTLOG_CHANNEL_ID)
            await channel.send(f"🔧 **[MAINTENANCE]** {text}", allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Maintenance: could not post to #botlog")

    async def _edit_notice(self, marker: dict, text: str) -> bool:
        """Edit the stored notice message in place. One API call, no fetch."""
        try:
            partial = self.bot.get_partial_messageable(marker["channel_id"]).get_partial_message(marker["message_id"])
            await partial.edit(content=text)
            return True
        except Exception:
            logger.exception("Maintenance: could not edit notice message %s", marker["message_id"])
            return False

    # ---------- the three moments ----------

    async def _do_notice(self) -> None:
        """05:59: post the nap message and leave the marker."""
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except Exception:
                logger.exception("Maintenance: channel %s not reachable; no notice sent.", self.channel_id)
                return
        try:
            msg = await channel.send(NAP_TEXT, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Maintenance: could not post the nap notice.")
            return
        try:
            self._write_marker(channel.id, msg.id)
        except OSError:
            logger.exception("Maintenance: notice posted but marker not written; it will stay on 'nap'.")
            await self._botlog("Nap notice posted but the marker file could not be written. The message will NOT auto-update.")
            return

        waiting = "?"
        try:
            waiting = len(await with_retry(adb.queue_current))
        except Exception:
            logger.warning("Maintenance: queue count unavailable for the botlog line.", exc_info=True)
        await self._botlog(f"Nap notice posted. Waiting queue entries right now: {waiting}.")

    async def _do_check(self) -> None:
        """Notice + 8 min: marker still there = the restart never happened."""
        marker = self._read_marker()
        if marker is None:
            return
        await self._edit_notice(marker, SKIPPED_TEXT)
        self._clear_marker()
        await self._botlog("Scheduled restart did NOT happen (bot never rebooted after the notice). Check the Katabump schedule.")

    async def _do_boot(self) -> None:
        """First on_ready of this process: finish a planned restart, if any."""
        if self._boot_handled:
            return
        self._boot_handled = True

        planned = False
        marker = self._read_marker()
        if marker is not None:
            age = time.time() - marker["posted_ts"]
            if age <= MARKER_MAX_AGE_SECONDS:
                planned = True
                await self._edit_notice(marker, BACK_TEXT)
            else:
                logger.info("Maintenance: stale marker (%.0f min old) removed without editing.", age / 60)
            self._clear_marker()

        rss = _rss_mb()
        took = time.monotonic() - _IMPORTED_AT
        await self._botlog(
            f"Boot OK ({'planned restart' if planned else 'unplanned/manual start'}); "
            f"ready {took:.0f}s after cog load"
            + (f", RAM {rss:.0f} MB." if rss is not None else ".")
        )

    # ---------- discord.py hooks ----------

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.enabled:
            return
        try:
            await self._do_boot()
        except Exception:
            logger.exception("Maintenance: boot handling failed")

    @tasks.loop(time=_notice_time())
    async def notice_loop(self):
        try:
            await self._do_notice()
        except Exception:
            logger.exception("Maintenance: notice failed")

    @tasks.loop(time=_check_time())
    async def check_loop(self):
        try:
            await self._do_check()
        except Exception:
            logger.exception("Maintenance: restart check failed")

    @notice_loop.before_loop
    @check_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Maintenance(
        bot,
        enabled=switches.NIGHTLY_RESTART_NOTICES,
        channel_id=config.MAINTENANCE_CHANNEL_ID,
        marker_path=config.MAINTENANCE_MARKER_PATH,
    ))
