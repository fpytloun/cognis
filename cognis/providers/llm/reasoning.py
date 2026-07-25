"""Reasoning effort normalization and provider translation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cognis.models.config import ModelInfo, normalize_reasoning_level

_OPENAI_PRESETS = {"openai", "openai_compatible", "litellm_proxy", "azure", "chatgpt"}
_GOOGLE_PRESETS = {"gemini", "google", "vertex_ai"}
_ANTHROPIC_MIN_VISIBLE_OUTPUT_TOKENS = 4096
_MIN_ANTHROPIC_MAX_TOKENS = 2048
_ANTHROPIC_ADAPTIVE_MODEL_KEYS = frozenset(
    {
        "fable-5",
        "mythos-5",
        "mythos-preview",
        "opus-4-6",
        "opus-4-7",
        "opus-4-8",
        "sonnet-4-6",
        "sonnet-5",
    }
)
_ANTHROPIC_LEGACY_MODEL_KEYS = frozenset(
    {
        "3-5-haiku",
        "3-7-sonnet",
        "haiku-3-5",
        "haiku-4-5",
        "opus-4",
        "opus-4-1",
        "opus-4-5",
        "sonnet-4",
        "sonnet-4-5",
    }
)
_ANTHROPIC_ALWAYS_ON_MODEL_KEYS = frozenset({"fable-5", "mythos-5", "mythos-preview"})
_ANTHROPIC_XHIGH_MODEL_KEYS = frozenset({"fable-5", "mythos-5", "opus-4-7", "opus-4-8", "sonnet-5"})
_THINKING_EFFORT_ORDER: tuple[str, ...] = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
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

    - ``supports_embedding`` — inferred from known embedding model names
    - ``supports_reasoning`` — inferred from known reasoning model names
    - ``reasoning_efforts`` — computed from model id/provider family

    Explicitly-configured values are preserved: the helper only fills fields
    that are missing or empty.
    """

    if not isinstance(entry, dict):
        return entry

    model_id = entry.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        return entry

    if "supports_embedding" not in entry and looks_like_embedding_model(model_id):
        entry["supports_embedding"] = True

    inferred_reasoning = "supports_reasoning" not in entry and _looks_like_reasoning_model(
        model_id, provider_preset
    )
    supports_reasoning = bool(entry.get("supports_reasoning")) or inferred_reasoning
    if inferred_reasoning:
        entry["supports_reasoning"] = True
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


def looks_like_embedding_model(model_name: str) -> bool:
    normalized = model_name.strip().lower().replace("_", "-")
    return any(
        token in normalized
        for token in (
            "embedding",
            "embed-",
            "-embed",
            "e5-",
            "bge-",
            "gte-",
            "nomic-embed",
        )
    )


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
    if (
        family in {"unsupported", "generic"}
        and preset_family is not None
        and (
            preset_family != "anthropic"
            or any(
                _is_known_anthropic_reasoning_model(candidate)
                for candidate in _candidate_model_names(model_id, model_info)
            )
        )
    ):
        family = preset_family
    if family == "anthropic" and _looks_like_adaptive_anthropic_model(model_id, model_info):
        family = "anthropic_adaptive"

    if family == "openai":
        positive = _positive_model_info_reasoning_efforts(model_info)
        if not positive:
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
        adaptive_available = ["default"]
        if _supports_anthropic_disabled(model_id, model_info):
            adaptive_available.append("none")
        adaptive_available.extend(positive)
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=tuple(adaptive_available),
            native_efforts=tuple(level for level in adaptive_available if level != "default"),
        )

    if family == "anthropic":
        legacy_anthropic_available = ("default", "none", "low", "medium", "high", "max")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=legacy_anthropic_available,
            native_efforts=("none", "low", "medium", "high", "max"),
        )

    if family == "google":
        google_available = ("default", "none", "low", "medium", "high", "max")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=google_available,
            native_efforts=("none", "low", "medium", "high", "max"),
        )

    if family == "generic":
        generic_available = ("default", "none", "low", "medium", "high")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=generic_available,
            native_efforts=("none", "low", "medium", "high"),
        )
    if family == "groq":
        groq_available = ("default", "none", "low", "medium", "high")
        return ReasoningProfile(
            family=family,
            supports_reasoning=True,
            available_efforts=groq_available,
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

    if (
        profile.family == "openai"
        and "max_tokens" in result
        and "max_completion_tokens" not in result
    ):
        result["max_completion_tokens"] = result.pop("max_tokens")
        translated_max_tokens = True
    else:
        translated_max_tokens = False

    had_reasoning_effort = "reasoning_effort" in result
    raw_reasoning_effort = result.pop("reasoning_effort", None)
    requested = normalize_reasoning_effort(_coerce_reasoning_value(raw_reasoning_effort))
    if profile.family in {"anthropic", "anthropic_adaptive"} and "thinking" in result:
        # A native Anthropic thinking object is authoritative. The generic
        # Cognis effort shorthand must never rewrite it, but provider
        # compatibility cleanup and manual-budget validation still apply.
        thinking = result.get("thinking")
        thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
        if profile.family == "anthropic_adaptive" or thinking_type in {"adaptive", "enabled"}:
            stripped_params = _strip_sampling_params(result)
        if thinking_type == "enabled":
            result = _enforce_anthropic_thinking_budget(result, model_info=model_info)
        return PreparedReasoningConfig(
            request_kwargs=result,
            family=profile.family,
            stripped_params=tuple(stripped_params),
            translated_max_tokens=translated_max_tokens,
        )
    if (
        requested is None
        and (not had_reasoning_effort or raw_reasoning_effort is None)
        and profile.family == "anthropic_adaptive"
    ):
        requested = "default"
    if requested is None:
        if _is_openai_reasoning_model(model_id):
            stripped_params = _strip_sampling_params(result)
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

    if profile.family == "unsupported" and requested != "default":
        raise ValueError(f"Reasoning effort {requested!r} is not supported by model {model_id!r}")
    if profile.family == "anthropic_adaptive" and requested not in profile.available_efforts:
        available = ", ".join(profile.available_efforts)
        raise ValueError(
            f"Reasoning effort {requested!r} is not supported by Anthropic model "
            f"{model_id!r}; available levels: {available}"
        )
    resolved = _resolve_requested_effort(requested, profile)
    stripped_params = _strip_sampling_params(result)
    effective = _apply_resolved_effort(result, resolved=resolved, profile=profile)
    if profile.family == "anthropic":
        result = _enforce_anthropic_thinking_budget(result, model_info=model_info)
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
        if getattr(model_info, "supports_reasoning", False):
            return True
        info_model_id = getattr(model_info, "model_id", "unknown")
        if info_model_id == model_id and info_model_id != "unknown":
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


def _is_openai_reasoning_model(model_id: str) -> bool:
    normalized = model_id.lower()
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def _strip_sampling_params(request_kwargs: dict[str, Any]) -> list[str]:
    stripped: list[str] = []
    for key in ("temperature", "top_p", "top_k"):
        if key in request_kwargs:
            request_kwargs.pop(key, None)
            stripped.append(key)
    return stripped


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
        _matches_anthropic_model_key(normalized_model, key)
        for key in _ANTHROPIC_ADAPTIVE_MODEL_KEYS | _ANTHROPIC_LEGACY_MODEL_KEYS
    )


def looks_like_anthropic_reasoning_model(model_id: str) -> bool:
    """Return whether a model ID matches a documented Anthropic reasoning model."""

    return _is_known_anthropic_reasoning_model(_normalize_model_name(model_id))


def _normalized_anthropic_candidate(model_name: str) -> str:
    return re.sub(r"[\s._/]+", "-", model_name.strip().lower())


def _matches_anthropic_model_key(candidate: str, key: str) -> bool:
    normalized = _normalized_anthropic_candidate(candidate)
    for prefix in (f"claude-{key}", key):
        start = 0
        while True:
            index = normalized.find(prefix, start)
            if index < 0:
                break
            before = normalized[index - 1] if index > 0 else ""
            if before and before.isalnum():
                start = index + 1
                continue
            suffix = normalized[index + len(prefix) :]
            if (
                not suffix
                or suffix == "-alias"
                or re.fullmatch(r"-\d{8}(?:-[a-z0-9:-]+)?", suffix)
                or re.fullmatch(r"-v\d+(?::\d+)?", suffix)
            ):
                return True
            start = index + 1
    return False


def _adaptive_anthropic_model_key(model_id: str, model_info: ModelInfo | None) -> str | None:
    for candidate in _candidate_model_names(model_id, model_info):
        for key in _ANTHROPIC_ADAPTIVE_MODEL_KEYS:
            if _matches_anthropic_model_key(candidate, key):
                return key
    return None


def _looks_like_adaptive_anthropic_model(model_id: str, model_info: ModelInfo | None) -> bool:
    return _adaptive_anthropic_model_key(model_id, model_info) is not None


def _supports_anthropic_xhigh(model_id: str, model_info: ModelInfo | None) -> bool:
    return _adaptive_anthropic_model_key(model_id, model_info) in _ANTHROPIC_XHIGH_MODEL_KEYS


def _supports_anthropic_disabled(model_id: str, model_info: ModelInfo | None) -> bool:
    key = _adaptive_anthropic_model_key(model_id, model_info)
    return key is not None and key not in _ANTHROPIC_ALWAYS_ON_MODEL_KEYS


def reasoning_mode_for_model(
    model_id: str,
    *,
    model_info: ModelInfo | None = None,
    requested_effort: str | None = None,
) -> str | None:
    """Return observable Anthropic reasoning mode for runtime metadata."""

    if not _supports_reasoning(model_info, model_id, "anthropic"):
        return None
    key = _adaptive_anthropic_model_key(model_id, model_info)
    if key is None:
        return None
    requested = normalize_reasoning_effort(requested_effort)
    if requested == "none":
        return "disabled" if key not in _ANTHROPIC_ALWAYS_ON_MODEL_KEYS else None
    return "adaptive"


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


def _positive_model_info_reasoning_efforts(model_info: ModelInfo | None) -> list[str]:
    if model_info is None:
        return []
    values: list[str] = []
    for raw_effort in model_info.reasoning_efforts:
        effort = normalize_reasoning_effort(raw_effort)
        if effort is None or effort in {"default", "none"} or effort in values:
            continue
        values.append(effort)
    return values


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
        if profile.family == "anthropic_adaptive":
            request_kwargs["thinking"] = {"type": "disabled"}
            request_kwargs.pop("thinking_config", None)
            _remove_output_config_effort(request_kwargs)
            return "none"
        if profile.family == "anthropic":
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
        mapped = "high" if resolved in {"xhigh", "max", "ultra"} else resolved
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


def _enforce_anthropic_thinking_budget(
    request_kwargs: dict[str, Any],
    *,
    model_info: ModelInfo | None,
) -> dict[str, Any]:
    thinking = request_kwargs.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return request_kwargs
    budget = thinking.get("budget_tokens")
    if not isinstance(budget, int) or budget <= 0:
        return request_kwargs
    requested_max_tokens = request_kwargs.get("max_tokens")
    if not isinstance(requested_max_tokens, int):
        requested_max_tokens = None
    minimum = max(
        _MIN_ANTHROPIC_MAX_TOKENS,
        budget + max(_ANTHROPIC_MIN_VISIBLE_OUTPUT_TOKENS, budget // 2),
    )
    target = max(requested_max_tokens or 0, minimum)
    model_max = _model_max_output_tokens(model_info)
    if model_max is not None:
        target = min(target, model_max)
    if requested_max_tokens != target:
        request_kwargs["max_tokens"] = target
    return request_kwargs


def _model_max_output_tokens(model_info: ModelInfo | None) -> int | None:
    if model_info is None:
        return None
    for attr in (
        "max_output_tokens",
        "max_completion_tokens",
        "output_token_limit",
        "max_tokens",
    ):
        value = getattr(model_info, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    return None
