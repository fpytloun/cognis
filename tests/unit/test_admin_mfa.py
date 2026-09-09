from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

import cognis.cli.admin as admin_module
from cognis.api.app import create_app
from cognis.cli.admin import admin_app
from cognis.mfa import activate_totp_factor
from cognis.store.models import UserTotpFactor
from cognis.store.queries import create_browser_session, create_user


def test_reset_mfa_command_removes_factor_and_revokes_sessions(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_INTARIS_URL", "http://localhost:8060")  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_MNEMORY_URL", "http://localhost:8050")  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:
        app = client.app

        async def _seed() -> None:
            from datetime import UTC, datetime, timedelta

            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="reset@example.com",
                    name="Reset",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await activate_totp_factor(
                    session,
                    user_email="reset@example.com",
                    encrypted_secret=app.state.mfa_cipher.encrypt("JBSWY3DPEHPK3PXP"),
                    accepted_counter=1,
                )
                await create_browser_session(
                    session,
                    user_email="reset@example.com",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    auth_version=0,
                )
                await session.commit()

        asyncio.run(_seed())
        old_tokens = [
            app.state.auth_provider.sign_access_token(
                "reset@example.com",
                "Reset",
                "user",
                auth_version=0,
            )
            for _ in range(2)
        ]

        async def _runtime() -> tuple[object, object, object]:
            return app.state.config, app.state.password_hasher, app.state.session_factory

        monkeypatch.setattr(admin_module, "_get_runtime", _runtime)  # type: ignore[attr-defined]
        result = CliRunner().invoke(admin_app, ["reset-mfa", "reset@example.com"])
        assert result.exit_code == 0
        assert "Reset MFA and revoked sessions" in result.stdout

        async def _factor() -> UserTotpFactor | None:
            async with app.state.session_factory() as session:
                return (
                    await session.execute(
                        select(UserTotpFactor).where(
                            UserTotpFactor.user_email == "reset@example.com"
                        )
                    )
                ).scalar_one_or_none()

        assert asyncio.run(_factor()) is None
        for token in old_tokens:
            assert (
                client.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 401
            )
        current = client.post(
            "/api/auth/login",
            json={
                "email": "reset@example.com",
                "password": "password123",
                "mode": "native",
            },
        ).json()["token"]
        assert (
            client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {current}"},
            ).status_code
            == 200
        )


def test_reset_password_command_invalidates_all_access_tokens(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="password-reset@example.com",
                    name="Password Reset",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())
        old_tokens = [
            app.state.auth_provider.sign_access_token(
                "password-reset@example.com",
                "Password Reset",
                "user",
                auth_version=0,
            )
            for _ in range(2)
        ]

        async def _runtime() -> tuple[object, object, object]:
            return app.state.config, app.state.password_hasher, app.state.session_factory

        monkeypatch.setattr(admin_module, "_get_runtime", _runtime)  # type: ignore[attr-defined]
        result = CliRunner().invoke(
            admin_app,
            ["reset-password", "password-reset@example.com"],
            input="new-password-123\nnew-password-123\n",
        )
        assert result.exit_code == 0
        for token in old_tokens:
            assert (
                client.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 401
            )
        current = client.post(
            "/api/auth/login",
            json={
                "email": "password-reset@example.com",
                "password": "new-password-123",
                "mode": "native",
            },
        ).json()["token"]
        assert (
            client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {current}"},
            ).status_code
            == 200
        )
