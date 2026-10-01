"""Vision parsing rules. No real model call."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agent.perception.vision import VisionError, _parse
from agent.providers import TextResult, TokenUsage


def test_parse_accepts_wrapped_object() -> None:
    payload = json.dumps({"elements": [{"role": "button", "name": "Buy", "x": 0, "y": 0, "w": 10, "h": 10}]})
    assert _parse(payload) == [{"role": "button", "name": "Buy", "x": 0, "y": 0, "w": 10, "h": 10}]


def test_parse_accepts_bare_array() -> None:
    payload = json.dumps([{"role": "button", "name": "Buy", "x": 0, "y": 0, "w": 10, "h": 10}])
    assert _parse(payload)[0]["role"] == "button"


def test_parse_rejects_non_list_elements() -> None:
    payload = json.dumps({"elements": {"role": "button"}})
    with pytest.raises(VisionError):
        _parse(payload)


@dataclass
class StubAdapter:
    reply: str
    calls: int = 0

    async def complete(self, system, user, *, images=None, json_object=False, max_tokens=1024):
        del system, user, images, json_object, max_tokens
        self.calls += 1
        return TextResult(
            text=self.reply,
            model="stub-vision",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50),
            latency_ms=1,
        )
