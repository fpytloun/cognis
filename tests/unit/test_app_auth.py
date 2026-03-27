from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def test_login_and_me(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await session.commit()

        import asyncio

        asyncio.run(_seed())

        response = client.post(
            "/api/auth/login", json={"email": "admin@example.com", "password": "password123"}
        )
        assert response.status_code == 200
        token = response.json()["token"]

        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["email"] == "admin@example.com"


def test_setup_requires_token(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/setup",
            json={
                "token": "invalid",
                "email": "admin@example.com",
                "name": "Admin",
                "password": "password123",
            },
        )
        assert response.status_code == 401


def test_valid_setup_flow_creates_first_admin(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        token = client.app.state.setup_token_manager.issue()
        response = client.post(
            "/api/setup",
            json={
                "token": token,
                "email": "admin@example.com",
                "name": "Admin",
                "password": "password123",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        )
        assert login.status_code == 200


def test_invalid_refresh_token_returns_401(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.post("/api/auth/refresh", json={"refresh_token": "invalid"})
        assert response.status_code == 401
