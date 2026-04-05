from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_executor,
    create_pairing_request,
    create_user,
    get_channel_account,
    get_channel_contact,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_list_channel_types(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/channels/types", headers=_auth_headers(client.app, email="user@example.com")
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) >= 8
        assert any(item["channel_type"] == "signal" for item in body)


def test_create_channel_account_defaults_to_pairing(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        created = client.post(
            "/api/v1/channels/accounts",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "channel_type": "telegram",
                "agent_id": "agent-1",
                "display_name": "Telegram Bot",
            },
        )
        assert created.status_code == 200
        account_id = created.json()["account_id"]

        async def _fetch() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                row = await get_channel_account(session, account_id)
                assert row is not None
                return row.dm_policy, row.group_policy

        dm_policy, group_policy = asyncio.run(_fetch())
        assert dm_policy == "pairing"
        assert group_policy == "pairing"


def test_create_webhook_channel_generates_secret(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/channels/accounts",
            headers=_auth_headers(app, email="user@example.com"),
            json={"channel_type": "whatsapp", "agent_id": "agent-1", "display_name": "WhatsApp"},
        )
        assert response.status_code == 200
        assert isinstance(response.json()["webhook_secret"], str)
        assert response.json()["webhook_secret"]


def test_create_signal_account_accepts_non_secret_fields_in_settings(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/channels/accounts",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "channel_type": "signal",
                "agent_id": "agent-1",
                "display_name": "Signal",
                "settings": {
                    "transport": "rest_api",
                    "account_number": "+420111222333",
                    "api_url": "http://localhost:8080",
                },
                "credential_refs": {},
            },
        )
        assert response.status_code == 200, response.text


def test_create_signal_direct_account_requires_executor_opt_in(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_executor(
                    session,
                    executor_id="exec-signal",
                    name="Signal Exec",
                    executor_type="websocket",
                    config={"signal": {"direct_enabled": True}},
                    owner_email="user@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/channels/accounts",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "channel_type": "signal",
                "agent_id": "agent-1",
                "display_name": "Signal Direct",
                "adapter_location": "executor",
                "executor_id": "exec-signal",
                "settings": {
                    "transport": "direct_jsonrpc",
                    "account_number": "+420111222333",
                },
                "credential_refs": {},
            },
        )
        assert response.status_code == 200, response.text


def test_update_signal_account_accepts_partial_non_secret_updates(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_channel_account(
                    session,
                    account_id="signal-1",
                    channel_type="signal",
                    display_name="Signal",
                    agent_id="agent-1",
                    user_email="user@example.com",
                    config={
                        "transport": "rest_api",
                        "account_number": "+420111222333",
                        "api_url": "http://localhost:8080",
                    },
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.patch(
            "/api/v1/channels/accounts/signal-1",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "config": {
                    "transport": "rest_api",
                    "api_url": "http://localhost:9090",
                }
            },
        )
        assert response.status_code == 200, response.text


def test_create_channel_account_rejects_secondary_agent(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-secondary",
                    owner_email="user@example.com",
                    name="Secondary Agent",
                    agent_type="secondary",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/channels/accounts",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "channel_type": "signal",
                "agent_id": "agent-secondary",
                "display_name": "Signal",
                "settings": {
                    "transport": "rest_api",
                    "account_number": "+420111222333",
                    "api_url": "http://localhost:8080",
                },
            },
        )
        assert response.status_code == 400, response.text
        assert "primary agents only" in response.text


def test_pairing_endpoints(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_channel_account(
                    session,
                    account_id="ch_signal",
                    channel_type="signal",
                    display_name="Signal",
                    agent_id="agent-1",
                    user_email="user@example.com",
                )
                await create_pairing_request(
                    session,
                    owner_email="user@example.com",
                    account_id="ch_signal",
                    channel_type="signal",
                    sender_id="+420111222333",
                    sender_name="Filip",
                    chat_id="+420111222333",
                    chat_name=None,
                    code="ABC123",
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
                await session.commit()

        asyncio.run(_seed())

        listed = client.get(
            "/api/v1/channels/pairing-requests",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        assert listed.json()[0]["account_display_name"] == "Signal"
        assert listed.json()[0]["agent_id"] == "agent-1"
        assert listed.json()[0]["agent_name"] == "Agent 1"

        invalid = client.post(
            "/api/v1/channels/pair",
            headers=_auth_headers(app, email="user@example.com"),
            json={"code": "BAD999"},
        )
        assert invalid.status_code == 400

        valid = client.post(
            "/api/v1/channels/pair",
            headers=_auth_headers(app, email="user@example.com"),
            json={"code": "ABC-123"},
        )
        assert valid.status_code == 200
        assert valid.json()["status"] == "completed"
        assert valid.json()["account_display_name"] == "Signal"
        assert valid.json()["agent_name"] == "Agent 1"

        async def _verify_contact() -> bool:
            async with app.state.session_factory() as session:
                contact = await get_channel_contact(session, "signal", "+420111222333")
                return bool(contact and contact.verified)

        assert asyncio.run(_verify_contact()) is True


def test_reject_nonexistent_pairing_request(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/channels/pairing-requests/missing/reject",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 404
