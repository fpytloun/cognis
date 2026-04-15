"""Reasoning effort normalization and provider translation helpers."""

from __future__ import annotations

import re
from typing import Any

from cognis.models.config import NORMALIZED_REASONING_LEVELS, ModelInfo

_OPENAI_PRESETS = {"openai", "openai_compatible", "litellm_proxy", "azure"}
_GOOGLE_PRESETS = {"gemini", "google", "vertex_ai"}


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

    if not supports_reasoning and not _looks_like_reasoning_model(model_id, provider_preset):
        return []
    return list(NORMALIZED_REASONING_LEVELS)


def apply_reasoning_config(
    request_kwargs: dict[str, Any],
    *,
    model_id: str,
    provider_preset: str = "",
    model_info: ModelInfo | None = None,
) -> dict[str, Any]:
    """Translate normalized reasoning settings into LiteLLM request kwargs."""

    result = dict(request_kwargs)
    requested = normalize_reasoning_effort(
        _coerce_reasoning_value(result.pop("reasoning_effort", None))
    )
    if requested is None:
        return result

    family = _detect_reasoning_family(model_id, provider_preset)
    if requested == "default":
        if family == "anthropic_adaptive":
            result["thinking"] = {"type": "adaptive"}
        elif _supports_reasoning(model_info, model_id, provider_preset):
            result["reasoning_effort"] = "default"
        return result

    if requested == "none":
        if family in {"openai", "groq"}:
            result["reasoning_effort"] = "none"
        return result

    mapped = _map_positive_effort(requested, family)
    if mapped is None:
        return result
    result["reasoning_effort"] = mapped
    return result


def _coerce_reasoning_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _supports_reasoning(
    model_info: ModelInfo | None,
    model_id: str,
    provider_preset: str,
) -> bool:
    if model_info is not None and model_info.supports_reasoning:
        return True
    return _looks_like_reasoning_model(model_id, provider_preset)


def _looks_like_reasoning_model(model_id: str, provider_preset: str) -> bool:
    family = _detect_reasoning_family(model_id, provider_preset)
    return family != "unsupported"


def _detect_reasoning_family(model_id: str, provider_preset: str) -> str:
    normalized_model = _normalize_model_name(model_id)
    preset = provider_preset.strip().lower()

    if "claude" in normalized_model or preset == "anthropic":
        if any(
            token in normalized_model
            for token in ("opus-4-6", "opus-4.6", "sonnet-4-6", "sonnet-4.6")
        ):
            return "anthropic_adaptive"
        return "anthropic"
    if (
        any(normalized_model.startswith(prefix) for prefix in ("gpt-", "o1", "o3", "o4"))
        or preset in _OPENAI_PRESETS
    ):
        return "openai"
    if "gemini" in normalized_model or preset in _GOOGLE_PRESETS:
        return "google"
    if preset == "groq":
        return "groq"
    if re.search(r"(reason|think)", normalized_model):
        return "generic"
    return "unsupported"


def _normalize_model_name(model_name: str) -> str:
    lowered = model_name.strip().lower()
    for prefix in ("litellm_proxy/", "openai/", "azure/", "openai_compatible/"):
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def _map_positive_effort(level: str, family: str) -> str | None:
    if family == "unsupported":
        return None
    if family == "openai":
        if level == "max":
            return "xhigh"
        return "low" if level == "minimal" else level
    if family == "anthropic_adaptive":
        return "low" if level == "minimal" else level
    if family == "anthropic":
        if level == "max":
            return "high"
        return level
    if family == "google":
        if level == "minimal":
            return "low"
        if level == "max":
            return "high"
        return level
    if family in {"groq", "generic"}:
        if level == "minimal":
            return "low"
        if level == "max":
            return "high"
        return level
    return None
