#!/usr/bin/env python3
"""Copy Supabase logs into local files before the free plan's ~1-day retention
deletes them.

The Management API logs endpoint saves nothing by itself: it only lets us READ
the last ~day. This script reads it in small time slices and appends every row
to files on THIS machine, so the logs stay as long as you keep the files.

    python tools/pull_supabase_logs.py discover   # one-time check (column names only)
    python tools/pull_supabase_logs.py pull       # copy new logs; run hourly

Settings (env vars, or the git-ignored file tools/.supabase_logs.env):
    SUPABASE_ACCESS_TOKEN   Management API token (account level - treat as a master key)
    PROJECT_REF             project reference (prod: the part before .supabase.co)

Why small slices: reading logs through the Management API counts toward
Supabase's "Log Query" allowance, and what is billed against it is the TIME RANGE
scanned, not the rows returned. Narrow windows keep that small.

Standard library only; no new dependency for the bot. Never run this inside the
bot or put the token in Katabump's env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.supabase.com/v1/projects/{ref}/analytics/endpoints/logs"
USER_AGENT = "champqueue-log-puller/1.0"   # a custom UA avoids generic-bot blocking
DEFAULT_SOURCES = ("edge_logs", "postgres_logs")
ROW_LIMIT = 1000                      # per query; a full page triggers a split
MAX_RETRIES = 3
PAUSE_SECONDS = 2.5                   # keeps well under 30 requests/minute
LAG = timedelta(minutes=2)            # newest logs arrive slightly late; skip them
MAX_LOOKBACK = timedelta(hours=23)    # retention ~1 day; API window must be <= 24 h
MIN_SLICE = timedelta(minutes=1)
HERE = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = HERE / ".supabase_logs.env"
DEFAULT_OUT = HERE.parent / "logs" / "supabase"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PullError(Exception):
    """A problem the operator should read and act on (never contains the token)."""


# ── settings ────────────────────────────────────────────────────────────

def load_settings(env_file: Path | None) -> tuple[str, str]:
    values: dict[str, str] = {}
    if env_file and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    token = os.environ.get("SUPABASE_ACCESS_TOKEN") or values.get("SUPABASE_ACCESS_TOKEN", "")
    ref = os.environ.get("PROJECT_REF") or values.get("PROJECT_REF", "")
    if not token or not ref:
        raise PullError(
            "Missing SUPABASE_ACCESS_TOKEN or PROJECT_REF. Put both in "
            f"{DEFAULT_ENV_FILE} (one KEY=value per line) or set them as environment variables."
        )
    return token, ref


# ── HTTP ────────────────────────────────────────────────────────────────

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_fetch(token: str, ref: str, pause: float = PAUSE_SECONDS, timeout: int = 60):
    """Return fetch(sql, start, end) -> parsed JSON. Retries 429/5xx and network errors."""

    def fetch(sql: str, start: datetime, end: datetime):
        if pause:
            time.sleep(pause)
        query = urllib.parse.urlencode(
            {"sql": sql, "iso_timestamp_start": iso(start), "iso_timestamp_end": iso(end)}
        )
        req = urllib.request.Request(
            API.format(ref=ref) + "?" + query,
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
                     "Accept": "application/json"},
        )
        for attempt in range(MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:500]
                if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    wait = int(exc.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
                    time.sleep(wait)
                    continue
                hint = {
                    401: "The token was rejected - create a new one and update the env file.",
                    403: "The token cannot read this project's logs (or the request was blocked).",
                    404: "Project ref not found - check PROJECT_REF.",
                }.get(exc.code, "")
                raise PullError(f"HTTP {exc.code} from Supabase. {hint} Response: {body}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise PullError(f"Network error talking to Supabase: {exc}") from None
            except json.JSONDecodeError:
                raise PullError("Supabase returned something that is not JSON.") from None
        raise PullError("Gave up after repeated failures.")

    return fetch


# ── response handling ───────────────────────────────────────────────────

def describe_shape(resp) -> str:
    if isinstance(resp, dict):
        return f"dict with keys {sorted(resp.keys())}"
    if isinstance(resp, list):
        return f"list of {len(resp)}"
    return type(resp).__name__


def extract_rows(resp) -> list[dict]:
    """The API's wrapper shape is not documented in detail, so accept the usual ones."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        err = resp.get("error")
        if err:
            raise PullError(f"Supabase reported a query error: {str(err)[:500]}")
        for key in ("result", "results", "data", "rows"):
            val = resp.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for inner in ("rows", "data", "result"):
                    if isinstance(val.get(inner), list):
                        return val[inner]
    raise PullError(f"Unrecognised response shape: {describe_shape(resp)}. "
                    "Run 'discover' and send me this line.")


def build_sql(source: str, source_col: str, limit: int) -> str:
    if not _NAME.match(source) or not _NAME.match(source_col):
        raise PullError("Source and column names may only contain letters, digits and underscores.")
    return (f"select timestamp, id, event_message, log_attributes from logs "
            f"where {source_col} = '{source}' order by timestamp asc limit {limit}")


def row_time(row: dict) -> datetime | None:
    value = row.get("timestamp")
    if isinstance(value, (int, float)):
        if value > 1e14:
            value /= 1e6          # microseconds
        elif value > 1e11:
            value /= 1e3          # milliseconds
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip().replace("Z", "+00:00"))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# ── fetching a window without losing rows ───────────────────────────────

def fetch_window(fetch, source: str, source_col: str, start: datetime, end: datetime,
                 limit: int, stats: dict, log=print) -> list[dict]:
    """A full page may be truncated, so split the window in half and retry."""
    rows = extract_rows(fetch(build_sql(source, source_col, limit), start, end))
    if len(rows) < limit:
        return rows
    if end - start > MIN_SLICE:
        mid = (start + (end - start) / 2).replace(microsecond=0)
        if start < mid < end:
            return (fetch_window(fetch, source, source_col, start, mid, limit, stats, log)
                    + fetch_window(fetch, source, source_col, mid, end, limit, stats, log))
    stats["truncated"] = stats.get("truncated", 0) + 1
    log(f"WARNING {source}: {iso(start)}..{iso(end)} still returned a full page "
        f"({limit} rows) at the smallest slice; some rows may be missing.")
    return rows


# ── local storage ───────────────────────────────────────────────────────

class Store:
    def __init__(self, out_dir: Path):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "state.json"
        self.seen: set[str] = set()

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def path_for(self, source: str, day: str) -> Path:
        return self.out / f"{day}_{source}.jsonl"

    def load_seen(self, source: str, days: list[str]) -> None:
        for day in days:
            path = self.path_for(source, day)
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rid = json.loads(line).get("id")
                except ValueError:
                    continue
                if rid is not None:
                    self.seen.add(f"{source}:{rid}")

    def write(self, source: str, rows: list[dict], fallback_day: str) -> int:
        """Append rows not seen before (window edges can overlap). Returns the new count."""
        written = 0
        handles: dict[str, object] = {}
        try:
            for row in rows:
                rid = row.get("id")
                key = f"{source}:{rid}"
                if rid is not None and key in self.seen:
                    continue
                when = row_time(row)
                day = when.strftime("%Y-%m-%d") if when else fallback_day
                if day not in handles:
                    handles[day] = self.path_for(source, day).open("a", encoding="utf-8")
                out = dict(row)
                out["source"] = source
                handles[day].write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
                if rid is not None:
                    self.seen.add(key)
                written += 1
        finally:
            for handle in handles.values():
                handle.close()
        return written


# ── commands ────────────────────────────────────────────────────────────

def run_pull(fetch, out_dir: Path, sources, source_col: str = "source", slice_minutes: int = 60,
             since_hours: float = 23, now: datetime | None = None, limit: int = ROW_LIMIT,
             log=print) -> dict:
    if not 1 <= slice_minutes <= 1440:
        raise PullError("--slice-minutes must be between 1 and 1440.")
    now = now or datetime.now(timezone.utc)
    end_all = (now - LAG).replace(microsecond=0)
    floor = end_all - MAX_LOOKBACK
    store = Store(out_dir)
    state = store.load_state()
    stats: dict = {"new_rows": 0, "slices": 0, "gaps": 0, "truncated": 0}

    for source in sources:
        last_raw = state.get(source)
        last = datetime.fromisoformat(last_raw.replace("Z", "+00:00")) if last_raw else None
        if last is None:
            start = max(end_all - timedelta(hours=since_hours), floor)
        elif last < floor:
            stats["gaps"] += 1
            msg = (f"GAP {source}: nothing pulled between {iso(last)} and {iso(floor)} - "
                   "those logs are older than Supabase's retention and cannot be recovered.")
            log("WARNING " + msg)
            with (store.out / "gaps.txt").open("a", encoding="utf-8") as fh:
                fh.write(f"{iso(now)} {msg}\n")
            start = floor
        else:
            start = last

        store.load_seen(source, [start.strftime("%Y-%m-%d"),
                                 (start - timedelta(days=1)).strftime("%Y-%m-%d")])
        cursor = start
        while cursor < end_all:
            nxt = min(cursor + timedelta(minutes=slice_minutes), end_all)
            rows = fetch_window(fetch, source, source_col, cursor, nxt, limit, stats, log)
            added = store.write(source, rows, cursor.strftime("%Y-%m-%d"))
            state[source] = iso(nxt)
            store.save_state(state)          # progress survives a crash mid-run
            stats["slices"] += 1
            stats["new_rows"] += added
            log(f"{source} {iso(cursor)}..{iso(nxt)}: {len(rows)} rows, {added} new")
            cursor = nxt
    return stats


def run_discover(fetch, sources, source_col: str = "source", minutes: int = 5,
                 now: datetime | None = None, log=print) -> None:
    """Print only column names, counts and formats - never log contents."""
    now = now or datetime.now(timezone.utc)
    end = (now - LAG).replace(microsecond=0)
    start = end - timedelta(minutes=minutes)
    for source in sources:
        log(f"== {source} (last {minutes} min) ==")
        probe = fetch(build_sql(source, source_col, 1), start, end)
        log(f"response shape: {describe_shape(probe)}")
        rows = extract_rows(probe)
        if rows:
            first = rows[0]
            log(f"columns returned: {sorted(first.keys())}")
            ts = first.get("timestamp")
            log(f"timestamp example: {ts!r} (type {type(ts).__name__})")
            log(f"id type: {type(first.get('id')).__name__}; "
                f"log_attributes type: {type(first.get('log_attributes')).__name__}")
        else:
            log("no rows in this window (try --minutes 15, or use the app first)")
        keys_sql = ("select arrayJoin(mapKeys(log_attributes)) as key, count() as events "
                    f"from logs where {source_col} = '{source}' group by key "
                    "order by events desc limit 100")
        for row in extract_rows(fetch(keys_sql, start, end)):
            log(f"  {row.get('key')}\t{row.get('events')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Copy Supabase logs to local files.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "pull"):
        p = sub.add_parser(name)
        p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
        p.add_argument("--sources", default=",".join(DEFAULT_SOURCES))
        p.add_argument("--source-col", default="source",
                       help="column holding the log source (try source_name if 'source' errors)")
        p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    sub.choices["discover"].add_argument("--minutes", type=int, default=5)
    sub.choices["pull"].add_argument("--slice-minutes", type=int, default=60)
    sub.choices["pull"].add_argument("--since-hours", type=float, default=23)
    args = parser.parse_args(argv)

    try:
        token, ref = load_settings(args.env_file)
        fetch = make_fetch(token, ref)
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
        if args.cmd == "discover":
            run_discover(fetch, sources, args.source_col, args.minutes)
        else:
            stats = run_pull(fetch, args.out, sources, args.source_col,
                             args.slice_minutes, args.since_hours)
            print(f"Done: {stats['new_rows']} new rows in {stats['slices']} slices"
                  f" ({stats['gaps']} gaps, {stats['truncated']} possibly truncated).")
        return 0
    except PullError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
