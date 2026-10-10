"""Exact roster match wins over the streamer-mask guard in Match._resolve_ign."""
from cogs.match import Match

MASK_NOTE = "possible streamer-mode masked name — resolve manually"

NAMES = [
    "Tomato12Crab", "Bal27Booster1", "Alpha", "Bravo", "Charlie",
    "Delta", "Echo", "Foxtrot", "Golf", "Hotel",
]


def _roster(names=NAMES):
    return {n.lower(): {"players": {"ign": n}} for n in names}


def test_exact_roster_names_that_look_like_masks_resolve():
    roster = _roster()
    for name in ("Tomato12Crab", "Bal27Booster1"):
        assert Match._looks_like_streamer_mask(name.lower())
        row, note = Match._resolve_ign(f"  {name}  ", roster)
        assert row is roster[name.lower()]
        assert note is None


def test_unknown_mask_shaped_name_is_still_flagged():
    mask = "x7k2m9q4p1z8w3"
    assert len(mask) == 14
    assert Match._resolve_ign(mask, _roster()) == (None, MASK_NOTE)


def test_one_character_misread_is_not_exact_resolved():
    roster = _roster()
    # Short name: fuzzy-resolved to the roster row, as before.
    row, note = Match._resolve_ign("Charlle", roster)
    assert row is roster["charlie"]
    assert note is None
    # Mask-shaped roster name misread by one char: guard still fires.
    assert Match._resolve_ign("Tomato12Crah", roster) == (None, MASK_NOTE)
