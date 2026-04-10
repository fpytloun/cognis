from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.tool_router import ToolRouter
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.credential import CredentialRecord, CredentialResolution
from cognis.models.tool import ExecutorHandle
from cognis.tools.executor.browser import handlers as browser_handlers
from cognis.tools.executor.browser.handlers import (
    handle_browser_fill,
    handle_browser_save_auth_state,
)
from cognis.tools.registry import ToolExecutionContext


class _FakeLocator:
    def __init__(self) -> None:
        self.filled: str | None = None

    @property
    def first(self) -> _FakeLocator:
        return self

    async def fill(self, value: str) -> None:
        self.filled = value


class _FakePage:
    def __init__(self) -> None:
        self.locator_calls: list[str] = []
        self.locator_obj = _FakeLocator()
        self.url = "https://github.com/settings"

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        return self.locator_obj


class _FakeManager:
    def __init__(self) -> None:
        self.session = SimpleNamespace(
            ref_map={"e1": "#password"},
            page=_FakePage(),
        )

    def get_session(self, session_id: str) -> Any:
        assert session_id == "sess-1"
        return self.session

    async def storage_state(self, session_id: str) -> dict[str, Any]:
        assert session_id == "sess-1"
        return {"cookies": [{"name": "sid"}], "origins": []}


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="exec-1", executor_type="in_process"),
        runtime_metadata={},
    )


@pytest.mark.asyncio
async def test_browser_fill_uses_ref_map_and_resolved_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_fill(
        {"session_id": "sess-1", "ref": "e1", "value": "secret"},
        _context(),
    )
    assert result.is_error is False
    assert manager.session.page.locator_calls == ["#password"]
    assert manager.session.page.locator_obj.filled == "secret"


@pytest.mark.asyncio
async def test_browser_save_auth_state_returns_persistence_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_save_auth_state(
        {
            "session_id": "sess-1",
            "credential_id": "github_state",
            "label": "GitHub saved session",
            "origin": "https://github.com",
        },
        _context(),
    )
    assert result.metadata is not None
    auth_state = result.metadata["browser_auth_state"]
    assert auth_state["credential_id"] == "github_state"
    assert auth_state["metadata"]["origin"] == "https://github.com"
    assert auth_state["payload"]["storage_state"]["cookies"][0]["name"] == "sid"


class _FakeCredentialsProvider:
    def __init__(self, origin: str) -> None:
        self.origin = origin

    async def resolve_ref(
        self, ref: str, *, agent: AgentDefinition, user_email: str
    ) -> CredentialResolution:
        return CredentialResolution(
            credential_id="github_state",
            field=None,
            value={"storage_state": {"cookies": [], "origins": []}},
        )

    async def get_credential(self, credential_id: str, user_email: str) -> CredentialRecord:
        return CredentialRecord(
            credential_id=credential_id,
            user_email=user_email,
            kind="browser_storage_state",
            label="Saved state",
            metadata={"origin": self.origin},
        )


@pytest.mark.asyncio
async def test_tool_router_unwraps_storage_state_for_auth_state_ref() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_FakeCredentialsProvider("https://github.com"),
    )
    resolved = await router._resolve_credential_refs(  # noqa: SLF001
        {"url": "https://github.com/settings", "auth_state_ref": "$credential:github_state"},
        SimpleNamespace(user_email="user@example.com"),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(allowed_credentials=["github_state"]),
        ),
    )
    assert resolved["auth_state"] == {"cookies": [], "origins": []}


@pytest.mark.asyncio
async def test_tool_router_rejects_cross_origin_auth_state_ref() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_FakeCredentialsProvider("https://github.com"),
    )
    with pytest.raises(PermissionError):
        await router._resolve_credential_refs(  # noqa: SLF001
            {"url": "https://evil.example", "auth_state_ref": "$credential:github_state"},
            SimpleNamespace(user_email="user@example.com"),
            AgentDefinition(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
                permissions=AgentPermissions(allowed_credentials=["github_state"]),
            ),
        )
