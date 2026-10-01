"""Executor: drive a browser. One page, one CDP session, one contract."""

from .browser import Executor, FakeExecutor, PlaywrightExecutor, with_executor
from .kinds import (
    Action,
    ActionKind,
    NavigationInterrupted,
    Occluded,
    Outcome,
    StalePage,
)
from .motion import Waypoint, move_duration, path, typing_delays
from .screencast import Frame, FrameSink, decode, start_params
from .stealth import init_script

__all__ = [
    "Action",
    "ActionKind",
    "Executor",
    "FakeExecutor",
    "Frame",
    "FrameSink",
    "NavigationInterrupted",
    "Occluded",
    "Outcome",
    "PlaywrightExecutor",
    "StalePage",
    "Waypoint",
    "decode",
    "init_script",
    "move_duration",
    "path",
    "start_params",
    "typing_delays",
    "with_executor",
]
