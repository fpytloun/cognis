from __future__ import annotations

import pytest

from cognis.models.config import ModelInfo
from cognis.providers.llm.reasoning import (
    apply_reasoning_config,
    reasoning_efforts_for_model,
    reasoning_mode_for_model,
)


def _model_info(model_id: str) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        supports_reasoning=True,
        supports_extended_thinking=True,
        max_output_tokens=128_000,
    )


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-mythos-5",
        "claude-mythos-preview",
    ],
)
@pytest.mark.parametrize(
    "request_kwargs",
    [{}, {"reasoning_effort": None}, {"reasoning_effort": "default"}],
)
def test_adaptive_models_use_adaptive_thinking_for_omitted_and_default_effort(
    model_id: str,
    request_kwargs: dict[str, str | None],
) -> None:
    prepared = apply_reasoning_config(
        {
            **request_kwargs,
            "temperature": 0.4,
            "top_p": 0.8,
            "top_k": 20,
            "max_tokens": 32_000,
            "output_config": {"format": {"type": "json_schema"}, "effort": "low"},
        },
        model_id=model_id,
        provider_preset="anthropic",
        model_info=_model_info(model_id),
    )

    assert prepared.family == "anthropic_adaptive"
    assert prepared.request_kwargs["thinking"] == {"type": "adaptive"}
    assert prepared.request_kwargs["output_config"] == {"format": {"type": "json_schema"}}
    assert prepared.request_kwargs["max_tokens"] == 32_000
    assert not {"temperature", "top_p", "top_k"} & prepared.request_kwargs.keys()
    assert prepared.effective_effort == "adaptive"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_adaptive_models_send_explicit_supported_effort(effort: str) -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": effort},
        model_id="claude-sonnet-4-6",
        provider_preset="anthropic",
        model_info=_model_info("claude-sonnet-4-6"),
    )

    assert prepared.request_kwargs["thinking"] == {"type": "adaptive"}
    assert prepared.request_kwargs["output_config"] == {"effort": effort}
    assert prepared.effective_effort == effort


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    ],
)
def test_xhigh_is_available_only_on_documented_models(model_id: str) -> None:
    efforts = reasoning_efforts_for_model(
        model_id,
        provider_preset="anthropic",
        model_info=_model_info(model_id),
        supports_reasoning=True,
    )

    assert "xhigh" in efforts


@pytest.mark.parametrize("model_id", ["claude-opus-4-6", "claude-sonnet-4-6"])
def test_xhigh_is_rejected_on_46_models(model_id: str) -> None:
    with pytest.raises(ValueError, match="not supported"):
        apply_reasoning_config(
            {"reasoning_effort": "xhigh"},
            model_id=model_id,
            provider_preset="anthropic",
            model_info=_model_info(model_id),
        )


@pytest.mark.parametrize("model_id", ["claude-fable-5", "claude-mythos-5", "claude-mythos-preview"])
def test_always_on_models_reject_disabled_thinking(model_id: str) -> None:
    efforts = reasoning_efforts_for_model(
        model_id,
        provider_preset="anthropic",
        model_info=_model_info(model_id),
        supports_reasoning=True,
    )
    assert "none" not in efforts

    with pytest.raises(ValueError, match="not supported"):
        apply_reasoning_config(
            {"reasoning_effort": "none"},
            model_id=model_id,
            provider_preset="anthropic",
            model_info=_model_info(model_id),
        )


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ],
)
def test_disable_capable_adaptive_models_send_documented_disabled_mode(model_id: str) -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "none"},
        model_id=model_id,
        provider_preset="anthropic",
        model_info=_model_info(model_id),
    )

    assert prepared.request_kwargs["thinking"] == {"type": "disabled"}
    assert prepared.effective_effort == "none"


def test_legacy_anthropic_and_non_anthropic_omission_behavior_is_unchanged() -> None:
    legacy = apply_reasoning_config(
        {"temperature": 0.2},
        model_id="claude-sonnet-4-5",
        provider_preset="anthropic",
        model_info=_model_info("claude-sonnet-4-5"),
    )
    openai = apply_reasoning_config(
        {"temperature": 0.2},
        model_id="gpt-5.4",
        provider_preset="openai",
        model_info=_model_info("gpt-5.4"),
    )
    gemini = apply_reasoning_config(
        {"temperature": 0.2},
        model_id="gemini-2.5-pro",
        provider_preset="gemini",
        model_info=_model_info("gemini-2.5-pro"),
    )

    assert legacy.request_kwargs == {"temperature": 0.2}
    assert openai.request_kwargs == {}
    assert gemini.request_kwargs == {"temperature": 0.2}


@pytest.mark.parametrize(
    ("model_id", "display_name"),
    [
        ("claude-opus-4-1-20250805", None),
        ("internal-opus", "Claude Opus 4.1 Alias"),
    ],
)
def test_claude_opus_41_keeps_legacy_manual_thinking(
    model_id: str, display_name: str | None
) -> None:
    model_info = _model_info(model_id).model_copy(update={"display_name": display_name})

    prepared = apply_reasoning_config(
        {"reasoning_effort": "low"},
        model_id=model_id,
        provider_preset="anthropic",
        model_info=model_info,
    )

    assert prepared.family == "anthropic"
    assert prepared.request_kwargs["thinking"] == {
        "type": "enabled",
        "budget_tokens": 2_048,
    }


def test_native_anthropic_thinking_configuration_is_preserved() -> None:
    native = {"type": "enabled", "budget_tokens": 12_000}
    prepared = apply_reasoning_config(
        {
            "reasoning_effort": "default",
            "thinking": native,
            "temperature": 0.2,
            "output_config": {"effort": "medium"},
            "max_tokens": 1_000,
        },
        model_id="claude-opus-4-6",
        provider_preset="anthropic",
        model_info=_model_info("claude-opus-4-6"),
    )

    assert prepared.request_kwargs == {
        "thinking": native,
        "output_config": {"effort": "medium"},
        "max_tokens": 18_000,
    }


@pytest.mark.parametrize("model_id", ["claude-haiku-5", "claude-opus-4-9", "claude-sonnet-5-1"])
def test_unknown_future_anthropic_models_are_not_classified_as_adaptive(model_id: str) -> None:
    prepared = apply_reasoning_config(
        {},
        model_id=model_id,
        provider_preset="anthropic",
        model_info=_model_info(model_id),
    )

    assert prepared.family != "anthropic_adaptive"
    assert "thinking" not in prepared.request_kwargs


@pytest.mark.parametrize("model_id", ["claude-haiku-5", "claude-opus-4-9", "claude-sonnet-5-1"])
def test_unknown_future_anthropic_models_reject_explicit_effort(model_id: str) -> None:
    with pytest.raises(ValueError, match="not supported"):
        apply_reasoning_config(
            {"reasoning_effort": "high"},
            model_id=model_id,
            provider_preset="anthropic",
            model_info=_model_info(model_id),
        )


@pytest.mark.parametrize("model_id", ["claude-haiku-5", "claude-opus-4-9", "claude-sonnet-5-1"])
def test_unknown_future_anthropic_models_are_not_inferred_as_reasoning_capable(
    model_id: str,
) -> None:
    from cognis.providers.llm.litellm import LiteLLMProvider

    provider = type(
        "Provider",
        (),
        {"config": {"preset": "anthropic"}, "location": "controller"},
    )()
    inferred = LiteLLMProvider.__new__(LiteLLMProvider)._infer_model_capabilities(
        model_id, provider
    )

    assert inferred["supports_reasoning"] is False
    assert inferred["supports_extended_thinking"] is False


def test_runtime_mode_observes_adaptive_without_mislabeling_default_as_high() -> None:
    model_info = _model_info("claude-opus-4-8")

    assert reasoning_mode_for_model("claude-opus-4-8", model_info=model_info) == "adaptive"
    assert (
        reasoning_mode_for_model(
            "claude-opus-4-8",
            model_info=model_info,
            requested_effort="default",
        )
        == "adaptive"
    )
    assert (
        reasoning_mode_for_model(
            "claude-opus-4-8",
            model_info=model_info,
            requested_effort="none",
        )
        == "disabled"
    )
