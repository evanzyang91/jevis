"""Action shape: the label-with-item-tail-removed form used by both session
memory and the Postgres playbook.

Two actions belong to the same "move" when their shapes agree. That means
"Add to cart · Robin Hood Flour" and "Add to cart · Redpath Sugar" become one
shape — otherwise a cycle of near-identical clicks never registers as such.
"""

from __future__ import annotations

from agent.executor import Action

SEPARATORS = (" - ", " — ", " · ", ",")
LABEL_HEAD_LIMIT = 40


def action_shape(action: Action) -> str:
    """Return `kind:label-head`. The label head is trimmed to 40 characters
    so long product names do not defeat the equality test."""
    label = (action.label or "").strip()
    for separator in SEPARATORS:
        head, sep, _ = label.partition(separator)
        if sep:
            label = head.strip()
            break
    return f"{action.kind}:{label[:LABEL_HEAD_LIMIT].strip()}"


def situation(url: str, previous: Action | None) -> tuple[str, str]:
    """One playbook key. Path (not full URL, so query strings do not multiply
    identical situations) plus the shape of the previous action, or 'start'."""
    from urllib.parse import urlparse

    path = urlparse(url).path or "/"
    previous_shape = action_shape(previous) if previous is not None else "start"
    return path, previous_shape


def path_pattern(url: str) -> str:
    """The path with its item-specific parts as `*`, so every product page of
    a site is one situation: "/en/ip/Great-Value-Large-12-Eggs/10052944" ->
    "/en/ip/*/*", Amazon's "/Logitech-M185/dp/B004YAVF8I" -> "/*/dp/*"."""
    from urllib.parse import urlparse

    parts = []
    for segment in (urlparse(url).path or "/").split("/"):
        if not segment:
            continue
        specific = any(c.isdigit() for c in segment) or len(segment) > 24 or segment.count("-") >= 2
        parts.append("*" if specific else segment.lower())
    return "/" + "/".join(parts)
