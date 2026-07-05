from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_user


def _create_test_client(
    monkeypatch: object, tmp_path: Path, env: dict[str, str] | None = None
) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_INTARIS_URL", "http://localhost:8060")  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_MNEMORY_URL", "http://localhost:8050")  # type: ignore[attr-defined]
    monkeypatch.delenv("PUBLIC_INTARIS_UI_URL", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("PUBLIC_MNEMORY_UI_URL", raising=False)  # type: ignore[attr-defined]
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _seed_user(client: TestClient, email: str = "admin@example.com", role: str = "admin") -> None:
    app = client.app

    async def _seed() -> None:
        async with app.state.session_factory() as session:
            await create_user(
                session,
                email=email,
                name="Admin",
                password_hash=app.state.password_hasher.hash("password123"),
                role=role,
            )
            await session.commit()

    asyncio.run(_seed())


def _login(client: TestClient, email: str = "admin@example.com") -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert response.status_code == 200


def test_login_and_me(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)

        response = client.post(
            "/api/auth/login", json={"email": "admin@example.com", "password": "password123"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["user"]["email"] == "admin@example.com"
        assert payload["expires_at"]
        assert client.cookies.get("cognis_session")

        me = client.get("/api/auth/me")
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


def test_refresh_requires_active_browser_session(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.post("/api/auth/refresh")
        assert response.status_code == 401


def test_exchange_token_accepts_known_targets(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _login(client)

        for target in ("intaris", "mnemory"):
            response = client.post(f"/api/v1/auth/exchange-token?target={target}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["target"] == target
            assert payload["expires_in"] == 60
            assert payload["token"]
            assert payload["ui_url"] == (
                "http://localhost:8060" if target == "intaris" else "http://localhost:8050"
            )


def test_exchange_token_uses_public_ui_url_override(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(
        monkeypatch,
        tmp_path,
        env={
            "PUBLIC_INTARIS_UI_URL": "https://intaris.example.com/",
            "PUBLIC_MNEMORY_UI_URL": "https://mnemory.example.com/",
        },
    ) as client:
        _seed_user(client)
        _login(client)

        intaris = client.post("/api/v1/auth/exchange-token?target=intaris")
        mnemory = client.post("/api/v1/auth/exchange-token?target=mnemory")

        assert intaris.status_code == 200
        assert mnemory.status_code == 200
        assert intaris.json()["ui_url"] == "https://intaris.example.com"
        assert mnemory.json()["ui_url"] == "https://mnemory.example.com"


def test_exchange_token_falls_back_to_internal_service_url(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(
        monkeypatch,
        tmp_path,
        env={
            "COGNIS_INTARIS_URL": "http://intaris.internal:8060/",
            "COGNIS_MNEMORY_URL": "http://mnemory.internal:8050/",
        },
    ) as client:
        _seed_user(client)
        _login(client)

        intaris = client.post("/api/v1/auth/exchange-token?target=intaris")
        mnemory = client.post("/api/v1/auth/exchange-token?target=mnemory")

        assert intaris.status_code == 200
        assert mnemory.status_code == 200
        assert intaris.json()["ui_url"] == "http://intaris.internal:8060"
        assert mnemory.json()["ui_url"] == "http://mnemory.internal:8050"


def test_exchange_token_rejects_unknown_target(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _login(client)

        response = client.post("/api/v1/auth/exchange-token?target=unknown")
        assert response.status_code == 422


def test_admin_can_set_user_password(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _seed_user(client, email="user@example.com", role="user")
        _login(client)

        response = client.patch(
            "/api/v1/admin/users/user@example.com",
            json={"password": "newpassword123"},
        )

        assert response.status_code == 200
        assert response.json()["email"] == "user@example.com"

        old_login = client.post(
            "/api/auth/login",
            json={"email": "user@example.com", "password": "password123"},
        )
        assert old_login.status_code == 401

        new_login = client.post(
            "/api/auth/login",
            json={"email": "user@example.com", "password": "newpassword123"},
        )
        assert new_login.status_code == 200


def test_admin_user_password_update_requires_min_length(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _seed_user(client, email="user@example.com", role="user")
        _login(client)

        response = client.patch(
            "/api/v1/admin/users/user@example.com",
            json={"password": "short"},
        )

        assert response.status_code == 422
