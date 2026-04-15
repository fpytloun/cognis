from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(app: object, email: str = "user@example.com") -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role="user",
        )
        await session.commit()


def test_generate_field_handles_reasoning_only_response(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        client.app.state.providers.llm.generate = AsyncMock(  # type: ignore[attr-defined]
            return_value={"choices": [{"message": {"content": None}}]}
        )

        response = client.post(
            "/api/v1/agents/generate-field",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "field": "description",
                "current_value": "",
                "context": {"name": "Test agent"},
            },
        )

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "provider_error"
        assert response.json()["error"]["message"] == "Failed to generate field value"


def test_generate_avatar_prompt_falls_back_on_reasoning_only_response(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        client.app.state.providers.llm.generate = AsyncMock(  # type: ignore[attr-defined]
            return_value={"choices": [{"message": {"content": None}}]}
        )

        response = client.post(
            "/api/v1/images/generate-prompt",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"name": "Test agent", "description": "Helpful"},
        )

        assert response.status_code == 200
        assert "Test agent" in response.json()["prompt"]
        assert response.json()["prompt"].startswith("A professional, modern avatar")
