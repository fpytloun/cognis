from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.security import generate_api_key_material
from cognis.store.queries import create_agent, create_api_key, create_browser_session, create_user


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


def test_middleware_rejects_invalid_api_key(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.get("/api/auth/me", headers={"X-API-Key": "cognis_bad_bad"})
        assert response.status_code == 401
