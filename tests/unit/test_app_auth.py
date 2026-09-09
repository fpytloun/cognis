from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from cognis.api.app import create_app
from cognis.api.routes.auth import _cookie_secure
from cognis.store.models import MfaChallenge, NativeSession
from cognis.store.queries import create_user, get_user, upsert_setting


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


def _set_mfa_policy(client: TestClient, policy: str) -> None:
    async def _set() -> None:
        async with client.app.state.session_factory() as session:
            await upsert_setting(
                session,
                key="security.mfa_policy",
                value=policy,
                category="security",
                updated_by="admin@example.com",
            )
            await session.commit()

    asyncio.run(_set())


def _complete_required_setup(
    client: TestClient, *, mode: str = "browser"
) -> tuple[dict[str, object], list[str]]:
    login = client.post(
        "/api/auth/login",
        json={
            "email": "admin@example.com",
            "password": "password123",
            "mode": mode,
        },
    )
    assert login.status_code == 200
    challenge = login.json()
    assert challenge["status"] == "mfa_setup_required"
    assert client.cookies.get("cognis_session") is None
    assert "token" not in challenge
    setup = client.post(
        "/api/auth/mfa/setup/start",
        json={"challenge_token": challenge["challenge_token"]},
    ).json()
    code = pyotp.TOTP(setup["secret"]).now()
    completed = client.post(
        "/api/auth/mfa/setup/confirm",
        json={"challenge_token": setup["challenge_token"], "code": code},
    )
    assert completed.status_code == 200
    body = completed.json()
    return body, body["recovery_codes"]


def test_required_policy_forces_setup_without_preauth_credentials(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        completed, recovery_codes = _complete_required_setup(client)
        assert completed["status"] == "authenticated"
        assert recovery_codes and len(recovery_codes) == 10
        assert client.cookies.get("cognis_session")


def test_required_challenge_is_hashed_bounded_and_does_not_update_last_login(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        ).json()

        async def _read() -> tuple[MfaChallenge, datetime | None]:
            async with client.app.state.session_factory() as session:
                challenge = (await session.execute(select(MfaChallenge))).scalar_one()
                user = await get_user(session, "admin@example.com")
                assert user is not None
                return challenge, user.last_login_at

        challenge, last_login = asyncio.run(_read())
        assert challenge.token_hash != login["challenge_token"]
        assert login["challenge_token"].encode() not in (challenge.pending_secret or b"")
        assert last_login is None
        for _ in range(5):
            failed = client.post(
                "/api/auth/mfa/setup/confirm",
                json={"challenge_token": login["challenge_token"], "code": "000000"},
            )
            assert failed.status_code == 401
        exhausted = client.post(
            "/api/auth/mfa/setup/start",
            json={"challenge_token": login["challenge_token"]},
        )
        assert exhausted.status_code == 401
        relogin = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        )
        assert relogin.status_code == 429


def test_required_native_setup_preserves_native_mode(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        completed, _ = _complete_required_setup(client, mode="native")
        assert completed["token"]
        assert completed["refresh_token"]
        assert client.cookies.get("cognis_session") is None


def test_optional_policy_still_challenges_enrolled_user(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        _complete_required_setup(client)
        client.post("/api/auth/logout")
        _set_mfa_policy(client, "optional")
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        )
        assert login.json()["status"] == "mfa_required"
        assert client.cookies.get("cognis_session") is None


def test_recovery_code_is_single_use(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        _, recovery_codes = _complete_required_setup(client)
        client.post("/api/auth/logout")
        challenge = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        ).json()
        first = client.post(
            "/api/auth/mfa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": recovery_codes[0],
            },
        )
        assert first.status_code == 200
        client.post("/api/auth/logout")
        second_challenge = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        ).json()
        replay = client.post(
            "/api/auth/mfa/verify",
            json={
                "challenge_token": second_challenge["challenge_token"],
                "code": recovery_codes[0],
            },
        )
        assert replay.status_code == 401


def test_mfa_management_uses_global_attempt_budget(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        _, recovery_codes = _complete_required_setup(client)
        for _ in range(5):
            failed = client.post(
                "/api/auth/mfa/recovery-codes/regenerate",
                json={"current_password": "password123", "code": "000000"},
            )
            assert failed.status_code == 401
        blocked = client.post(
            "/api/auth/mfa/recovery-codes/regenerate",
            json={"current_password": "password123", "code": recovery_codes[0]},
        )
        assert blocked.status_code == 429


def test_mfa_management_current_password_proof_locks_after_failures(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        _, recovery_codes = _complete_required_setup(client)
        for _ in range(5):
            wrong = client.post(
                "/api/auth/mfa/recovery-codes/regenerate",
                json={"current_password": "wrong-password", "code": recovery_codes[0]},
            )
            assert wrong.status_code == 401
            assert wrong.json()["error"]["message"] == "Invalid credentials"
        blocked = client.post(
            "/api/auth/mfa/recovery-codes/regenerate",
            json={"current_password": "password123", "code": recovery_codes[0]},
        )
        assert blocked.status_code == 429


def test_account_enrollment_revokes_session_and_required_policy_blocks_disable(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _login(client)
        missing_password = client.post("/api/auth/mfa/enroll/start")
        assert missing_password.status_code == 422
        wrong_password = client.post(
            "/api/auth/mfa/enroll/start",
            json={"current_password": "wrong-password"},
        )
        assert wrong_password.status_code == 401
        setup = client.post(
            "/api/auth/mfa/enroll/start",
            json={"current_password": "password123"},
        )
        assert setup.status_code == 200
        setup_body = setup.json()
        owner_cookie = client.cookies.get("cognis_session")
        assert owner_cookie is not None
        confirmation_payload = {
            "challenge_token": setup_body["challenge_token"],
            "code": pyotp.TOTP(setup_body["secret"]).now(),
        }
        client.cookies.clear()
        unauthenticated = client.post(
            "/api/auth/mfa/enroll/confirm",
            json=confirmation_payload,
        )
        assert unauthenticated.status_code == 401
        _seed_user(client, email="other@example.com")
        _login(client, email="other@example.com")
        cross_user = client.post(
            "/api/auth/mfa/enroll/confirm",
            json=confirmation_payload,
        )
        assert cross_user.status_code == 403
        client.cookies.clear()
        client.cookies.set("cognis_session", owner_cookie)
        enabled = client.post(
            "/api/auth/mfa/enroll/confirm",
            json=confirmation_payload,
        )
        assert enabled.status_code == 200
        recovery_code = enabled.json()["recovery_codes"][0]
        assert client.get("/api/auth/me").status_code == 401

        challenge = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        ).json()
        verified = client.post(
            "/api/auth/mfa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": recovery_code,
            },
        )
        assert verified.status_code == 200
        _set_mfa_policy(client, "required")
        disabled = client.post(
            "/api/auth/mfa/disable",
            json={"current_password": "password123", "code": recovery_code},
        )
        assert disabled.status_code == 409


def test_mfa_current_password_proofs_share_distinct_rate_limit(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _login(client)
        for _ in range(4):
            wrong = client.post(
                "/api/auth/mfa/enroll/start",
                json={"current_password": "wrong-password"},
            )
            assert wrong.status_code == 401
            assert wrong.json()["error"]["message"] == "Invalid credentials"
        successful = client.post(
            "/api/auth/mfa/enroll/start",
            json={"current_password": "password123"},
        )
        assert successful.status_code == 200

        for _ in range(5):
            wrong = client.post(
                "/api/auth/mfa/enroll/start",
                json={"current_password": "wrong-password"},
            )
            assert wrong.status_code == 401
            assert wrong.json()["error"]["message"] == "Invalid credentials"
        blocked = client.post(
            "/api/auth/mfa/enroll/start",
            json={"current_password": "password123"},
        )
        assert blocked.status_code == 429

        client.cookies.clear()
        regular_login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        )
        assert regular_login.status_code == 200


def test_api_key_cannot_start_mfa_enrollment(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _login(client)
        key = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "MFA enrollment test"},
        ).json()["api_key"]
        client.cookies.clear()
        response = client.post(
            "/api/auth/mfa/enroll/start",
            headers={"X-API-Key": key},
            json={"current_password": "password123"},
        )
        assert response.status_code == 403


def test_stale_setup_challenge_cannot_replace_active_factor(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        _set_mfa_policy(client, "required")
        first_login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        ).json()
        second_login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "password123"},
        ).json()
        first_setup = client.post(
            "/api/auth/mfa/setup/start",
            json={"challenge_token": first_login["challenge_token"]},
        ).json()
        second_setup = client.post(
            "/api/auth/mfa/setup/start",
            json={"challenge_token": second_login["challenge_token"]},
        ).json()
        confirmed = client.post(
            "/api/auth/mfa/setup/confirm",
            json={
                "challenge_token": first_setup["challenge_token"],
                "code": pyotp.TOTP(first_setup["secret"]).now(),
            },
        )
        assert confirmed.status_code == 200
        stale = client.post(
            "/api/auth/mfa/setup/confirm",
            json={
                "challenge_token": second_setup["challenge_token"],
                "code": pyotp.TOTP(second_setup["secret"]).now(),
            },
        )
        assert stale.status_code == 401


def test_factor_changes_invalidate_all_native_access_tokens_durably(
    monkeypatch: object, tmp_path: Path
) -> None:
    def native_password_login(client: TestClient) -> dict[str, object]:
        response = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "password123",
                "mode": "native",
            },
        )
        assert response.status_code == 200
        return response.json()

    def complete_login(
        client: TestClient,
        *,
        mode: str,
        recovery_code: str,
    ) -> dict[str, object]:
        challenge = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "password123",
                "mode": mode,
            },
        ).json()
        completed = client.post(
            "/api/auth/mfa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": recovery_code,
            },
        )
        assert completed.status_code == 200
        return completed.json()

    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        old_tokens = [
            str(native_password_login(client)["token"]),
            str(native_password_login(client)["token"]),
        ]
        _login(client)
        setup = client.post(
            "/api/auth/mfa/enroll/start",
            json={"current_password": "password123"},
        ).json()
        enabled = client.post(
            "/api/auth/mfa/enroll/confirm",
            json={
                "challenge_token": setup["challenge_token"],
                "code": pyotp.TOTP(setup["secret"]).now(),
            },
        )
        assert enabled.status_code == 200
        recovery_codes = enabled.json()["recovery_codes"]
        for token in old_tokens:
            assert (
                client.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 401
            )

    with _create_test_client(monkeypatch, tmp_path) as restarted:
        for token in old_tokens:
            assert (
                restarted.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 401
            )

        current = complete_login(
            restarted,
            mode="native",
            recovery_code=recovery_codes.pop(),
        )
        assert (
            restarted.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {current['token']}"},
            ).status_code
            == 200
        )

        complete_login(restarted, mode="browser", recovery_code=recovery_codes.pop())
        pre_regeneration = [
            str(
                complete_login(
                    restarted,
                    mode="native",
                    recovery_code=recovery_codes.pop(),
                )["token"]
            )
            for _ in range(2)
        ]
        regenerated = restarted.post(
            "/api/auth/mfa/recovery-codes/regenerate",
            json={
                "current_password": "password123",
                "code": recovery_codes.pop(),
            },
        )
        assert regenerated.status_code == 200
        for token in pre_regeneration:
            assert (
                restarted.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 401
            )
        new_codes = regenerated.json()["recovery_codes"]
        current_after_regeneration = complete_login(
            restarted,
            mode="native",
            recovery_code=new_codes.pop(),
        )
        assert (
            restarted.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {current_after_regeneration['token']}"},
            ).status_code
            == 200
        )

        complete_login(restarted, mode="browser", recovery_code=new_codes.pop())
        pre_disable = [
            str(
                complete_login(
                    restarted,
                    mode="native",
                    recovery_code=new_codes.pop(),
                )["token"]
            )
            for _ in range(2)
        ]
        disabled = restarted.post(
            "/api/auth/mfa/disable",
            json={"current_password": "password123", "code": new_codes.pop()},
        )
        assert disabled.status_code == 200
        for token in pre_disable:
            assert (
                restarted.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 401
            )
        current_after_disable = native_password_login(restarted)
        assert (
            restarted.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {current_after_disable['token']}"},
            ).status_code
            == 200
        )


def test_password_change_invalidates_all_native_access_tokens(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        tokens = [
            client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "password123",
                    "mode": "native",
                },
            ).json()["token"]
            for _ in range(2)
        ]
        _login(client)
        changed = client.post(
            "/api/auth/change-password",
            json={
                "current_password": "password123",
                "new_password": "new-password-123",
            },
        )
        assert changed.status_code == 200
        for token in tokens:
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
                "email": "admin@example.com",
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
        assert "token" not in payload
        assert "refresh_token" not in payload
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


def test_untrusted_forwarded_proto_does_not_set_secure_cookie(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        response = client.post(
            "/api/auth/login",
            headers={"X-Forwarded-Proto": "https"},
            json={"email": "admin@example.com", "password": "password123"},
        )
        assert response.status_code == 200
        assert "Secure" not in response.headers["set-cookie"]


def test_trusted_proxy_forwarded_proto_sets_secure_cookie() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": [(b"x-forwarded-proto", b"https")],
            "client": ("10.1.2.3", 12345),
            "server": ("cognis", 80),
            "app": SimpleNamespace(
                state=SimpleNamespace(config=SimpleNamespace(trusted_proxy_cidrs=("10.0.0.0/8",)))
            ),
        }
    )
    assert _cookie_secure(request) is True


def test_native_refresh_rotates_and_survives_app_restart(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        login = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "password123",
                "mode": "native",
            },
        )
        assert login.status_code == 200
        first = login.json()
        assert first["token"]
        assert first["refresh_token"]
        assert client.cookies.get("cognis_session") is None

    with _create_test_client(monkeypatch, tmp_path) as restarted:
        refreshed = restarted.post(
            "/api/auth/refresh",
            json={"mode": "native", "refresh_token": first["refresh_token"]},
        )
        assert refreshed.status_code == 200
        second = refreshed.json()
        assert second["refresh_token"] != first["refresh_token"]
        assert restarted.cookies.get("cognis_session") is None
        replay = restarted.post(
            "/api/auth/refresh",
            json={"mode": "native", "refresh_token": first["refresh_token"]},
        )
        assert replay.status_code == 401
        successor = restarted.post(
            "/api/auth/refresh",
            json={"mode": "native", "refresh_token": second["refresh_token"]},
        )
        assert successor.status_code == 401


def test_native_logout_with_consumed_token_revokes_successor(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        login = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "password123",
                "mode": "native",
            },
        ).json()
        rotated = client.post(
            "/api/auth/refresh",
            json={"mode": "native", "refresh_token": login["refresh_token"]},
        ).json()

        logout = client.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {rotated['token']}"},
            json={"refresh_token": login["refresh_token"]},
        )
        assert logout.status_code == 200
        successor = client.post(
            "/api/auth/refresh",
            json={"mode": "native", "refresh_token": rotated["refresh_token"]},
        )
        assert successor.status_code == 401


def test_native_logout_revocation_is_durable(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        login = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "password123",
                "mode": "native",
            },
        ).json()
        logout = client.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {login['token']}"},
            json={"refresh_token": login["refresh_token"]},
        )
        assert logout.status_code == 200
        rejected = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {login['token']}"},
        )
        assert rejected.status_code == 401

    with _create_test_client(monkeypatch, tmp_path) as restarted:
        response = restarted.post(
            "/api/auth/refresh",
            json={"mode": "native", "refresh_token": login["refresh_token"]},
        )
        assert response.status_code == 401


def test_native_refresh_rejects_expired_session(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        _seed_user(client)
        login = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "password123",
                "mode": "native",
            },
        ).json()

        async def _expire() -> None:
            async with client.app.state.session_factory() as session:
                row = (await session.execute(select(NativeSession))).scalar_one()
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()

        asyncio.run(_expire())
        response = client.post(
            "/api/auth/refresh",
            json={"mode": "native", "refresh_token": login["refresh_token"]},
        )
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
