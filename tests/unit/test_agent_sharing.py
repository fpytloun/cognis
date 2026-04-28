from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_conversation,
    create_executor,
    create_user,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_grantee_can_list_and_view_shared_agent(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(session, email="guest@example.com", name="Guest", password_hash=password_hash)
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        list_response = client.get(
            "/api/v1/agents",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )
        detail_response = client.get(
            "/api/v1/agents/shared-agent",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )

        assert list_response.status_code == 200
        items = list_response.json()["items"]
        assert any(item["agent_id"] == "shared-agent" and item["is_shared_with_me"] for item in items)

        assert detail_response.status_code == 200
        payload = detail_response.json()
        assert payload["shared_by_email"] == "owner@example.com"
        assert payload["executor_scope"] == "owner_executor"
        assert payload["is_readonly_for_caller"] is True


def test_grantee_can_load_shared_agent_avatar(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            image_id = "img_shared_avatar"
            await client.app.state.artifact_store.async_save(
                "avatars",
                image_id,
                "image",
                b"avatar-bytes",
                "image/png",
                owner_email="owner@example.com",
            )
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(session, email="guest@example.com", name="Guest", password_hash=password_hash)
                await create_user(session, email="other@example.com", name="Other", password_hash=password_hash)
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    avatar_image_id=image_id,
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        list_response = client.get(
            "/api/v1/agents",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )
        assert list_response.status_code == 200
        shared = next(item for item in list_response.json()["items"] if item["agent_id"] == "shared-agent")
        assert shared["avatar_url"] == "/api/v1/images/img_shared_avatar"

        image_response = client.get(
            "/api/v1/images/img_shared_avatar",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )
        assert image_response.status_code == 200
        assert image_response.content == b"avatar-bytes"

        other_response = client.get(
            "/api/v1/images/img_shared_avatar",
            headers=_auth_headers(client.app, email="other@example.com"),
        )
        assert other_response.status_code == 404


def test_grantee_can_update_own_executor_override(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(session, email="guest@example.com", name="Guest", password_hash=password_hash)
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="grantee_executor",
                    granted_by="owner@example.com",
                )
                await create_executor(
                    session,
                    executor_id="guest_exec",
                    name="Guest Exec",
                    executor_type="websocket",
                    owner_email="guest@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.patch(
            "/api/v1/agents/shared-agent/my-share",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json={"execution": {"executor_id": "guest_exec"}},
        )

        assert response.status_code == 200
        assert response.json()["grantee_overrides"] == {"execution": {"executor_id": "guest_exec"}}

        owner_response = client.get(
            "/api/v1/agents/shared-agent/shares",
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
        assert owner_response.status_code == 200
        assert owner_response.json()[0]["grantee_overrides"] is None


def test_owner_executor_share_rejects_grantee_executor_override(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(session, email="guest@example.com", name="Guest", password_hash=password_hash)
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.patch(
            "/api/v1/agents/shared-agent/my-share",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json={"execution": {"executor_id": "guest_exec"}},
        )

        assert response.status_code == 400


def test_admin_has_no_bypass_for_shared_agent_access(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=password_hash,
                    role="admin",
                )
                await create_agent(
                    session,
                    agent_id="private-agent",
                    owner_email="owner@example.com",
                    name="Private Agent",
                    display_name="Private Agent",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/agents/private-agent",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
        )

        assert response.status_code == 403


def test_grantee_cannot_mutate_shared_agent_bindings(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(session, email="guest@example.com", name="Guest", password_hash=password_hash)
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.put(
            "/api/v1/agents/shared-agent/bindings",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json=[],
        )

        assert response.status_code == 403


def test_revoked_share_blocks_future_messages(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(session, email="guest@example.com", name="Guest", password_hash=password_hash)
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                grant = await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await create_conversation(
                    session,
                    user_email="guest@example.com",
                    agent_id="shared-agent",
                    context_type="direct",
                    conversation_id="conv-shared",
                )
                await session.commit()
                client.app.state._test_grant_id = grant.grant_id

        asyncio.run(_seed())

        revoke_response = client.delete(
            f"/api/v1/agents/shared-agent/shares/{client.app.state._test_grant_id}",
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
        message_response = client.post(
            "/api/v1/conversations/conv-shared/messages",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json={"content": "hello"},
        )

        assert revoke_response.status_code == 200
        assert message_response.status_code == 403


def test_revoked_share_can_be_granted_again(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(session, email="owner@example.com", name="Owner", password_hash=password_hash)
                await create_user(session, email="guest@example.com", name="Guest", password_hash=password_hash)
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                grant = await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()
                client.app.state._test_regrant_id = grant.grant_id

        asyncio.run(_seed())

        revoke_response = client.delete(
            f"/api/v1/agents/shared-agent/shares/{client.app.state._test_regrant_id}",
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
        regrant_response = client.post(
            "/api/v1/agents/shared-agent/shares",
            headers=_auth_headers(client.app, email="owner@example.com"),
            json={"grantee_email": "guest@example.com", "executor_scope": "grantee_executor"},
        )

        assert revoke_response.status_code == 200
        assert regrant_response.status_code == 201
        assert regrant_response.json()["executor_scope"] == "grantee_executor"
