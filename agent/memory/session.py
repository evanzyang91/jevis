"""Session memory: one run's remembered next moves.

Only trusts a move after two independent confirmations from the same situation.
This filters out the first pass through a page, which is usually exploratory
and often takes wrong turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.executor import Action
from agent.perception import Observation

from .shape import action_shape, situation


@dataclass(slots=True)
class SessionMemory:
    """One instance per run. Records what followed each (path, previous)
    situation, and offers a recalled move only when it has been seen twice."""

    moves: dict[tuple[str, str], str] = field(default_factory=dict)
    seen: dict[tuple[str, str], int] = field(default_factory=dict)

    def learn(self, url: str, previous: Action | None, action: Action) -> None:
        key = situation(url, previous)
        current = action_shape(action)
        if self.moves.get(key) == current:
            self.seen[key] = self.seen.get(key, 1) + 1
        else:
            self.moves[key] = current
            self.seen[key] = 1

    def forget(self, url: str, previous: Action | None) -> None:
        key = situation(url, previous)
        self.moves.pop(key, None)
        self.seen.pop(key, None)

    def recall(self, observation: Observation, previous: Action | None) -> Action | None:
        """The move that followed this situation before, if exactly one
        element on the current page matches its shape.

        More than one match means the choice carries information this store
        cannot supply (which of seven Add-to-cart buttons is the right item).
        Zero matches means the page has moved on.
        """
        key = situation(observation.url, previous)
        wanted = self.moves.get(key)
        if not wanted or self.seen.get(key, 0) < 2:
            return None
        from agent.policy.action_space import build

        space = build(observation)
        matches: list[Action] = []
        for operation in space.operations:
            for target in operation.targets:
                if action_shape(target.action) == wanted:
                    matches.append(target.action)
        return matches[0] if len(matches) == 1 else None
