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


def test_idle_checkpoint_threshold_allows_zero_to_disable() -> None:
    validate_setting_value("session.long_lived_chat_idle_compaction_seconds", 0)
    validate_setting_value("session.long_lived_chat_idle_compaction_seconds", 21600)
    with pytest.raises(ValueError, match="zero or greater"):
        validate_setting_value("session.long_lived_chat_idle_compaction_seconds", -1)


def test_idle_checkpoint_min_events_requires_positive_integer() -> None:
    validate_setting_value("session.long_lived_chat_idle_compaction_min_events", 20)
    with pytest.raises(ValueError, match="greater than zero"):
        validate_setting_value("session.long_lived_chat_idle_compaction_min_events", 0)
