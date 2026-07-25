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


class _FakeChannelStatus:
    def model_dump(self) -> dict[str, str]:
        return {"status": "connected"}


class _FakeChannelManager:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.restarted: list[str] = []
        self.stopped: list[str] = []

    async def get_account_status(self, account_id: str) -> _FakeChannelStatus | None:
        del account_id
        return _FakeChannelStatus() if self.running else None

    async def restart_account(self, account_id: str) -> None:
        self.restarted.append(account_id)

    async def stop_account(self, account_id: str) -> None:
        self.stopped.append(account_id)


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
        signal = next(item for item in body if item["channel_type"] == "signal")
        delivery_field = next(
            field
            for field in signal["setting_fields"]
            if field["name"] == "assistant_delivery_mode"
        )
        assert delivery_field["default"] == "final_only"
        assert delivery_field["options"] == ["final_only", "concatenated", "immediate"]


def test_create_channel_account_applies_defaults(monkeypatch: object, tmp_path: Path) -> None:
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

        async def _fetch() -> tuple[str, str, dict[str, object]]:
            async with app.state.session_factory() as session:
                row = await get_channel_account(session, account_id)
                assert row is not None
                return row.dm_policy, row.group_policy, row.config or {}

        dm_policy, group_policy, config = asyncio.run(_fetch())
        assert dm_policy == "pairing"
        assert group_policy == "pairing"
        assert config["assistant_delivery_mode"] == "final_only"


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


def test_update_channel_account_hot_reloads_running_account(
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
                    account_id="matrix-1",
                    channel_type="matrix",
                    display_name="Matrix",
                    agent_id="agent-1",
                    user_email="user@example.com",
                    config={"homeserver_url": "https://matrix.example.org"},
                )
                await session.commit()

        asyncio.run(_seed())
        manager = _FakeChannelManager(running=True)
        app.state.channel_manager = manager

        response = client.patch(
            "/api/v1/channels/accounts/matrix-1",
            headers=_auth_headers(app, email="user@example.com"),
            json={"config": {"direct_rooms": ["!dm:example.org"]}},
        )

        assert response.status_code == 200, response.text
        assert manager.restarted == ["matrix-1"]
        assert manager.stopped == []


def test_update_channel_account_does_not_start_stopped_account_on_config_edit(
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
                    account_id="matrix-1",
                    channel_type="matrix",
                    display_name="Matrix",
                    agent_id="agent-1",
                    user_email="user@example.com",
                    config={"homeserver_url": "https://matrix.example.org"},
                )
                await session.commit()

        asyncio.run(_seed())
        manager = _FakeChannelManager(running=False)
        app.state.channel_manager = manager

        response = client.patch(
            "/api/v1/channels/accounts/matrix-1",
            headers=_auth_headers(app, email="user@example.com"),
            json={"config": {"direct_rooms": ["!dm:example.org"]}},
        )

        assert response.status_code == 200, response.text
        assert manager.restarted == []
        assert manager.stopped == []


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


def test_channel_default_profile_round_trip_and_agent_change_clear(
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
                for agent_id in ("agent-1", "agent-2"):
                    await create_agent(
                        session,
                        agent_id=agent_id,
                        owner_email="user@example.com",
                        name=agent_id,
                        status="active",
                        agent_profiles={
                            "chat": {
                                "profile_id": "chat",
                                "description": "Interactive chat",
                                "enabled": True,
                            }
                        },
                    )
                await session.commit()

        asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")
        created = client.post(
            "/api/v1/channels/accounts",
            headers=headers,
            json={
                "channel_type": "telegram",
                "agent_id": "agent-1",
                "display_name": "Telegram",
                "default_agent_profile_id": "chat",
            },
        )
        assert created.status_code == 200, created.text
        account_id = created.json()["account_id"]

        fetched = client.get(f"/api/v1/channels/accounts/{account_id}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["default_agent_profile_id"] == "chat"

        updated = client.patch(
            f"/api/v1/channels/accounts/{account_id}",
            headers=headers,
            json={"agent_id": "agent-2"},
        )
        assert updated.status_code == 200, updated.text

        fetched = client.get(f"/api/v1/channels/accounts/{account_id}", headers=headers)
        assert fetched.json()["agent_id"] == "agent-2"
        assert fetched.json()["default_agent_profile_id"] is None


def test_channel_default_profile_rejects_missing_disabled_or_foreign_agent(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                for email in ("user@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                    agent_profiles={
                        "disabled": {
                            "profile_id": "disabled",
                            "description": "Disabled",
                            "enabled": False,
                        }
                    },
                )
                await create_agent(
                    session,
                    agent_id="foreign-agent",
                    owner_email="other@example.com",
                    name="Foreign",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")
        for agent_id, profile_id, status in (
            ("agent-1", "missing", 400),
            ("agent-1", "disabled", 400),
            ("foreign-agent", None, 404),
        ):
            response = client.post(
                "/api/v1/channels/accounts",
                headers=headers,
                json={
                    "channel_type": "telegram",
                    "agent_id": agent_id,
                    "display_name": "Telegram",
                    "default_agent_profile_id": profile_id,
                },
            )
            assert response.status_code == status, response.text


def test_channel_account_detail_mutations_are_owner_scoped(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                for email in ("owner@example.com", "viewer@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await create_agent(
                    session,
                    agent_id="owner-agent",
                    owner_email="owner@example.com",
                    name="Owner agent",
                    status="active",
                )
                account = await create_channel_account(
                    session,
                    channel_type="telegram",
                    display_name="Private",
                    agent_id="owner-agent",
                    user_email="owner@example.com",
                )
                await session.commit()
                return account.account_id

        account_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="viewer@example.com")
        assert (
            client.get(f"/api/v1/channels/accounts/{account_id}", headers=headers).status_code
            == 404
        )
        assert (
            client.patch(
                f"/api/v1/channels/accounts/{account_id}",
                headers=headers,
                json={"display_name": "Changed"},
            ).status_code
            == 404
        )
        assert (
            client.delete(f"/api/v1/channels/accounts/{account_id}", headers=headers).status_code
            == 404
        )
