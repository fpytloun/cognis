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


def test_auth_challenge_requires_code_when_declared(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            notification = await client.app.state.notification_service.create(
                notification_type="auth_challenge",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={"kind": "otp_code", "required_fields": ["code"]},
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "approve"},
        )

        assert response.status_code == 400


def test_credential_request_deny_does_not_store_credential(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            notification = await client.app.state.notification_service.create(
                notification_type="credential_request",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={"credential_id": "github_work", "kind": "token", "label": "GitHub"},
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "decision": "deny",
                "credential": {
                    "credential_id": "github_work",
                    "kind": "token",
                    "label": "GitHub",
                    "payload": {"token": "abc123"},
                    "metadata": {},
                },
            },
        )

        assert response.status_code == 200
        listed = client.get(
            "/api/v1/credentials",
            headers=_auth_headers(client.app, email="user@example.com"),
        )
        assert listed.status_code == 200
        assert listed.json() == []


def test_credential_request_approve_requires_declared_fields(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            notification = await client.app.state.notification_service.create(
                notification_type="credential_request",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "credential_id": "github_login",
                    "kind": "username_password",
                    "label": "GitHub Login",
                    "scope": "agent",
                    "agent_id": "agent-1",
                    "required_fields": ["username", "password"],
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "decision": "approve",
                "credential": {
                    "credential_id": "wrong_id",
                    "kind": "token",
                    "label": "Wrong",
                    "payload": {"username": "alice"},
                    "metadata": {},
                    "scope": "user",
                },
            },
        )

        assert response.status_code == 400
