from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.models import PushSubscriptionRow
from cognis.store.queries import create_user


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
