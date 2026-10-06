"""Single-script crown-read test (no Discord, no database).

Runs the SAME extraction the bot uses (services.vision_extraction, the provider
and model from your .env) on real scoreboard screenshots and checks that the
Impact crown is read correctly, so we can confirm it before shipping.

Setup
  1. Put 6-8 screenshots in  crown_samples/   (png/jpg/webp). Include the blurry
     ones, a tie on Impact, a crown on row 4/5, and one where the MVP tag and
     the crown are on DIFFERENT players.
  2. python tools/test_crown_read.py --template      (writes crown_samples/expected.json)
  3. Fill expected.json with the row (1-5) that really has the crown, per team.
     Team A = top/left group on the screen, B = the other. Use null for a team
     that is not on the screenshot (e.g. a winners-only view).
     Use 0 when the crown is HIDDEN (loading bar, notification, painted
     over): the test then PASSES only if the model reads NO crown for that
     team (so the bot sends it to the admin crown picker) and FAILS if the
     model invents one.
         {"summit.png": {"A": 2, "B": 1}, "shipment.png": {"A": 2, "B": null}}
  4. python tools/test_crown_read.py                 (3 runs per image by default)

PASS for an image = every run read exactly one crown per team, on the expected
row, with the crown holder's Impact never below a teammate's.
Costs one vision API call per image per run.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# config.py refuses to import without these; this script never uses them.
for _k, _v in {"DISCORD_BOT_TOKEN": "x", "GUILD_ID": "1", "ADMIN_ROLE_IDS": "1",
               "SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_KEY": "x"}.items():
    os.environ.setdefault(_k, _v)

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate_run(extraction: dict, expected: dict) -> tuple[bool, list[str], dict]:
    """Check one extraction. Returns (ok, problems, {team: crown_position_or_None})."""
    problems: list[str] = []
    crowns: dict[str, int | None] = {}
    players = extraction.get("players") or []
    for team in ("A", "B"):
        want = expected.get(team)
        rows = sorted((p for p in players if p.get("team") == team),
                      key=lambda p: p.get("position") if isinstance(p.get("position"), int) else 9)
        if want is None:
            crowns[team] = None
            continue
        if not rows:
            problems.append(f"Team {team}: no rows read")
            crowns[team] = None
            continue
        if want == 0:
            invented = [p for p in rows if p.get("has_crown") is True]
            crowns[team] = invented[0].get("position") if invented else None
            if invented:
                problems.append(f"Team {team}: crown is hidden in this image but the model read one on row "
                                f"{invented[0].get('position')} ({invented[0].get('ign')}) — it must not guess")
            continue
        bad = [p.get("ign") for p in rows if not isinstance(p.get("has_crown"), bool)]
        if bad:
            problems.append(f"Team {team}: has_crown missing/not true-false for {bad}")
        crowned = [p for p in rows if p.get("has_crown") is True]
        if len(crowned) != 1:
            problems.append(f"Team {team}: {len(crowned)} crowns read (need exactly 1)")
            crowns[team] = None
            continue
        c = crowned[0]
        crowns[team] = c.get("position")
        if c.get("position") != want:
            problems.append(f"Team {team}: crown read on row {c.get('position')} ({c.get('ign')}), expected row {want}")
        imps = [_num(p.get("impact")) for p in rows]
        readable = [v for v in imps if v is not None]
        cv = _num(c.get("impact"))
        if cv is not None and readable and cv < max(readable):
            problems.append(f"Team {team}: crown holder Impact {cv:g} is below teammate's {max(readable):g}")
    return (not problems), problems, crowns


def _show(extraction: dict, expected: dict) -> None:
    players = extraction.get("players") or []
    for team in ("A", "B"):
        rows = sorted((p for p in players if p.get("team") == team),
                      key=lambda p: p.get("position") if isinstance(p.get("position"), int) else 9)
        if not rows:
            continue
        print(f"    Team {team}  (expected crown row: {expected.get(team)})")
        imps = [_num(p.get("impact")) for p in rows]
        top = max((v for v in imps if v is not None), default=None)
        for p, imp in zip(rows, imps):
            tie = "  [tied top Impact: decided by icon only]" if (
                p.get("has_crown") is True and top is not None and imps.count(top) > 1 and imp == top) else ""
            mark = "👑" if p.get("has_crown") is True else "  "
            print(f"      {p.get('position')}  {str(p.get('ign'))[:18]:<18} "
                  f"{p.get('kills')}/{p.get('deaths')}/{p.get('assists')}  impact={p.get('impact')}  {mark}{tie}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="crown_samples")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", nargs="+", metavar="TEXT",
                    help="only test images whose filename contains any of these texts, e.g. --only e5a26873 d3231de0")
    ap.add_argument("--template", action="store_true", help="write a blank expected.json and exit")
    args = ap.parse_args()

    folder = ROOT / args.dir
    images = sorted(p for p in folder.glob("*") if p.suffix.lower() in IMG_EXT) if folder.exists() else []
    exp_path = folder / "expected.json"

    if args.template:
        folder.mkdir(exist_ok=True)
        tmpl = {p.name: {"A": None, "B": None} for p in images} or {"example.png": {"A": 2, "B": 1}}
        exp_path.write_text(json.dumps(tmpl, indent=2), encoding="utf-8")
        print(f"wrote {exp_path} — fill in the crown row (1-5) per team, null = team not on screenshot")
        return 0

    if not images or not exp_path.exists():
        print(f"Need images + expected.json in {folder} (run with --template first)")
        return 2
    expected_all = json.loads(exp_path.read_text(encoding="utf-8"))
    if args.only:
        images = [i for i in images if any(t.lower() in i.name.lower() for t in args.only)]
        if not images:
            print(f"No image in {folder} matches {args.only}")
            return 2

    import config
    from services import vision_extraction
    model = config.OPENAI_VISION_MODEL if config.VISION_PROVIDER == "openai" else "(provider default)"
    print(f"provider={config.VISION_PROVIDER}  model={model}  runs/image={args.runs}\n")

    passed = 0
    for img in images:
        exp = expected_all.get(img.name)
        if exp is None or all(v is None for v in exp.values()):
            print(f"SKIP {img.name}: no expected crown in expected.json\n")
            continue
        mt = mimetypes.guess_type(img.name)[0] or "image/png"
        data = img.read_bytes()
        print(f"=== {img.name}")
        image_ok, seen = True, []
        for n in range(1, args.runs + 1):
            try:
                ext = vision_extraction.extract_scoreboard(data, mt)
            except Exception as e:  # noqa: BLE001 - report any provider/parse failure as a failed run
                print(f"  run {n}: ERROR {type(e).__name__}: {e}")
                image_ok = False
                continue
            ok, problems, crowns = evaluate_run(ext, exp)
            seen.append(crowns)
            print(f"  run {n}: {'OK  ' if ok else 'FAIL'} crowns read: A={crowns.get('A')} B={crowns.get('B')}")
            for pr in problems:
                print(f"           - {pr}")
            if n == 1 or not ok:
                _show(ext, exp)
            image_ok &= ok
        if len({json.dumps(c, sort_keys=True) for c in seen}) > 1:
            print("  runs disagree with each other (unstable read)")
            image_ok = False
        print(f"  -> {'PASS' if image_ok else 'FAIL'}\n")
        passed += image_ok

    total = sum(1 for i in images if (expected_all.get(i.name) and any(v is not None for v in expected_all[i.name].values())))
    print(f"RESULT: {passed}/{total} images passed")
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
