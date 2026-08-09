from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import cognis.api.routes.secrets as secrets_routes
from cognis.api.app import create_app
from cognis.api.models import SecretUpsertRequest
from cognis.store.queries import create_user


class _SessionContext:
    async def __aenter__(self) -> _SessionContext:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _session_factory() -> _SessionContext:
    return _SessionContext()


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_regular_user_can_create_user_secret(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

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
        headers = _auth_headers(client.app, email="user@example.com", role="user")

        user_response = client.post(
            "/api/v1/secrets",
            headers=headers,
            json={"name": "MCP_TOKEN", "value": "secret", "scope": "user"},
        )
        system_response = client.post(
            "/api/v1/secrets",
            headers=headers,
            json={"name": "SYSTEM_TOKEN", "value": "secret", "scope": "system"},
        )

        assert user_response.status_code == 200
        assert system_response.status_code == 403


@pytest.mark.asyncio
async def test_system_web_secret_mutations_reconfigure_executors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Secrets:
        async def set_secret(self, name: str, *_args: object, **_kwargs: object) -> None:
            events.append(f"set:{name}")

        async def delete_secret(self, name: str, *_args: object, **_kwargs: object) -> bool:
            events.append(f"delete:{name}")
            return True

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                providers=SimpleNamespace(secrets=_Secrets()),
                session_factory=_session_factory,
                settings_update_lock=asyncio.Lock(),
            )
        )
    )

    async def _finalize(_app: object, *, reason: str) -> list[str]:
        events.append(f"reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(secrets_routes, "forbid_mutation_for_viewer", lambda _request: None)
    monkeypatch.setattr(
        secrets_routes,
        "require_current_user",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(secrets_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(
        secrets_routes,
        "finalize_web_executor_reconfigure_for_app",
        _finalize,
    )

    await secrets_routes.secret_upsert(
        request,
        SecretUpsertRequest(
            name="brave_api_key",
            value="secret",
            scope="system",
        ),
    )
    await secrets_routes.secret_delete(
        request,
        "brave_api_key",
        scope="system",
    )

    assert events == [
        "set:brave_api_key",
        "reconfigure:web_secret_upsert:brave_api_key",
        "delete:brave_api_key",
        "reconfigure:web_secret_delete:brave_api_key",
    ]


@pytest.mark.asyncio
async def test_system_web_secret_upsert_finishes_reconfigure_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    mutation_started = asyncio.Event()
    release_mutation = asyncio.Event()

    class _Secrets:
        async def set_secret(self, name: str, *_args: object, **_kwargs: object) -> None:
            mutation_started.set()
            await release_mutation.wait()
            events.append(f"set:{name}")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                providers=SimpleNamespace(secrets=_Secrets()),
                session_factory=_session_factory,
                settings_update_lock=asyncio.Lock(),
            )
        )
    )

    async def _finalize(_app: object, *, reason: str) -> list[str]:
        events.append(f"reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(secrets_routes, "forbid_mutation_for_viewer", lambda _request: None)
    monkeypatch.setattr(
        secrets_routes,
        "require_current_user",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(secrets_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(
        secrets_routes,
        "finalize_web_executor_reconfigure_for_app",
        _finalize,
    )

    task = asyncio.create_task(
        secrets_routes.secret_upsert(
            request,
            SecretUpsertRequest(
                name="brave_api_key",
                value="secret",
                scope="system",
            ),
        )
    )
    await mutation_started.wait()
    task.cancel()
    release_mutation.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == [
        "set:brave_api_key",
        "reconfigure:web_secret_upsert:brave_api_key",
    ]


@pytest.mark.asyncio
async def test_system_web_secret_upsert_uses_web_settings_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    settings_lock = asyncio.Lock()
    await settings_lock.acquire()

    class _Secrets:
        async def set_secret(self, name: str, *_args: object, **_kwargs: object) -> None:
            events.append(f"set:{name}")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                providers=SimpleNamespace(secrets=_Secrets()),
                session_factory=_session_factory,
                settings_update_lock=settings_lock,
            )
        )
    )

    async def _finalize(_app: object, *, reason: str) -> list[str]:
        events.append(f"reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(secrets_routes, "forbid_mutation_for_viewer", lambda _request: None)
    monkeypatch.setattr(
        secrets_routes,
        "require_current_user",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(secrets_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(
        secrets_routes,
        "finalize_web_executor_reconfigure_for_app",
        _finalize,
    )

    task = asyncio.create_task(
        secrets_routes.secret_upsert(
            request,
            SecretUpsertRequest(
                name="brave_api_key",
                value="secret",
                scope="system",
            ),
        )
    )
    await asyncio.sleep(0)
    assert events == []

    settings_lock.release()
    await task
    assert events == [
        "set:brave_api_key",
        "reconfigure:web_secret_upsert:brave_api_key",
    ]
