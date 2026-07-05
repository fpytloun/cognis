from __future__ import annotations

import pytest

from cognis.bootstrap import DEFAULT_SETTINGS
from cognis.settings_schema import validate_setting_value


def test_idle_checkpoint_defaults_are_registered() -> None:
    assert DEFAULT_SETTINGS["session.long_lived_chat_idle_compaction_seconds"] == (
        "session",
        21600,
    )
    assert DEFAULT_SETTINGS["session.long_lived_chat_idle_compaction_min_events"] == (
        "session",
        20,
    )
    assert DEFAULT_SETTINGS["session.compaction_max_input_tokens"] == ("session", 0)
    assert DEFAULT_SETTINGS["session.compaction_llm_max_attempts"] == ("session", 2)
    assert DEFAULT_SETTINGS["session.compaction_max_recursion"] == ("session", 2)
    assert DEFAULT_SETTINGS["session.compaction_fallback_enabled"] == ("session", True)


def test_idle_checkpoint_threshold_allows_zero_to_disable() -> None:
    validate_setting_value("session.long_lived_chat_idle_compaction_seconds", 0)
    validate_setting_value("session.long_lived_chat_idle_compaction_seconds", 21600)
    with pytest.raises(ValueError, match="zero or greater"):
        validate_setting_value("session.long_lived_chat_idle_compaction_seconds", -1)


def test_idle_checkpoint_min_events_requires_positive_integer() -> None:
    validate_setting_value("session.long_lived_chat_idle_compaction_min_events", 20)
    with pytest.raises(ValueError, match="greater than zero"):
        validate_setting_value("session.long_lived_chat_idle_compaction_min_events", 0)


def test_compaction_settings_ranges_are_validated() -> None:
    validate_setting_value("session.compaction_threshold", 0.3)
    validate_setting_value("session.compaction_threshold", 0.85)
    validate_setting_value("session.compaction_threshold", 0.99)
    with pytest.raises(ValueError, match="between 0.3 and 0.99"):
        validate_setting_value("session.compaction_threshold", 0.29)
    with pytest.raises(ValueError, match="between 0.3 and 0.99"):
        validate_setting_value("session.compaction_threshold", 1.0)

    validate_setting_value("session.compaction_max_input_tokens", 0)
    validate_setting_value("session.compaction_max_input_tokens", 100_000)
    with pytest.raises(ValueError, match="zero or greater"):
        validate_setting_value("session.compaction_max_input_tokens", -1)

    validate_setting_value("session.compaction_llm_max_attempts", 1)
    validate_setting_value("session.compaction_max_recursion", 1)
    with pytest.raises(ValueError, match="greater than zero"):
        validate_setting_value("session.compaction_llm_max_attempts", 0)
    with pytest.raises(ValueError, match="greater than zero"):
        validate_setting_value("session.compaction_max_recursion", 0)
