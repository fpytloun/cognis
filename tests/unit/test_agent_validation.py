"""Stage 36: API validation for agent execution config (additional_executors)."""

from __future__ import annotations

import pytest

from cognis.api.routes.agents import _validate_agent_execution


def test_none_passes() -> None:
    _validate_agent_execution(None)


def test_executor_id_string_passes() -> None:
    _validate_agent_execution({"executor_id": "exec-1"})


def test_executor_id_must_be_non_empty() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution({"executor_id": ""})
    assert "executor_id" in str(exc.value)


def test_executor_selector_must_be_non_empty() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution({"executor_selector": {}})
    assert "executor_selector" in str(exc.value)


def test_additional_executors_explicit_id_ok() -> None:
    _validate_agent_execution(
        {
            "executor_id": "exec-primary",
            "additional_executors": [
                {"executor_id": "exec-extra", "description": "Mac"}
            ],
        }
    )


def test_additional_executors_selector_ok() -> None:
    _validate_agent_execution(
        {
            "executor_id": "exec-primary",
            "additional_executors": [
                {"executor_selector": {"role": "browser"}}
            ],
        }
    )


def test_additional_must_be_list() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution({"additional_executors": "not a list"})
    assert "must be a list" in str(exc.value)


def test_additional_entry_must_be_dict() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution({"additional_executors": ["not a dict"]})
    assert "must be an object" in str(exc.value)


def test_additional_must_have_exactly_one_of_id_or_selector() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution(
            {
                "additional_executors": [
                    {"executor_id": "x", "executor_selector": {"a": "b"}}
                ]
            }
        )
    assert "exactly one of" in str(exc.value)


def test_additional_must_have_at_least_one_of_id_or_selector() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution(
            {"additional_executors": [{"description": "no id no selector"}]}
        )
    assert "exactly one of" in str(exc.value)


def test_additional_id_must_not_collide_with_primary() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution(
            {
                "executor_id": "shared",
                "additional_executors": [{"executor_id": "shared"}],
            }
        )
    assert "duplicates" in str(exc.value)


def test_additional_id_must_not_collide_with_earlier_additional() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution(
            {
                "additional_executors": [
                    {"executor_id": "a"},
                    {"executor_id": "a"},
                ]
            }
        )
    assert "duplicates" in str(exc.value)


def test_additional_selector_keys_must_be_non_empty() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution(
            {
                "additional_executors": [
                    {"executor_selector": {"": "value"}}
                ]
            }
        )
    assert "non-empty" in str(exc.value)


def test_additional_description_must_be_string() -> None:
    with pytest.raises(Exception) as exc:
        _validate_agent_execution(
            {
                "additional_executors": [
                    {"executor_id": "a", "description": 123}
                ]
            }
        )
    assert "description" in str(exc.value)
