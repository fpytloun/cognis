from __future__ import annotations

import pytest

from cognis.models.config import ModelInfo
from cognis.providers.llm.anthropic.transport import _compat_usage
from cognis.providers.llm.fast_mode import ANTHROPIC_FAST_BETA, enrich_fast_mode, prepare_fast_mode


@pytest.mark.parametrize(
    "model,supported",
    [
        ("claude-opus-5-5", True),
        ("anthropic/claude-opus-5-5", True),
        ("claude-opus-5", True),
        ("anthropic/claude-opus-5", True),
        ("claude-opus-4-8", True),
        ("claude-opus-4-7", False),
        ("claude-opus-4-6", False),
        ("claude-sonnet-5", False),
        ("claude-fable-5-1", False),
        ("claude-fable-5-2", False),
        ("claude-opus-5-invented", False),
    ],
)
def test_native_fast_mode_catalog(model, supported):
    entry = enrich_fast_mode({"model_id": model}, "anthropic")
    assert entry["supports_fast_mode"] is supported
    assert entry["fast_mode_parameter"] == ("speed" if supported else None)


@pytest.mark.parametrize(
    "preset,config",
    [
        ("bedrock", {}),
        ("vertex_ai", {}),
        ("openai_compatible", {}),
        ("anthropic", {"base_url": "https://proxy.example.com"}),
        ("anthropic", {"protocol": "litellm"}),
    ],
)
def test_model_name_does_not_enable_unsupported_transport(preset, config):
    assert not enrich_fast_mode({"model_id": "claude-opus-5"}, preset, config)["supports_fast_mode"]


def test_administrator_restriction_and_incomplete_tier():
    assert not enrich_fast_mode(
        {"model_id": "claude-opus-5", "supports_fast_mode": False}, "anthropic"
    )["supports_fast_mode"]
    assert not enrich_fast_mode({"model_id": "gpt-5.4", "supports_fast_mode": True}, "chatgpt")[
        "supports_fast_mode"
    ]


@pytest.mark.parametrize("parameter,tier", [("speed", "fast"), ("service_tier", "priority")])
def test_fast_selection_off_inheritance_and_cached_fallback(parameter, tier):
    info = ModelInfo(
        model_id="claude-opus-5" if parameter == "speed" else "gpt-5.4",
        supports_fast_mode=True,
        fast_mode_tier=tier,
        fast_mode_parameter=parameter,
    )
    base = {
        "service_tier": "priority",
        "speed": "fast",
        "extra_body": {"speed": "fast", "service_tier": "priority", "other": 1},
        "extra_headers": {"anthropic-beta": "existing-beta"},
    }
    assert prepare_fast_mode(base, info) == base
    for selection, unavailable in [(False, False), (True, True)]:
        result = prepare_fast_mode({**base, "fast_mode": selection}, info, unavailable=unavailable)
        assert "speed" not in result
        assert "service_tier" not in result
        assert result["extra_body"] == {"other": 1}
    result = prepare_fast_mode({**base, "fast_mode": True}, info)
    assert result[parameter] == tier
    if parameter == "speed":
        assert result["extra_headers"]["anthropic-beta"] == f"existing-beta,{ANTHROPIC_FAST_BETA}"
        assert "service_tier" not in result
    assert base["extra_body"]["speed"] == "fast"


def test_unsupported_explicit_enable_is_not_silently_ignored():
    with pytest.raises(ValueError, match="unsupported"):
        prepare_fast_mode({"fast_mode": True}, ModelInfo(model_id="claude-sonnet-5"))


def test_disabled_removes_inherited_fast_beta_but_preserves_other_betas():
    result = prepare_fast_mode(
        {"fast_mode": False, "extra_headers": {"Anthropic-Beta": f"{ANTHROPIC_FAST_BETA},other"}},
        ModelInfo(model_id="claude-opus-5"),
    )
    assert result["extra_headers"] == {"Anthropic-Beta": "other"}


def test_rate_limits_do_not_poison_fast_capability():
    from cognis.providers.llm.litellm import _fast_mode_rejection_reason

    class RateLimitError(Exception):
        status_code = 429

    assert (
        _fast_mode_rejection_reason(
            RateLimitError("service_tier capacity exceeded"), {"service_tier": "priority"}
        )
        is None
    )


@pytest.mark.parametrize("speed", ["standard", "fast"])
def test_actual_provider_speed_is_retained_independently_of_request(speed):
    assert _compat_usage({"input_tokens": 2, "output_tokens": 3, "speed": speed})["speed"] == speed
