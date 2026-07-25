from __future__ import annotations

import pytest

from cognis.api.routes.settings import (
    _llm_provider_preview_requires_admin,
    _validate_llm_provider_payload,
)


def test_executor_preview_validation_requires_single_selector() -> None:
    with pytest.raises(Exception, match="must specify executor_id or executor_labels"):
        _validate_llm_provider_payload("executor", {"preset": "ollama"})

    with pytest.raises(Exception, match="either executor_id or executor_labels"):
        _validate_llm_provider_payload(
            "executor",
            {
                "preset": "ollama",
                "executor_id": "olorin",
                "executor_labels": {"host": "mac"},
            },
        )


def test_executor_preview_validation_rejects_invalid_labels() -> None:
    with pytest.raises(Exception, match="executor_labels must contain only non-empty string"):
        _validate_llm_provider_payload(
            "executor",
            {
                "preset": "ollama",
                "executor_labels": {"host": ""},
            },
        )


def test_preview_discovery_requires_admin_except_safe_saved_executor_ollama() -> None:
    assert not _llm_provider_preview_requires_admin(
        saved_provider_location="executor",
        saved_provider_preset="ollama",
        preset="ollama",
        location="executor",
        has_auth_override=False,
    )

    assert _llm_provider_preview_requires_admin(
        saved_provider_location=None,
        saved_provider_preset=None,
        preset="ollama",
        location="executor",
        has_auth_override=False,
    )
    assert _llm_provider_preview_requires_admin(
        saved_provider_location="executor",
        saved_provider_preset="ollama",
        preset="ollama",
        location="executor",
        has_auth_override=True,
    )
    assert _llm_provider_preview_requires_admin(
        saved_provider_location="executor",
        saved_provider_preset="openai",
        preset="ollama",
        location="executor",
        has_auth_override=False,
    )
    assert _llm_provider_preview_requires_admin(
        saved_provider_location="executor",
        saved_provider_preset="ollama",
        preset="ollama",
        location="controller",
        has_auth_override=False,
    )
