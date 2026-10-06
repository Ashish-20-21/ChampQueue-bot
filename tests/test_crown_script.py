"""Offline checks for tools/test_crown_read.py's judging logic (no API calls)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("crown_tool", Path(__file__).resolve().parent.parent / "tools" / "test_crown_read.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def _team(t, crown_pos, impacts):
    return [{"team": t, "position": i + 1, "ign": f"{t}{i+1}", "impact": imp, "has_crown": (i + 1) == crown_pos}
            for i, imp in enumerate(impacts)]


def test_pass_and_tie_pass():
    ext = {"players": _team("A", 2, [200, 200, 111, 112, 95])}
    ok, problems, crowns = tool.evaluate_run(ext, {"A": 2, "B": None})
    assert ok and crowns["A"] == 2 and problems == []


def test_wrong_row_fails():
    ext = {"players": _team("A", 1, [152, 168, 117, 123, 113]) + _team("B", 1, [196, 194, 192, 153, 113])}
    ok, problems, _ = tool.evaluate_run(ext, {"A": 2, "B": 1})
    assert not ok and any("expected row 2" in p for p in problems)


def test_no_or_missing_crown_fails():
    ext = {"players": _team("A", 0, [1, 2, 3, 4, 5])}
    assert not tool.evaluate_run(ext, {"A": 2})[0]
    legacy = {"players": [{"team": "A", "position": 1, "ign": "x", "impact": 1, "is_mvp": True}]}
    assert not tool.evaluate_run(legacy, {"A": 1})[0]


def test_crown_below_teammate_impact_fails():
    ext = {"players": _team("A", 5, [152, 168, 117, 123, 113])}
    ok, problems, _ = tool.evaluate_run(ext, {"A": 5})
    assert not ok and any("below teammate" in p for p in problems)


def test_unreadable_teammates_do_not_switch_off_the_impact_check():
    ext = {"players": [
        {"team": "B", "position": 1, "ign": "b1", "impact": 63, "has_crown": True},
        {"team": "B", "position": 2, "ign": "b2", "impact": 73, "has_crown": False},
        {"team": "B", "position": 3, "ign": "b3", "impact": None, "has_crown": False}]}
    ok, problems, _ = tool.evaluate_run(ext, {"B": 1})
    assert not ok and any("below teammate" in p for p in problems)


def test_hidden_crown_passes_only_when_no_crown_is_read():
    none_read = {"players": _team("A", 0, [152, 168, 117, 123, 113])}
    ok, problems, crowns = tool.evaluate_run(none_read, {"A": 0})
    assert ok and crowns["A"] is None
    guessed = {"players": _team("A", 2, [152, 168, 117, 123, 113])}
    ok, problems, _ = tool.evaluate_run(guessed, {"A": 0})
    assert not ok and any("must not guess" in p for p in problems)
