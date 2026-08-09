from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.artifacts.store import ArtifactMetadata
from cognis.core.events import Event, EventBus, EventType
from cognis.core.web_push import WebPushRuntimeConfig, WebPushService
from cognis.store.models import PushSubscriptionRow
from cognis.store.queries import create_agent, create_conversation, create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(client: TestClient, email: str = "user@example.com") -> None:
    async with client.app.state.session_factory() as session:
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=client.app.state.password_hasher.hash("password123"),
            role="user",
        )
        await session.commit()


class _FakeArtifactStore:
    async def async_load_metadata(
        self,
        namespace: str,
        object_id: str,
        filename: str,
    ) -> ArtifactMetadata:
        assert namespace == "avatars"
        assert filename == "image"
        return ArtifactMetadata(content_type="image/png", size=128, owner_email="user@example.com")

    async def async_get_public_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        assert namespace == "avatars"
        assert filename == "image"
        assert ttl_seconds == 3600
        return f"https://cognis.example/api/v1/artifacts/content/avatars/{object_id}/image?sig=test"


class _FailingArtifactStore:
    async def async_load_metadata(
        self,
        namespace: str,
        object_id: str,
        filename: str,
    ) -> ArtifactMetadata:
        return ArtifactMetadata(content_type="image/png", size=128, owner_email="user@example.com")

    async def async_get_public_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        raise ValueError("signing unavailable")


class _WrongOwnerArtifactStore:
    async def async_load_metadata(
        self,
        namespace: str,
        object_id: str,
        filename: str,
    ) -> ArtifactMetadata:
        return ArtifactMetadata(content_type="image/png", size=128, owner_email="other@example.com")

    async def async_get_public_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        raise AssertionError("avatar URL should not be signed for the wrong owner")


def test_vapid_public_key_is_available(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        asyncio.run(_seed_user(client))

        response = client.get(
            "/api/v1/push/vapid-public-key",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["enabled"] is True
        assert isinstance(payload["public_key"], str)
        assert len(payload["public_key"]) > 80


def test_register_push_subscription_upserts_endpoint(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        asyncio.run(_seed_user(client))
        headers = _auth_headers(client.app, email="user@example.com")
        payload = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/sub-1",
            "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
            "platform": "pwa",
        }

        response = client.post("/api/v1/push/subscriptions", headers=headers, json=payload)
        assert response.status_code == 200
        first_id = response.json()["subscription_id"]

        response = client.post(
            "/api/v1/push/subscriptions",
            headers=headers,
            json={**payload, "keys": {"p256dh": "next-key", "auth": "next-auth"}},
        )
        assert response.status_code == 200
        assert response.json()["subscription_id"] == first_id

        async def _load() -> PushSubscriptionRow | None:
            async with client.app.state.session_factory() as session:
                return await session.get(PushSubscriptionRow, first_id)

        row = asyncio.run(_load())
        assert row is not None
        assert row.p256dh == "next-key"
        assert row.auth == "next-auth"
        assert row.enabled is True


def test_push_subscription_status_reports_delivery_error(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        asyncio.run(_seed_user(client))
        headers = _auth_headers(client.app, email="user@example.com")
        response = client.post(
            "/api/v1/push/subscriptions",
            headers=headers,
            json={
                "endpoint": "https://fcm.googleapis.com/fcm/send/sub-status",
                "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
            },
        )
        assert response.status_code == 200
        subscription_id = response.json()["subscription_id"]

        async def _set_error() -> None:
            async with client.app.state.session_factory() as session:
                row = await session.get(PushSubscriptionRow, subscription_id)
                assert row is not None
                row.last_error = "push endpoint rejected request"
                await session.commit()

        asyncio.run(_set_error())

        response = client.get("/api/v1/push/subscriptions/status", headers=headers)

        assert response.status_code == 200
        assert response.json() == {
            "configured": True,
            "enabled_subscriptions": 1,
            "last_error": "push endpoint rejected request",
        }


def test_unregister_push_subscription_disables_endpoint(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        asyncio.run(_seed_user(client))
        headers = _auth_headers(client.app, email="user@example.com")
        endpoint = "https://fcm.googleapis.com/fcm/send/sub-2"
        response = client.post(
            "/api/v1/push/subscriptions",
            headers=headers,
            json={
                "endpoint": endpoint,
                "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
            },
        )
        subscription_id = response.json()["subscription_id"]

        response = client.post(
            "/api/v1/push/subscriptions/unsubscribe",
            headers=headers,
            json={"endpoint": endpoint},
        )

        assert response.status_code == 200
        assert response.json()["removed"] is True

        async def _load() -> PushSubscriptionRow | None:
            async with client.app.state.session_factory() as session:
                return await session.get(PushSubscriptionRow, subscription_id)

        row = asyncio.run(_load())
        assert row is not None
        assert row.enabled is False


def test_push_subscription_test_endpoint_sends_to_current_user(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        asyncio.run(_seed_user(client))

        async def _fake_send_to_user(**kwargs: object) -> dict[str, int]:
            assert kwargs["user_email"] == "user@example.com"
            assert kwargs["kind"] == "test"
            return {"sent_to": 1, "errors": 0}

        monkeypatch.setattr(  # type: ignore[attr-defined]
            client.app.state.web_push_service,
            "send_to_user",
            _fake_send_to_user,
        )

        response = client.post(
            "/api/v1/push/subscriptions/test",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json() == {"sent_to": 1, "errors": 0}


def test_turn_completed_web_chat_creates_push_payload(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _run() -> dict[str, str] | None:
            await _seed_user(client)
            async with client.app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent_1",
                    owner_email="user@example.com",
                    name="Agent",
                    display_name="Research Agent",
                    avatar_image_id="img_avatar",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent_1",
                    context_type="web",
                    conversation_id="conv_1",
                    title="Launch planning",
                )
                await session.commit()
            service = WebPushService(
                session_factory=client.app.state.session_factory,
                event_bus=EventBus(),
                config=WebPushRuntimeConfig(
                    enabled=True,
                    public_key="public",
                    private_key="private",
                    subject="mailto:test@example.com",
                ),
                artifact_store=_FakeArtifactStore(),
            )
            return await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_COMPLETED,
                    data={
                        "conversation_id": "conv_1",
                        "session_id": "session_1",
                        "message_id": "message_1",
                    },
                )
            )

        payload = asyncio.run(_run())

        assert payload == {
            "user_email": "user@example.com",
            "title": "Research Agent",
            "body": "New reply in Launch planning.",
            "url": "/chat/conv_1",
            "tag": "conv_1",
            "kind": "message",
            "conversation_id": "conv_1",
            "icon": "https://cognis.example/api/v1/artifacts/content/avatars/img_avatar/image?sig=test",
        }


def test_nonterminal_turn_completion_does_not_create_push_payload(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _run() -> tuple[dict[str, str] | None, dict[str, str] | None]:
            await _seed_user(client)
            async with client.app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent_nonterminal",
                    owner_email="user@example.com",
                    name="Agent",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent_nonterminal",
                    context_type="web",
                    conversation_id="conv_nonterminal",
                )
                await session.commit()
            service = WebPushService(
                session_factory=client.app.state.session_factory,
                event_bus=EventBus(),
                config=WebPushRuntimeConfig(
                    enabled=True,
                    public_key="public",
                    private_key="private",
                    subject="mailto:test@example.com",
                ),
            )
            continuation = await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_COMPLETED,
                    data={
                        "conversation_id": "conv_nonterminal",
                        "managed_continuation_pending": True,
                    },
                )
            )
            partial = await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_COMPLETED,
                    data={
                        "conversation_id": "conv_nonterminal",
                        "partial": True,
                        "finish_reason": "user_cancelled",
                    },
                )
            )
            return continuation, partial

        continuation, partial = asyncio.run(_run())

        assert continuation is None
        assert partial is None


def test_turn_error_web_chat_creates_push_payload(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _run() -> tuple[dict[str, str] | None, dict[str, str] | None]:
            await _seed_user(client)
            async with client.app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent_error",
                    owner_email="user@example.com",
                    name="Agent",
                    display_name="Research Agent",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent_error",
                    context_type="web",
                    conversation_id="conv_error",
                    title="Launch planning",
                )
                await session.commit()
            service = WebPushService(
                session_factory=client.app.state.session_factory,
                event_bus=EventBus(),
                config=WebPushRuntimeConfig(
                    enabled=True,
                    public_key="public",
                    private_key="private",
                    subject="mailto:test@example.com",
                ),
            )
            failed = await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_ERROR,
                    data={
                        "conversation_id": "conv_error",
                        "error_code": "step_failed",
                    },
                )
            )
            stopped = await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_ERROR,
                    data={
                        "conversation_id": "conv_error",
                        "error_code": "queued_turn_cancelled",
                    },
                )
            )
            return failed, stopped

        failed, stopped = asyncio.run(_run())

        assert failed == {
            "user_email": "user@example.com",
            "title": "Research Agent",
            "body": "Reply failed in Launch planning.",
            "url": "/chat/conv_error",
            "tag": "conv_error",
            "kind": "message",
            "conversation_id": "conv_error",
        }
        assert stopped is not None
        assert stopped["body"] == "Reply stopped in Launch planning."


def test_turn_completed_push_payload_omits_avatar_when_signing_fails(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _run() -> dict[str, str] | None:
            await _seed_user(client)
            async with client.app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent_signing_failure",
                    owner_email="user@example.com",
                    name="Agent",
                    avatar_image_id="img_avatar",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent_signing_failure",
                    context_type="web",
                    conversation_id="conv_signing_failure",
                )
                await session.commit()
            service = WebPushService(
                session_factory=client.app.state.session_factory,
                event_bus=EventBus(),
                config=WebPushRuntimeConfig(
                    enabled=True,
                    public_key="public",
                    private_key="private",
                    subject="mailto:test@example.com",
                ),
                artifact_store=_FailingArtifactStore(),
            )
            return await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_COMPLETED,
                    data={"conversation_id": "conv_signing_failure"},
                )
            )

        payload = asyncio.run(_run())

        assert payload is not None
        assert payload["conversation_id"] == "conv_signing_failure"
        assert "icon" not in payload


def test_turn_completed_push_payload_does_not_sign_wrong_owner_avatar(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _run() -> dict[str, str] | None:
            await _seed_user(client)
            async with client.app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent_wrong_owner_avatar",
                    owner_email="user@example.com",
                    name="Agent",
                    avatar_image_id="img_other_user",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent_wrong_owner_avatar",
                    context_type="web",
                    conversation_id="conv_wrong_owner_avatar",
                )
                await session.commit()
            service = WebPushService(
                session_factory=client.app.state.session_factory,
                event_bus=EventBus(),
                config=WebPushRuntimeConfig(
                    enabled=True,
                    public_key="public",
                    private_key="private",
                    subject="mailto:test@example.com",
                ),
                artifact_store=_WrongOwnerArtifactStore(),
            )
            return await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_COMPLETED,
                    data={"conversation_id": "conv_wrong_owner_avatar"},
                )
            )

        payload = asyncio.run(_run())

        assert payload is not None
        assert payload["conversation_id"] == "conv_wrong_owner_avatar"
        assert "icon" not in payload


def test_turn_completed_push_payload_rejects_protocol_relative_avatar_url(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _run() -> dict[str, str] | None:
            await _seed_user(client)
            async with client.app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent_unsafe_avatar",
                    owner_email="user@example.com",
                    name="Agent",
                    avatar_url="//tracker.example/icon.png",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent_unsafe_avatar",
                    context_type="web",
                    conversation_id="conv_unsafe_avatar",
                    title="Private chat",
                )
                await session.commit()
            service = WebPushService(
                session_factory=client.app.state.session_factory,
                event_bus=EventBus(),
                config=WebPushRuntimeConfig(
                    enabled=True,
                    public_key="public",
                    private_key="private",
                    subject="mailto:test@example.com",
                ),
            )
            return await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_COMPLETED,
                    data={"conversation_id": "conv_unsafe_avatar"},
                )
            )

        payload = asyncio.run(_run())

        assert payload is not None
        assert "icon" not in payload


def test_turn_completed_push_payload_allows_same_origin_avatar_url(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _run() -> dict[str, str] | None:
            await _seed_user(client)
            async with client.app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent_safe_avatar",
                    owner_email="user@example.com",
                    name="Agent",
                    avatar_url="/avatars/local.png",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent_safe_avatar",
                    context_type="web",
                    conversation_id="conv_safe_avatar",
                )
                await session.commit()
            service = WebPushService(
                session_factory=client.app.state.session_factory,
                event_bus=EventBus(),
                config=WebPushRuntimeConfig(
                    enabled=True,
                    public_key="public",
                    private_key="private",
                    subject="mailto:test@example.com",
                ),
            )
            return await service._event_payload(  # noqa: SLF001
                Event(
                    type=EventType.TURN_COMPLETED,
                    data={"conversation_id": "conv_safe_avatar"},
                )
            )

        payload = asyncio.run(_run())

        assert payload is not None
        assert payload["icon"] == "/avatars/local.png"


def test_register_push_subscription_rejects_untrusted_endpoint(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        asyncio.run(_seed_user(client))
        response = client.post(
            "/api/v1/push/subscriptions",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "endpoint": "https://127.0.0.1/internal",
                "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
            },
        )

        assert response.status_code == 400
