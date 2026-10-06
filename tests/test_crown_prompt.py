"""Guards the crown wording in the vision prompt: zero crowns must stay a valid
answer, or the model starts guessing a crown when it is hidden (found by the
hidden-crown test, 2026-10)."""
from services import vision_extraction as v


def _prompt() -> str:
    for name in dir(v):
        val = getattr(v, name)
        if isinstance(val, str) and "has_crown" in val and len(val) > 500:
            return val
    raise AssertionError("vision prompt not found")


def test_zero_crowns_is_explicitly_allowed():
    p = _prompt()
    assert "NO crown" in p and "EVERY row of that team" in p
    assert "NEVER choose a crown because one" in p


def test_old_wording_that_pushed_the_model_to_guess_is_gone():
    p = _prompt()
    assert "never fewer" not in p
    assert "exactly one player per team" not in p


def test_mvp_tag_and_impact_numbers_are_ruled_out():
    p = _prompt()
    assert "IGNORE the yellow" in p and "Impact numbers" in p
