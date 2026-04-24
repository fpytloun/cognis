from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_regular_user_can_create_user_secret(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())
        headers = _auth_headers(client.app, email="user@example.com", role="user")

        user_response = client.post(
            "/api/v1/secrets",
            headers=headers,
            json={"name": "MCP_TOKEN", "value": "secret", "scope": "user"},
        )
        system_response = client.post(
            "/api/v1/secrets",
            headers=headers,
            json={"name": "SYSTEM_TOKEN", "value": "secret", "scope": "system"},
        )

        assert user_response.status_code == 200
        assert system_response.status_code == 403
