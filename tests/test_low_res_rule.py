"""Lower-limit-only rule: a small screenshot's crown is confirmed by an admin.
Big images (iPad, any resolution, any aspect ratio) are never affected."""
import struct

import pytest

import config
from cogs import match as m
from services import localization, vision_extraction as v
from tests.test_impact_crown import SUMMIT, _extraction, _roster

localization.load_map_translations()


def _png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"


def _jpeg(w, h):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9          # a segment to skip first
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0


def _webp_vp8x(w, h):
    return b"RIFF" + struct.pack("<I", 22) + b"WEBP" + b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" \
        + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")


def test_image_size_reads_png_jpeg_webp():
    assert v.image_size(_png(2800, 1272)) == (2800, 1272)
    assert v.image_size(_jpeg(2400, 1080)) == (2400, 1080)
    assert v.image_size(_webp_vp8x(1568, 723)) == (1568, 723)


@pytest.mark.parametrize("junk", [b"", b"not an image", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"])
def test_unknown_or_broken_files_give_none_never_small(junk):
    assert v.image_size(junk) is None


def _ext(size):
    e = _extraction(SUMMIT)
    if size:
        e["image_size"] = list(size)
    return e


@pytest.mark.parametrize("size", [(2800, 1272), (2688, 1216), (1568, 702), (2048, 1536),   # iPad 4:3
                                  (2732, 2048), (1000, 400), (400, 1000), (8000, 6000), None])
def test_normal_and_large_images_are_never_flagged(size):
    assert m._crown_problems(_ext(size)) == {}


@pytest.mark.parametrize("size", [(376, 815), (684, 270), (999, 500)])
def test_small_images_need_an_admin_for_both_teams(size):
    problems = m._crown_problems(_ext(size))
    assert set(problems) == {"A", "B"} and all("low-resolution" in r for r in problems.values())


def test_admin_pick_clears_the_rule_per_team():
    ext = _ext((376, 815))
    assert set(m._crown_problems(ext, trusted_teams=("A",))) == {"B"}
    assert m._crown_problems(ext, trusted_teams=("A", "B")) == {}


def test_small_image_goes_to_review_then_is_clean_after_admin_picks():
    ext = _ext((376, 815))
    rd, reasons, _, non_ign = m.Match._prepare_round(_roster(SUMMIT), "Summit", ext)
    assert not rd["clean"] and non_ign is False and len(reasons) == 2
    override = {"A": {"position": 2, "by": "1", "at": "x"}, "B": {"position": 1, "by": "1", "at": "x"}}
    rd2, reasons2, *_ = m.Match._prepare_round(_roster(SUMMIT), "Summit", ext, crown_override=override)
    assert reasons2 == [] and rd2["clean"]


def test_rule_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "CROWN_MIN_IMAGE_SIDE", 0)
    assert m._crown_problems(_ext((376, 815))) == {}
