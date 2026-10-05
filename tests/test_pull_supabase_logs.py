"""tools/pull_supabase_logs.py - fake Supabase only, no network, no token."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "pull_supabase_logs", Path(__file__).resolve().parent.parent / "tools" / "pull_supabase_logs.py")
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


def mkrow(i, when):
    return {"timestamp": when.strftime("%Y-%m-%d %H:%M:%S.%f"), "id": f"id{i}",
            "event_message": "GET /rest/v1/players", "log_attributes": {"request.path": "/rest/v1/players"}}


class FakeApi:
    """Holds timestamped rows; returns those inside the requested window, honouring `limit`."""
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, sql, start, end):
        self.calls.append((sql, start, end))
        limit = int(sql.rsplit("limit", 1)[1])
        hit = [r for r in self.rows if start <= pl.row_time(r) <= end]   # inclusive edges: forces dedupe
        return {"result": hit[:limit]}


def rows_every_minute(n, start):
    return [mkrow(i, start + timedelta(minutes=i)) for i in range(n)]


def test_pull_saves_rows_and_dedupes_slice_edges(tmp_path):
    base = NOW - timedelta(hours=3)
    api = FakeApi(rows_every_minute(120, base))
    stats = pl.run_pull(api, tmp_path, ["edge_logs"], now=NOW, since_hours=3, log=lambda *a: None)
    lines = [json.loads(l) for f in tmp_path.glob("*_edge_logs.jsonl") for l in f.read_text().splitlines()]
    ids = [r["id"] for r in lines]
    assert len(ids) == len(set(ids)) == 120                  # nothing lost, nothing doubled
    assert all(r["source"] == "edge_logs" for r in lines)
    assert stats["slices"] == 3 and stats["truncated"] == 0


def test_second_run_resumes_and_adds_only_new_rows(tmp_path):
    base = NOW - timedelta(hours=2)
    api = FakeApi(rows_every_minute(100, base))
    pl.run_pull(api, tmp_path, ["edge_logs"], now=NOW, since_hours=2, log=lambda *a: None)
    later = NOW + timedelta(hours=1)
    api.rows += [mkrow(1000 + i, NOW + timedelta(minutes=i)) for i in range(1, 30)]
    api.calls.clear()
    stats = pl.run_pull(api, tmp_path, ["edge_logs"], now=later, log=lambda *a: None)
    assert stats["new_rows"] == 29
    assert min(c[1] for c in api.calls) >= NOW - pl.LAG - timedelta(seconds=1)   # did not re-read the old hours


def test_full_page_is_split_so_no_rows_are_dropped(tmp_path):
    base = NOW - timedelta(minutes=60)
    api = FakeApi(rows_every_minute(55, base))
    stats = pl.run_pull(api, tmp_path, ["edge_logs"], now=NOW, since_hours=1, limit=10, log=lambda *a: None)
    got = {json.loads(l)["id"] for f in tmp_path.glob("*.jsonl") for l in f.read_text().splitlines()}
    assert len(got) == 55 and stats["truncated"] == 0
    assert len(api.calls) > 3                                  # it had to split the window


def test_gap_beyond_retention_is_reported_and_recorded(tmp_path):
    store = pl.Store(tmp_path)
    store.save_state({"edge_logs": pl.iso(NOW - timedelta(days=3))})
    logged = []
    stats = pl.run_pull(FakeApi([]), tmp_path, ["edge_logs"], now=NOW, log=logged.append)
    assert stats["gaps"] == 1 and any("GAP" in m for m in logged)
    assert (tmp_path / "gaps.txt").exists()


def test_query_error_in_body_raises(tmp_path):
    with pytest.raises(pl.PullError, match="query error"):
        pl.run_pull(lambda s, a, b: {"error": "bad column"}, tmp_path, ["edge_logs"], now=NOW,
                    log=lambda *a: None)


def test_unknown_response_shape_names_the_keys_only():
    with pytest.raises(pl.PullError) as exc:
        pl.extract_rows({"weird": 1})
    assert "weird" in str(exc.value)


@pytest.mark.parametrize("resp", [[{"id": 1}], {"result": [{"id": 1}]}, {"data": [{"id": 1}]},
                                  {"result": {"rows": [{"id": 1}]}}])
def test_accepts_common_response_shapes(resp):
    assert pl.extract_rows(resp) == [{"id": 1}]


@pytest.mark.parametrize("value", ["2026-10-04T10:00:00Z", "2026-10-04 10:00:00.123456789",
                                   1791108000000000, 1791108000000, 1791108000])
def test_row_time_understands_common_timestamp_formats(value):
    assert pl.row_time({"timestamp": value}).year == 2026


def test_bad_source_name_is_rejected():
    with pytest.raises(pl.PullError):
        pl.build_sql("edge_logs'; drop table x;--", "source", 10)


def test_settings_prefer_env_and_file_and_never_echo_token(tmp_path, monkeypatch):
    env = tmp_path / "e.env"
    env.write_text("SUPABASE_ACCESS_TOKEN=sbp_secret123\nPROJECT_REF=abcdef\n")
    monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PROJECT_REF", raising=False)
    assert pl.load_settings(env) == ("sbp_secret123", "abcdef")
    with pytest.raises(pl.PullError) as exc:
        pl.load_settings(tmp_path / "missing.env")
    assert "sbp_" not in str(exc.value)


def test_discover_prints_names_not_contents(tmp_path):
    secret_msg = "player 1483803832436654100 did something"
    def fetch(sql, a, b):
        if "arrayJoin" in sql:
            return {"result": [{"key": "request.path", "events": 7}]}
        return {"result": [{"timestamp": "2026-10-04 10:00:00.000000", "id": "x",
                            "event_message": secret_msg, "log_attributes": {}}]}
    out = []
    pl.run_discover(fetch, ["edge_logs"], now=NOW, log=out.append)
    text = "\n".join(out)
    assert "request.path" in text and "columns returned" in text
    assert "1483803832436654100" not in text
