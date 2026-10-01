"""Per-interaction timing meter: how long a click or command takes, press to reaction.

utils/discord_meter.py counts Discord calls per minute. This module answers a
different question: for ONE button press / slash command / modal submit, where
did the time go? It writes one log line per interaction:

  INTERACTION_TIMING kind=component action=join_queue_INDIA_ME arrive=90 ack=230
    done=1210 handler=1120 lock=0 db=860(n=4,pool=3,retry=0) discord=210(n=2)
    | discord.defer_update=140 db.get_player_by_discord_id=290 db.queue_current=260 ...

Fields (all milliseconds):
  arrive   press -> our handler started. Discord stamps every interaction with its
           creation time (inside the id), so this is gateway + network + our event
           loop being busy. The two clocks (Discord's and this host's) can differ by
           a few ms, so treat small values as noise; negative values are clamped to 0.
  ack      press -> first Discord reply finished (defer / edit / send). The spinner
           stops here. Absent if the handler made no interaction reply.
  done     press -> LAST Discord reply finished. For defer-then-edit flows this is
           the moment the player actually sees the change. Absent if no reply.
  handler  how long our handler ran (start to finish).
  lock     time spent waiting to get an asyncio lock (queue lock). Only the places
           that use timed_lock() are measured.
  db       total time inside DB calls (n = how many, pool = time they sat waiting for
           a free worker thread before starting, retry = with_retry retries).
           db is a SUM, so parallel calls can add up to more than the handler time.
  discord  total time inside Discord API calls made during the interaction.
  After the "|": each step in the order it FINISHED, so the slowest step is easy to spot.
  A trailing "slow" means done (or handler, if there was no reply) took >= 2000 ms.

How it is attached: install() wraps the three places discord.py runs one task per
interaction (View._scheduled_task for buttons/selects, Modal._scheduled_task, and
CommandTree._call for slash commands). A ContextVar carries the timing object into
everything that handler awaits. The DB proxy (database/db.py), with_retry, and
utils/discord_meter.py report into it. Work started by an unrelated task (no
timing in its context) is simply not measured.

Safety: this module only reads clocks and writes one log line. Every public function
catches its own errors, the original function is ALWAYS called, and exceptions from
the original pass through untouched. Turning it off (INTERACTION_TIMING=false) means
install() patches nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
import time

logger = logging.getLogger("champions_queue")

_SLOW_MS = 2000
_MAX_STEPS_LOGGED = 14   # a long handler can't make a huge log line

_current: contextvars.ContextVar["_Timing | None"] = contextvars.ContextVar(
    "interaction_timing", default=None)

_HEX_ID = re.compile(r"^[0-9a-f]{16,}$")
_LONG_DIGITS = re.compile(r"\d{4,}")


class _Timing:
    __slots__ = ("kind", "action", "arrive_ms", "t0", "steps", "lock_ms",
                 "db_ms", "db_n", "db_pool_ms", "retries", "disc_ms", "disc_n",
                 "first_ack", "last_reply", "closed")

    def __init__(self, kind: str, action: str, arrive_ms: float) -> None:
        self.kind = kind
        self.action = action
        self.arrive_ms = arrive_ms
        self.t0 = time.perf_counter()
        self.steps: list[tuple[str, float]] = []
        self.lock_ms = 0.0
        self.db_ms = 0.0
        self.db_n = 0
        self.db_pool_ms = 0.0
        self.retries = 0
        self.disc_ms = 0.0
        self.disc_n = 0
        self.first_ack: float | None = None    # ms after handler start
        self.last_reply: float | None = None   # ms after handler start
        self.closed = False


# ── helpers ──────────────────────────────────────────────────────────

def current() -> "_Timing | None":
    """The timing object for the interaction being handled, or None. Never raises."""
    try:
        t = _current.get()
        return None if (t is None or t.closed) else t
    except Exception:
        return None


def _elapsed_ms(t: _Timing) -> float:
    return (time.perf_counter() - t.t0) * 1000.0


def normalize_action(custom_id: str | None, fallback: str) -> str:
    """Make a custom_id safe and low-cardinality for the log: random hex ids
    (e.g. operator-skill buttons) become the fallback name, long digit runs
    (match ids, user ids) become '#'."""
    try:
        if not custom_id:
            return fallback
        cid = str(custom_id)
        if _HEX_ID.match(cid):
            return fallback
        return _LONG_DIGITS.sub("#", cid)[:60]
    except Exception:
        return fallback


# ── reporting hooks (called by db.py, discord_meter.py, queue.py) ─────

def note_db(name: str, total_ms: float, pool_ms: float | None) -> None:
    try:
        t = current()
        if t is None:
            return
        t.db_ms += total_ms
        t.db_n += 1
        if pool_ms is not None:
            t.db_pool_ms += pool_ms
        t.steps.append((f"db.{name}", total_ms))
    except Exception:
        pass


def note_retry() -> None:
    try:
        t = current()
        if t is not None:
            t.retries += 1
    except Exception:
        pass


def note_discord(label: str, dur_ms: float, cat: str) -> None:
    """One finished Discord API call. cat is 'reply' (interaction reply) or 'chan'."""
    try:
        t = current()
        if t is None:
            return
        t.disc_ms += dur_ms
        t.disc_n += 1
        t.steps.append((f"discord.{label}", dur_ms))
        if cat == "reply":
            now = _elapsed_ms(t)
            if t.first_ack is None:
                t.first_ack = now
            t.last_reply = now
    except Exception:
        pass


def note_lock(wait_ms: float) -> None:
    try:
        t = current()
        if t is None:
            return
        t.lock_ms += wait_ms
        if wait_ms >= 1.0:
            t.steps.append(("lock.wait", wait_ms))
    except Exception:
        pass


async def timed_db(name: str, fn, args: tuple, kwargs: dict):
    """Same as `await asyncio.to_thread(fn, *args, **kwargs)`, but also records how
    long the call took and how long it waited for a free worker thread. The result
    and any exception are passed through unchanged."""
    t_submit = time.perf_counter()
    started: list[float] = []

    def _run():
        started.append(time.perf_counter())
        return fn(*args, **kwargs)

    try:
        return await asyncio.to_thread(_run)
    finally:
        try:
            end = time.perf_counter()
            note_db(name, (end - t_submit) * 1000.0,
                    (started[0] - t_submit) * 1000.0 if started else None)
        except Exception:
            pass


@contextlib.asynccontextmanager
async def timed_lock(lock: asyncio.Lock):
    """`async with timed_lock(lock):` behaves exactly like `async with lock:` and
    records how long acquiring it took."""
    t_wait = time.perf_counter()
    await lock.acquire()
    try:
        note_lock((time.perf_counter() - t_wait) * 1000.0)
    except Exception:
        pass
    try:
        yield
    finally:
        lock.release()


# ── start / finish ───────────────────────────────────────────────────

def _find_interaction(args: tuple):
    for a in args:
        if hasattr(a, "created_at") and hasattr(a, "data") and hasattr(a, "type"):
            return a
    return None


def _start(kind: str, args: tuple) -> "_Timing | None":
    """Build the timing object for this interaction, or None if it should not be
    timed (autocomplete, or anything we cannot read). Never raises."""
    try:
        interaction = _find_interaction(args)
        if interaction is None:
            return None
        if getattr(getattr(interaction, "type", None), "name", "") == "autocomplete":
            return None
        data = getattr(interaction, "data", None) or {}
        if kind == "command":
            action = str(data.get("name") or "command")[:60]
        else:
            action = normalize_action(data.get("custom_id"), kind)
        try:
            arrive = (time.time() - interaction.created_at.timestamp()) * 1000.0
        except Exception:
            arrive = 0.0
        return _Timing(kind, action, max(0.0, arrive))
    except Exception:
        return None


def _finish(t: _Timing) -> None:
    """Close the timing object and write the one log line. Never raises."""
    try:
        t.closed = True
        handler = _elapsed_ms(t)
        parts = [f"kind={t.kind}", f"action={t.action}", f"arrive={t.arrive_ms:.0f}"]
        if t.first_ack is not None:
            parts.append(f"ack={t.arrive_ms + t.first_ack:.0f}")
        if t.last_reply is not None:
            parts.append(f"done={t.arrive_ms + t.last_reply:.0f}")
        parts.append(f"handler={handler:.0f}")
        parts.append(f"lock={t.lock_ms:.0f}")
        parts.append(f"db={t.db_ms:.0f}(n={t.db_n},pool={t.db_pool_ms:.0f},retry={t.retries})")
        parts.append(f"discord={t.disc_ms:.0f}(n={t.disc_n})")
        steps = " ".join(f"{name}={ms:.0f}" for name, ms in t.steps[:_MAX_STEPS_LOGGED])
        if len(t.steps) > _MAX_STEPS_LOGGED:
            steps += f" +{len(t.steps) - _MAX_STEPS_LOGGED}more"
        felt = (t.arrive_ms + t.last_reply) if t.last_reply is not None else handler
        slow = " slow" if felt >= _SLOW_MS else ""
        logger.info("INTERACTION_TIMING %s | %s%s", " ".join(parts), steps or "-", slow)
    except Exception:
        pass


# ── install: wrap the three per-interaction entry points ──────────────

_installed = False
_originals: dict[str, tuple[object, str, object]] = {}


def _make_wrapper(original, kind: str):
    async def wrapper(self, *args, **kwargs):
        timing = _start(kind, args)
        token = None
        if timing is not None:
            try:
                token = _current.set(timing)
            except Exception:
                token = None
        try:
            return await original(self, *args, **kwargs)
        finally:
            try:
                if token is not None:
                    _current.reset(token)
            except Exception:
                pass
            if timing is not None:
                _finish(timing)
    wrapper.__wrapped__ = original   # type: ignore[attr-defined]
    return wrapper


def install(bot=None) -> None:
    """Wrap View._scheduled_task, Modal._scheduled_task and CommandTree._call.
    Call once at startup (bot.setup_hook), after discord_meter.install."""
    global _installed
    if _installed:
        return
    import switches
    if not getattr(switches, "INTERACTION_TIMING", True):
        logger.info("interaction_timer disabled (INTERACTION_TIMING=false)")
        return
    from discord import app_commands, ui

    targets = (
        ("view", ui.View, "_scheduled_task", "component"),
        ("modal", ui.Modal, "_scheduled_task", "modal"),
        ("tree", app_commands.CommandTree, "_call", "command"),
    )
    for key, owner, attr, kind in targets:
        original = getattr(owner, attr)
        _originals[key] = (owner, attr, original)
        setattr(owner, attr, _make_wrapper(original, kind))
    _installed = True
    logger.info("interaction_timer installed")


def uninstall() -> None:
    """Restore the originals (for tests)."""
    global _installed
    for owner, attr, original in _originals.values():
        setattr(owner, attr, original)
    _originals.clear()
    _installed = False
