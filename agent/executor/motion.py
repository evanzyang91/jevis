"""Human-like pointer motion.

The synthetic cursor overlay in the UI renders the same path the executor
drives into the browser, so what the user sees is what the site sees. Real
pointers do not travel in straight lines and do not click in zero time; the
functions here shape both.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Waypoint:
    x: float
    y: float
    hold_ms: int


def move_duration(distance: float) -> int:
    """Fitts-shaped duration. Short moves feel snappy, long moves feel
    deliberate. Bounded so a full-screen move never stalls the step."""
    return min(int(180 + 90 * math.log2(1 + distance / 64)), 550)


def path(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    samples: int = 20,
    rng: random.Random | None = None,
) -> list[Waypoint]:
    """Cubic bezier from start to end with a perpendicular midpoint bulge.

    The bulge sign flips each call so a run does not always curve the same way,
    and every sample gets sub-pixel jitter so the trail is not a mathematically
    perfect curve.
    """
    rng = rng or random.Random()
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    if distance < 2:
        return [Waypoint(x1, y1, 0)]
    total_ms = move_duration(distance)
    step_ms = max(1, total_ms // samples)
    perp_x = -dy / distance
    perp_y = dx / distance
    bulge = distance * 0.15 * rng.uniform(0.6, 1.2) * rng.choice((-1, 1))
    c1x = x0 + dx * 0.33 + perp_x * bulge
    c1y = y0 + dy * 0.33 + perp_y * bulge
    c2x = x0 + dx * 0.66 + perp_x * bulge * 0.5
    c2y = y0 + dy * 0.66 + perp_y * bulge * 0.5
    waypoints: list[Waypoint] = []
    for i in range(1, samples + 1):
        t = i / samples
        one = 1 - t
        x = one**3 * x0 + 3 * one**2 * t * c1x + 3 * one * t**2 * c2x + t**3 * x1
        y = one**3 * y0 + 3 * one**2 * t * c1y + 3 * one * t**2 * c2y + t**3 * y1
        waypoints.append(Waypoint(x + rng.uniform(-0.4, 0.4), y + rng.uniform(-0.4, 0.4), step_ms))
    if distance > 400:
        angle = math.atan2(dy, dx)
        waypoints.append(Waypoint(x1 + math.cos(angle) * 8, y1 + math.sin(angle) * 8, 40))
        waypoints.append(Waypoint(x1, y1, 60))
    return waypoints


def typing_delays(text: str, rng: random.Random | None = None) -> list[int]:
    """Per-character delays for a typed value.

    Gaussian around ~40 ms — fast-typist range. The point of typing char by char
    is to fire keydown/keyup events (bot-detection hooks watch for them); the
    delay itself only needs to keep the cadence irregular, not slow.
    """
    rng = rng or random.Random()
    return [max(15, int(rng.gauss(40, 12))) for _ in text]
