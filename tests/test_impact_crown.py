"""Impact-crown +5 bonus (2026-10).

The +5 goes to the player holding the CROWN on the Impact column (any row
1-5 of each team), not the yellow MVP tag (always row 1). Real numbers below
come from two real scoreboards:

  * Summit 243-250 (CQ-1459 sample): crown on Team A row 2 (Cyfur., 168) and
    Team B row 1 (TempestAngryyy, 196). MVP tags were on both row 1s.
  * Shipment 150-49: winning team's row 1 and row 2 BOTH show Impact 200 and
    only row 2 has the crown -> proves the crown, not the number, decides.
"""
import pytest

from cogs import match
from services import localization
from utils import embeds

localization.load_map_translations()  # the bot does this at startup (cogs/match.py)

SUMMIT = [
    # team, pos, ign, k, d, a, score, impact, crown
    ("A", 1, "CVX_Pride_", 50, 49, 17, 6566, 152, False),
    ("A", 2, "Cyfur.", 48, 41, 13, 6252, 168, True),
    ("A", 3, "intensity¿", 47, 47, 10, 5650, 117, False),
    ("A", 4, "War", 27, 54, 15, 4504, 123, False),
    # NOTE: the real name is "vulture002as" (12 alnum chars, letters+digits) which the
    # existing streamer-mask heuristic flags for manual confirmation; underscore added so
    # these tests exercise the crown logic, not name matching.
    ("A", 5, "vulture_002as", 31, 45, 15, 4391, 113, False),
    ("B", 1, "TempestAngryyy", 67, 40, 16, 8002, 196, True),
    ("B", 2, "ATH · Lowëe", 50, 41, 14, 6744, 194, False),
    ("B", 3, "Sir.Death.", 47, 45, 18, 6591, 192, False),
    ("B", 4, "ErenCodm", 44, 37, 15, 5434, 153, False),
    ("B", 5, "ITS_RAZOR1", 26, 41, 17, 3835, 113, False),
]


def _extraction(rows, score="243-250", map_name="Summit"):
    return {
        "map": map_name, "final_score": score,
        "players": [
            {"ign": ign, "team": t, "position": pos, "has_crown": crown, "kills": k, "deaths": d,
             "assists": a, "damage": None, "hill_time": 30.0, "score": sc, "impact": imp}
            for (t, pos, ign, k, d, a, sc, imp, crown) in rows
        ],
    }


def _roster(rows):
    return [{"player_id": i + 1, "team": r[0], "players": {"ign": r[2], "discord_id": 1000 + i}}
            for i, r in enumerate(rows)]


def _run(rows, **kw):
    return match.Match._prepare_round(_roster(rows), "Summit", _extraction(rows, **kw))


def _deltas(round_dict, team):
    return [r["mmr_delta"] for r in sorted((r for r in round_dict["results"] if r["team"] == team),
                                            key=lambda r: r["position"])]


def test_crown_player_gets_bonus_not_row_one():
    rd, reasons, fails, non_ign = _run(SUMMIT)
    assert reasons == [] and rd["clean"] and not fails
    # A lost 243-250: row 2 holds the crown -> -4 + 5 = +1, row 1 stays plain -3
    assert _deltas(rd, "A") == [-3, 1, -6, -8, -9]
    # B won: row 1 holds the crown -> +9 + 5 = +14
    assert _deltas(rd, "B") == [14, 8, 6, 4, 3]


def test_crown_on_a_lower_row_of_the_winning_team():
    rows = [r if r[0] == "A" else (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[1] == 3)
            for r in SUMMIT]
    # make B row 3 the top-impact player so the cross-check agrees with the crown
    rows = [(t, p, i, k, d, a, s, 250 if (t == "B" and p == 3) else imp, c)
            for (t, p, i, k, d, a, s, imp, c) in rows]
    rd, reasons, *_ = _run(rows)
    assert reasons == []
    assert _deltas(rd, "B") == [9, 8, 11, 4, 3]


def test_tie_on_impact_follows_the_crown_not_the_number():
    # Shipment-style tie: rows 1 and 2 both Impact 200, crown on row 2.
    rows = [(t, p, i, k, d, a, s, 200 if (t == "A" and p in (1, 2)) else imp, c)
            for (t, p, i, k, d, a, s, imp, c) in SUMMIT]
    rd, reasons, *_ = _run(rows)
    assert reasons == []
    assert _deltas(rd, "A")[:2] == [-3, 1]


def test_legacy_is_mvp_only_extraction_is_refused_not_guessed():
    ext = _extraction(SUMMIT)
    for p in ext["players"]:
        p["is_mvp"] = p.pop("has_crown")
    rd, reasons, *_ = match.Match._prepare_round(_roster(SUMMIT), "Summit", ext)
    assert not rd["clean"]
    assert any("Impact crown flag is missing" in r for r in reasons)


@pytest.mark.parametrize("bad_team_flags", [(), (1, 2)])
def test_zero_or_two_crowns_on_a_team_goes_to_review(bad_team_flags):
    rows = [(t, p, i, k, d, a, s, imp, (p in bad_team_flags) if t == "A" else c)
            for (t, p, i, k, d, a, s, imp, c) in SUMMIT]
    rd, reasons, *_ = _run(rows)
    assert not rd["clean"]
    assert match._crown_count_reason("A") in reasons


def test_crown_on_a_player_with_lower_impact_than_a_teammate_is_flagged():
    # crown moved onto row 5 (Impact 113) while row 2 has 168 -> misread
    rows = [(t, p, i, k, d, a, s, imp, (p == 5) if t == "A" else c)
            for (t, p, i, k, d, a, s, imp, c) in SUMMIT]
    rd, reasons, _, non_ign = _run(rows)
    assert not rd["clean"] and non_ign
    assert any("crown read on vulture_002as" in r and "Cyfur." in r for r in reasons)


def test_missing_impact_numbers_skip_the_cross_check_but_not_the_count():
    rows = [(t, p, i, k, d, a, s, None, c) for (t, p, i, k, d, a, s, imp, c) in SUMMIT]
    rd, reasons, *_ = _run(rows)
    # impact is None -> unreadable -> cross-check skipped; crown still read, bonus still paid
    assert reasons == [] and _deltas(rd, "A") == [-3, 1, -6, -8, -9]


def test_ign_confirm_expected_reasons_use_the_same_string():
    assert match._crown_count_reason("A") == "Team A must have exactly one Impact crown"


def test_verification_card_shows_crowns_and_impact_line():
    rd, reasons, *_ = _run(SUMMIT)
    card = embeds.verification_card({"match_id": "CQ-1459"}, [rd], _extraction(SUMMIT), "Summit")
    text = card.fields[0].value
    assert "2  Cyfur." in text and "👑 +5" in text
    assert text.count("👑 +5") == 2          # exactly one per team in the rows
    assert "MVP" not in text
    assert "MMR (proposed): A -3/+1/-6/-8/-9  ·  B +14/+8/+6/+4/+3" in text
    assert "SP (proposed): A -3  ·  B +5" in text
    assert "Impact 👑 (+5 MMR): W — pos 1 TempestAngryyy  ·  L — pos 2 Cyfur." in text
    assert "Impact crown" in card.description
