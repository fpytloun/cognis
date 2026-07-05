"""Validation helpers for DB-backed application settings."""

from __future__ import annotations

from typing import Any

from cognis.bootstrap import DEFAULT_SETTINGS

_POSITIVE_INT_KEYS = {
    "security.api_read_requests_per_minute",
    "security.api_write_requests_per_minute",
    "session.step_timeout_seconds",
    "session.stale_after_seconds",
    "session.compaction_llm_max_attempts",
    "session.compaction_max_recursion",
    "session.llm_stream_idle_timeout_seconds",
    "session.llm_stream_max_retries",
    "session.memory_instructions_max_tokens",
    "session.core_memories_max_tokens",
    "session.recall_ttl_seconds",
    "session.max_tool_calls_per_turn",
    "session.cache_max_entries",
    "session.long_lived_chat_idle_compaction_min_events",
    "session.max_active_turns_per_user",
    "session.max_queued_messages",
    "session.step_request_questions_timeout_seconds",
    "evaluator.timeout_ms",
    "mcp.tool_timeout_seconds",
    "mcp.connect_timeout_seconds",
    "web.concurrency.global_cap",
    "web.concurrency.per_host_cap",
    "web.concurrency.direct_cap",
    "web.concurrency.tavily_cap",
    "web.concurrency.brave_cap",
    "web.concurrency.searxng_cap",
    "web.concurrency.browser_cap",
    "web.browser_fetch.session_idle_seconds",
    "web.browser_fetch.wait_timeout_seconds",
    "web.browser_fetch.navigation_timeout_seconds",
}

_NON_NEGATIVE_INT_KEYS = {
    "session.compaction_max_input_tokens",
    "session.immutable_prefix_repair_cooldown_seconds",
    "session.long_lived_chat_idle_compaction_seconds",
    "web.browser_fetch.network_idle_after_dom_seconds",
}

_ENUM_KEYS: dict[str, set[str]] = {
    "session.anthropic_cache_ttl": {"5m", "1h"},
    "web.backend": {"direct", "tavily", "brave", "searxng"},
    "web.search_backend": {"direct", "tavily", "brave", "searxng"},
    "web.fetch_backend": {"direct", "tavily", "browser"},
    "web.browser_fetch.wait_until": {"commit", "domcontentloaded", "load", "networkidle"},
}

_FLOAT_RANGE_KEYS: dict[str, tuple[float, float]] = {
    "session.compaction_threshold": (0.3, 0.99),
    "search.display_min_score": (0.0, 1.0),
}


def known_setting_keys() -> set[str]:
    return set(DEFAULT_SETTINGS)


def setting_category(key: str) -> str:
    if key not in DEFAULT_SETTINGS:
        raise ValueError("Unknown setting key")
    return DEFAULT_SETTINGS[key][0]


def validate_setting_value(key: str, value: Any) -> None:
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"Unknown setting key: {key}")

    if key in _ENUM_KEYS:
        if value not in _ENUM_KEYS[key]:
            allowed = ", ".join(sorted(_ENUM_KEYS[key]))
            raise ValueError(f"Setting {key} must be one of: {allowed}")
        return

    expected = DEFAULT_SETTINGS[key][1]
    if isinstance(expected, bool):
        if not isinstance(value, bool):
            raise ValueError(f"Setting {key} must be a boolean")
        return
    if isinstance(expected, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Setting {key} must be an integer")
        if key in _POSITIVE_INT_KEYS and value <= 0:
            raise ValueError(f"Setting {key} must be greater than zero")
        if key in _NON_NEGATIVE_INT_KEYS and value < 0:
            raise ValueError(f"Setting {key} must be zero or greater")
        return
    if isinstance(expected, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Setting {key} must be a number")
        if key in _FLOAT_RANGE_KEYS:
            minimum, maximum = _FLOAT_RANGE_KEYS[key]
            if not minimum <= float(value) <= maximum:
                raise ValueError(f"Setting {key} must be between {minimum} and {maximum}")
        return
    if isinstance(expected, str):
        if not isinstance(value, str):
            raise ValueError(f"Setting {key} must be a string")
        return
    if isinstance(expected, list):
        if not isinstance(value, list):
            raise ValueError(f"Setting {key} must be a list")
        if (
            expected
            and all(isinstance(item, str) for item in expected)
            and not all(isinstance(item, str) for item in value)
        ):
            raise ValueError(f"Setting {key} must be a list of strings")
        return
