"""Shared context-budget helpers for prompt assembly and loop pressure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_RESERVE_FLOOR_TOKENS = 2_048
_RESERVE_CEILING_TOKENS = 32_768
_RESERVE_SHARE_DIVISOR = 8
_PROMPT_SERIALIZATION_MARGIN_RATIO = 0.04
_RESPONSES_PROMPT_SERIALIZATION_MARGIN_RATIO = 0.08
_PROMPT_SERIALIZATION_MARGIN_FLOOR_TOKENS = 1_024
_PROMPT_SERIALIZATION_MARGIN_CEILING_TOKENS = 32_768


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Resolved prompt budget for one model/context window."""

    max_context_tokens: int
    max_input_tokens: int
    reserve_output_tokens: int
    effective_reserve_output_tokens: int
    prompt_serialization_margin_tokens: int
    available_prompt_tokens: int
    reserve_clamped: bool


def prompt_serialization_margin_ratio_for_model(
    model_info: Any | None,
    model_id: str | None = None,
) -> float:
    """Return the prompt safety margin ratio for local-token/provider drift.

    OpenAI Responses style models serialize tool and conversation state into a
    provider-side envelope that can be larger than local message token counts.
    Keep the default margin conservative for all providers, and use a larger
    margin when model metadata or model id indicates Responses/GPT-5.x traffic.
    """

    if model_info is not None and bool(getattr(model_info, "supports_responses_api", False)):
        return _RESPONSES_PROMPT_SERIALIZATION_MARGIN_RATIO
    normalized_model = str(model_id or getattr(model_info, "model_id", "") or "").lower()
    if "gpt-5" in normalized_model or "chatgpt-5" in normalized_model:
        return _RESPONSES_PROMPT_SERIALIZATION_MARGIN_RATIO
    return _PROMPT_SERIALIZATION_MARGIN_RATIO


def _prompt_serialization_margin_tokens(
    available_prompt_tokens: int,
    prompt_serialization_margin_ratio: float | None,
) -> int:
    if available_prompt_tokens <= 0:
        return 0
    try:
        ratio = float(
            _PROMPT_SERIALIZATION_MARGIN_RATIO
            if prompt_serialization_margin_ratio is None
            else prompt_serialization_margin_ratio
        )
    except (TypeError, ValueError):
        ratio = _PROMPT_SERIALIZATION_MARGIN_RATIO
    if ratio <= 0:
        return 0
    margin = int(available_prompt_tokens * ratio)
    margin = max(_PROMPT_SERIALIZATION_MARGIN_FLOOR_TOKENS, margin)
    margin = min(_PROMPT_SERIALIZATION_MARGIN_CEILING_TOKENS, margin)
    return min(margin, max(0, available_prompt_tokens - 1))


def requested_output_tokens(
    agent_max_tokens: int | None,
    model_max_output_tokens: int | None,
) -> int:
    """Return the requested LLM output ceiling before controller-side adjustment."""

    if isinstance(agent_max_tokens, int) and agent_max_tokens > 0:
        return agent_max_tokens
    if isinstance(model_max_output_tokens, int) and model_max_output_tokens > 0:
        return model_max_output_tokens
    return 0


def resolve_context_budget(
    *,
    max_context_tokens: int,
    max_input_tokens: int | None = None,
    agent_max_tokens: int | None,
    model_max_output_tokens: int | None,
    prompt_serialization_margin_ratio: float | None = None,
) -> ContextBudget:
    """Resolve model window, requested output ceiling, and prompt reserve.

    The provider may still receive the full requested output ceiling. This
    helper only decides how much prompt headroom the controller reserves when
    assembling context and enforcing tool-loop pressure checks.
    """

    max_context_tokens = max(0, int(max_context_tokens or 0))
    max_input_tokens = max(0, int(max_input_tokens or 0))
    reserve_output_tokens = requested_output_tokens(agent_max_tokens, model_max_output_tokens)
    effective_reserve_output_tokens = reserve_output_tokens
    reserve_clamped = False

    if max_context_tokens > 0 and reserve_output_tokens > 0:
        target_reserve = min(_RESERVE_CEILING_TOKENS, max_context_tokens // _RESERVE_SHARE_DIVISOR)
        target_reserve = max(_RESERVE_FLOOR_TOKENS, target_reserve)
        effective_reserve_output_tokens = min(reserve_output_tokens, target_reserve)
        if effective_reserve_output_tokens >= max_context_tokens:
            effective_reserve_output_tokens = max(1, max_context_tokens // 4)
        reserve_clamped = effective_reserve_output_tokens != reserve_output_tokens

    total_prompt_tokens = max(0, max_context_tokens - effective_reserve_output_tokens)
    raw_available_prompt_tokens = total_prompt_tokens
    if max_input_tokens > 0:
        raw_available_prompt_tokens = min(max_input_tokens, total_prompt_tokens)
    prompt_serialization_margin_tokens = _prompt_serialization_margin_tokens(
        raw_available_prompt_tokens,
        prompt_serialization_margin_ratio,
    )
    available_prompt_tokens = max(
        0,
        raw_available_prompt_tokens - prompt_serialization_margin_tokens,
    )
    return ContextBudget(
        max_context_tokens=max_context_tokens,
        max_input_tokens=max_input_tokens,
        reserve_output_tokens=reserve_output_tokens,
        effective_reserve_output_tokens=effective_reserve_output_tokens,
        prompt_serialization_margin_tokens=prompt_serialization_margin_tokens,
        available_prompt_tokens=available_prompt_tokens,
        reserve_clamped=reserve_clamped,
    )
