from __future__ import annotations

import asyncio
from pathlib import Path

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

        asyncio.run(_seed())
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

        asyncio.run(_set_error())
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
