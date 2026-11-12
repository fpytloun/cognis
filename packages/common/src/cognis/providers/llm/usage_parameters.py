"""Content-free scalar projection at requested and normalized wire boundaries."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ValidationError

from cognis.models.usage import UsageModelParameters, UsageParameterProvenance

_ALIASES = {
    "max_tokens": "max_output_tokens",
    "max_completion_tokens": "max_output_tokens",
    "max_output_tokens": "max_output_tokens",
    "max_input_tokens": "max_input_tokens",
    "max_total_tokens": "max_total_tokens",
}
_NUMERIC = {
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "reasoning_budget",
    *_ALIASES.values(),
}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "auto", "default"}
_TIERS = {"auto", "default", "standard", "priority", "flex", "batch", "scale"}


def project_parameters(
    kwargs: Mapping[str, Any],
    *,
    source: Literal[
        "request",
        "adapter_override",
        "model_config",
        "route",
        "profile",
        "provider_default",
        "unknown",
    ] = "request",
) -> UsageModelParameters:
    """Project typed scalar evidence, never schemas, named tools or stop strings.

    Adapter evidence describes what the adapter sent, not what the provider
    honored. Missing settings remain unknown, with no invented provider defaults.
    """
    values: dict[str, Any] = {}
    for key, value in kwargs.items():
        name = _ALIASES.get(key, key)
        if name not in _NUMERIC or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        if isinstance(value, int) and not -(2**63) <= value <= 2**63 - 1:
            continue
        if not math.isfinite(value):
            continue
        try:
            checked = UsageModelParameters.model_validate({name: value})
        except ValidationError:
            continue
        values[name] = getattr(checked, name)

    reasoning = kwargs.get("reasoning")
    effort = kwargs.get("reasoning_effort")
    if isinstance(reasoning, Mapping):
        effort = reasoning.get("effort", effort)
    if isinstance(effort, str) and effort in _EFFORTS:
        values["reasoning_effort"] = effort
    thinking = kwargs.get("thinking")
    enabled = kwargs.get("reasoning_enabled")
    if type(enabled) is bool:
        values["reasoning_enabled"] = enabled
    if isinstance(thinking, Mapping):
        if thinking.get("type") in ("enabled", "disabled", "adaptive"):
            values["reasoning_enabled"] = thinking["type"] != "disabled"
        budget = thinking.get("budget_tokens")
        if type(budget) is int and 0 <= budget <= 2**63 - 1:
            values["reasoning_budget"] = budget
    tier = kwargs.get("service_tier")
    if isinstance(tier, str) and tier in _TIERS:
        values["service_tier"] = tier
    size = kwargs.get("size") or kwargs.get("image_size")
    if isinstance(size, str) and size in {
        "auto",
        "256x256",
        "512x512",
        "1024x1024",
        "1024x1536",
        "1536x1024",
        "1024x1792",
        "1792x1024",
    }:
        values["image_size"] = size
    quality = kwargs.get("quality") or kwargs.get("image_quality")
    if isinstance(quality, str) and quality in {"auto", "standard", "hd", "low", "medium", "high"}:
        values["image_quality"] = quality
    response_format = kwargs.get("response_format")
    if isinstance(response_format, Mapping):
        mode = response_format.get("type")
        values["response_format"] = (
            mode if mode in ("text", "json_object", "json_schema") else "other"
        )
    elif isinstance(response_format, str):
        values["response_format"] = (
            response_format
            if response_format in ("text", "json_object", "json_schema")
            else "other"
        )
    choice = kwargs.get("tool_choice")
    if isinstance(choice, str):
        values["tool_choice"] = choice if choice in ("auto", "none", "required") else "other"
    elif isinstance(choice, Mapping):
        mode = choice.get("type")
        values["tool_choice"] = (
            "auto"
            if mode == "auto"
            else "required"
            if mode == "any"
            else "named"
            if mode in ("tool", "function")
            else "other"
        )
    provenance = tuple(UsageParameterProvenance(parameter=name, source=source) for name in values)
    return UsageModelParameters.model_validate({**values, "provenance": provenance})
