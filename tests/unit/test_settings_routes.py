from __future__ import annotations

import pytest
from fastapi import HTTPException

from cognis.api.routes.settings import _validate_llm_provider_payload


def test_validate_llm_provider_payload_accepts_chatgpt_direct_codex() -> None:
    _validate_llm_provider_payload(
        "controller",
        {"preset": "chatgpt", "codex_transport": "direct"},
    )


def test_validate_llm_provider_payload_rejects_unknown_codex_transport() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "controller",
            {"preset": "chatgpt", "codex_transport": "websocket"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"


def test_validate_llm_provider_payload_rejects_direct_codex_for_non_chatgpt() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "controller",
            {"preset": "openai", "codex_transport": "direct"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"
