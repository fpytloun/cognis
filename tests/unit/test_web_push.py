from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.config import load_config
from cognis.core.events import Event, EventBus, EventType
from cognis.core.web_push import (
    WebPushRuntimeConfig,
    WebPushService,
    _generate_vapid_private_key,
    _public_key_from_pem,
    _push_body,
    _serialize_push_payload,
    _to_sec1_pem,
    _validate_py_vapid_key,
    load_web_push_config,
)
from cognis.store.models import PushSubscriptionRow
from cognis.store.queries import create_user


def _pkcs8_private_key() -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["cancelled", "queued_turn_cancelled", "turn_cancelled"])
async def test_cancelled_turn_does_not_create_push(code: str) -> None:
    service = object.__new__(WebPushService)
    service._session_factory = AsyncMock()
    assert (
        await service._event_payload(
            Event(
                type=EventType.TURN_ERROR,
                data={"conversation_id": "conv-cancel", "error_code": code},
            )
        )
        is None
    )
    service._session_factory.assert_not_called()


def test_push_payload_is_bounded_by_utf8_bytes() -> None:
    payload = _serialize_push_payload(
        {
            "title": "Cognis",
            "body": "🚀" * 2_000,
            "url": "/chat/conv_1?notification=notif_1",
            "actions": [
                {"action": "approve", "title": "Approve"},
                {"action": "deny", "title": "Deny"},
            ],
        }
    )

    assert len(payload.encode("utf-8")) <= 3_800
    assert json.loads(payload)["body"].endswith("...")


def test_push_body_strips_markdown_presentation() -> None:
    body = _push_body(
        "## Result\n"
        "**Correct.** [Read more](https://example.com)\n"
        "- [x] `pytest` passed\n"
        "> ~~Old~~ *new* text"
    )

    assert body == "Result Correct. Read more pytest passed Old new text"
    assert not any(token in body for token in ("##", "**", "`", "[", "]", "~~"))


def test_push_body_preserves_plain_text_operators_and_identifiers() -> None:
    body = _push_body("Latency < 3 ms and errors > 0 for foo_bar_baz; 2 * 3 = 6.")

    assert body == "Latency < 3 ms and errors > 0 for foo_bar_baz; 2 * 3 = 6."


def test_push_body_handles_entities_and_escaped_markers_as_literal_text() -> None:
    body = _push_body(r"\*literal\* and &#42;&#42;encoded&#42;&#42;")

    assert body == "*literal* and **encoded**"


def test_push_body_preserves_markers_inside_code() -> None:
    body = _push_body(
        "`**literal**` and:\n```\n~~literal~~\n- [x] literal\n```\n\n    - [x] indented"
    )

    assert body == "**literal** and: ~~literal~~ - [x] literal - [x] indented"


def test_push_body_preserves_escaped_and_encoded_strike_markers() -> None:
    body = _push_body(r"\~\~literal\~\~ and &#126;&#126;encoded&#126;&#126;")

    assert body == "~~literal~~ and ~~encoded~~"


def test_push_body_strips_markdown_before_truncating() -> None:
    body = _push_body(f"### Verdict\n**{'word ' * 149}word**")

    assert body.startswith("Verdict word")
    assert body.endswith("...")
    assert len(body) == 500
    assert "**" not in body


def test_send_to_user_normalizes_title_and_body_to_plain_text() -> None:
    captured: dict[str, object] = {}

    class _Scalars:
        def all(self) -> list[object]:
            return [object()]

    class _Result:
        def scalars(self) -> _Scalars:
            return _Scalars()

        def scalar_one_or_none(self) -> None:
            return None

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> _Result:
            return _Result()

    service = WebPushService(
        session_factory=_Session,  # type: ignore[arg-type]
        event_bus=EventBus(),
        config=WebPushRuntimeConfig(
            enabled=True,
            public_key="public-key",
            private_key="private-key",
            subject="mailto:test@example.com",
        ),
    )

    async def _send_one(_row: object, payload: str) -> str:
        captured["payload"] = json.loads(payload)
        return "sent"

    service._send_one = _send_one  # type: ignore[method-assign]
    asyncio.run(
        service.send_to_user(
            user_email="user@example.com",
            title="**LaForge**",
            body="## Result\n**Complete**",
            url="/chat/conversation-a",
            tag="conversation-a",
            kind="message",
        )
    )

    assert captured["payload"] == {
        "title": "LaForge",
        "body": "Result Complete",
        "url": "/chat/conversation-a",
        "tag": "conversation-a",
        "kind": "message",
    }


def test_generated_vapid_key_uses_sec1_format(tmp_path: Path) -> None:
    key = _generate_vapid_private_key(tmp_path / "vapid.pem")

    assert key.startswith("-----BEGIN EC PRIVATE KEY-----")
    assert _validate_py_vapid_key(key) is None


def test_pkcs8_key_converts_to_py_vapid_compatible_sec1() -> None:
    pkcs8 = _pkcs8_private_key()
    public_key = _public_key_from_pem(pkcs8)

    sec1 = _to_sec1_pem(pkcs8)

    assert sec1 is not None
    assert sec1.startswith("-----BEGIN EC PRIVATE KEY-----")
    assert _public_key_from_pem(sec1) == public_key
    assert _validate_py_vapid_key(sec1) is None
    assert _to_sec1_pem(sec1) == sec1


def test_load_web_push_config_preserves_public_key_for_pkcs8_env_key(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    pkcs8 = _pkcs8_private_key()
    expected_public_key = _public_key_from_pem(pkcs8)
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_VAPID_PRIVATE_KEY", pkcs8)  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_VAPID_SUBJECT", "mailto:test@example.com")  # type: ignore[attr-defined]

    runtime = load_web_push_config(load_config())

    assert runtime.enabled is True
    assert runtime.public_key == expected_public_key
    assert runtime.private_key.startswith("-----BEGIN EC PRIVATE KEY-----")


def test_load_web_push_config_rejects_mismatched_public_key(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    private_key = _pkcs8_private_key()
    mismatched_public_key = _public_key_from_pem(_pkcs8_private_key())
    assert mismatched_public_key is not None
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_VAPID_PRIVATE_KEY", private_key)  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_VAPID_PUBLIC_KEY", mismatched_public_key)  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_VAPID_SUBJECT", "mailto:test@example.com")  # type: ignore[attr-defined]

    runtime = load_web_push_config(load_config())

    assert runtime.enabled is False
    assert runtime.reason == "COGNIS_VAPID_PUBLIC_KEY does not match the resolved private key"


def test_load_web_push_config_disables_invalid_py_vapid_key(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    pkcs8 = _pkcs8_private_key()
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_VAPID_PRIVATE_KEY", pkcs8)  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_VAPID_SUBJECT", "mailto:test@example.com")  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "cognis.core.web_push._validate_py_vapid_key",
        lambda _pem: "VAPID key cannot be loaded by py_vapid: ValueError",
    )

    runtime = load_web_push_config(load_config())

    assert runtime.enabled is False
    assert runtime.reason == "VAPID key cannot be loaded by py_vapid: ValueError"


def test_send_one_clears_last_error_after_success(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]

    with TestClient(create_app()) as client:

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

        client.portal.call(_seed)
        headers = {
            "Authorization": f"Bearer {client.app.state.auth_provider.sign_access_token('user@example.com', 'User', 'user')}"
        }
        response = client.post(
            "/api/v1/push/subscriptions",
            headers=headers,
            json={
                "endpoint": "https://fcm.googleapis.com/fcm/send/sub-clear",
                "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
            },
        )
        assert response.status_code == 200
        subscription_id = response.json()["subscription_id"]

        async def _set_error() -> None:
            async with client.app.state.session_factory() as session:
                row = await session.get(PushSubscriptionRow, subscription_id)
                assert row is not None
                row.last_error = "previous failure"
                await session.commit()

        client.portal.call(_set_error)
        monkeypatch.setattr(  # type: ignore[attr-defined]
            client.app.state.web_push_service,
            "_send_sync",
            lambda _row, _payload: ("sent", None),
        )

        response = client.post("/api/v1/push/subscriptions/test", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"sent_to": 1, "errors": 0}

        response = client.get("/api/v1/push/subscriptions/status", headers=headers)

    assert response.status_code == 200
    assert response.json()["last_error"] is None


def test_gone_endpoint_is_not_silently_re_enabled(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]

    with TestClient(create_app()) as client:

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

        client.portal.call(_seed)
        headers = {
            "Authorization": f"Bearer {client.app.state.auth_provider.sign_access_token('user@example.com', 'User', 'user')}"
        }

        def register(endpoint: str) -> dict[str, object]:
            response = client.post(
                "/api/v1/push/subscriptions",
                headers=headers,
                json={"endpoint": endpoint, "keys": {"p256dh": "p256dh-key", "auth": "auth-key"}},
            )
            assert response.status_code == 200
            return response.json()

        dead = "https://web.push.apple.com/dead-endpoint"
        first = register(dead)
        assert first["enabled"] is True and first["gone"] is False

        monkeypatch.setattr(  # type: ignore[attr-defined]
            client.app.state.web_push_service,
            "_send_sync",
            lambda _row, _payload: ("gone", "Push failed: 410 Gone"),
        )
        client.post("/api/v1/push/subscriptions/test", headers=headers)

        again = register(dead)
        assert again == {
            "subscription_id": first["subscription_id"],
            "enabled": False,
            "gone": True,
        }
        status = client.get("/api/v1/push/subscriptions/status", headers=headers).json()
        assert status["enabled_subscriptions"] == 0

        fresh = register("https://web.push.apple.com/fresh-endpoint")
        assert fresh["enabled"] is True and fresh["gone"] is False

        # A user-disabled (not gone) subscription re-registers normally.
        client.post(
            "/api/v1/push/subscriptions/unsubscribe",
            headers=headers,
            json={"endpoint": "https://web.push.apple.com/fresh-endpoint"},
        )
        assert register("https://web.push.apple.com/fresh-endpoint")["enabled"] is True


def test_legacy_gone_errors_are_recognized() -> None:
    from cognis.core.web_push import _is_gone_row

    assert _is_gone_row(SimpleNamespace(enabled=False, last_error="Push failed: 410 Gone"))
    assert _is_gone_row(SimpleNamespace(enabled=False, last_error="gone: Unregistered"))
    assert not _is_gone_row(SimpleNamespace(enabled=True, last_error="Push failed: 410 Gone"))
    assert not _is_gone_row(SimpleNamespace(enabled=False, last_error=None))
    assert not _is_gone_row(SimpleNamespace(enabled=False, last_error="Push failed: 500"))


def test_send_sync_passes_loaded_vapid_key_to_pywebpush(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    captured: dict[str, object] = {}

    def _fake_webpush(**kwargs: object) -> None:
        captured["vapid_private_key"] = kwargs.get("vapid_private_key")

    monkeypatch.setattr("pywebpush.webpush", _fake_webpush)  # type: ignore[attr-defined]

    with TestClient(create_app()) as client:
        from py_vapid import Vapid01

        row = PushSubscriptionRow(
            subscription_id="push_object_key",
            user_email="user@example.com",
            endpoint="https://fcm.googleapis.com/fcm/send/sub-object-key",
            p256dh="p256dh-key",
            auth="auth-key",
            enabled=True,
        )

        status, error = client.app.state.web_push_service._send_sync(row, "{}")

    assert status == "sent"
    assert error is None
    assert isinstance(captured["vapid_private_key"], Vapid01)
    assert not isinstance(captured["vapid_private_key"], str)


def test_schedule_error_event_payload_does_not_require_conversation_id() -> None:
    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    def _session_factory() -> _Session:
        return _Session()

    service = WebPushService(
        session_factory=_session_factory,  # type: ignore[arg-type]
        event_bus=EventBus(),
        config=WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject="mailto:test@example.com",
            reason="disabled",
        ),
    )

    payload = asyncio.run(
        service._event_payload(  # type: ignore[attr-defined]
            Event(
                type=EventType.SCHEDULE_ERROR,
                data={
                    "schedule_id": "sched_1",
                    "schedule_name": "Daily check",
                    "created_by": "user@example.com",
                },
            )
        )
    )

    assert payload is not None
    assert payload["user_email"] == "user@example.com"
    assert payload["kind"] == "schedule"
    assert payload["tag"] == "schedule:sched_1"
    assert payload["url"] == "/schedules/sched_1"
    # System alerts keep Cognis branding.
    assert payload["title"] == "Cognis"
    assert "icon" not in payload


def _branding_service(monkeypatch: object, agent: object) -> WebPushService:
    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    conversation = SimpleNamespace(
        conversation_id="conversation-a",
        agent_id="agent-a",
        context_type="web",
        user_email="user@example.com",
        title="Branding",
    )

    async def _get_conversation(_session: object, _conversation_id: str) -> object:
        return conversation

    async def _get_agent(_session: object, _agent_id: str) -> object:
        return agent

    monkeypatch.setattr("cognis.core.web_push.get_conversation", _get_conversation)  # type: ignore[attr-defined]
    monkeypatch.setattr("cognis.core.web_push.get_agent", _get_agent)  # type: ignore[attr-defined]
    return WebPushService(
        session_factory=lambda: _Session(),  # type: ignore[arg-type]
        event_bus=EventBus(),
        config=WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject="mailto:test@example.com",
            reason="disabled",
        ),
    )


def test_agent_notifications_use_agent_branding_and_system_alerts_do_not(
    monkeypatch: object,
) -> None:
    agent = SimpleNamespace(
        display_name="Atlas",
        name="atlas",
        avatar_image_id=None,
        avatar_url="/avatars/atlas.png",
        owner_email="user@example.com",
    )
    service = _branding_service(monkeypatch, agent)

    reply = asyncio.run(
        service._event_payload(  # type: ignore[attr-defined]
            Event(
                type=EventType.TURN_COMPLETED,
                data={"conversation_id": "conversation-a", "final_content": "Done."},
            )
        )
    )
    assert reply is not None
    assert reply["title"] == "Atlas"
    assert reply["icon"] == "/avatars/atlas.png"

    schedule = asyncio.run(
        service._event_payload(  # type: ignore[attr-defined]
            Event(
                type=EventType.SCHEDULE_DISABLED,
                data={
                    "schedule_id": "sched_1",
                    "schedule_name": "Daily check",
                    "created_by": "user@example.com",
                    "agent_id": "agent-a",
                },
            )
        )
    )
    assert schedule is not None
    assert schedule["title"] == "Cognis"
    assert "icon" not in schedule
    assert schedule["body"] == (
        'Schedule "Daily check" (Atlas) was disabled after repeated failures.'
    )


def test_turn_completed_payload_correlates_push_with_observed_completion(
    monkeypatch: object,
) -> None:
    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    def _session_factory() -> _Session:
        return _Session()

    conversation = SimpleNamespace(
        conversation_id="conversation-a",
        agent_id="agent-a",
        context_type="web",
        user_email="user@example.com",
        title="Notification race",
    )

    async def _get_conversation(_session: object, _conversation_id: str) -> object:
        return conversation

    async def _get_agent(_session: object, _agent_id: str) -> None:
        return None

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "cognis.core.web_push.get_conversation",
        _get_conversation,
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "cognis.core.web_push.get_agent",
        _get_agent,
    )
    service = WebPushService(
        session_factory=_session_factory,  # type: ignore[arg-type]
        event_bus=EventBus(),
        config=WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject="mailto:test@example.com",
            reason="disabled",
        ),
    )
    completed_at = "2026-08-26T21:00:00+00:00"

    payload = asyncio.run(
        service._event_payload(  # type: ignore[attr-defined]
            Event(
                type=EventType.TURN_COMPLETED,
                data={
                    "conversation_id": "conversation-a",
                    "completed_at": completed_at,
                },
                timestamp=datetime(2026, 8, 26, 21, 0, 1, tzinfo=UTC),
            )
        )
    )

    assert payload is not None
    assert payload["conversation_id"] == "conversation-a"
    assert payload["occurred_at"] == completed_at


def test_send_to_user_serializes_completion_timestamp() -> None:
    captured: dict[str, object] = {}

    class _Scalars:
        def all(self) -> list[object]:
            return [object()]

    class _Result:
        def scalars(self) -> _Scalars:
            return _Scalars()

        def scalar_one_or_none(self) -> None:
            return None

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> _Result:
            return _Result()

    def _session_factory() -> _Session:
        return _Session()

    service = WebPushService(
        session_factory=_session_factory,  # type: ignore[arg-type]
        event_bus=EventBus(),
        config=WebPushRuntimeConfig(
            enabled=True,
            public_key="public-key",
            private_key="private-key",
            subject="mailto:test@example.com",
        ),
    )

    async def _send_one(_row: object, payload: str) -> str:
        captured["payload"] = json.loads(payload)
        return "sent"

    service._send_one = _send_one  # type: ignore[method-assign]
    result = asyncio.run(
        service.send_to_user(
            user_email="user@example.com",
            title="Cognis",
            body="New reply",
            url="/chat/conversation-a",
            tag="conversation-a",
            kind="message",
            conversation_id="conversation-a",
            occurred_at="2026-08-26T21:00:00+00:00",
        )
    )

    assert result == {"sent_to": 1, "errors": 0}
    assert captured["payload"] == {
        "title": "Cognis",
        "body": "New reply",
        "url": "/chat/conversation-a",
        "tag": "conversation-a",
        "kind": "message",
        "conversation_id": "conversation-a",
        "occurred_at": "2026-08-26T21:00:00+00:00",
    }


def test_send_to_user_hides_content_when_user_disables_it() -> None:
    captured: dict[str, object] = {}

    class _Scalars:
        def all(self) -> list[object]:
            return [object()]

    class _StateResult:
        def scalar_one_or_none(self) -> object:
            return SimpleNamespace(value={"notifications": {"include_content": False}})

    class _SubscriptionsResult:
        def scalars(self) -> _Scalars:
            return _Scalars()

    class _Session:
        calls = 0

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> object:
            self.calls += 1
            return _SubscriptionsResult() if self.calls == 1 else _StateResult()

    service = WebPushService(
        session_factory=_Session,  # type: ignore[arg-type]
        event_bus=EventBus(),
        config=WebPushRuntimeConfig(
            enabled=True,
            public_key="public-key",
            private_key="private-key",
            subject="mailto:test@example.com",
        ),
    )

    async def _send_one(_row: object, payload: str) -> str:
        captured["payload"] = json.loads(payload)
        return "sent"

    service._send_one = _send_one  # type: ignore[method-assign]
    asyncio.run(
        service.send_to_user(
            user_email="user@example.com",
            title="Cognis",
            body="Complete assistant reply",
            fallback_body="New reply in this chat.",
            url="/chat/conversation-a",
            tag="conversation-a",
            kind="message",
        )
    )

    assert captured["payload"]["body"] == "New reply in this chat."  # type: ignore[index]
