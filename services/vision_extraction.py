"""
Vision AI scoreboard extraction — built as a swappable provider interface
so you can flip config.VISION_PROVIDER between "anthropic" / "openai" /
"qwen" (or add another) without touching any calling code.

Canonical extraction schema (resolves the two field lists in the source
doc into one): for each of the 10 players —
    ign, team, kills, deaths, assists, damage, hill_time, impact, score

To add a new provider: implement `extract(image_bytes) -> dict` on a new
class following the same contract as AnthropicVisionProvider below, then
register it in `_PROVIDERS`.
"""

from __future__ import annotations
import base64
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

import config

log = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are extracting structured data from a Call of Duty Mobile \
Hardpoint match scoreboard screenshot. Return ONLY valid JSON, no markdown fences, \
no commentary, matching exactly this schema:

{
  "map": "string or null if not visible",
  "final_score": "string like '250-210' or null",
  "players": [
    {
      "ign": "string, exactly as shown",
      "team": "A or B — infer from screen position/grouping, top group = A",
      "position": integer 1-5 — the player's game-provided rank WITHIN THEIR OWN TEAM,
          shown as a numbered badge/rank marker next to their row (1 = top of that
          team's list). This is NOT their overall placement across all 10 players —
          each team has its own 1-5 ranking. Never derive this from score/kills
          yourself; read the number the game already shows.
      "has_crown": true or false. Look closely at the IMPACT column (far right) of
          EVERY row of each team. On a normal scoreboard each team has exactly ONE row
          with a small CROWN icon sitting directly above the Impact number — find it and
          set has_crown true for that row. The crown can be on ANY row 1-5, not just row
          1. It is small, and its colour differs: usually yellow on the winning team and
          pale white/lavender on the losing team (the pale one is easy to miss on the
          dark row background — check every row before concluding it is absent).
          ONLY IF, after checking every row, the crown of a team is really not visible
          because that area is covered (loading bar, notification, overlay) or
          unreadable, set has_crown to false for EVERY row of that team. Zero crowns for
          a team is then the correct answer and a human resolves it; a guessed crown
          would pay +5 MMR to the wrong player. Never choose a crown because one "should"
          exist, and never infer it from the Impact numbers (two players can show the
          same Impact and only one has the crown), from the MVP tag, or from row position.
          IGNORE the yellow "MVP" tag/flag next to a player's name — that is a
          different marker, it is NOT the crown, and it must never make has_crown
          true. Report the crown only where you actually see the icon.
      "kills": integer,
      "deaths": integer,
      "assists": integer or null if not shown,
      "damage": integer or null — MANY scoreboard views do NOT have a Damage
          column at all (only K/D/A, Score, Time, Impact are shown in some
          layouts). If you do not see a column literally labeled "Damage",
          return null for this field. Do NOT substitute the Score, Impact, or
          any other column's value here — an absent Damage column means null,
          never a borrowed number from elsewhere in the row.
      "hill_time": number (seconds),
      "score": integer,
      "impact": number or null if not shown
    }
    // one entry per player visible, up to 10
  ]
}

FIRST, identify the actual column headers present in this specific image, left to
right (e.g. "Player, Score, K/D/A, Time, Impact" — headers vary between screenshot
styles, don't assume every field in the schema above has a matching column). THEN,
for each player row, read each value strictly from the column whose header matches
that field — never move a value from one column into a different field just because
that field's own column is missing or you're unsure. A missing column means null for
that field, not a value copied from a neighboring column.

"position" and "has_crown" are REQUIRED for every player — they are read directly off
the scoreboard (a numbered rank badge and a crown icon by the Impact number), not
calculated. "position" must still be returned even if you are unsure; "has_crown" must
be true or false for every player (false when no crown icon is visible on that row —
this includes every row of a team whose crown is hidden). Both are load-bearing for
match results.

If a field is not legible or not present in the image, use null for that field —
never guess or fabricate a number, and never substitute a different column's value.
Double-check digits that could be visually ambiguous (e.g. 0 vs O, 1 vs 7, 8 vs 3,
6 vs 8) by cross-referencing column alignment across all 10 rows.

Return exactly as many player entries as are actually visible in the scoreboard —
normally 10, but sometimes fewer (e.g. 9, if a player left the match before it
ended). Do NOT invent a placeholder row to reach 10 if only 9 players are shown.
An incomplete roster is expected and handled downstream; a fabricated player row
is not — it would silently corrupt that match's results."""


class VisionProvider(ABC):
    @abstractmethod
    def extract(self, image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
        """Return the parsed extraction dict per EXTRACTION_PROMPT schema."""
        raise NotImplementedError


class AnthropicVisionProvider(VisionProvider):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def extract(self, image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 2000,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": b64_image},
                            },
                            {"type": "text", "text": EXTRACTION_PROMPT},
                        ],
                    }
                ],
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)


def _log_openai_usage(response_json: Any, model: str) -> None:
    """Log one VISION_USAGE INFO line per OpenAI scoreboard read. Missing
    fields count as 0; never raises, so logging can't break a read."""
    try:
        usage = response_json.get("usage") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        log.info(
            "VISION_USAGE model=%s prompt=%d completion=%d reasoning=%d cached=%d total=%d",
            model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            int(completion_details.get("reasoning_tokens") or 0),
            int(prompt_details.get("cached_tokens") or 0),
            int(usage.get("total_tokens") or 0),
        )
    except Exception:
        pass


class OpenAIVisionProvider(VisionProvider):
    """OpenAI Chat Completions API, vision-capable model. Model name is
    configurable via config.OPENAI_VISION_MODEL rather than hardcoded —
    verify the exact current string against platform.openai.com/docs/models
    before your first real run."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @staticmethod
    def _max_tokens_kwarg(model: str) -> dict:
        """gpt-5.x models rejected the old "max_tokens" param outright
        during live testing (400 Bad Request: use max_completion_tokens
        instead). gpt-4.x still wants the old name. Confirmed via a real
        API call against gpt-5.4-mini before this fix was added."""
        if model.startswith("gpt-5") or model.startswith("o"):
            return {"max_completion_tokens": 2000}
        return {"max_tokens": 2000}

    def extract(self, image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.OPENAI_VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": EXTRACTION_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{media_type};base64,{b64_image}",
                                    "detail": "high",  # dense small-text scoreboard — confirmed during
                                                        # testing this matters more than a no-op on some images
                                },
                            },
                        ],
                    }
                ],
                **self._max_tokens_kwarg(config.OPENAI_VISION_MODEL),
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=90,
        )
        if resp.status_code >= 400:
            # Surface the real OpenAI error message rather than a bare
            # HTTPStatusError — this is what let us diagnose the
            # max_tokens rejection quickly during testing instead of
            # guessing at it.
            raise RuntimeError(f"OpenAI vision API error ({resp.status_code}) for model "
                                f"{config.OPENAI_VISION_MODEL!r}: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        _log_openai_usage(data, config.OPENAI_VISION_MODEL)
        text = data["choices"][0]["message"]["content"]
        return json.loads(text)

class QwenVisionProvider(VisionProvider):
    """Stub — implement when you finalize whether you're using Qwen-VL."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def extract(self, image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
        raise NotImplementedError(
            "QwenVisionProvider.extract() not implemented yet — "
            "wire this up to the Qwen-VL API once you confirm the endpoint/model."
        )

class NvidiaVisionProvider(VisionProvider):
    """NVIDIA NIM provider using meta/llama-3.2-90b-vision-instruct"""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def extract(self, image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        resp = httpx.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "meta/llama-3.2-90b-vision-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{media_type};base64,{b64_image}"
                                }
                            },
                            {
                                "type": "text",
                                "text": EXTRACTION_PROMPT
                            }
                        ]
                    }
                ],
                "max_tokens": 2000,
                "temperature": 0.1,
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)

_PROVIDERS = {
    "anthropic": lambda: AnthropicVisionProvider(config.ANTHROPIC_API_KEY),
    "openai": lambda: OpenAIVisionProvider(config.OPENAI_API_KEY),
    "qwen": lambda: QwenVisionProvider(config.QWEN_API_KEY),
    "nvidia_nim": lambda: NvidiaVisionProvider(config.NVIDIA_NIM_API_KEY),
}


def get_provider() -> VisionProvider:
    factory = _PROVIDERS.get(config.VISION_PROVIDER)
    if factory is None:
        raise RuntimeError(f"Unknown VISION_PROVIDER: {config.VISION_PROVIDER}")
    return factory()


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG / JPEG / WebP straight from the file header —
    no image library needed. None when the format isn't recognised or the
    header is damaged; callers must treat None as "unknown", never as small."""
    import struct
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":                       # JPEG: walk segments to the SOF marker
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
            if kind == b"VP8 ":
                return (struct.unpack("<H", data[26:28])[0] & 0x3FFF, struct.unpack("<H", data[28:30])[0] & 0x3FFF)
            if kind == b"VP8L":
                b = data[21:25]
                return (1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
    except (struct.error, IndexError):
        return None
    return None


def extract_scoreboard(image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
    provider = get_provider()
    result = provider.extract(image_bytes, media_type)
    # Stored with the raw extraction (audit trail) and read by the crown check:
    # a very small screenshot is never trusted to show the crown (see
    # cogs/match.py _crown_problems). Recorded only — no behaviour here.
    size = image_size(image_bytes)
    if size and isinstance(result, dict):
        result["image_size"] = [size[0], size[1]]
    return result