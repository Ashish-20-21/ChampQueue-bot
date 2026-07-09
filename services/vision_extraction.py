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
      "kills": integer,
      "deaths": integer,
      "assists": integer or null if not shown,
      "damage": integer,
      "hill_time": number (seconds),
      "score": integer,
      "impact": number or null if not shown
    }
    // one entry per player visible, up to 10
  ]
}

If a field is not legible or not present in the image, use null for that field —
never guess or fabricate a number. Double-check digits that could be visually
ambiguous (e.g. 0 vs O, 1 vs 7, 8 vs 3) by cross-referencing column alignment."""


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
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)


class OpenAIVisionProvider(VisionProvider):
    """Stub — implement when you finalize whether you're using GPT-4.1.
    Same contract as AnthropicVisionProvider: return the schema above."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def extract(self, image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
        raise NotImplementedError(
            "OpenAIVisionProvider.extract() not implemented yet — "
            "wire this up to /v1/chat/completions with an image_url content block "
            "once you confirm you're using GPT-4.1 for extraction."
        )


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
            timeout=60,
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
