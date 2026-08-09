"""Typed registry and validation for DB-backed application settings."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

SettingValueType = Literal["boolean", "integer", "number", "string", "string_list"]
SettingApplicationScope = Literal["hot", "next_operation", "next_runtime", "hidden"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Authoritative metadata and runtime contract for one persisted setting."""

    key: str
    label: str
    description: str
    category: str
    section: str
    default: object
    value_type: SettingValueType
    application_scope: SettingApplicationScope
    exposed: bool = True
    options: tuple[object, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    unit: str | None = None
    deprecated: bool = False


_DEFAULT_VALUES: Final[dict[str, tuple[str, object]]] = {
    "session.compaction_threshold": ("session", 0.85),
    "session.compaction_preserve_turns": ("session", 10),
    "session.compaction_max_input_tokens": ("session", 0),
    "session.compaction_llm_max_attempts": ("session", 2),
    "session.compaction_max_recursion": ("session", 2),
    "session.compaction_fallback_enabled": ("session", True),
    "session.step_timeout_seconds": ("session", 14400),
    "session.stale_after_seconds": ("session", 300),
    "session.llm_stream_idle_timeout_seconds": ("session", 300),
    "session.llm_stream_max_retries": ("session", 3),
    "session.anthropic_cache_ttl": ("session", "5m"),
    "session.memory_instructions_max_tokens": ("session", 2000),
    "session.core_memories_max_tokens": ("session", 2000),
    "session.immutable_prefix_repair_cooldown_seconds": ("session", 300),
    "session.recall_ttl_seconds": ("session", 86400),
    "session.max_tool_calls_per_turn": ("session", 500),
    "session.max_llm_cycles_per_turn": ("session", 150),
    "session.idle_timeout_seconds": ("session", 1800),
    "session.long_lived_chat_idle_compaction_seconds": ("session", 21600),
    "session.long_lived_chat_idle_compaction_min_events": ("session", 20),
    "session.max_session_age_seconds": ("session", 86400),
    "session.max_delegation_depth": ("session", 5),
    "session.max_active_turns_per_user": ("session", 20),
    "session.max_queued_messages": ("session", 20),
    "session.escalation_timeout_seconds": ("session", 300),
    "session.step_request_questions_timeout_seconds": ("session", 3600),
    "session.cache_max_entries": ("session", 200),
    "managed_conversations.cleanup_retention_days": ("managed_conversations", 7),
    "search.display_min_score": ("search", 0.2),
    "evaluator.timeout_ms": ("evaluator", 180000),
    "decision_engine.inline_max_length": ("decision_engine", 200),
    "security.non_bypassable_tools": (
        "security",
        ["shell", "bash", "write_file", "delete_file"],
    ),
    "security.api_read_requests_per_minute": ("security", 600),
    "security.api_write_requests_per_minute": ("security", 200),
    "security.token_ttl_seconds": ("security", 3600),
    "security.max_connections": ("security", 100),
    "security.ws_auth_timeout_seconds": ("security", 10),
    "mcp.tool_timeout_seconds": ("mcp", 300),
    "mcp.connect_timeout_seconds": ("mcp", 15),
    "web.backend": ("web", "direct"),
    "web.search_backend": ("web", "direct"),
    "web.fetch_backend": ("web", "direct"),
    "web.fetch_fallback_browser": ("web", True),
    "web.tavily_enabled": ("web", True),
    "web.brave_enabled": ("web", True),
    "web.searxng_enabled": ("web", True),
    "web.searxng_url": ("web", ""),
    "web.searxng_engines": ("web", ""),
    "web.searxng_categories": ("web", ""),
    "web.searxng_language": ("web", ""),
    "web.browser_fetch.session_idle_seconds": ("web", 60),
    "web.browser_fetch.wait_timeout_seconds": ("web", 30),
    "web.browser_fetch.navigation_timeout_seconds": ("web", 60),
    "web.browser_fetch.wait_until": ("web", "domcontentloaded"),
    "web.browser_fetch.network_idle_after_dom_seconds": ("web", 3),
    "web.browser_fetch.headed_fallback_enabled": ("web", True),
    "web.concurrency.global_cap": ("web", 32),
    "web.concurrency.per_host_cap": ("web", 4),
    "web.concurrency.direct_cap": ("web", 16),
    "web.concurrency.direct_search_cap": ("web", 2),
    "web.concurrency.tavily_cap": ("web", 8),
    "web.concurrency.brave_cap": ("web", 2),
    "web.concurrency.searxng_cap": ("web", 4),
    "web.concurrency.browser_cap": ("web", 4),
    "web.rate_limit.tavily_qps": ("web", 5.0),
    "web.rate_limit.direct_search_qps": ("web", 1.0),
    "web.rate_limit.brave_qps": ("web", 1.0),
    "web.rate_limit.searxng_qps": ("web", 5.0),
    "executors.allow_in_process": ("executors", True),
    "executors.allow_subprocess": ("executors", True),
    "executors.secondary_assignment_ttl_seconds": ("executors", 3600),
    "executors.secondary_disconnect_retry_seconds": ("executors", 15),
    "executors.secondary_disconnect_retry_interval_seconds": ("executors", 3),
    "tts.enabled": ("tts", True),
    "tts.default_voice": ("tts", "alloy"),
    "tts.cache_ttl_days": ("tts", 30),
}

_HIDDEN_KEYS: Final[set[str]] = {
    # Kept in storage/default bootstrapping for compatibility. These have no
    # sound restartless runtime consumer and therefore are not advertised.
    "session.stale_after_seconds",
    "session.idle_timeout_seconds",
    "session.max_session_age_seconds",
    "security.max_connections",
    "web.backend",
}

_HOT_KEYS: Final[set[str]] = {
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

_NEXT_OPERATION_KEYS: Final[set[str]] = {
    "session.compaction_preserve_turns",
    "session.compaction_max_input_tokens",
    "session.compaction_llm_max_attempts",
    "session.compaction_max_recursion",
    "session.compaction_fallback_enabled",
    "session.long_lived_chat_idle_compaction_seconds",
    "session.long_lived_chat_idle_compaction_min_events",
    "session.max_active_turns_per_user",
    "session.max_queued_messages",
    "session.escalation_timeout_seconds",
    "session.step_request_questions_timeout_seconds",
    "managed_conversations.cleanup_retention_days",
    "search.display_min_score",
    "executors.secondary_assignment_ttl_seconds",
    "executors.secondary_disconnect_retry_seconds",
    "executors.secondary_disconnect_retry_interval_seconds",
    "tts.enabled",
    "tts.default_voice",
    "tts.cache_ttl_days",
}

_NEXT_RUNTIME_KEYS: Final[set[str]] = {
    key for key in _DEFAULT_VALUES if key.startswith(("mcp.", "web."))
} | {
    "executors.allow_in_process",
    "executors.allow_subprocess",
}

_UNCLASSIFIED_KEYS = (
    set(_DEFAULT_VALUES) - _HIDDEN_KEYS - _HOT_KEYS - _NEXT_OPERATION_KEYS - _NEXT_RUNTIME_KEYS
)
if _UNCLASSIFIED_KEYS:
    raise RuntimeError(f"Settings lack runtime semantics: {sorted(_UNCLASSIFIED_KEYS)}")

_ENUM_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "session.anthropic_cache_ttl": ("5m", "1h"),
    "web.backend": ("direct", "tavily", "brave", "searxng"),
    "web.search_backend": ("direct", "tavily", "brave", "searxng"),
    "web.fetch_backend": ("direct", "tavily", "browser"),
    "web.browser_fetch.wait_until": ("commit", "domcontentloaded", "load", "networkidle"),
}

_RANGES: Final[dict[str, tuple[int | float | None, int | float | None]]] = {
    "session.compaction_threshold": (0.3, 0.99),
    "session.max_llm_cycles_per_turn": (1, 1000),
    "search.display_min_score": (0.0, 1.0),
}

_DESCRIPTIONS: Final[dict[str, str]] = {
    "session.compaction_threshold": (
        "Context utilization ratio that triggers automatic compaction."
    ),
    "session.step_timeout_seconds": (
        "Default wall-clock timeout for a workflow step; agent execution overrides take precedence."
    ),
    "session.max_tool_calls_per_turn": (
        "Default counted tool-call ceiling for normal turns; secondary delegated agents are unlimited."
    ),
    "session.max_llm_cycles_per_turn": (
        "Maximum LLM request cycles within one turn before work continues automatically."
    ),
    "session.llm_stream_idle_timeout_seconds": (
        "Maximum idle interval while waiting for meaningful LLM stream activity."
    ),
    "session.llm_stream_max_retries": "Maximum retries after a retryable LLM stream failure.",
    "session.anthropic_cache_ttl": "Prompt-cache TTL used for Anthropic-compatible requests.",
    "session.max_delegation_depth": "Maximum permitted primary-agent delegation chain depth.",
    "session.cache_max_entries": "Maximum in-memory session-state entries retained by this worker.",
    "decision_engine.inline_max_length": (
        "Maximum message length eligible for deterministic inline classification."
    ),
    "evaluator.timeout_ms": "Wall-clock timeout for the semantic step evaluator.",
    "security.non_bypassable_tools": (
        "Tool-name patterns that must always pass through guardrail evaluation."
    ),
    "security.api_read_requests_per_minute": "Per-client API read request rate limit.",
    "security.api_write_requests_per_minute": "Per-client API mutation request rate limit.",
    "security.token_ttl_seconds": "Lifetime assigned to newly issued access and service tokens.",
    "security.ws_auth_timeout_seconds": (
        "Maximum time a new WebSocket connection may remain unauthenticated."
    ),
    "executors.allow_in_process": "Allow new tool runtimes to use the controller process.",
    "executors.allow_subprocess": "Allow new tool runtimes to use local subprocess executors.",
    "web.search_backend": "Default backend selected for subsequent web search runtimes.",
    "web.fetch_backend": "Default backend selected for subsequent web fetch runtimes.",
}

_NON_NEGATIVE_INT_KEYS: Final[set[str]] = {
    "session.compaction_max_input_tokens",
    "session.immutable_prefix_repair_cooldown_seconds",
    "session.long_lived_chat_idle_compaction_seconds",
    "managed_conversations.cleanup_retention_days",
    "web.browser_fetch.network_idle_after_dom_seconds",
}

_UNITS: Final[dict[str, str]] = {
    key: "seconds"
    for key in _DEFAULT_VALUES
    if key.endswith("_seconds") or key.endswith("_timeout_seconds")
}
_UNITS.update(
    {
        "evaluator.timeout_ms": "milliseconds",
        "managed_conversations.cleanup_retention_days": "days",
        "tts.cache_ttl_days": "days",
        "web.rate_limit.tavily_qps": "requests/second",
        "web.rate_limit.brave_qps": "requests/second",
        "web.rate_limit.searxng_qps": "requests/second",
    }
)


def _value_type(value: object) -> SettingValueType:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "string_list"
    return "string"


def _humanize(value: str) -> str:
    return value.replace("_", " ").replace(".", " ").strip().title()


def _build_spec(key: str, category: str, default: object) -> SettingSpec:
    hidden = key in _HIDDEN_KEYS
    if hidden:
        scope: SettingApplicationScope = "hidden"
    elif key in _HOT_KEYS:
        scope = "hot"
    elif key in _NEXT_RUNTIME_KEYS:
        scope = "next_runtime"
    elif key in _NEXT_OPERATION_KEYS:
        scope = "next_operation"
    else:  # guarded by _UNCLASSIFIED_KEYS above
        raise RuntimeError(f"Setting {key} lacks runtime semantics")
    minimum, maximum = _RANGES.get(key, (None, None))
    if isinstance(default, int) and not isinstance(default, bool):
        if key in _NON_NEGATIVE_INT_KEYS:
            minimum = 0
        elif minimum is None:
            minimum = 1
    parts = key.split(".")
    section = parts[1] if len(parts) > 2 else category
    return SettingSpec(
        key=key,
        label=_humanize(parts[-1]),
        description=_DESCRIPTIONS.get(key, f"Controls {_humanize(key).lower()}."),
        category=category,
        section=section,
        default=default,
        value_type=_value_type(default),
        application_scope=scope,
        exposed=not hidden,
        options=tuple(_ENUM_KEYS.get(key, ())),
        minimum=minimum,
        maximum=maximum,
        unit=_UNITS.get(key),
        deprecated=hidden,
    )


SETTINGS_REGISTRY: Final[MappingProxyType[str, SettingSpec]] = MappingProxyType(
    {
        key: _build_spec(key, category, default)
        for key, (category, default) in _DEFAULT_VALUES.items()
    }
)

# Compatibility for bootstrap and older imports that expect category/default tuples.
DEFAULT_SETTINGS: Final[MappingProxyType[str, tuple[str, object]]] = MappingProxyType(
    {key: (spec.category, spec.default) for key, spec in SETTINGS_REGISTRY.items()}
)


def known_setting_keys() -> set[str]:
    return set(SETTINGS_REGISTRY)


def exposed_setting_keys() -> set[str]:
    return {key for key, spec in SETTINGS_REGISTRY.items() if spec.exposed}


def get_setting_spec(key: str, *, require_exposed: bool = False) -> SettingSpec:
    spec = SETTINGS_REGISTRY.get(key)
    if spec is None or (require_exposed and not spec.exposed):
        raise ValueError(f"Unknown setting key: {key}")
    return spec


def setting_category(key: str) -> str:
    return get_setting_spec(key, require_exposed=True).category


def validate_setting_value(key: str, value: Any) -> None:
    spec = get_setting_spec(key, require_exposed=True)

    if spec.options:
        if value not in spec.options:
            allowed = ", ".join(str(option) for option in spec.options)
            raise ValueError(f"Setting {key} must be one of: {allowed}")
        return

    expected = spec.default
    if isinstance(expected, bool):
        if not isinstance(value, bool):
            raise ValueError(f"Setting {key} must be a boolean")
        return
    if isinstance(expected, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Setting {key} must be an integer")
        if spec.minimum is not None and value < spec.minimum:
            if spec.minimum == 1:
                raise ValueError(f"Setting {key} must be greater than zero")
            if spec.minimum == 0:
                raise ValueError(f"Setting {key} must be zero or greater")
            raise ValueError(f"Setting {key} must be at least {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"Setting {key} must be at most {spec.maximum}")
        return
    if isinstance(expected, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Setting {key} must be a number")
        if (
            spec.minimum is not None
            and spec.maximum is not None
            and not spec.minimum <= float(value) <= spec.maximum
        ):
            raise ValueError(f"Setting {key} must be between {spec.minimum} and {spec.maximum}")
        if spec.minimum is not None and float(value) < spec.minimum:
            raise ValueError(f"Setting {key} must be at least {spec.minimum}")
        if spec.maximum is not None and float(value) > spec.maximum:
            raise ValueError(f"Setting {key} must be at most {spec.maximum}")
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
