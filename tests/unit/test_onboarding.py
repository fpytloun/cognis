from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.models import OnboardingStateRequest, OnboardingStateResponse
from cognis.store.queries import create_user


def test_state_contract_only_accepts_terminal_client_updates() -> None:
    from pydantic import ValidationError

    assert OnboardingStateResponse().status == "pending"
    with pytest.raises(ValidationError):
        OnboardingStateRequest(status="pending")


def test_compose_empty_admin_credentials_preserve_interactive_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.config import load_config
    from cognis.store.queries import count_users

    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    monkeypatch.setenv("COGNIS_INITIAL_ADMIN_EMAIL", "")
    monkeypatch.setenv("COGNIS_INITIAL_ADMIN_PASSWORD", "")
    config = load_config()
    assert config.initial_admin_email is None
    assert config.initial_admin_password is None
    with TestClient(create_app()) as client:

        async def users() -> int:
            async with client.app.state.session_factory() as session:
                return await count_users(session)

        assert client.portal.call(users) == 0
        assert client.app.state.setup_token_manager is not None
        assert client.get("/api/bootstrap-status").json()["setup_available"] is True


def test_onboarding_is_durable_owner_scoped_and_does_not_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    with TestClient(create_app()) as client:
        app = client.app

        async def seed() -> None:
            async with app.state.session_factory() as session:
                for email, role in (
                    ("owner@localhost", "admin"),
                    ("other@localhost", "user"),
                    ("viewer@localhost", "viewer"),
                ):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=app.state.password_hasher.hash("test-password"),
                        role=role,
                    )
                await session.commit()

        client.portal.call(seed)

        def headers(email: str, role: str) -> dict[str, str]:
            token = app.state.auth_provider.sign_access_token(email, email, role)
            return {"Authorization": f"Bearer {token}"}

        owner = headers("owner@localhost", "admin")
        other = headers("other@localhost", "user")
        viewer = headers("viewer@localhost", "viewer")
        assert client.get("/api/v1/onboarding").status_code == 401
        assert client.get("/api/v1/onboarding", headers=owner).json() == {"status": "pending"}
        assert (
            client.put(
                "/api/v1/onboarding", headers=viewer, json={"status": "dismissed"}
            ).status_code
            == 403
        )
        assert (
            client.put(
                "/api/v1/onboarding", headers=owner, json={"status": "dismissed"}
            ).status_code
            == 200
        )
        assert client.get("/api/v1/onboarding", headers=other).json() == {"status": "pending"}
        assert (
            client.put("/api/v1/onboarding", headers=owner, json={"status": "pending"}).status_code
            == 422
        )
        diagnostics = client.get("/api/v1/system/diagnostics", headers=owner)
        assert diagnostics.status_code == 200
        readiness = diagnostics.json()["readiness"]
        assert readiness["onboarding_pending"] is False
        assert readiness["companion_routes_configured"] is False
        assert readiness["agent_model_configured"] is False
        assert readiness["chat_ready"] is False

    # A fresh app instance represents a different device/controller restart.
    with TestClient(create_app()) as client:
        token = client.app.state.auth_provider.sign_access_token(
            "owner@localhost", "Owner", "admin"
        )
        assert client.get(
            "/api/v1/onboarding", headers={"Authorization": f"Bearer {token}"}
        ).json() == {"status": "dismissed"}
