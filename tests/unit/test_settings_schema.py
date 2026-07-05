from __future__ import annotations

import pytest

from cognis.settings_schema import known_setting_keys, validate_setting_value


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
