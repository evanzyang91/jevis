"""Budget rolls up cost across model calls."""

from __future__ import annotations

from agent.supervisor import Budget


def test_budget_prices_a_known_model_from_the_registry() -> None:
    budget = Budget()
    budget.spent("haiku-4-5", tokens_in=1_000_000, tokens_out=0, latency_ms=200)
    assert round(budget.usd, 4) == 1.0
    assert budget.tokens_in == 1_000_000
    assert budget.model_ms == 200


def test_budget_records_an_unpriced_model_without_dollars() -> None:
    budget = Budget()
    budget.spent("unknown-model", tokens_in=500, tokens_out=500, latency_ms=10)
    assert budget.usd == 0.0
    assert budget.tokens_in + budget.tokens_out == 1000


def test_budget_step_counter_exhausts_at_the_configured_limit() -> None:
    budget = Budget(max_steps=2)
    assert not budget.exhausted()
    budget.stepped()
    budget.stepped()
    assert budget.exhausted()


def test_budget_aggregates_per_model_totals() -> None:
    budget = Budget()
    budget.spent("haiku-4-5", tokens_in=100, tokens_out=50, latency_ms=10)
    budget.spent("haiku-4-5", tokens_in=200, tokens_out=100, latency_ms=15)
    assert budget.by_model["haiku-4-5"] == {"in": 300, "out": 150, "ms": 25}
