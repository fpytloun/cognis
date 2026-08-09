from __future__ import annotations

import pytest

from cognis.runtime_settings import HOT_APPLIED_SETTING_KEYS
from cognis.settings_schema import (
    DEFAULT_SETTINGS,
    SETTINGS_REGISTRY,
    exposed_setting_keys,
    known_setting_keys,
    validate_setting_value,
)


@pytest.mark.parametrize(
    "key,value",
    [
        ("session.memory_instructions_max_tokens", 2000),
        ("session.core_memories_max_tokens", 2000),
        ("session.immutable_prefix_repair_cooldown_seconds", 300),
        ("session.recall_ttl_seconds", 86400),
    ],
)
def test_ws6_session_settings_are_known_and_validated(key: str, value: int) -> None:
    assert key in known_setting_keys()
    validate_setting_value(key, value)


@pytest.mark.parametrize(
    "key",
    [
        "session.memory_instructions_max_tokens",
        "session.core_memories_max_tokens",
        "session.recall_ttl_seconds",
    ],
)
def test_ws6_positive_session_settings_reject_zero(key: str) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        validate_setting_value(key, 0)


def test_immutable_prefix_repair_cooldown_allows_zero() -> None:
    validate_setting_value("session.immutable_prefix_repair_cooldown_seconds", 0)


def test_new_runtime_defaults_are_registered() -> None:
    assert DEFAULT_SETTINGS["session.step_timeout_seconds"] == ("session", 14400)
    assert DEFAULT_SETTINGS["session.max_tool_calls_per_turn"] == ("session", 500)
    assert DEFAULT_SETTINGS["session.max_llm_cycles_per_turn"] == ("session", 150)


def test_every_exposed_setting_has_metadata_and_runtime_semantics() -> None:
    exposed = exposed_setting_keys()
    assert exposed
    for key in exposed:
        spec = SETTINGS_REGISTRY[key]
        assert spec.label
        assert spec.description
        assert spec.category
        assert spec.section
        assert spec.value_type
        assert spec.application_scope in {"hot", "next_operation", "next_runtime"}
        assert not spec.deprecated

    registered_hot = {
        key
        for key, spec in SETTINGS_REGISTRY.items()
        if spec.exposed and spec.application_scope == "hot"
    }
    assert registered_hot == set(HOT_APPLIED_SETTING_KEYS)


def test_zero_disables_managed_conversation_cleanup() -> None:
    validate_setting_value("managed_conversations.cleanup_retention_days", 0)


def test_executor_policy_changes_apply_to_next_runtime_without_cleanup() -> None:
    for key in ("executors.allow_in_process", "executors.allow_subprocess"):
        spec = SETTINGS_REGISTRY[key]
        assert spec.application_scope == "next_runtime"
        assert key not in HOT_APPLIED_SETTING_KEYS


def test_hidden_settings_are_preserved_but_explicitly_deprecated() -> None:
    hidden = {key for key, spec in SETTINGS_REGISTRY.items() if not spec.exposed}
    assert hidden
    assert hidden <= known_setting_keys()
    assert all(SETTINGS_REGISTRY[key].deprecated for key in hidden)
