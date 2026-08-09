from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from cognis.api import middleware as auth_middleware
from cognis.api.app import create_app
from cognis.core.deliverable_links import signed_deliverable_view_link
from cognis.security import generate_api_key_material
from cognis.store.queries import (
    create_agent,
    create_api_key,
    create_browser_session,
    create_conversation,
    create_deliverable,
    create_user,
    delete_api_key,
    disable_user,
    get_api_key,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_public_health_route_bypasses_auth(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.get("/api/health")
        assert response.status_code == 200


def test_signed_artifact_content_route_bypasses_auth(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.get(
            "/api/v1/artifacts/content/images/example/image",
            params={"exp": 1, "sig": "invalid"},
        )
        assert response.status_code != 401


def test_deliverable_private_and_signed_short_links_follow_real_auth_middleware(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-deliverable",
                    owner_email="owner@example.com",
                    name="Deliverable Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-deliverable",
                    context_type="web",
                    context_ref="web:deliverable-auth",
                    context_data={},
                    title="Deliverable auth",
                )
                await create_deliverable(
                    session,
                    conversation_id=conversation.conversation_id,
                    content="# Private document\n\nAuthenticated content.",
                    format="markdown",
                    title="Private document",
                    deliverable_id="dlv_middleware_auth",
                    artifact_store=app.state.artifact_store,
                )
                await session.commit()

        asyncio.run(_seed())
        private_path = "/api/v1/deliverables/dlv_middleware_auth/view"
        assert client.get(private_path).status_code == 401
        headers = _auth_headers(app, email="owner@example.com")
        assert client.get(private_path, headers=headers).status_code == 200

        link = signed_deliverable_view_link(
            app.state.artifact_store,
            "dlv_middleware_auth",
            base_url="http://testserver",
        )
        short_path = urlsplit(link.url).path
        short_response = client.get(short_path)
        assert short_response.status_code == 200
        assert "Authenticated content" in short_response.text

        token = short_path.rsplit("/", 1)[-1]
        public_path = f"/api/v1/deliverables/share/{token}/view"
        assert client.get(public_path).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/deliverables/s/token%2Fview",
        "/api/v1/deliverables/s/token%252Fview",
        "/api/v1/deliverables/share/token%2Fextra/view",
        "/api/v1/deliverables/share/token/%2e%2e/view",
    ],
)
def test_deliverable_public_auth_bypass_rejects_encoded_path_boundaries(
    monkeypatch: object,
    tmp_path: Path,
    path: str,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        assert client.get(path).status_code == 401


def test_middleware_rejects_malformed_bearer_token(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"})
        assert response.status_code == 401


def test_middleware_rejects_wrong_audience_token(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        token = client.app.state.auth_provider.sign_service_jwt(  # type: ignore[attr-defined]
            "user@example.com", "system", ["intaris"]
        )
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_middleware_authenticates_browser_session_cookie(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                _, raw_token = await create_browser_session(
                    session,
                    user_email="user@example.com",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    user_agent="pytest",
                )
                await session.commit()
                return raw_token

        raw_token = asyncio.run(_seed())
        client.cookies.set("cognis_session", raw_token)
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["email"] == "user@example.com"


def test_middleware_rate_limits_jwt_requests(monkeypatch: object, tmp_path: Path) -> None:
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
        app.state.api_rate_limiter.update_limits(
            read_requests_per_minute=1, write_requests_per_minute=1
        )

        headers = _auth_headers(app, email="user@example.com")
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 429


def test_middleware_rate_limits_api_key_requests(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            key_id, api_key = generate_api_key_material()
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_api_key(
                    session,
                    user_email="user@example.com",
                    key_hash=app.state.password_hasher.hash(api_key),
                    name="CLI",
                    key_id=key_id,
                )
                await session.commit()
            return api_key

        api_key = asyncio.run(_seed())
        app.state.api_rate_limiter.update_limits(
            read_requests_per_minute=1, write_requests_per_minute=1
        )

        assert client.get("/api/auth/me", headers={"X-API-Key": api_key}).status_code == 200
        assert client.get("/api/auth/me", headers={"X-API-Key": api_key}).status_code == 429


def test_middleware_rate_limit_applies_across_different_read_routes(
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
                await session.commit()

        asyncio.run(_seed())
        app.state.api_rate_limiter.update_limits(
            read_requests_per_minute=1, write_requests_per_minute=1
        )

        headers = _auth_headers(app, email="user@example.com")
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        assert client.get("/api/v1/settings", headers=headers).status_code == 429


def test_middleware_rate_limit_applies_across_different_write_routes(
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
        app.state.api_rate_limiter.update_limits(
            read_requests_per_minute=5, write_requests_per_minute=1
        )

        headers = _auth_headers(app, email="user@example.com")
        assert (
            client.post(
                "/api/v1/tasks",
                headers=headers,
                json={"agent_id": "agent-1", "title": "Task one"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/workflows",
                headers=headers,
                json={
                    "name": "Workflow one",
                    "steps": [{"name": "step_one", "type": "run", "prompt": "Do work"}],
                },
            ).status_code
            == 429
        )


def test_client_performance_endpoint_uses_authenticated_write_rate_limit(
    monkeypatch: object,
    tmp_path: Path,
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
                await session.commit()

        asyncio.run(_seed())
        app.state.api_rate_limiter.update_limits(
            read_requests_per_minute=5,
            write_requests_per_minute=1,
        )
        headers = _auth_headers(app, email="user@example.com")
        payload = {"metric": "cached_restore_ms", "duration_ms": 12.5}

        assert (
            client.post(
                "/api/v1/chat/v2/client-performance",
                headers=headers,
                json=payload,
            ).status_code
            == 204
        )
        assert (
            client.post(
                "/api/v1/chat/v2/client-performance",
                headers=headers,
                json=payload,
            ).status_code
            == 429
        )


def test_middleware_rejects_invalid_api_key(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.get("/api/auth/me", headers={"X-API-Key": "cognis_bad_bad"})
        assert response.status_code == 401


def test_api_key_cache_debounces_touch_and_invalidates_on_delete(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            key_id, api_key = generate_api_key_material()
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_api_key(
                    session,
                    user_email="user@example.com",
                    key_hash=app.state.password_hasher.hash(api_key),
                    name="CLI",
                    key_id=key_id,
                )
                await session.commit()
            return key_id, api_key

        async def _last_used_at(key_id: str) -> datetime | None:
            async with app.state.session_factory() as session:
                record = await get_api_key(session, key_id)
                return None if record is None else record.last_used_at

        async def _delete(key_id: str) -> None:
            async with app.state.session_factory() as session:
                assert await delete_api_key(session, key_id, "user@example.com")
                await session.commit()

        key_id, api_key = asyncio.run(_seed())

        assert client.get("/api/auth/me", headers={"X-API-Key": api_key}).status_code == 200
        first_last_used = asyncio.run(_last_used_at(key_id))
        assert first_last_used is not None

        assert client.get("/api/auth/me", headers={"X-API-Key": api_key}).status_code == 200
        assert asyncio.run(_last_used_at(key_id)) == first_last_used

        asyncio.run(_delete(key_id))
        response = client.get("/api/auth/me", headers={"X-API-Key": api_key})
        assert response.status_code == 401


def test_jwt_active_user_cache_invalidates_on_disable(monkeypatch: object, tmp_path: Path) -> None:
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

        async def _disable() -> None:
            async with app.state.session_factory() as session:
                assert await disable_user(session, "user@example.com", disabled_by="admin")
                await session.commit()

        asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        assert client.get("/api/auth/me", headers=headers).status_code == 200
        asyncio.run(_disable())
        response = client.get("/api/auth/me", headers=headers)
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_api_key_verify_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _blocking_verify(_password_hasher: object, _api_key: str, _key_hash: str) -> bool:
        time.sleep(0.05)
        return True

    monkeypatch.setattr(auth_middleware, "verify_api_key", _blocking_verify)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.03
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
            ticks += 1

    verified, _ = await asyncio.gather(
        auth_middleware._verify_api_key_async(object(), "api-key", "key-hash"),
        _ticker(),
    )

    assert verified is True
    assert ticks > 0
