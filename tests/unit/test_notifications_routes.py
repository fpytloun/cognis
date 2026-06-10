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


def test_credential_request_approval_grants_created_credential_to_agent(
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
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent",
                    permissions={},
                    status="active",
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
                "response_payload": {"username": "alice", "password": "secret"},
            },
        )

        assert response.status_code == 200

        async def _agent_permissions() -> dict:
            async with client.app.state.session_factory() as session:
                agent = await get_agent(session, "agent-1")
                assert agent is not None
                return agent.permissions or {}

        permissions = asyncio.run(_agent_permissions())
        assert permissions["allowed_credentials"] == ["github_login"]


def test_credential_request_approve_rejects_empty_declared_fields(
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
                "response_payload": {"username": "alice", "password": "   "},
            },
        )

        assert response.status_code == 400
        assert "password" in response.text


def test_credential_request_deny_drops_supplied_secret_payload(
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
                "response": "secret-token",
                "response_payload": {"token": "secret-token"},
            },
        )

        assert response.status_code == 200
        detail = client.get(
            f"/api/v1/notifications/{notification_id}",
            headers=_auth_headers(client.app, email="user@example.com"),
        )
        assert detail.status_code == 200
        resolution = detail.json()["resolution"]
        assert resolution == {"decision": "deny", "state": "resolved"}


def test_credential_request_parses_username_password_formats(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed_user() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        async def _create_request() -> str:
            notification = await client.app.state.notification_service.create(
                notification_type="credential_request",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "credential_id": "reddit_login",
                    "kind": "username_password",
                    "label": "Reddit login",
                    "required_fields": ["username", "password"],
                },
            )
            return notification.notification_id

        asyncio.run(_seed_user())

        for response_text in [
            "user@example.com:secret-pass",
            "user@example.com\nsecret-pass",
            "username: user@example.com\npassword: secret-pass",
        ]:
            notification_id = asyncio.run(_create_request())
            response = client.post(
                f"/api/v1/notifications/{notification_id}/resolve",
                headers=_auth_headers(client.app, email="user@example.com"),
                json={"decision": "approve", "response": response_text},
            )
            assert response.status_code == 200


def test_credential_request_parses_token_reply(monkeypatch: object, tmp_path: Path) -> None:
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
                    "credential_id": "reddit_token",
                    "kind": "token",
                    "label": "Reddit token",
                    "required_fields": ["token"],
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())
        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "approve", "response": "token: abc123"},
        )
        assert response.status_code == 200


def test_credential_request_accepts_response_payload(monkeypatch: object, tmp_path: Path) -> None:
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
                    "credential_id": "site_login",
                    "kind": "username_password",
                    "label": "Site login",
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
                "response_payload": {"username": "alice", "password": "secret"},
            },
        )
        assert response.status_code == 200
