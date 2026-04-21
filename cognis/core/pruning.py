"""Per-turn fallback pruning of projected tool outputs from the LLM context."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cognis.core.context_projection import default_token_estimate, prune_projected_messages

PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MINIMUM_TOKENS = 20_000
_ARG_CLEAR_THRESHOLD = 1_000  # clear tool call arguments above this size


def prune_tool_outputs(
    messages: list[dict[str, Any]],
    *,
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    minimum_savings: int = PRUNE_MINIMUM_TOKENS,
    min_index_to_modify: int = 0,
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
    token_counter: Callable[[str], int] | None = None,
) -> list[dict[str, Any]]:
    """Fallback pruning for the mutable tail of a projected transcript."""

    return prune_projected_messages(
        messages,
        protect_tokens=protect_tokens,
        minimum_savings=minimum_savings,
        min_index_to_modify=min_index_to_modify,
        arg_clear_threshold=arg_clear_threshold,
        token_counter=token_counter or default_token_estimate,
    )
