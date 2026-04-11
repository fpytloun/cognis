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
    handle_browser_click,
    handle_browser_fill,
    handle_browser_open,
    handle_browser_save_auth_state,
    handle_browser_snapshot,
)
from cognis.tools.registry import ToolExecutionContext


class _FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        enabled: bool = True,
        editable: bool = True,
    ) -> None:
        self.filled: str | None = None
        self.clicked = False
        self.visible = visible
        self.enabled = enabled
        self.editable = editable
        self.children: list[_FakeLocator] | None = None

    @property
    def first(self) -> _FakeLocator:
        return self

    async def count(self) -> int:
        return len(self.children) if self.children is not None else 1

    def nth(self, index: int) -> _FakeLocator:
        if self.children is None:
            if index == 0:
                return self
            raise IndexError(index)
        return self.children[index]

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def is_editable(self) -> bool:
        return self.editable

    async def fill(self, value: str) -> None:
        self.filled = value

    async def click(self) -> None:
        self.clicked = True


class _FakePage:
    def __init__(self) -> None:
        self.locator_calls: list[str] = []
        self.locator_obj = _FakeLocator()
        self.locator_map: dict[str, _FakeLocator] = {}
        self.url = "https://github.com/settings"
        self.last_evaluate_args: tuple[object, ...] = ()

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        return self.locator_map.get(selector, self.locator_obj)

    async def evaluate(self, script: str, *args: object) -> object:
        self.last_evaluate_args = args
        if "querySelectorAll" in script:
            max_elements = int(args[0]) if args else 40
            return [
                {
                    "ref": f"e{i + 1}",
                    "exact_selector": f'[data-cognis-ref="e{i + 1}"]',
                    "selector": f"button:nth-of-type({i + 1})",
                    "tag": "button",
                    "role": "",
                    "text": f"Button {i + 1}",
                    "type": "",
                    "name": "",
                    "placeholder": "",
                    "aria_label": "",
                    "autocomplete": "",
                    "inputmode": "",
                    "visible": True,
                    "enabled": True,
                    "editable": False,
                    "disabled": False,
                    "read_only": False,
                    "value_state": "empty",
                }
                for i in range(max_elements)
            ]
        return ""

    async def title(self) -> str:
        return "Settings"


class _FakeManager:
    def __init__(self) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.session = SimpleNamespace(
            ref_map={"e1": '[data-cognis-ref="e1"]'},
            page=_FakePage(),
            session_id="sess-1",
            profile_mode="persistent_local",
            profile_id="www-reddit-com",
        )

    async def open_session(self, **kwargs: Any) -> Any:
        self.open_calls.append(kwargs)
        return self.session

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
    visible = _FakeLocator(visible=True, enabled=True, editable=True)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = visible
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_fill(
        {"session_id": "sess-1", "ref": "e1", "value": "secret"},
        _context(),
    )
    assert result.is_error is False
    assert manager.session.page.locator_calls == ['[data-cognis-ref="e1"]']
    assert visible.filled == "secret"
    assert result.output == "Filled exact ref e1."


@pytest.mark.asyncio
async def test_browser_fill_errors_when_no_visible_editable_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = _FakeLocator(
        visible=False, enabled=True, editable=True
    )
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    with pytest.raises(ValueError, match="visible enabled editable input"):
        await handle_browser_fill(
            {"session_id": "sess-1", "ref": "e1", "value": "secret"},
            _context(),
        )


@pytest.mark.asyncio
async def test_browser_fill_errors_on_stale_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    aggregate = _FakeLocator()
    aggregate.children = []
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = aggregate
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    with pytest.raises(ValueError, match="stale"):
        await handle_browser_fill(
            {"session_id": "sess-1", "ref": "e1", "value": "secret"},
            _context(),
        )


@pytest.mark.asyncio
async def test_browser_click_prefers_visible_enabled_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    visible = _FakeLocator(visible=True, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = visible
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_click(
        {"session_id": "sess-1", "ref": "e1"},
        _context(),
    )
    assert result.is_error is False
    assert manager.session.page.locator_calls == ['[data-cognis-ref="e1"]']
    assert visible.clicked is True
    assert result.output == "Clicked exact ref e1."


@pytest.mark.asyncio
async def test_browser_click_errors_when_no_visible_enabled_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = _FakeLocator(
        visible=False, enabled=True, editable=False
    )
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    with pytest.raises(ValueError, match="visible enabled target"):
        await handle_browser_click(
            {"session_id": "sess-1", "ref": "e1"},
            _context(),
        )


@pytest.mark.asyncio
async def test_browser_click_errors_on_stale_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    aggregate = _FakeLocator()
    aggregate.children = []
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = aggregate
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    with pytest.raises(ValueError, match="stale"):
        await handle_browser_click(
            {"session_id": "sess-1", "ref": "e1"},
            _context(),
        )


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


@pytest.mark.asyncio
async def test_browser_snapshot_uses_smaller_default_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_snapshot({"session_id": "sess-1"}, _context())
    assert result.output is not None
    assert manager.session.page.last_evaluate_args == (40,)
    assert '"elements"' in result.output
    assert '"exact_selector"' in result.output
    assert manager.session.ref_map["e1"] == '[data-cognis-ref="e1"]'
    assert '"visible": true' in result.output
    assert '"editable": false' in result.output


@pytest.mark.asyncio
async def test_browser_open_uses_default_profile_mode_and_reports_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_open(
        {
            "session_id": "sess-1",
            "url": "https://www.reddit.com/r/openwebui/new/",
            "headless": False,
        },
        _context(),
    )
    assert manager.open_calls[0]["profile_mode"] == "default"
    assert manager.open_calls[0]["profile_id"] is None
    assert '"profile_mode": "persistent_local"' in result.output
    assert '"profile_id": "www-reddit-com"' in result.output


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


@pytest.mark.asyncio
async def test_tool_router_ignores_blank_auth_state_ref() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_FakeCredentialsProvider("https://github.com"),
    )
    resolved = await router._resolve_credential_refs(  # noqa: SLF001
        {"url": "https://github.com/settings", "auth_state_ref": "   "},
        SimpleNamespace(user_email="user@example.com"),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(allowed_credentials=["github_state"]),
        ),
    )
    assert "auth_state" not in resolved
    assert "auth_state_ref" not in resolved


@pytest.mark.asyncio
async def test_tool_router_ignores_blank_value_ref() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_FakeCredentialsProvider("https://github.com"),
    )
    resolved = await router._resolve_credential_refs(  # noqa: SLF001
        {"value_ref": ""},
        SimpleNamespace(user_email="user@example.com"),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(allowed_credentials=["github_state"]),
        ),
    )
    assert "value" not in resolved
    assert "value_ref" not in resolved
