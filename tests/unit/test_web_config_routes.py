from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import cognis.api.routes.settings as settings_routes
import cognis.store.queries as store_queries
from cognis.api.models import (
    WebBackendUpdateRequest,
    WebConfigStatusResponse,
    WebDefaultsUpdateRequest,
)


@pytest.fixture(autouse=True)
def _stub_web_executor_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _schedule(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(
        settings_routes,
        "finalize_web_executor_reconfigure_for_app",
        _schedule,
    )


class _SessionContext:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _SessionContext:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.events.append("settings.commit")


class _SessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __call__(self) -> _SessionContext:
        return _SessionContext(self.events)


class _BlockingCommitSession(_SessionContext):
    def __init__(
        self,
        events: list[str],
        commit_started: asyncio.Event,
        release_commit: asyncio.Event,
    ) -> None:
        super().__init__(events)
        self.commit_started = commit_started
        self.release_commit = release_commit

    async def commit(self) -> None:
        self.events.append("settings.commit.started")
        self.commit_started.set()
        await self.release_commit.wait()
        self.events.append("settings.commit.completed")


class _BlockingCommitSessionFactory:
    def __init__(
        self,
        events: list[str],
        commit_started: asyncio.Event,
        release_commit: asyncio.Event,
    ) -> None:
        self.events = events
        self.commit_started = commit_started
        self.release_commit = release_commit

    def __call__(self) -> _BlockingCommitSession:
        return _BlockingCommitSession(
            self.events,
            self.commit_started,
            self.release_commit,
        )


class _FailingCommitSession(_SessionContext):
    async def commit(self) -> None:
        self.events.append("settings.commit.failed")
        raise RuntimeError("commit failed")


class _FailingCommitSessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __call__(self) -> _FailingCommitSession:
        return _FailingCommitSession(self.events)


class _PostgresLockConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.invalidated = False

    async def execute(self, statement: object, _params: object) -> SimpleNamespace:
        self.calls.append(str(statement))
        await asyncio.sleep(0)
        return SimpleNamespace()

    async def invalidate(self) -> None:
        self.invalidated = True


class _PostgresLockSession(_SessionContext):
    def __init__(self, connection: _PostgresLockConnection) -> None:
        super().__init__([])
        self.connection_value = connection

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def connection(self) -> _PostgresLockConnection:
        return self.connection_value


class _PostgresLockFactory:
    def __init__(self, connection: _PostgresLockConnection) -> None:
        self.session = _PostgresLockSession(connection)

    def __call__(self) -> _PostgresLockSession:
        return self.session


class _Secrets:
    def __init__(self, events: list[str], configured: set[str]) -> None:
        self.events = events
        self.configured = configured

    async def get_secret(self, name: str, _user_email: str) -> str:
        if name not in self.configured:
            raise KeyError(name)
        return "stored-key"

    async def set_secret(self, name: str, *_: object, **__: object) -> None:
        self.events.append(f"secret.set:{name}")
        self.configured.add(name)

    async def delete_secret(self, name: str, *_: object, **__: object) -> bool:
        self.events.append(f"secret.delete:{name}")
        self.configured.discard(name)
        return True


class _FailingSetSecrets(_Secrets):
    async def set_secret(self, name: str, *_: object, **__: object) -> None:
        self.events.append(f"secret.set:{name}")
        raise RuntimeError("secret store unavailable")


class _BlockingDeleteSecrets(_Secrets):
    def __init__(self, events: list[str], configured: set[str]) -> None:
        super().__init__(events, configured)
        self.delete_started = asyncio.Event()
        self.release_delete = asyncio.Event()

    async def delete_secret(self, name: str, *_: object, **__: object) -> bool:
        self.events.append(f"secret.delete:{name}")
        self.delete_started.set()
        await self.release_delete.wait()
        self.configured.discard(name)
        return True


def _request(events: list[str], secrets: _Secrets) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=_SessionFactory(events),
                providers=SimpleNamespace(secrets=secrets),
                settings_update_lock=asyncio.Lock(),
            )
        )
    )


@pytest.mark.asyncio
async def test_web_settings_distributed_lock_unlocks_when_body_is_cancelled() -> None:
    connection = _PostgresLockConnection()
    entered = asyncio.Event()

    async def _worker() -> None:
        async with settings_routes._web_settings_distributed_lock(_PostgresLockFactory(connection)):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(_worker())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.calls == [
        "SELECT pg_advisory_lock(:lock_id)",
        "SELECT pg_advisory_unlock(:lock_id)",
    ]
    assert connection.invalidated is False


@pytest.mark.asyncio
async def test_web_config_status_separates_configured_and_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, object] = {
        "web.search_backend": "tavily",
        "web.fetch_backend": "tavily",
        "web.tavily_enabled": False,
        "web.brave_enabled": True,
        "web.searxng_enabled": False,
        "web.searxng_url": "https://search.example.com",
        "web.searxng_engines": "google",
    }

    async def _get_setting_value(_session: object, key: str, default: object = None) -> object:
        return values.get(key, default)

    monkeypatch.setattr(settings_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    response = await settings_routes.web_config_status(
        _request([], _Secrets([], {"tavily_api_key"}))
    )

    assert response.tavily_configured is True
    assert response.tavily_enabled is False
    assert response.searxng_configured is True
    assert response.searxng_enabled is False
    assert response.search_backend == "direct"
    assert response.fetch_backend == "direct"
    assert response.available_search_backends == ["direct"]
    assert response.searxng_engines == "google"


@pytest.mark.asyncio
async def test_web_backend_update_stores_key_before_enabling_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    updated: dict[str, object] = {}

    async def _upsert_setting(
        _session: object,
        *,
        key: str,
        value: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        updated[key] = value
        return SimpleNamespace()

    async def _status(_request: object) -> WebConfigStatusResponse:
        return WebConfigStatusResponse(tavily_configured=True, tavily_enabled=True)

    async def _schedule(_app: object, *, reason: str) -> list[str]:
        events.append(f"executor.reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(settings_routes, "web_config_status", _status)
    monkeypatch.setattr(settings_routes, "finalize_web_executor_reconfigure_for_app", _schedule)

    response = await settings_routes.web_backend_update(
        _request(events, _Secrets(events, {"tavily_api_key"})),
        "tavily",
        WebBackendUpdateRequest(enabled=True, api_key="replacement"),
    )

    assert response.tavily_enabled is True
    assert updated == {"web.tavily_enabled": True}
    assert events == [
        "secret.set:tavily_api_key",
        "settings.commit",
        "executor.reconfigure:web_backend_update:tavily",
    ]


@pytest.mark.asyncio
async def test_web_backend_removal_disables_and_falls_back_before_deleting_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    updated: dict[str, object] = {}
    current = {
        "web.backend": "tavily",
        "web.search_backend": "tavily",
        "web.fetch_backend": "tavily",
    }

    async def _get_setting_value(_session: object, key: str, default: object = None) -> object:
        return current.get(key, default)

    async def _upsert_setting(
        _session: object,
        *,
        key: str,
        value: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        updated[key] = value
        return SimpleNamespace()

    async def _status(_request: object) -> WebConfigStatusResponse:
        return WebConfigStatusResponse()

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(settings_routes, "web_config_status", _status)

    await settings_routes.web_backend_update(
        _request(events, _Secrets(events, {"tavily_api_key"})),
        "tavily",
        WebBackendUpdateRequest(enabled=False, remove_configuration=True),
    )

    assert updated == {
        "web.tavily_enabled": False,
        "web.search_backend": "direct",
        "web.fetch_backend": "direct",
    }
    assert events == ["settings.commit", "secret.delete:tavily_api_key"]


@pytest.mark.asyncio
async def test_web_defaults_update_writes_only_canonical_keys_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    updated: dict[str, object] = {}
    statuses = iter(
        [
            WebConfigStatusResponse(
                available_search_backends=["direct", "brave"],
                available_fetch_backends=["direct", "browser"],
            ),
            WebConfigStatusResponse(search_backend="brave", fetch_backend="browser"),
        ]
    )

    async def _upsert_setting(
        _session: object,
        *,
        key: str,
        value: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        updated[key] = value
        return SimpleNamespace()

    async def _status(_request: object) -> WebConfigStatusResponse:
        return next(statuses)

    async def _schedule(_app: object, *, reason: str) -> list[str]:
        events.append(f"executor.reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(settings_routes, "web_config_status", _status)
    monkeypatch.setattr(settings_routes, "finalize_web_executor_reconfigure_for_app", _schedule)

    response = await settings_routes.web_defaults_update(
        _request(events, _Secrets(events, set())),
        WebDefaultsUpdateRequest(search_backend="brave", fetch_backend="browser"),
    )

    assert updated == {
        "web.search_backend": "brave",
        "web.fetch_backend": "browser",
    }
    assert "web.backend" not in updated
    assert events == [
        "settings.commit",
        "executor.reconfigure:web_defaults_update",
    ]
    assert response.search_backend == "brave"
    assert response.fetch_backend == "browser"


@pytest.mark.asyncio
async def test_web_defaults_update_rejects_unavailable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _status(_request: object) -> WebConfigStatusResponse:
        return WebConfigStatusResponse(
            available_search_backends=["direct"],
            available_fetch_backends=["direct", "browser"],
        )

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "web_config_status", _status)

    with pytest.raises(Exception, match="not configured and enabled"):
        await settings_routes.web_defaults_update(
            _request([], _Secrets([], set())),
            WebDefaultsUpdateRequest(search_backend="brave", fetch_backend="browser"),
        )


@pytest.mark.asyncio
async def test_web_defaults_update_does_not_commit_partial_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    calls = 0

    async def _upsert_setting(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second write failed")
        return SimpleNamespace()

    async def _status(_request: object) -> WebConfigStatusResponse:
        return WebConfigStatusResponse(
            available_search_backends=["direct", "brave"],
            available_fetch_backends=["direct", "browser"],
        )

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(settings_routes, "web_config_status", _status)

    with pytest.raises(RuntimeError, match="second write failed"):
        await settings_routes.web_defaults_update(
            _request(events, _Secrets(events, set())),
            WebDefaultsUpdateRequest(search_backend="brave", fetch_backend="browser"),
        )

    assert calls == 2
    assert "settings.commit" not in events


@pytest.mark.asyncio
async def test_web_defaults_update_finishes_commit_and_reconfigure_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    statuses = iter(
        [
            WebConfigStatusResponse(
                available_search_backends=["direct", "brave"],
                available_fetch_backends=["direct", "browser"],
            ),
        ]
    )

    async def _status(_request: object) -> WebConfigStatusResponse:
        return next(statuses)

    async def _upsert_setting(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace()

    async def _finalize(_app: object, *, reason: str) -> list[str]:
        events.append(f"executor.reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "web_config_status", _status)
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(
        settings_routes,
        "finalize_web_executor_reconfigure_for_app",
        _finalize,
    )
    request = _request(events, _Secrets(events, set()))
    request.app.state.session_factory = _BlockingCommitSessionFactory(
        events,
        commit_started,
        release_commit,
    )

    task = asyncio.create_task(
        settings_routes.web_defaults_update(
            request,
            WebDefaultsUpdateRequest(search_backend="brave", fetch_backend="browser"),
        )
    )
    await commit_started.wait()
    task.cancel()
    release_commit.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == [
        "settings.commit.started",
        "settings.commit.completed",
        "executor.reconfigure:web_defaults_update",
    ]


@pytest.mark.asyncio
async def test_web_backend_key_failure_does_not_enable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    updated: dict[str, object] = {}

    async def _upsert_setting(
        _session: object,
        *,
        key: str,
        value: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        updated[key] = value
        return SimpleNamespace()

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)

    with pytest.raises(RuntimeError, match="secret store unavailable"):
        await settings_routes.web_backend_update(
            _request(events, _FailingSetSecrets(events, set())),
            "tavily",
            WebBackendUpdateRequest(enabled=True, api_key="new-key"),
        )

    assert updated == {}
    assert events == ["secret.set:tavily_api_key"]


@pytest.mark.asyncio
async def test_web_backend_key_rotation_reconfigures_when_settings_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def _upsert_setting(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace()

    async def _finalize(_app: object, *, reason: str) -> list[str]:
        events.append(f"executor.reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(
        settings_routes,
        "finalize_web_executor_reconfigure_for_app",
        _finalize,
    )
    request = _request(events, _Secrets(events, {"tavily_api_key"}))
    request.app.state.session_factory = _FailingCommitSessionFactory(events)

    with pytest.raises(RuntimeError, match="commit failed"):
        await settings_routes.web_backend_update(
            request,
            "tavily",
            WebBackendUpdateRequest(enabled=True, api_key="replacement"),
        )

    assert events == [
        "secret.set:tavily_api_key",
        "settings.commit.failed",
        "executor.reconfigure:web_backend_update:tavily",
    ]


@pytest.mark.asyncio
async def test_web_backend_removal_reconfigures_after_cancellation_during_secret_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    secrets = _BlockingDeleteSecrets(events, {"tavily_api_key"})

    async def _get_setting_value(_session: object, key: str, default: object = None) -> object:
        return {
            "web.search_backend": "tavily",
            "web.fetch_backend": "tavily",
        }.get(key, default)

    async def _upsert_setting(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace()

    async def _finalize(_app: object, *, reason: str) -> list[str]:
        events.append(f"executor.reconfigure:{reason}")
        return ["executor-1"]

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(
        settings_routes,
        "finalize_web_executor_reconfigure_for_app",
        _finalize,
    )

    task = asyncio.create_task(
        settings_routes.web_backend_update(
            _request(events, secrets),
            "tavily",
            WebBackendUpdateRequest(enabled=False, remove_configuration=True),
        )
    )
    await secrets.delete_started.wait()
    task.cancel()
    secrets.release_delete.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == [
        "settings.commit",
        "secret.delete:tavily_api_key",
        "executor.reconfigure:web_backend_update:tavily",
    ]


@pytest.mark.asyncio
async def test_searxng_disable_and_reenable_preserve_omitted_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stored: dict[str, object] = {
        "web.searxng_url": "https://search.example.com",
        "web.searxng_engines": "google,bing",
        "web.searxng_categories": "general",
        "web.searxng_language": "en-US",
    }

    async def _get_setting_value(_session: object, key: str, default: object = None) -> object:
        return stored.get(key, default)

    async def _upsert_setting(
        _session: object,
        *,
        key: str,
        value: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        stored[key] = value
        return SimpleNamespace()

    async def _status(_request: object) -> WebConfigStatusResponse:
        return WebConfigStatusResponse(
            searxng_url=str(stored["web.searxng_url"]),
            searxng_configured=True,
            searxng_enabled=bool(stored["web.searxng_enabled"]),
        )

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(settings_routes, "web_config_status", _status)
    request = _request(events, _Secrets(events, set()))

    disabled = await settings_routes.web_backend_update(
        request,
        "searxng",
        WebBackendUpdateRequest(enabled=False),
    )
    enabled = await settings_routes.web_backend_update(
        request,
        "searxng",
        WebBackendUpdateRequest(enabled=True),
    )

    assert disabled.searxng_url == "https://search.example.com"
    assert disabled.searxng_enabled is False
    assert enabled.searxng_url == "https://search.example.com"
    assert enabled.searxng_enabled is True
    assert stored["web.searxng_engines"] == "google,bing"
    assert stored["web.searxng_categories"] == "general"
    assert stored["web.searxng_language"] == "en-US"
