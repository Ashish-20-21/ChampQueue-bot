"""Per-minute Discord API call meter.

Counts every outgoing Discord request by method + path pattern + outcome,
in memory, with zero Discord or DB calls of its own. One log line per
minute (only when something happened). Install once at bot startup with
`install(bot)`.

The meter monkey-patches two chokepoints in discord.py 2.7.1:
  1. HTTPClient.request — every channel.send, message.edit, channel create/
     delete, permission update, etc.
  2. AsyncWebhookAdapter.request — every interaction reply (defer, edit,
     followup, send_message).
Both are restored cleanly if uninstall() is called.

Interaction callbacks are split by type (defer / edit_message / send_message ...)
so one-call replies can be told apart from defer + follow-up. A 10-second timer
prints each finished minute on time (without it a line only appeared when the
NEXT Discord call happened, sometimes many minutes late). Minutes are UTC.

Bursts and rate limits (added 2026-09-22): every minute line also carries the busiest
single second and busiest 5 seconds per category (peak1s_reply / peak5s_reply for
interaction replies, peak1s_chan / peak5s_chan for normal channel calls), because a
per-minute total hides the short bursts Discord limits actually act on. Every 429
also gets one DISCORD_429 warning line (max 5 per minute) with the response headers
(X-RateLimit-*, Via, Retry-After) and how many calls we had just made in the last
1 s / 5 s, so the real rule can be read off instead of guessed. Only the route
TEMPLATE is logged, never a URL, so interaction tokens can't leak into the log.

Safety: the counter dict and the log line can never raise into the caller.
The original function is ALWAYS called, even if counting fails.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

logger = logging.getLogger("champions_queue")

# ── In-memory counters ──────────────────────────────────────────────
# Key: (minute_str, method, path_pattern, outcome)  ->  count
_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
_last_flush_minute: str = ""
_lock = asyncio.Lock()

# ── Originals (saved on install, restored on uninstall) ─────────────
_orig_http_request = None
_orig_webhook_request = None
_installed = False

# Per-second hit counters, by category: "reply" = interaction replies (webhook
# adapter), "chan" = normal channel/guild calls. Key: (category, epoch_second).
_sec_hits: dict[tuple[str, int], int] = defaultdict(int)
# DISCORD_429 detail lines already written, per minute (cap so a storm can't flood the log)
_429_lines: dict[str, int] = defaultdict(int)
_MAX_429_LINES_PER_MINUTE = 5
_FORENSIC_HEADERS = (
    "Retry-After", "X-RateLimit-Scope", "X-RateLimit-Limit", "X-RateLimit-Remaining",
    "X-RateLimit-Reset-After", "X-RateLimit-Bucket", "X-RateLimit-Global", "Via",
)


def _now() -> float:
    return time.time()


def _minute_of(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


def _minute_key() -> str:
    return _minute_of(_now())


def _simplify_path(path: str) -> str:
    """Turn /channels/123456/messages into /channels/x/messages etc."""
    parts = path.strip("/").split("/")
    out = []
    for p in parts:
        if p.isdigit() or (len(p) > 15 and p.replace("-", "").isalnum()):
            out.append("x")
        else:
            out.append(p)
    return "/" + "/".join(out)


_CALLBACK_KINDS = {
    4: "send_message", 5: "defer_reply", 6: "defer_update",
    7: "edit_message", 8: "autocomplete", 9: "modal",
}


def _callback_kind(path: str, kwargs: dict) -> str:
    """For POST /interactions/{id}/{token}/callback, name the response type
    (defer / edit_message / send_message ...). Empty for every other route.
    Must never raise."""
    try:
        if not str(path).endswith("/callback"):
            return ""
        payload = kwargs.get("payload")
        if not isinstance(payload, dict):
            payload = None
            for part in kwargs.get("multipart") or []:
                if isinstance(part, dict) and part.get("name") == "payload_json":
                    payload = json.loads(part.get("value") or "{}")
                    break
        return _CALLBACK_KINDS.get((payload or {}).get("type"), "")
    except Exception:
        return ""


def _record(method: str, path: str, outcome: str, kind: str = "", cat: str = "",
            ts: float | None = None) -> None:
    """Add one tick (ts = when the request STARTED). Must never raise."""
    try:
        now = _now() if ts is None else ts
        minute = _minute_of(now)
        simple = _simplify_path(path) + (f" [{kind}]" if kind else "")
        _counts[(minute, method, simple, outcome)] += 1
        if cat:
            _sec_hits[(cat, int(now))] += 1
    except Exception:
        pass


def _recent(cat: str, seconds: int, now: float) -> int:
    """Calls of this category started in the last `seconds` seconds (incl. the current one)."""
    return sum(_sec_hits.get((cat, int(now) - k), 0) for k in range(seconds))


def _take_peaks(minute: str) -> dict[str, tuple[int, int]]:
    """{category: (busiest 1 s, busiest 5 s)} for one finished minute; drops those entries.
    (A 5 s window is cut at the minute edge — a small undercount, fine for spotting bursts.)"""
    by_cat: dict[str, dict[int, int]] = {}
    for (cat, sec), n in list(_sec_hits.items()):
        if _minute_of(sec) == minute:
            by_cat.setdefault(cat, {})[sec] = n
            del _sec_hits[(cat, sec)]
    out = {}
    for cat, secs in by_cat.items():
        p1 = max(secs.values())
        p5 = max(sum(secs.get(sec + k, 0) for k in range(5)) for sec in secs)
        out[cat] = (p1, p5)
    return out


def _log_429(method: str, path: str, kind: str, cat: str, exc: Exception, ts: float) -> None:
    """One warning with everything Discord told us about a 429. Never raises."""
    try:
        minute = _minute_of(ts)
        if _429_lines[minute] >= _MAX_429_LINES_PER_MINUTE:
            return
        _429_lines[minute] += 1
        for old_minute in [m for m in _429_lines if m < minute]:
            del _429_lines[old_minute]
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        seen = {}
        for h in _FORENSIC_HEADERS:
            try:
                v = headers.get(h)
            except Exception:
                v = None
            if v is not None:
                seen[h] = str(v)
        logger.warning(
            "DISCORD_429 route=%s %s%s cat=%s code=%s text=%r headers=%s "
            "recent_1s=%d recent_5s=%d all_1s=%d",
            method, _simplify_path(path), f" [{kind}]" if kind else "", cat,
            getattr(exc, "code", None), str(getattr(exc, "text", "") or "")[:120],
            seen or "none", _recent(cat, 1, ts), _recent(cat, 5, ts),
            sum(_recent(c, 1, ts) for c in ("reply", "chan")),
        )
    except Exception:
        pass


def _after_call(method: str, path: str, kind: str, cat: str, t0: float,
                exc: Exception | None) -> None:
    """All metering for one finished call, fully guarded: the meter must never
    change the outcome of the Discord call it is watching."""
    try:
        if exc is None:
            outcome = "ok"
        else:
            code = getattr(getattr(exc, "response", None), "status", 0)
            outcome = str(code) if code else "err"
        _record(method, path, outcome, kind, cat, t0)
        if outcome == "429":
            _log_429(method, path, kind, cat, exc, t0)
    except Exception:
        pass


async def _safe_flush() -> None:
    try:
        await _flush_if_new_minute()
    except Exception:
        pass


async def _flush_if_new_minute() -> None:
    """Log one summary line per FINISHED minute (any minute before the current one)."""
    global _last_flush_minute
    minute = _minute_key()
    if minute == _last_flush_minute:
        return
    async with _lock:
        if minute == _last_flush_minute:
            return
        _last_flush_minute = minute
        for prev in sorted({k[0] for k in _counts if k[0] < minute}):
            lines = []
            total_ok = total_429 = total_err = 0
            for k in sorted(k for k in _counts if k[0] == prev):
                _, method, path, outcome = k
                count = _counts.pop(k)
                lines.append(f"{method} {path} {outcome}={count}")
                if outcome == "ok":
                    total_ok += count
                elif outcome == "429":
                    total_429 += count
                else:
                    total_err += count
            peaks = _take_peaks(prev)
            peak_txt = "".join(
                f" peak1s_{c}={p1} peak5s_{c}={p5}" for c, (p1, p5) in sorted(peaks.items()))
            logger.info(
                "DISCORD_METER minute=%s ok=%d 429=%d err=%d%s | %s",
                prev, total_ok, total_429, total_err, peak_txt, " | ".join(lines),
            )
        for key in [k for k in _sec_hits if _minute_of(k[1]) < minute]:   # safety net
            del _sec_hits[key]


_ticker_task: "asyncio.Task | None" = None


async def _ticker(interval: float) -> None:
    """Background timer: makes sure a finished minute is printed on time."""
    while True:
        try:
            await asyncio.sleep(interval)
            await _flush_if_new_minute()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # a metering hiccup must never kill the timer or the bot


def _start_ticker(interval: float = 10.0) -> None:
    global _ticker_task
    if _ticker_task is None or _ticker_task.done():
        _ticker_task = asyncio.get_running_loop().create_task(_ticker(interval))


def _stop_ticker() -> None:
    global _ticker_task
    if _ticker_task is not None:
        _ticker_task.cancel()
        _ticker_task = None


# ── Monkey-patch wrappers ───────────────────────────────────────────

def _wrap_http_request(original):
    """Wrap HTTPClient.request (channel sends, edits, creates, deletes)."""
    async def wrapper(self, route, **kwargs):
        method, path, t0 = route.method, route.path, _now()
        try:
            result = await original(self, route, **kwargs)
        except Exception as exc:
            _after_call(method, path, "", "chan", t0, exc)
            await _safe_flush()
            raise
        _after_call(method, path, "", "chan", t0, None)
        await _safe_flush()
        return result
    return wrapper


def _wrap_webhook_request(original):
    """Wrap AsyncWebhookAdapter.request (interaction replies)."""
    async def wrapper(self, route, session, **kwargs):
        method, path, t0 = route.method, route.path, _now()
        kind = _callback_kind(path, kwargs)
        try:
            result = await original(self, route, session, **kwargs)
        except Exception as exc:
            _after_call(method, path, kind, "reply", t0, exc)
            await _safe_flush()
            raise
        _after_call(method, path, kind, "reply", t0, None)
        await _safe_flush()
        return result
    return wrapper


# ── Public API ──────────────────────────────────────────────────────

def install(bot=None) -> None:
    """Patch discord.py's two request chokepoints. Call once at startup."""
    global _orig_http_request, _orig_webhook_request, _installed
    if _installed:
        return
    from discord import http as _http
    from discord.webhook import async_ as _webhook

    _orig_http_request = _http.HTTPClient.request
    _orig_webhook_request = _webhook.AsyncWebhookAdapter.request

    _http.HTTPClient.request = _wrap_http_request(_orig_http_request)
    _webhook.AsyncWebhookAdapter.request = _wrap_webhook_request(_orig_webhook_request)
    _installed = True
    try:
        _start_ticker()
    except RuntimeError:
        pass  # no running loop (e.g. imported outside the bot): lines then flush on the next call
    logger.info("discord_meter installed")


def uninstall() -> None:
    """Restore the originals (for tests or clean shutdown)."""
    global _installed
    if not _installed:
        return
    from discord import http as _http
    from discord.webhook import async_ as _webhook
    _http.HTTPClient.request = _orig_http_request
    _webhook.AsyncWebhookAdapter.request = _orig_webhook_request
    _stop_ticker()
    _installed = False


def snapshot() -> dict[tuple, int]:
    """Return a copy of current counts (for tests)."""
    return dict(_counts)


def reset() -> None:
    """Clear all counters (for tests)."""
    global _last_flush_minute
    _counts.clear()
    _sec_hits.clear()
    _429_lines.clear()
    _last_flush_minute = ""
