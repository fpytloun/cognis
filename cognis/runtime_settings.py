"""Coherent live-application path for DB-backed settings."""

from __future__ import annotations

from typing import Any, Final, cast

HOT_APPLIED_SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "session.compaction_threshold",
        "session.step_timeout_seconds",
        "session.llm_stream_idle_timeout_seconds",
        "session.llm_stream_max_retries",
        "session.anthropic_cache_ttl",
        "session.memory_instructions_max_tokens",
        "session.core_memories_max_tokens",
        "session.immutable_prefix_repair_cooldown_seconds",
        "session.recall_ttl_seconds",
        "session.max_tool_calls_per_turn",
        "session.max_llm_cycles_per_turn",
        "session.max_delegation_depth",
        "session.cache_max_entries",
        "decision_engine.inline_max_length",
        "evaluator.timeout_ms",
        "security.non_bypassable_tools",
        "security.api_read_requests_per_minute",
        "security.api_write_requests_per_minute",
        "security.token_ttl_seconds",
        "security.ws_auth_timeout_seconds",
    }
)


async def apply_runtime_setting(app: Any, key: str, value: object) -> None:
    """Apply a committed setting to this worker's subsequent operations."""

    state = app.state
    raw = cast(Any, value)
    if key == "session.compaction_threshold":
        state.context_assembler.compaction_threshold = float(raw)
        await state.compaction_strategy.refresh_settings()
    elif key == "session.step_timeout_seconds":
        state.agent_loop.default_step_timeout_seconds = max(1, int(raw))
    elif key == "session.llm_stream_idle_timeout_seconds":
        state.agent_loop.default_llm_stream_idle_timeout_seconds = max(1, int(raw))
    elif key == "session.llm_stream_max_retries":
        state.agent_loop.default_llm_stream_max_retries = max(0, int(raw))
    elif key == "session.anthropic_cache_ttl":
        state.agent_loop.default_anthropic_cache_ttl = str(raw)
    elif key == "session.memory_instructions_max_tokens":
        state.context_assembler.memory_instructions_max_tokens = max(1, int(raw))
        await state.session_cache.mark_all_prefix_repairs_needed()
    elif key == "session.core_memories_max_tokens":
        state.context_assembler.core_memories_max_tokens = max(1, int(raw))
        await state.session_cache.mark_all_prefix_repairs_needed()
    elif key == "session.immutable_prefix_repair_cooldown_seconds":
        state.context_assembler.immutable_prefix_repair_cooldown_seconds = max(0, int(raw))
    elif key == "session.recall_ttl_seconds":
        state.context_assembler.recall_ttl_seconds = max(1, int(raw))
    elif key == "session.max_tool_calls_per_turn":
        state.agent_loop.default_max_tool_calls_per_turn = max(1, int(raw))
    elif key == "session.max_llm_cycles_per_turn":
        state.agent_loop.default_max_llm_cycles_per_turn = max(1, int(raw))
    elif key == "session.max_delegation_depth":
        state.decision_engine.max_delegation_depth = max(1, int(raw))
    elif key == "session.cache_max_entries":
        await state.session_cache.resize(max(1, int(raw)))
    elif key == "decision_engine.inline_max_length":
        state.decision_engine.inline_max_length = max(1, int(raw))
    elif key == "evaluator.timeout_ms":
        state.step_evaluator.evaluator_timeout_seconds = max(1, int(raw)) / 1000
    elif key == "security.non_bypassable_tools":
        state.tool_router.non_bypassable_patterns = list(raw)
        state.tool_router._decision_cache.clear()
    elif key == "security.api_read_requests_per_minute":
        state.api_rate_limiter.update_limits(read_requests_per_minute=int(raw))
    elif key == "security.api_write_requests_per_minute":
        state.api_rate_limiter.update_limits(write_requests_per_minute=int(raw))
    elif key == "security.token_ttl_seconds":
        state.auth_provider.token_ttl_seconds = max(1, int(raw))
    elif key == "security.ws_auth_timeout_seconds":
        state.ws_auth_timeout_seconds = max(1, int(raw))
    else:
        raise ValueError(f"Setting {key} has no live application handler")
