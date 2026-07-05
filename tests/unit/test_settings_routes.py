from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.routes.settings import _validate_llm_provider_payload
from cognis.store.queries import create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(app: object, *, email: str = "user@example.com", role: str = "user") -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email=email,
            name=email.split("@")[0].title(),
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role=role,
        )
        await session.commit()


def test_validate_llm_provider_payload_accepts_chatgpt_direct_codex() -> None:
    _validate_llm_provider_payload(
        "controller",
        {"preset": "chatgpt", "codex_transport": "direct"},
    )


def test_validate_llm_provider_payload_accepts_controller_anthropic_subscription() -> None:
    _validate_llm_provider_payload(
        "controller",
        {
            "preset": "anthropic",
            "auth_config": {"mode": "oauth", "provider": "anthropic_subscription"},
        },
    )


def test_validate_llm_provider_payload_rejects_executor_anthropic_subscription() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "executor",
            {
                "preset": "anthropic",
                "auth_config": {"mode": "oauth", "provider": "anthropic_subscription"},
            },
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"


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


def test_validate_llm_provider_payload_accepts_message_projection_policy() -> None:
    _validate_llm_provider_payload(
        "controller",
        {"preset": "openai_compatible", "message_projection_policy": "anthropic_messages"},
    )


def test_validate_llm_provider_payload_rejects_unknown_message_projection_policy() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "controller",
            {"preset": "openai_compatible", "message_projection_policy": "surprising"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"


def test_user_preferences_default_and_update(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        default_response = client.get("/api/v1/user-preferences", headers=headers)
        assert default_response.status_code == 200
        assert default_response.json()["chat"] == {
            "show_thinking_blocks": False,
            "group_tool_calls": True,
            "show_internal_tool_calls": False,
        }

        payload = {
            "display": {"theme": "system", "language": "cs-CZ"},
            "chat": {
                "show_thinking_blocks": True,
                "group_tool_calls": True,
                "show_internal_tool_calls": True,
            },
        }
        update_response = client.put("/api/v1/user-preferences", headers=headers, json=payload)
        assert update_response.status_code == 200
        assert update_response.json() == payload

        persisted_response = client.get("/api/v1/user-preferences", headers=headers)
        assert persisted_response.status_code == 200
        assert persisted_response.json() == payload


def test_user_preferences_reject_invalid_language(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.put(
            "/api/v1/user-preferences",
            headers=headers,
            json={
                "display": {"theme": "system", "language": "../bad"},
                "chat": {
                    "show_thinking_blocks": True,
                    "group_tool_calls": True,
                    "show_internal_tool_calls": False,
                },
            },
        )

        assert response.status_code == 422
