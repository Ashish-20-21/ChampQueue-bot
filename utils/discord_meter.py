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

Safety: the counter dict and the log line can never raise into the caller.
The original function is ALWAYS called, even if counting fails.
"""
from __future__ import annotations

import asyncio
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


def _minute_key() -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime())


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


def _record(method: str, path: str, outcome: str) -> None:
    """Add one tick. Must never raise."""
    try:
        minute = _minute_key()
        simple = _simplify_path(path)
        _counts[(minute, method, simple, outcome)] += 1
    except Exception:
        pass


async def _flush_if_new_minute() -> None:
    """Log one summary line when the minute rolls over."""
    global _last_flush_minute
    minute = _minute_key()
    if minute == _last_flush_minute:
        return
    async with _lock:
        if minute == _last_flush_minute:
            return
        prev = _last_flush_minute
        _last_flush_minute = minute
        if not prev:
            return  # first call, nothing to flush
        # Collect everything from the previous minute
        lines = []
        total_ok = total_429 = total_err = 0
        keys_to_pop = [k for k in _counts if k[0] == prev]
        for k in sorted(keys_to_pop):
            _, method, path, outcome = k
            count = _counts.pop(k)
            lines.append(f"{method} {path} {outcome}={count}")
            if outcome == "ok":
                total_ok += count
            elif outcome == "429":
                total_429 += count
            else:
                total_err += count
        if not lines:
            return
        detail = " | ".join(lines)
        logger.info(
            "DISCORD_METER minute=%s ok=%d 429=%d err=%d | %s",
            prev, total_ok, total_429, total_err, detail,
        )


# ── Monkey-patch wrappers ───────────────────────────────────────────

def _wrap_http_request(original):
    """Wrap HTTPClient.request (channel sends, edits, creates, deletes)."""
    async def wrapper(self, route, **kwargs):
        method = route.method
        path = route.path
        try:
            result = await original(self, route, **kwargs)
            _record(method, path, "ok")
            await _flush_if_new_minute()
            return result
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status", 0)
            _record(method, path, str(code) if code else "err")
            await _flush_if_new_minute()
            raise
    return wrapper


def _wrap_webhook_request(original):
    """Wrap AsyncWebhookAdapter.request (interaction replies)."""
    async def wrapper(self, route, session, **kwargs):
        method = route.method
        path = route.path
        try:
            result = await original(self, route, session, **kwargs)
            _record(method, path, "ok")
            await _flush_if_new_minute()
            return result
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status", 0)
            _record(method, path, str(code) if code else "err")
            await _flush_if_new_minute()
            raise
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
    _installed = False


def snapshot() -> dict[tuple, int]:
    """Return a copy of current counts (for tests)."""
    return dict(_counts)


def reset() -> None:
    """Clear all counters (for tests)."""
    global _last_flush_minute
    _counts.clear()
    _last_flush_minute = ""
