from __future__ import annotations

from types import SimpleNamespace

import pytest

import cognis.api.routes.settings as settings_routes
import cognis.store.queries as store_queries
from cognis.api.models import WebBackendUpdateRequest, WebConfigStatusResponse


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


def _request(events: list[str], secrets: _Secrets) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=_SessionFactory(events),
                providers=SimpleNamespace(secrets=secrets),
            )
        )
    )


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
async def test_web_backend_update_commits_settings_before_replacing_key(
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

    monkeypatch.setattr(
        settings_routes,
        "require_admin",
        lambda _request: SimpleNamespace(email="admin@example.com"),
    )
    monkeypatch.setattr(settings_routes, "upsert_setting", _upsert_setting)
    monkeypatch.setattr(settings_routes, "web_config_status", _status)

    response = await settings_routes.web_backend_update(
        _request(events, _Secrets(events, {"tavily_api_key"})),
        "tavily",
        WebBackendUpdateRequest(enabled=True, api_key="replacement"),
    )

    assert response.tavily_enabled is True
    assert updated == {"web.tavily_enabled": True}
    assert events == ["settings.commit", "secret.set:tavily_api_key"]


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
        "web.backend": "direct",
        "web.search_backend": "direct",
        "web.fetch_backend": "direct",
    }
    assert events == ["settings.commit", "secret.delete:tavily_api_key"]


@pytest.mark.asyncio
async def test_web_backend_key_failure_occurs_after_safe_settings_commit(
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

    assert updated == {"web.tavily_enabled": True}
    assert events == ["settings.commit", "secret.set:tavily_api_key"]


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
