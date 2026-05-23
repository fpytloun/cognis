"""Reasoning effort normalization and provider translation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cognis.models.config import ModelInfo, normalize_reasoning_level

_OPENAI_PRESETS = {"openai", "openai_compatible", "litellm_proxy", "azure", "chatgpt"}
_GOOGLE_PRESETS = {"gemini", "google", "vertex_ai"}
_ANTHROPIC_BUDGET_BUFFER = 1024
_MIN_ANTHROPIC_MAX_TOKENS = 2048
_THINKING_EFFORT_ORDER: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
_ANTHROPIC_THINKING_BUDGETS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "max": 32768,
}
_GOOGLE_THINKING_BUDGETS: dict[str, int] = {
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


@dataclass(slots=True)
class ReasoningProfile:
    """Canonical Cognis thinking-effort options for a concrete model."""

    family: str
    supports_reasoning: bool
    available_efforts: tuple[str, ...] = ()
    native_efforts: tuple[str, ...] = ()

    @property
    def positive_efforts(self) -> tuple[str, ...]:
        return tuple(level for level in self.available_efforts if level not in {"default", "none"})


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Return a canonical normalized reasoning level or ``None``."""

    return normalize_reasoning_level(value)


def reasoning_efforts_for_model(
    model_id: str,
    *,
    provider_preset: str = "",
    model_info: ModelInfo | None = None,
    supports_reasoning: bool,
) -> list[str]:
    """Return the normalized user-facing reasoning levels for a model."""

    profile = build_reasoning_profile(
        model_id,
        provider_preset=provider_preset,
        model_info=model_info,
        supports_reasoning=supports_reasoning,
    )
    return list(profile.available_efforts)


def enrich_model_entry(
    entry: dict[str, Any],
    *,
    provider_preset: str = "",
) -> dict[str, Any]:
    """Populate derived capability fields on a stored model entry.

    This runs on read so provider list payloads and on-demand ``get_model_info``
    return consistent values without requiring admins to re-save their
    provider configurations. Currently fills:

    - ``reasoning_efforts`` — computed from ``supports_reasoning`` + model id

    Explicitly-configured values are preserved: the helper only fills fields
    that are missing or empty.
    """

    if not isinstance(entry, dict):
        return entry

    model_id = entry.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        return entry

    supports_reasoning = bool(entry.get("supports_reasoning"))
    existing_efforts = entry.get("reasoning_efforts")
    if supports_reasoning and (not isinstance(existing_efforts, list) or not existing_efforts):
        try:
            preview_source = {
                key: value for key, value in entry.items() if key in ModelInfo.model_fields
            }
            preview_source.setdefault("model_id", model_id)
            model_info = ModelInfo.model_validate(preview_source)
        except Exception:
            model_info = None
        entry["reasoning_efforts"] = reasoning_efforts_for_model(
            model_id,
            provider_preset=provider_preset,
            model_info=model_info,
            supports_reasoning=True,
        )
    return entry


def remap_reasoning_effort_to_available(
    value: str | None,
    *,
    available_efforts: list[str] | tuple[str, ...],
) -> str | None:
    """Return the closest model-valid thinking effort from an available set."""

    requested = normalize_reasoning_effort(value)
    if requested is None:
        return None
    profile = ReasoningProfile(
        family="generic",
        supports_reasoning=bool(available_efforts),
        available_efforts=tuple(available_efforts),
    )
    return _resolve_requested_effort(requested, profile)


def build_reasoning_profile(
    model_id: str,
    *,
    provider_preset: str = "",
    model_info: ModelInfo | None = None,
    supports_reasoning: bool,
) -> ReasoningProfile:
    """Return the concrete reasoning profile for a model/provider pair."""

    if not supports_reasoning:
        return ReasoningProfile(family="unsupported", supports_reasoning=False)

    family = detect_reasoning_family(model_id, provider_preset=provider_preset)
    preset_family = _family_from_provider_preset(provider_preset)
    if family in {"unsupported", "generic"} and preset_family is not None:
        family = preset_family
    if family == "anthropic" and _looks_like_adaptive_anthropic_model(model_id, model_info):
        family = "anthropic_adaptive"

    if family == "openai":
        positive = ["low", "medium", "high"]
        if _supports_openai_xhigh(model_id, model_info):
            positive.append("xhigh")
        available = ["default"]
        if _supports_openai_none(model_id, model_info):
            available.append("none")
        available.extend(positive)
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=tuple(available),
            native_efforts=tuple(level for level in available if level != "default"),
        )

    if family == "anthropic_adaptive":
        positive = ["low", "medium", "high"]
        if _supports_anthropic_xhigh(model_id, model_info):
            positive.append("xhigh")
        positive.append("max")
        available = ("default", "none", *positive)
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=available,
            native_efforts=tuple(level for level in available if level != "default"),
        )

    if family == "anthropic":
        available = ("default", "none", "low", "medium", "high", "max")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=available,
            native_efforts=("none", "low", "medium", "high", "max"),
        )

    if family == "google":
        available = ("default", "none", "low", "medium", "high", "max")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=available,
            native_efforts=("none", "low", "medium", "high", "max"),
        )

    if family == "generic":
        available = ("default", "none", "low", "medium", "high")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=available,
            native_efforts=("none", "low", "medium", "high"),
        )
    if family == "groq":
        available = ("default", "none", "low", "medium", "high")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=available,
            native_efforts=("low", "medium", "high"),
        )

    return ReasoningProfile(family="unsupported", supports_reasoning=False)


def auxiliary_reasoning_effort_for_model(
    model_id: str,
    *,
    provider_preset: str = "",
    model_info: ModelInfo | None = None,
    supports_reasoning: bool,
) -> str | None:
    """Return the cheapest model-valid Cognis thinking effort for a call."""

    profile = build_reasoning_profile(
        model_id,
        provider_preset=provider_preset,
        model_info=model_info,
        supports_reasoning=supports_reasoning,
    )
    if "none" in profile.available_efforts:
        return "none"
    positive = profile.positive_efforts
    return positive[0] if positive else None


def apply_reasoning_config(
    request_kwargs: dict[str, Any],
    *,
    model_id: str,
    provider_preset: str = "",
    model_info: ModelInfo | None = None,
) -> PreparedReasoningConfig:
    """Translate normalized reasoning settings into provider-ready kwargs."""

    result = dict(request_kwargs)
    supports_reasoning = _supports_reasoning(model_info, model_id, provider_preset)
    profile = build_reasoning_profile(
        model_id,
        provider_preset=provider_preset,
        model_info=model_info,
        supports_reasoning=supports_reasoning,
    )
    stripped_params: list[str] = []

    if supports_reasoning:
        for key in ("temperature", "top_p", "top_k"):
            if key in result:
                result.pop(key, None)
                stripped_params.append(key)
        if (
            profile.family == "openai"
            and "max_tokens" in result
            and "max_completion_tokens" not in result
        ):
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
        if profile.family == "anthropic":
            result = _enforce_anthropic_thinking_budget(result)
        return PreparedReasoningConfig(
            request_kwargs=result,
            family=profile.family,
            stripped_params=tuple(stripped_params),
            translated_max_tokens=translated_max_tokens,
        )
    if not supports_reasoning:
        return PreparedReasoningConfig(
            request_kwargs=result,
            family=profile.family,
            stripped_params=tuple(stripped_params),
            translated_max_tokens=translated_max_tokens,
        )

    resolved = _resolve_requested_effort(requested, profile)
    effective = _apply_resolved_effort(result, resolved=resolved, profile=profile)
    if profile.family == "anthropic":
        result = _enforce_anthropic_thinking_budget(result)
    return PreparedReasoningConfig(
        request_kwargs=result,
        family=profile.family,
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
    for prefix in ("litellm_proxy/", "openai/", "azure/", "openai_compatible/", "chatgpt/"):
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def _candidate_model_names(model_id: str, model_info: ModelInfo | None) -> list[str]:
    candidates = [model_id]
    if (
        model_info is not None
        and isinstance(model_info.display_name, str)
        and model_info.display_name
    ):
        candidates.append(model_info.display_name)
    return candidates


def _is_known_anthropic_reasoning_model(normalized_model: str) -> bool:
    return any(
        token in normalized_model
        for token in (
            "claude-3-7",
            "sonnet-4",
            "sonnet-4.5",
            "sonnet-4-5",
            "sonnet-4-6",
            "sonnet-4.6",
            "opus-4",
            "opus-4.5",
            "opus-4-5",
            "opus-4-6",
            "opus-4.6",
            "opus-4-7",
            "opus-4.7",
        )
    )


def _anthropic_series_version(model_name: str) -> tuple[str, int, int | None] | None:
    normalized = model_name.lower().replace("_", "-")
    match = re.search(r"(opus|sonnet)[\s-]+(\d+)(?:[.-](\d+))?", normalized)
    if match is None:
        return None
    family = match.group(1)
    major = int(match.group(2))
    raw_minor = match.group(3)
    minor = int(raw_minor) if raw_minor is not None and len(raw_minor) <= 2 else None
    return family, major, minor


def _looks_like_adaptive_anthropic_model(model_id: str, model_info: ModelInfo | None) -> bool:
    for candidate in _candidate_model_names(model_id, model_info):
        version = _anthropic_series_version(candidate)
        if version is None:
            continue
        _, major, minor = version
        if major > 4:
            return True
        if major == 4 and minor is not None and minor >= 6:
            return True
    return False


def _supports_anthropic_xhigh(model_id: str, model_info: ModelInfo | None) -> bool:
    for candidate in _candidate_model_names(model_id, model_info):
        version = _anthropic_series_version(candidate)
        if version is None:
            continue
        family, major, minor = version
        if family != "opus":
            continue
        if major > 4:
            return True
        if major == 4 and minor is not None and minor >= 7:
            return True
    return False


def _looks_like_gpt5_candidate(model_name: str) -> bool:
    normalized = _normalize_model_name(model_name).replace("_", "-").replace(" ", "-")
    return normalized.startswith("gpt-5")


def _supports_openai_none(model_id: str, model_info: ModelInfo | None) -> bool:
    return any(
        _looks_like_gpt5_candidate(candidate)
        for candidate in _candidate_model_names(model_id, model_info)
    )


def _supports_openai_xhigh(model_id: str, model_info: ModelInfo | None) -> bool:
    return any(
        _looks_like_gpt5_candidate(candidate)
        for candidate in _candidate_model_names(model_id, model_info)
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


def _resolve_requested_effort(requested: str, profile: ReasoningProfile) -> str | None:
    if requested == "default":
        return "default"
    available = profile.available_efforts
    if requested == "none":
        if "none" in available:
            return "none"
        positive = profile.positive_efforts
        return positive[0] if positive else None
    positive = profile.positive_efforts
    if not positive:
        return None
    if requested in positive:
        return requested
    requested_index = _THINKING_EFFORT_ORDER.index(requested)
    return min(
        positive,
        key=lambda candidate: (
            abs(_THINKING_EFFORT_ORDER.index(candidate) - requested_index),
            0 if _THINKING_EFFORT_ORDER.index(candidate) >= requested_index else 1,
            _THINKING_EFFORT_ORDER.index(candidate),
        ),
    )


def _apply_resolved_effort(
    request_kwargs: dict[str, Any],
    *,
    resolved: str | None,
    profile: ReasoningProfile,
) -> str | None:
    if resolved is None:
        return None
    if resolved == "default":
        if profile.family == "anthropic_adaptive":
            request_kwargs["thinking"] = {"type": "adaptive"}
            _remove_output_config_effort(request_kwargs)
            return "adaptive"
        _remove_output_config_effort(request_kwargs)
        return None
    if resolved == "none":
        if profile.family == "openai":
            request_kwargs["reasoning_effort"] = "none"
            return "none"
        if profile.family == "google":
            request_kwargs["thinking_config"] = {"thinking_budget": 0}
            return "none"
        if profile.family in {"anthropic", "anthropic_adaptive"}:
            request_kwargs.pop("thinking", None)
            request_kwargs.pop("thinking_config", None)
            _remove_output_config_effort(request_kwargs)
            return None
        request_kwargs.pop("reasoning_effort", None)
        return None
    if profile.family == "openai":
        request_kwargs["reasoning_effort"] = resolved
        return resolved
    if profile.family == "anthropic_adaptive":
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = _with_output_config_effort(
            request_kwargs.get("output_config"),
            resolved,
        )
        return resolved
    if profile.family == "anthropic":
        budget = _ANTHROPIC_THINKING_BUDGETS.get(resolved)
        if budget is None:
            return None
        request_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return resolved
    if profile.family == "google":
        budget = _GOOGLE_THINKING_BUDGETS.get(resolved)
        if budget is None:
            return None
        request_kwargs["thinking_config"] = {"thinking_budget": budget}
        return resolved
    if profile.family in {"groq", "generic"}:
        if resolved == "none" and resolved not in profile.native_efforts:
            request_kwargs.pop("reasoning_effort", None)
            return None
        mapped = "high" if resolved in {"xhigh", "max"} else resolved
        request_kwargs["reasoning_effort"] = mapped
        return mapped
    return None


def _with_output_config_effort(current: Any, effort: str) -> dict[str, Any]:
    updated = dict(current) if isinstance(current, dict) else {}
    updated["effort"] = effort
    return updated


def _remove_output_config_effort(request_kwargs: dict[str, Any]) -> None:
    output_config = request_kwargs.get("output_config")
    if not isinstance(output_config, dict):
        return
    updated = dict(output_config)
    updated.pop("effort", None)
    if updated:
        request_kwargs["output_config"] = updated
    else:
        request_kwargs.pop("output_config", None)


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
