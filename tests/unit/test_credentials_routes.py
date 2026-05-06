from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_agent, create_user, get_agent


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_credentials_crud(monkeypatch: object, tmp_path: Path) -> None:
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
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent",
                    permissions={},
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/credentials",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "credential_id": "github_work",
                "kind": "token",
                "label": "GitHub Work",
                "payload": {"token": "abc123"},
                "metadata": {"origin": "https://github.com"},
                "agent_id": "agent-1",
            },
        )
        assert response.status_code == 200
        assert response.json()["credential_id"] == "github_work"

        async def _agent_permissions() -> dict:
            async with client.app.state.session_factory() as session:
                agent = await get_agent(session, "agent-1")
                assert agent is not None
                return agent.permissions or {}

        permissions = asyncio.run(_agent_permissions())
        assert permissions["allowed_credentials"] == ["github_work"]

        listed = client.get(
            "/api/v1/credentials",
            headers=_auth_headers(client.app, email="user@example.com"),
        )
        assert listed.status_code == 200
        assert listed.json()[0]["label"] == "GitHub Work"

        revoked = client.post(
            "/api/v1/credentials/github_work/revoke",
            headers=_auth_headers(client.app, email="user@example.com"),
        )
        assert revoked.status_code == 200

        deleted = client.delete(
            "/api/v1/credentials/github_work",
            headers=_auth_headers(client.app, email="user@example.com"),
        )
        assert deleted.status_code == 200
