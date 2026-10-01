"""Cost and step budgets for one run.

Every model call reports its usage; the supervisor rolls that up into one
number the UI shows and the test suite asserts on. Kept small: this is a
bookkeeper, not a scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.providers import ModelInfo, get


@dataclass(slots=True)
class Budget:
    max_steps: int = 120
    steps: int = 0
    model_ms: int = 0
    load_ms: int = 0
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def spent(self, model_id: str, *, tokens_in: int, tokens_out: int, latency_ms: int) -> None:
        try:
            info: ModelInfo = get(model_id)
        except KeyError:
            info = None  # unknown model: tokens still counted, dollars omitted
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.model_ms += latency_ms
        if info is not None:
            self.usd += tokens_in * info.price_in_per_mtok / 1_000_000
            self.usd += tokens_out * info.price_out_per_mtok / 1_000_000
        record = self.by_model.setdefault(model_id, {"in": 0, "out": 0, "ms": 0})
        record["in"] += tokens_in
        record["out"] += tokens_out
        record["ms"] += latency_ms

    def loaded(self, load_ms: int) -> None:
        self.load_ms += load_ms

    def stepped(self) -> None:
        self.steps += 1

    def exhausted(self) -> bool:
        return self.steps >= self.max_steps
