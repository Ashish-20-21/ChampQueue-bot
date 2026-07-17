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
from abc import ABC, abstractmethod
from typing import Any

import httpx

import config

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
      "is_mvp": true or false — true for exactly one player per team, wherever the
          game shows an "MVP" tag/badge on that player's row. Exactly one true per
          team, never more, never fewer if the tag is visible.
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

"position" and "is_mvp" are REQUIRED for every player — they are read directly off
the scoreboard (a numbered rank badge and an MVP tag), not calculated. If either is
genuinely not visible for a player, still return your best read rather than omitting
the field, since both are load-bearing for match results.

If a field is not legible or not present in the image, use null for that field —
never guess or fabricate a number, and never substitute a different column's value.
Double-check digits that could be visually ambiguous (e.g. 0 vs O, 1 vs 7, 8 vs 3,
6 vs 8) by cross-referencing column alignment across all 10 rows."""


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


def extract_scoreboard(image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
    provider = get_provider()
    return provider.extract(image_bytes, media_type)