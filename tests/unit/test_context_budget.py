from __future__ import annotations

from cognis.core.context_budget import resolve_context_budget


def test_context_budget_honors_split_input_limit() -> None:
    budget = resolve_context_budget(
        max_context_tokens=400_000,
        max_input_tokens=272_000,
        agent_max_tokens=None,
        model_max_output_tokens=128_000,
    )

    assert budget.max_context_tokens == 400_000
    assert budget.max_input_tokens == 272_000
    assert budget.reserve_output_tokens == 128_000
    assert budget.effective_reserve_output_tokens == 32_768
    assert budget.reserve_clamped is True
    assert budget.available_prompt_tokens == 272_000


def test_context_budget_preserves_legacy_context_minus_reserve_behavior() -> None:
    budget = resolve_context_budget(
        max_context_tokens=400_000,
        agent_max_tokens=None,
        model_max_output_tokens=128_000,
    )

    assert budget.max_input_tokens == 0
    assert budget.effective_reserve_output_tokens == 32_768
    assert budget.available_prompt_tokens == 367_232
