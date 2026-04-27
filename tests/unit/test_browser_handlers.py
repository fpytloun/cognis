from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.tool_router import ToolRouter
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.credential import CredentialAccessError, CredentialRecord, CredentialResolution
from cognis.models.tool import ExecutorHandle
from cognis.tools.executor.browser import handlers as browser_handlers
from cognis.tools.executor.browser.handlers import (
    handle_browser_click,
    handle_browser_eval,
    handle_browser_fill,
    handle_browser_focus,
    handle_browser_get_console,
    handle_browser_get_network,
    handle_browser_list_profiles,
    handle_browser_list_sessions,
    handle_browser_open,
    handle_browser_query,
    handle_browser_save_auth_state,
    handle_browser_snapshot,
    handle_browser_submit_form,
    handle_browser_type,
    handle_browser_wait_for,
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
        self.focused = False
        self.typed: tuple[str, int | None] | None = None
        self.evaluated: str | None = None
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

    async def click(self, *, button: str = "left", click_count: int = 1) -> None:
        del button, click_count
        self.clicked = True

    async def focus(self) -> None:
        self.focused = True

    async def type(self, text: str) -> None:
        self.typed = (text, None)

    async def press_sequentially(self, text: str, delay: int | None = None) -> None:
        self.typed = (text, delay)

    async def evaluate(self, script: str) -> None:
        self.evaluated = script


class _FakePage:
    def __init__(self) -> None:
        self.locator_calls: list[str] = []
        self.locator_obj = _FakeLocator()
        self.locator_map: dict[str, _FakeLocator] = {}
        self.url = "https://github.com/settings"
        self.last_evaluate_args: tuple[object, ...] = ()
        self.evaluate_scripts: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        return self.locator_map.get(selector, self.locator_obj)

    async def evaluate(self, script: str, *args: object) -> object:
        self.last_evaluate_args = args
        self.evaluate_scripts.append(script)
        if "browser_eval script must evaluate to a function" in script:
            payload = args[0] if args else {}
            if isinstance(payload, dict) and payload.get("script") == "() => ({ ok: true })":
                return {"ok": True}
            raise RuntimeError("browser_eval script must evaluate to a function")
        if "querySelectorAll" in script:
            payload = args[0] if args else 40
            max_elements = (
                int(payload.get("maxResults", 40)) if isinstance(payload, dict) else int(payload)
            )
            selector = payload.get("selector") if isinstance(payload, dict) else None
            mode = payload.get("mode", "actionable") if isinstance(payload, dict) else "actionable"
            assign_attr = (
                payload.get("assignAttr") if isinstance(payload, dict) else "data-cognis-ref"
            )
            include_computed = (
                bool(payload.get("includeComputed", False)) if isinstance(payload, dict) else False
            )
            if selector == "input":
                items = [
                    {
                        "ref": "e1",
                        "exact_selector": f'[{assign_attr}="e1"]' if assign_attr else None,
                        "selector": selector,
                        "tag": "input",
                        "role": "",
                        "text": "",
                        "type": "text",
                        "name": "username",
                        "placeholder": "Username",
                        "aria_label": "",
                        "label_text": "Username or email",
                        "autocomplete": "username",
                        "inputmode": "text",
                        "visible": True,
                        "enabled": True,
                        "editable": True,
                        "disabled": False,
                        "read_only": False,
                        "value_state": "empty",
                        "is_clickable": False,
                        "is_fillable": True,
                        "purpose_score": 0,
                        "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 20},
                        "frame_url": self.url,
                        "frame_name": "",
                        "in_shadow_dom": False,
                        "computed": {"display": "block", "visibility": "visible", "opacity": "1"}
                        if include_computed
                        else None,
                    }
                ]
                if mode == "clickable":
                    return []
                return items[:max_elements]
            if selector == ".ambiguous":
                return [
                    {
                        "ref": "e1",
                        "exact_selector": f'[{assign_attr}="e1"]' if assign_attr else None,
                        "selector": selector,
                        "tag": "input",
                        "role": "",
                        "text": "",
                        "type": "text",
                        "name": "otp1",
                        "placeholder": "Code",
                        "aria_label": "",
                        "label_text": "Code",
                        "autocomplete": "one-time-code",
                        "inputmode": "numeric",
                        "visible": True,
                        "enabled": True,
                        "editable": True,
                        "disabled": False,
                        "read_only": False,
                        "value_state": "redacted",
                        "is_clickable": False,
                        "is_fillable": True,
                        "purpose_score": 120,
                        "bounding_box": {"x": 0, "y": 0, "width": 20, "height": 20},
                        "frame_url": self.url,
                        "frame_name": "",
                        "in_shadow_dom": False,
                        "computed": None,
                    },
                    {
                        "ref": "e2",
                        "exact_selector": f'[{assign_attr}="e2"]' if assign_attr else None,
                        "selector": selector,
                        "tag": "input",
                        "role": "",
                        "text": "",
                        "type": "text",
                        "name": "otp2",
                        "placeholder": "Code",
                        "aria_label": "",
                        "label_text": "Code",
                        "autocomplete": "one-time-code",
                        "inputmode": "numeric",
                        "visible": True,
                        "enabled": True,
                        "editable": True,
                        "disabled": False,
                        "read_only": False,
                        "value_state": "redacted",
                        "is_clickable": False,
                        "is_fillable": True,
                        "purpose_score": 120,
                        "bounding_box": {"x": 25, "y": 0, "width": 20, "height": 20},
                        "frame_url": self.url,
                        "frame_name": "",
                        "in_shadow_dom": False,
                        "computed": None,
                    },
                ]
            return [
                {
                    "ref": f"e{i + 1}",
                    "exact_selector": f'[{assign_attr}="e{i + 1}"]' if assign_attr else None,
                    "selector": f"button:nth-of-type({i + 1})",
                    "tag": "button",
                    "role": "",
                    "text": f"Button {i + 1}",
                    "type": "",
                    "name": "",
                    "placeholder": "",
                    "aria_label": "",
                    "label_text": "",
                    "autocomplete": "",
                    "inputmode": "",
                    "visible": True,
                    "enabled": True,
                    "editable": False,
                    "disabled": False,
                    "read_only": False,
                    "value_state": "empty",
                    "is_clickable": True,
                    "is_fillable": False,
                    "purpose_score": 0,
                    "bounding_box": {"x": i * 10, "y": 0, "width": 40, "height": 20},
                    "frame_url": self.url,
                    "frame_name": "",
                    "in_shadow_dom": False,
                    "computed": {"display": "block", "visibility": "visible", "opacity": "1"}
                    if include_computed
                    else None,
                }
                for i in range(max_elements)
            ]
        return ""

    async def title(self) -> str:
        return "Settings"

    async def wait_for_selector(self, selector: str, timeout: int) -> None:
        self.last_wait_for_selector = (selector, timeout)

    async def wait_for_timeout(self, timeout: int) -> None:
        self.last_wait_for_timeout = timeout


class _FakeManager:
    def __init__(self) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.session = SimpleNamespace(
            ref_map={"e1": '[data-cognis-ref="e1"]'},
            page=_FakePage(),
            session_id="sess-1",
            profile_mode="persistent_local",
            profile_id="www-reddit-com",
            console_events=[{"level": "error", "text": "boom"}],
            network_events=[{"resource_type": "xhr", "status": 400}],
        )
        # Stage C surface used by handlers._resolve_intensity. Default to
        # humanization off so existing assertions about ``locator.click``,
        # ``locator.fill`` and ``locator.type`` still hold (the humanizer
        # passes through to the locator API when intensity is "off").
        self.humanize_input = False
        self.humanize_intensity = "off"

    async def open_session(self, **kwargs: Any) -> Any:
        self.open_calls.append(kwargs)
        return self.session

    def get_session(self, session_id: str) -> Any:
        assert session_id == "sess-1"
        return self.session

    async def get_live_session(self, session_id: str) -> Any:
        return self.get_session(session_id)

    async def storage_state(self, session_id: str) -> dict[str, Any]:
        assert session_id == "sess-1"
        return {"cookies": [{"name": "sid"}], "origins": []}

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": "sess-1",
                "url": "https://github.com/settings",
                "profile_mode": "persistent_local",
                "profile_id": "github-com",
                "headless": False,
                "display": ":99",
                "last_used_at": "2026-04-11T00:00:00+00:00",
                "auth_origin": "https://github.com",
            }
        ]

    async def list_profiles(self) -> list[dict[str, Any]]:
        return [
            {
                "profile_id": "github-com",
                "currently_in_use": True,
                "last_used_at": "2026-04-11T00:00:00+00:00",
            }
        ]

    async def get_console_events(
        self, session_id: str, *, level: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        assert session_id == "sess-1"
        return self.session.console_events[:limit]

    async def get_network_events(
        self,
        session_id: str,
        *,
        limit: int = 100,
        resource_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        assert session_id == "sess-1"
        return self.session.network_events[:limit]


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
    assert '"action": "fill"' in result.output
    assert '"source": "ref"' in result.output


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
    assert '"action": "click"' in result.output
    assert '"source": "ref"' in result.output


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
    assert manager.session.page.last_evaluate_args == (
        {
            "mode": "actionable",
            "selector": None,
            "maxResults": 40,
            "assignAttr": "data-cognis-ref",
            "includeComputed": False,
        },
    )
    assert len(manager.session.page.evaluate_scripts) == 1
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


@pytest.mark.asyncio
async def test_browser_list_sessions_returns_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_list_sessions({}, _context())
    assert '"session_id": "sess-1"' in result.output
    assert '"profile_id": "github-com"' in result.output


@pytest.mark.asyncio
async def test_browser_list_profiles_returns_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_list_profiles({}, _context())
    assert '"profile_id": "github-com"' in result.output


@pytest.mark.asyncio
async def test_browser_query_returns_candidate_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_query(
        {
            "session_id": "sess-1",
            "selector": "input",
            "mode": "fillable",
            "include_computed": True,
        },
        _context(),
    )
    assert '"name": "username"' in result.output
    assert '"label_text": "Username or email"' in result.output
    assert manager.session.ref_map["e1"] == '[data-cognis-query-ref="e1"]'


@pytest.mark.asyncio
async def test_browser_query_ref_is_usable_by_later_action(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    visible = _FakeLocator(visible=True, enabled=True, editable=True)
    manager.session.page.locator_map['[data-cognis-query-ref="e1"]'] = visible
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    await handle_browser_query(
        {"session_id": "sess-1", "selector": "input", "mode": "fillable"},
        _context(),
    )
    result = await handle_browser_fill(
        {"session_id": "sess-1", "ref": "e1", "value": "secret"},
        _context(),
    )
    assert visible.filled == "secret"
    assert '"source": "ref"' in result.output


@pytest.mark.asyncio
async def test_selector_actions_do_not_overwrite_query_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    query_target = _FakeLocator(visible=True, enabled=True, editable=True)
    action_target = _FakeLocator(visible=True, enabled=True, editable=True)
    manager.session.page.locator_map['[data-cognis-query-ref="e1"]'] = query_target
    manager.session.page.locator_map['[data-cognis-action-ref="e1"]'] = action_target
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    await handle_browser_query(
        {"session_id": "sess-1", "selector": "input", "mode": "fillable"},
        _context(),
    )
    await handle_browser_fill(
        {"session_id": "sess-1", "selector": "input", "value": "abc"},
        _context(),
    )
    assert manager.session.ref_map["e1"] == '[data-cognis-query-ref="e1"]'


@pytest.mark.asyncio
async def test_browser_eval_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_eval(
        {"session_id": "sess-1", "script": "() => ({ ok: true })"},
        _context(),
    )
    assert '"ok": true' in result.output


@pytest.mark.asyncio
async def test_browser_eval_requires_function_script(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    with pytest.raises(RuntimeError, match="must evaluate to a function"):
        await handle_browser_eval(
            {"session_id": "sess-1", "script": "({ ok: true })"},
            _context(),
        )


@pytest.mark.asyncio
async def test_browser_get_console_returns_events(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_get_console({"session_id": "sess-1"}, _context())
    assert '"boom"' in result.output


@pytest.mark.asyncio
async def test_browser_get_network_returns_events(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_get_network({"session_id": "sess-1"}, _context())
    assert '"status": 400' in result.output


@pytest.mark.asyncio
async def test_browser_focus_targets_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    visible = _FakeLocator(visible=True, enabled=True, editable=True)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = visible
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_focus({"session_id": "sess-1", "ref": "e1"}, _context())
    assert visible.focused is True
    assert '"action": "focus"' in result.output


@pytest.mark.asyncio
async def test_browser_type_uses_key_events(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    visible = _FakeLocator(visible=True, enabled=True, editable=True)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = visible
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_type(
        {"session_id": "sess-1", "ref": "e1", "text": "123456", "delay_ms": 20},
        _context(),
    )
    assert visible.focused is True
    assert visible.typed == ("123456", 20)
    assert '"action": "type"' in result.output


@pytest.mark.asyncio
async def test_browser_submit_form_supports_native(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    visible = _FakeLocator(visible=True, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = visible
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_submit_form(
        {"session_id": "sess-1", "ref": "e1", "mode": "native"},
        _context(),
    )
    assert visible.evaluated is not None
    assert "requestSubmit" in visible.evaluated
    assert '"action": "submit_form"' in result.output


@pytest.mark.asyncio
async def test_browser_fill_selector_errors_on_ambiguous_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    with pytest.raises(ValueError, match="multiple viable candidates"):
        await handle_browser_fill(
            {"session_id": "sess-1", "selector": ".ambiguous", "value": "123456"},
            _context(),
        )


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
    with pytest.raises(CredentialAccessError, match="origin does not match"):
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


class _WrongKindCredentialsProvider(_FakeCredentialsProvider):
    async def get_credential(self, credential_id: str, user_email: str) -> CredentialRecord:
        del user_email
        return CredentialRecord(
            credential_id=credential_id,
            user_email="user@example.com",
            kind="username_password",
            label="Saved state",
            metadata={"origin": self.origin},
        )


@pytest.mark.asyncio
async def test_tool_router_auth_state_wrong_kind_includes_hint() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_WrongKindCredentialsProvider("https://github.com"),
    )

    with pytest.raises(CredentialAccessError, match="browser_storage_state") as excinfo:
        await router._resolve_credential_refs(  # noqa: SLF001
            {"url": "https://github.com/settings", "auth_state_ref": "$credential:github_state"},
            SimpleNamespace(user_email="user@example.com"),
            AgentDefinition(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
                permissions=AgentPermissions(allowed_credentials=["github_state"]),
            ),
        )

    assert excinfo.value.code == "credential_wrong_kind"
    assert excinfo.value.hint is not None
    assert "browser_fill value_ref" in excinfo.value.hint


@pytest.mark.asyncio
async def test_browser_wait_for_rejects_mixed_selector_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    with pytest.raises(ValueError, match="only supports CSS selectors"):
        await handle_browser_wait_for(
            {
                "session_id": "sess-1",
                "selector": "input[name='email'], text=Košík",
                "timeout_ms": 1000,
            },
            _context(),
        )


# ---------------------------------------------------------------------------
# Stage A: _get_manager reads from shared_runtime_metadata
# ---------------------------------------------------------------------------


def test_get_manager_reads_from_shared_runtime_metadata() -> None:
    from cognis.models.tool import ExecutorHandle
    from cognis.tools.executor.browser.handlers import _get_manager
    from cognis.tools.executor.browser.manager import BROWSER_MANAGER_KEY, BrowserManager
    from cognis.tools.registry import ToolExecutionContext

    manager = BrowserManager(enabled=True)
    shared: dict = {BROWSER_MANAGER_KEY: manager}
    per_call: dict = {}

    ctx = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="test"),
        runtime_metadata=per_call,
        shared_runtime_metadata=shared,
    )
    found = _get_manager(ctx)
    assert found is manager
    # Should also be mirrored into per_call for subsequent lookups
    assert per_call.get(BROWSER_MANAGER_KEY) is manager


def test_get_manager_raises_when_absent_from_both_dicts() -> None:
    from cognis.models.tool import ExecutorHandle
    from cognis.tools.executor.browser.handlers import _get_manager
    from cognis.tools.registry import ToolExecutionContext

    ctx = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="test"),
        runtime_metadata={},
        shared_runtime_metadata={},
    )
    with pytest.raises(RuntimeError, match="Browser manager is not available"):
        _get_manager(ctx)


def test_require_manager_returns_error_result_when_absent() -> None:
    from cognis.models.tool import ExecutorHandle
    from cognis.tools.executor.browser.handlers import _require_manager
    from cognis.tools.registry import ToolExecutionContext

    ctx = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="test"),
        runtime_metadata={},
        shared_runtime_metadata={},
    )
    result = _require_manager(ctx)
    assert result.is_error
    assert "Browser manager is not available" in result.output
    assert (result.metadata or {}).get("browser_unavailable") is True
