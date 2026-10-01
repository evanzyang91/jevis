"""Fence-stripping for JSON-mode responses."""

from __future__ import annotations

import json

from agent.providers.text.base import strip_json_fences


def test_bare_json_passes_through() -> None:
    assert strip_json_fences('{"text": null}') == '{"text": null}'


def test_json_fence_is_stripped() -> None:
    body = '```json\n{"text": null}\n```'
    assert json.loads(strip_json_fences(body)) == {"text": None}


def test_bare_fence_is_stripped() -> None:
    body = '```\n{"a": 1}\n```'
    assert json.loads(strip_json_fences(body)) == {"a": 1}


def test_inline_fence_without_newlines() -> None:
    body = '```json {"text": null} ```'
    assert json.loads(strip_json_fences(body)) == {"text": None}


def test_fence_around_array() -> None:
    body = '```json\n[1, 2, 3]\n```'
    assert json.loads(strip_json_fences(body)) == [1, 2, 3]


def test_uppercase_language_tag_accepted() -> None:
    body = '```JSON\n{"ok": true}\n```'
    assert json.loads(strip_json_fences(body)) == {"ok": True}
