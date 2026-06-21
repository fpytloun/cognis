from __future__ import annotations

from types import SimpleNamespace

from cognis.core.context_budget import (
    prompt_serialization_margin_ratio_for_model,
    resolve_context_budget,
)


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
    assert budget.prompt_serialization_margin_tokens == 10_880
    assert budget.reserve_clamped is True
    assert budget.available_prompt_tokens == 261_120


def test_context_budget_preserves_legacy_context_minus_reserve_behavior() -> None:
    budget = resolve_context_budget(
        max_context_tokens=400_000,
        agent_max_tokens=None,
        model_max_output_tokens=128_000,
    )

    assert budget.max_input_tokens == 0
    assert budget.effective_reserve_output_tokens == 32_768
    assert budget.prompt_serialization_margin_tokens == 14_689
    assert budget.available_prompt_tokens == 352_543


def test_context_budget_uses_larger_responses_serialization_margin() -> None:
    budget = resolve_context_budget(
        max_context_tokens=400_000,
        max_input_tokens=272_000,
        agent_max_tokens=None,
        model_max_output_tokens=128_000,
        prompt_serialization_margin_ratio=prompt_serialization_margin_ratio_for_model(
            SimpleNamespace(model_id="gpt-5.1", supports_responses_api=True),
            "gpt-5.1",
        ),
    )

    assert budget.prompt_serialization_margin_tokens == 21_760
    assert budget.available_prompt_tokens == 250_240


def test_context_budget_can_disable_serialization_margin_for_legacy_callers() -> None:
    budget = resolve_context_budget(
        max_context_tokens=400_000,
        max_input_tokens=272_000,
        agent_max_tokens=None,
        model_max_output_tokens=128_000,
        prompt_serialization_margin_ratio=0,
    )

    assert budget.prompt_serialization_margin_tokens == 0
    assert budget.available_prompt_tokens == 272_000
