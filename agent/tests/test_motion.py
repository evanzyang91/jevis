"""Motion primitives: bezier bulge, Fitts duration, keystroke spread."""

from __future__ import annotations

import math
import random

from agent.executor.motion import move_duration, path, typing_delays


def test_move_duration_grows_with_distance_but_stays_bounded() -> None:
    small = move_duration(20)
    big = move_duration(2000)
    assert small < big <= 550
    assert small >= 180


def test_path_returns_single_waypoint_when_endpoints_coincide() -> None:
    waypoints = path((100.0, 100.0), (100.5, 100.5), rng=random.Random(0))
    assert waypoints == [__import__("agent.executor.motion", fromlist=["Waypoint"]).Waypoint(100.5, 100.5, 0)]


def test_path_ends_at_target_within_pixel() -> None:
    rng = random.Random(7)
    waypoints = path((0.0, 0.0), (500.0, 300.0), rng=rng)
    assert waypoints, "path must not be empty"
    last = waypoints[-1]
    assert math.hypot(last.x - 500.0, last.y - 300.0) < 2.0


def test_path_curves_away_from_the_straight_line() -> None:
    """A straight line has zero perpendicular distance from any midpoint. A
    real bezier bulge must not."""
    waypoints = path((0.0, 0.0), (400.0, 0.0), rng=random.Random(1))
    midpoint = waypoints[len(waypoints) // 2]
    assert abs(midpoint.y) > 5.0


def test_typing_delays_never_stall_below_minimum() -> None:
    delays = typing_delays("hello world", rng=random.Random(3))
    assert len(delays) == len("hello world")
    assert min(delays) >= 15
