from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.notifications import NotificationService
from cognis.store.models import NotificationRow
from cognis.store.queries import create_agent, create_conversation, create_user, get_agent


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


def test_durable_escalation_lookup_enforces_user_and_conversation_ownership(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _exercise() -> None:
            async with client.app.state.session_factory() as session:
                for email in ("owner@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=client.app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await session.commit()
            created = await client.app.state.notification_service.create(
                notification_type="escalation",
                user_email="owner@example.com",
                conversation_id="conv-owned",
                session_id="sess-owned",
                notification_id="audit-owned",
                payload={"tool_call_id": "tool-owned", "tool_name": "bash"},
            )
            assert created.notification_id == "audit-owned"
            assert (
                await client.app.state.notification_service.find_oldest_pending_escalation(
                    user_email="other@example.com",
                    conversation_id="conv-owned",
                )
                is None
            )
            assert (
                await client.app.state.notification_service.find_oldest_pending_escalation(
                    user_email="owner@example.com",
                    conversation_id="conv-other",
                )
                is None
            )
            owned = await client.app.state.notification_service.find_oldest_pending_escalation(
                user_email="owner@example.com",
                conversation_id="conv-owned",
            )
            assert owned is not None
            assert owned.notification_id == "audit-owned"

        asyncio.run(_exercise())


def test_notification_list_includes_managed_origin_escalations_for_the_owner(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.app.state.providers.guardrails.get_escalation = AsyncMock(return_value=None)

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                for email in ("owner@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=client.app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await session.commit()
            await client.app.state.notification_service.create(
                notification_type="escalation",
                user_email="owner@example.com",
                conversation_id="conversation-child",
                notification_id="managed-escalation",
                payload={
                    "tool_name": "deploy_service",
                    "managed_origin_conversation_id": "conversation-parent",
                },
            )
            await client.app.state.notification_service.create(
                notification_type="escalation",
                user_email="owner@example.com",
                conversation_id="conversation-unrelated-child",
                notification_id="unrelated-escalation",
                payload={
                    "tool_name": "delete_service",
                    "managed_origin_conversation_id": "conversation-unrelated-parent",
                },
            )

        asyncio.run(_seed())

        owner_response = client.get(
            "/api/v1/notifications?conversation_id=conversation-parent",
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
        other_response = client.get(
            "/api/v1/notifications?conversation_id=conversation-parent",
            headers=_auth_headers(client.app, email="other@example.com"),
        )

        assert owner_response.status_code == 200
        assert [item["notification_id"] for item in owner_response.json()] == ["managed-escalation"]
        assert other_response.status_code == 200
        assert other_response.json() == []


def test_auth_challenge_cancel_does_not_require_declared_response_fields(
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
                notification_type="auth_challenge",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={"kind": "browser_login", "required_fields": ["confirmed"]},
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "cancel"},
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "cancel"


def test_oauth_auth_challenge_cancel_is_allowed(monkeypatch: object, tmp_path: Path) -> None:
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
                payload={"kind": "oauth_authorization", "label": "OAuth"},
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "cancel"},
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "cancel"


def test_expired_oauth_authorization_is_removed_from_pending_notifications(
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
                notification_type="auth_challenge",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "kind": "oauth_authorization",
                    "metadata": {
                        "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    },
                },
            )
            async with client.app.state.session_factory() as session:
                row = await session.get(NotificationRow, notification.notification_id)
                assert row is not None
                row.status = "resolving"
                row.resolved_at = datetime.now(UTC) - timedelta(minutes=1)
                await session.commit()
            return notification.notification_id

        notification_id = asyncio.run(_seed())
        client.app.state.pause_waiter.register(
            PendingPause(
                pause_id=notification_id,
                pause_type="auth_challenge",
                conversation_id="conv-1",
            )
        )

        response = client.get(
            "/api/v1/notifications?conversation_id=conv-1",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json() == []
        notification = asyncio.run(client.app.state.notification_service.get(notification_id))
        assert notification is not None
        assert notification.status == "resolved"
        assert notification.resolution == {"decision": "cancel", "reason": "expired"}
        assert client.app.state.pause_waiter.get(notification_id) is None


def test_callback_only_oauth_does_not_register_or_reconcile_as_pause(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _exercise() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            callback_only = await client.app.state.notification_service.create(
                notification_type="auth_challenge",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "kind": "oauth_authorization",
                    "metadata": {"callback_only": True},
                },
            )
            interactive = await client.app.state.notification_service.create(
                notification_type="auth_challenge",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={"kind": "otp_code", "required_fields": ["code"]},
            )

            assert client.app.state.pause_waiter.get(callback_only.notification_id) is None
            assert client.app.state.pause_waiter.get(interactive.notification_id) is not None

            replica_waiter = PauseWaiter()
            replica_waiter.register(
                PendingPause(
                    pause_id=callback_only.notification_id,
                    pause_type="auth_challenge",
                    conversation_id="conv-1",
                )
            )
            replica_service = NotificationService(
                client.app.state.session_factory,
                replica_waiter,
                client.app.state.event_bus,
                client.app.state.providers,
            )

            reconciled = await replica_service.reconcile_pending()

            assert reconciled == 1
            assert replica_waiter.get(callback_only.notification_id) is None
            assert replica_waiter.get(interactive.notification_id) is not None
            current = await replica_service.get(callback_only.notification_id)
            assert current is not None
            assert current.status == "pending"

        asyncio.run(_exercise())


def test_orphaned_cleanup_does_not_overwrite_a_fresh_resolution_claim(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _exercise() -> None:
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
                payload={"kind": "oauth_authorization"},
            )
            now = datetime.now(UTC)
            async with client.app.state.session_factory() as session:
                row = await session.get(NotificationRow, notification.notification_id)
                assert row is not None
                row.status = "resolving"
                row.resolved_at = now
                await session.commit()

            orphaned = await client.app.state.notification_service.mark_orphaned(
                notification.notification_id,
                reason="expired",
                resolving_before=now - timedelta(seconds=1),
            )

            assert orphaned is False
            current = await client.app.state.notification_service.get(notification.notification_id)
            assert current is not None
            assert current.status == "resolving"
            pause = client.app.state.pause_waiter.get(notification.notification_id)
            assert pause is not None
            assert pause.resolved is False

        asyncio.run(_exercise())


def test_direct_step_question_cancel_can_clear_stale_pending_notification(
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
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Continue?",
                            "required": True,
                            "allow_custom": True,
                        }
                    ]
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "cancel"},
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "cancel"


def test_direct_step_question_response_uses_persisted_questions_without_local_pause(
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
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Continue?",
                            "options": [{"id": "continue", "label": "Continue"}],
                            "required": True,
                            "allow_custom": False,
                        }
                    ]
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())
        client.app.state.pause_waiter.clear(notification_id)

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "decision": "continue",
                "response_payload": {
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["continue"],
                            "custom_answer": None,
                        }
                    ],
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "continue"


def test_direct_step_question_cancel_ignores_incomplete_response_payload(
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
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Continue?",
                            "required": True,
                            "allow_custom": False,
                        }
                    ]
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "decision": "cancel",
                "response_payload": {"mode": "structured", "answers": []},
            },
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "cancel"


def test_direct_step_question_continue_requires_response(
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
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Continue?",
                            "required": True,
                            "allow_custom": False,
                        }
                    ]
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "continue"},
        )

        assert response.status_code == 400


def test_direct_step_question_continue_rejects_empty_plain_text_response(
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
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Continue?",
                            "required": True,
                            "allow_custom": True,
                        }
                    ]
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "decision": "continue",
                "response_payload": {"mode": "plain_text", "answers": []},
            },
        )

        assert response.status_code == 400


def test_direct_step_question_continue_accepts_plain_text_response(
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
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id="conv-1",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Continue?",
                            "required": True,
                            "allow_custom": True,
                        }
                    ]
                },
            )
            return notification.notification_id

        notification_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "continue", "response": "Continue now"},
        )

        assert response.status_code == 200
        resolved = asyncio.run(client.app.state.notification_service.get(notification_id))
        assert resolved is not None
        assert resolved.resolution["answers"][0]["custom_answer"] == "Continue now"


def test_direct_step_question_owner_observes_remote_resolution(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _run() -> None:
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
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                await session.commit()
            owner_waiter = PauseWaiter()
            owner = NotificationService(
                client.app.state.session_factory,
                owner_waiter,
                client.app.state.event_bus,
                client.app.state.providers,
            )
            remote = client.app.state.notification_service
            remote._record_user_interaction = AsyncMock()
            notification = await owner.create(
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id=conversation.conversation_id,
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Continue?",
                            "options": [{"id": "continue", "label": "Continue"}],
                            "required": True,
                            "allow_custom": False,
                        }
                    ]
                },
            )
            owner_wait = asyncio.create_task(
                owner.wait_for_resolution(
                    notification.notification_id,
                    timeout=2,
                    poll_seconds=0.01,
                )
            )
            await remote.resolve(
                notification.notification_id,
                "continue",
                {
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["continue"],
                            "custom_answer": None,
                        }
                    ],
                },
                user_email="user@example.com",
            )
            resolution = await owner_wait

            assert resolution.decision == "continue"
            assert resolution.data["answers"][0]["selected_option_ids"] == ["continue"]

        asyncio.run(_run())


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
