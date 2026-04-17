"""Reasoning effort normalization and provider translation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cognis.models.config import NORMALIZED_REASONING_LEVELS, ModelInfo

_OPENAI_PRESETS = {"openai", "openai_compatible", "litellm_proxy", "azure"}
_GOOGLE_PRESETS = {"gemini", "google", "vertex_ai"}
_ANTHROPIC_BUDGET_BUFFER = 1024
_MIN_ANTHROPIC_MAX_TOKENS = 2048
_ANTHROPIC_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 2048,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "max": 32768,
}
_GOOGLE_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 1024,
    "medium": 4096,
    "high": 16384,
    "max": 32768,
}


@dataclass(slots=True)
class PreparedReasoningConfig:
    """Provider-ready request kwargs plus normalization metadata."""

    request_kwargs: dict[str, Any]
    family: str
    effective_effort: str | None = None
    stripped_params: tuple[str, ...] = ()
    translated_max_tokens: bool = False


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Return a canonical normalized reasoning level or ``None``."""

    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"off", "reset"}:
        return "default"
    if normalized in NORMALIZED_REASONING_LEVELS:
        return normalized
    return None


def reasoning_efforts_for_model(
    model_id: str,
    *,
    provider_preset: str = "",
    supports_reasoning: bool,
) -> list[str]:
    """Return the normalized user-facing reasoning levels for a model."""

    if not supports_reasoning:
        return []
    return list(NORMALIZED_REASONING_LEVELS)


def apply_reasoning_config(
    request_kwargs: dict[str, Any],
    *,
    model_id: str,
    provider_preset: str = "",
    model_info: ModelInfo | None = None,
) -> PreparedReasoningConfig:
    """Translate normalized reasoning settings into provider-ready kwargs."""

    result = dict(request_kwargs)
    family = detect_reasoning_family(model_id, provider_preset=provider_preset)
    supports_reasoning = _supports_reasoning(model_info, model_id, provider_preset)
    preset_family = _family_from_provider_preset(provider_preset)
    if supports_reasoning and family in {"unsupported", "generic"}:
        family = preset_family or family
    if family == "anthropic" and _looks_like_adaptive_anthropic_model(model_id, model_info):
        family = "anthropic_adaptive"
    stripped_params: list[str] = []

    if supports_reasoning:
        for key in ("temperature", "top_p", "top_k"):
            if key in result:
                result.pop(key, None)
                stripped_params.append(key)
        if family == "openai" and "max_tokens" in result and "max_completion_tokens" not in result:
            result["max_completion_tokens"] = result.pop("max_tokens")
            translated_max_tokens = True
        else:
            translated_max_tokens = False
    else:
        translated_max_tokens = False

    requested = normalize_reasoning_effort(
        _coerce_reasoning_value(result.pop("reasoning_effort", None))
    )
    if requested is None:
        if family in {"anthropic", "anthropic_adaptive"}:
            result = _enforce_anthropic_thinking_budget(result)
        return PreparedReasoningConfig(
            request_kwargs=result,
            family=family,
            stripped_params=tuple(stripped_params),
            translated_max_tokens=translated_max_tokens,
        )
    if not supports_reasoning:
        return PreparedReasoningConfig(
            request_kwargs=result,
            family=family,
            stripped_params=tuple(stripped_params),
            translated_max_tokens=translated_max_tokens,
        )

    if requested == "default":
        if family == "anthropic_adaptive":
            result["thinking"] = {"type": "adaptive"}
            effective = "adaptive"
        else:
            effective = None
        return PreparedReasoningConfig(
            request_kwargs=_enforce_anthropic_thinking_budget(result),
            family=family,
            effective_effort=effective,
            stripped_params=tuple(stripped_params),
            translated_max_tokens=translated_max_tokens,
        )

    if requested == "none":
        effective = None
        if family in {"openai", "groq"}:
            result["reasoning_effort"] = "minimal"
            effective = "minimal"
        elif family == "google":
            result["thinking_config"] = {"thinking_budget": 0}
            effective = "none"
        elif family in {"anthropic", "anthropic_adaptive"}:
            result.pop("thinking", None)
        return PreparedReasoningConfig(
            request_kwargs=_enforce_anthropic_thinking_budget(result),
            family=family,
            effective_effort=effective,
            stripped_params=tuple(stripped_params),
            translated_max_tokens=translated_max_tokens,
        )

    effective = _apply_positive_reasoning_level(result, requested=requested, family=family)
    return PreparedReasoningConfig(
        request_kwargs=_enforce_anthropic_thinking_budget(result),
        family=family,
        effective_effort=effective,
        stripped_params=tuple(stripped_params),
        translated_max_tokens=translated_max_tokens,
    )


def detect_reasoning_family(
    model_id: str,
    *,
    provider_preset: str = "",
) -> str:
    """Return the provider family for reasoning translation."""

    return _detect_reasoning_family(model_id, provider_preset)


def _coerce_reasoning_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _supports_reasoning(
    model_info: ModelInfo | None,
    model_id: str,
    provider_preset: str,
) -> bool:
    if model_info is not None:
        if model_info.supports_reasoning:
            return True
        if model_info.model_id == model_id and model_info.model_id != "unknown":
            return False
    return _looks_like_reasoning_model(model_id, provider_preset)


def _looks_like_reasoning_model(model_id: str, provider_preset: str) -> bool:
    family = _detect_reasoning_family(model_id, provider_preset)
    return family != "unsupported"


def _detect_reasoning_family(model_id: str, provider_preset: str) -> str:
    normalized_model = _normalize_model_name(model_id)
    preset = provider_preset.strip().lower()

    if "claude" in normalized_model or preset == "anthropic":
        if not _is_known_anthropic_reasoning_model(normalized_model):
            return "unsupported"
        if any(
            token in normalized_model
            for token in ("opus-4-6", "opus-4.6", "sonnet-4-6", "sonnet-4.6")
        ):
            return "anthropic_adaptive"
        return "anthropic"
    if any(normalized_model.startswith(prefix) for prefix in ("gpt-5", "o1", "o3", "o4")) or (
        preset in _OPENAI_PRESETS
        and any(normalized_model.startswith(prefix) for prefix in ("gpt-5", "o1", "o3", "o4"))
    ):
        return "openai"
    if (
        "gemini-2.5" in normalized_model
        or "gemini_2.5" in normalized_model
        or "gemini-2_5" in normalized_model
        or (preset in _GOOGLE_PRESETS and "gemini-2.5" in normalized_model)
    ):
        return "google"
    if preset == "groq" and re.search(
        r"(reason|think|o1|o3|o4|gpt-5|deepseek-r1|qwq|qwen3)", normalized_model
    ):
        return "groq"
    if re.search(r"(reason|think|deepseek-r1|qwq|qwen3|grok-4|kimi-k2)", normalized_model):
        return "generic"
    return "unsupported"


def _normalize_model_name(model_name: str) -> str:
    lowered = model_name.strip().lower()
    for prefix in ("litellm_proxy/", "openai/", "azure/", "openai_compatible/"):
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def _is_known_anthropic_reasoning_model(normalized_model: str) -> bool:
    return any(
        token in normalized_model
        for token in (
            "claude-3-7",
            "sonnet-4",
            "sonnet-4.5",
            "sonnet-4-5",
            "opus-4",
            "opus-4.5",
            "opus-4-5",
        )
    )


def _looks_like_adaptive_anthropic_model(model_id: str, model_info: ModelInfo | None) -> bool:
    candidates = [model_id]
    if model_info is not None and isinstance(model_info.display_name, str):
        candidates.append(model_info.display_name)
    return any(
        any(
            token in candidate.lower()
            for token in ("opus-4-6", "opus 4.6", "sonnet-4-6", "sonnet 4.6")
        )
        for candidate in candidates
        if isinstance(candidate, str)
    )


def _family_from_provider_preset(provider_preset: str) -> str | None:
    preset = provider_preset.strip().lower()
    if preset == "anthropic":
        return "anthropic"
    if preset in _OPENAI_PRESETS:
        return "openai"
    if preset in _GOOGLE_PRESETS:
        return "google"
    if preset == "groq":
        return "groq"
    return None


def auxiliary_reasoning_effort_for_family(family: str) -> str:
    """Return the default low-cost effort for classifier/evaluator style calls."""

    if family in {"anthropic", "anthropic_adaptive", "google"}:
        return "low"
    return "minimal"


def _apply_positive_reasoning_level(
    request_kwargs: dict[str, Any], *, requested: str, family: str
) -> str | None:
    if family == "unsupported":
        return None
    if family == "openai":
        mapped = "xhigh" if requested == "max" else requested
        request_kwargs["reasoning_effort"] = mapped
        return mapped
    if family in {"anthropic", "anthropic_adaptive"}:
        level = "low" if requested == "minimal" else ("high" if requested == "max" else requested)
        budget = _ANTHROPIC_THINKING_BUDGETS.get(level)
        if budget is None:
            return None
        request_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return level
    if family == "google":
        level = "low" if requested == "minimal" else ("high" if requested == "max" else requested)
        budget = _GOOGLE_THINKING_BUDGETS.get(level)
        if budget is None:
            return None
        request_kwargs["thinking_config"] = {"thinking_budget": budget}
        return level
    if family in {"groq", "generic"}:
        mapped = "high" if requested == "max" else requested
        request_kwargs["reasoning_effort"] = mapped
        return mapped
    return None


def _enforce_anthropic_thinking_budget(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    thinking = request_kwargs.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return request_kwargs
    budget = thinking.get("budget_tokens")
    if not isinstance(budget, int) or budget <= 0:
        return request_kwargs
    max_tokens = request_kwargs.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > budget:
        return request_kwargs
    request_kwargs["max_tokens"] = max(
        _MIN_ANTHROPIC_MAX_TOKENS,
        budget + _ANTHROPIC_BUDGET_BUFFER,
    )
    return request_kwargs
