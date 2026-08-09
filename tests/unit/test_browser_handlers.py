from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.tool_router import ToolRouter
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.credential import CredentialAccessError, CredentialRecord, CredentialResolution
from cognis.models.tool import ExecutorHandle, ToolCall
from cognis.tools.executor.browser import handlers as browser_handlers
from cognis.tools.executor.browser.handlers import (
    handle_browser_claim_profile,
    handle_browser_click,
    handle_browser_close,
    handle_browser_download_wait,
    handle_browser_drag_drop,
    handle_browser_eval,
    handle_browser_fill,
    handle_browser_focus,
    handle_browser_get_console,
    handle_browser_get_focus,
    handle_browser_get_network,
    handle_browser_hover,
    handle_browser_inspect_session,
    handle_browser_list_profiles,
    handle_browser_list_sessions,
    handle_browser_open,
    handle_browser_press,
    handle_browser_query,
    handle_browser_save_auth_state,
    handle_browser_scroll,
    handle_browser_select,
    handle_browser_snapshot,
    handle_browser_submit_form,
    handle_browser_type,
    handle_browser_upload,
    handle_browser_wait_for,
)
from cognis.tools.executor.browser.manager import BrowserManager, BrowserSessionSettings
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
        self.hovered = False
        self.dragged_to: _FakeLocator | None = None
        self.typed: tuple[str, int | None] | None = None
        self.evaluated: str | None = None
        self.evaluated_args: tuple[Any, ...] = ()
        self.selected_options: list[dict[str, Any]] | None = None
        self.input_files: list[Any] | None = None
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

    async def evaluate(self, script: str, *args: Any) -> None:
        self.evaluated = script
        self.evaluated_args = args

    async def select_option(self, options: list[dict[str, Any]]) -> list[str]:
        self.selected_options = options
        return [str(next(iter(item.values()))) for item in options]

    async def set_input_files(self, files: list[Any]) -> None:
        self.input_files = files

    async def hover(self) -> None:
        self.hovered = True

    async def drag_to(self, target: _FakeLocator) -> None:
        self.dragged_to = target


class _FakeAsyncValue:
    def __init__(self, value: Any) -> None:
        self._value = value

    @property
    def value(self) -> Any:
        async def _get() -> Any:
            return self._value

        return _get()

    async def __aenter__(self) -> _FakeAsyncValue:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeFileChooser:
    def __init__(self) -> None:
        self.files: list[Any] | None = None

    async def set_files(self, files: list[Any]) -> None:
        self.files = files


class _FakeDownload:
    suggested_filename = "report.txt"

    async def save_as(self, path: str) -> None:
        from pathlib import Path

        Path(path).write_text("downloaded", encoding="utf-8")


class _LargeFakeDownload:
    suggested_filename = "large.bin"

    async def save_as(self, path: str) -> None:
        from pathlib import Path

        Path(path).write_bytes(b"0123456789")


class _FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []
        self.typed: list[tuple[str, int | None]] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)

    async def type(self, text: str, delay: int | None = None) -> None:
        self.typed.append((text, delay))


class _FakePage:
    def __init__(self) -> None:
        self.locator_calls: list[str] = []
        self.locator_obj = _FakeLocator()
        self.locator_map: dict[str, _FakeLocator] = {}
        self.url = "https://github.com/settings"
        self.last_evaluate_args: tuple[object, ...] = ()
        self.evaluate_scripts: list[str] = []
        self.keyboard = _FakeKeyboard()
        self.frames = [self]
        self.active_element: dict[str, Any] | None = None
        self.file_chooser = _FakeFileChooser()
        self.download = _FakeDownload()
        self.content_text = ""

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
        if "document.activeElement" in script:
            return self.active_element
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

    async def content(self) -> str:
        return self.content_text

    async def wait_for_selector(
        self, selector: str, timeout: int, state: str | None = None
    ) -> None:
        self.last_wait_for_selector = (selector, timeout, state)

    async def wait_for_timeout(self, timeout: int) -> None:
        self.last_wait_for_timeout = timeout

    def expect_file_chooser(self) -> _FakeAsyncValue:
        return _FakeAsyncValue(self.file_chooser)

    def expect_download(self, timeout: int) -> _FakeAsyncValue:
        self.last_download_timeout = timeout
        return _FakeAsyncValue(self.download)


class _FakeManager:
    def __init__(self) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.session = SimpleNamespace(
            ref_map={"e1": '[data-cognis-ref="e1"]'},
            page=_FakePage(),
            session_id="sess-1",
            profile_mode="persistent_local",
            profile_id="www-reddit-com",
            browser_settings=BrowserSessionSettings(
                auto_consent="accept",
                stealth_enabled=True,
                fingerprint_hardening=True,
                humanize_input=False,
            ),
            console_events=[{"level": "error", "text": "boom"}],
            network_events=[{"resource_type": "xhr", "status": 400}],
        )
        # Stage C surface used by handlers._resolve_intensity. Default to
        # humanization off so existing assertions about ``locator.click``,
        # ``locator.fill`` and ``locator.type`` still hold (the humanizer
        # passes through to the locator API when intensity is "off").
        self.humanize_input = False
        self.humanize_intensity = "off"
        self.close_calls: list[dict[str, Any]] = []

    async def open_session(self, **kwargs: Any) -> Any:
        self.open_calls.append(kwargs)
        return self.session

    async def close_session(self, session_id: str, **kwargs: Any) -> bool:
        self.close_calls.append({"session_id": session_id, **kwargs})
        return True

    def get_session(self, session_id: str) -> Any:
        assert session_id == "sess-1"
        return self.session

    async def get_live_session(self, session_id: str, **_kwargs: Any) -> Any:
        return self.get_session(session_id)

    async def storage_state(self, session_id: str, **_kwargs: Any) -> dict[str, Any]:
        assert session_id == "sess-1"
        return {"cookies": [{"name": "sid"}], "origins": []}

    async def list_sessions(self, **_kwargs: Any) -> list[dict[str, Any]]:
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

    async def inspect_session(self, session_id: str, **_kwargs: Any) -> dict[str, Any]:
        assert session_id == "sess-1"
        return (await self.list_sessions())[0]

    async def list_profiles(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "profile_id": "github-com",
                "currently_in_use": True,
                "last_used_at": "2026-04-11T00:00:00+00:00",
            }
        ]

    async def claim_legacy_profile(self, profile_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "profile_id": profile_id,
            "ownership_status": "owned",
            "claimed": True,
        }

    async def get_console_events(
        self, session_id: str, *, level: str = "all", limit: int = 100, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        assert session_id == "sess-1"
        return self.session.console_events[:limit]

    async def get_network_events(
        self,
        session_id: str,
        *,
        limit: int = 100,
        resource_types: list[str] | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        assert session_id == "sess-1"
        return self.session.network_events[:limit]


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="exec-1", executor_type="in_process"),
        runtime_metadata={
            "runtime_access": {
                "session_id": "scope-1",
                "conversation_id": "conversation-1",
                "user_email": "user@example.com",
                "agent_id": "agent-1",
            }
        },
        execution_scope_id="scope-1",
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
    assert manager.session.ref_map["e1"]["selector"] == '[data-cognis-ref="e1"]'
    assert manager.session.ref_map["e1"]["frame_index"] == 0
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
    assert manager.open_calls[0]["browser_settings"] is None
    assert manager.open_calls[0]["owner"].execution_scope_id == "scope-1"
    assert manager.open_calls[0]["owner"].conversation_id == "conversation-1"
    assert '"profile_mode": "persistent_local"' in result.output
    assert '"profile_id": "www-reddit-com"' in result.output
    assert '"auto_consent": "accept"' in result.output


@pytest.mark.asyncio
async def test_browser_open_passes_browser_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    await handle_browser_open(
        {
            "session_id": "sess-1",
            "url": "https://login.microsoftonline.com/",
            "browser_settings": {
                "auto_consent": "off",
                "stealth_enabled": False,
                "fingerprint_hardening": False,
                "humanize_input": True,
            },
            "selected_executor_owner_email": "user@example.com",
        },
        _context(),
    )
    assert manager.open_calls[0]["browser_settings"] == {
        "auto_consent": "off",
        "stealth_enabled": False,
        "fingerprint_hardening": False,
        "humanize_input": True,
    }


@pytest.mark.asyncio
async def test_browser_open_reports_site_attributed_headless_waf_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    manager.session.navigation_status = 500
    manager.session.navigation_url = "https://patchright-init-script-inject.internal/script.js"
    manager.session.page.url = "https://www.cocky-kontaktni.cz/"
    manager.session.page.content_text = "<h1>Request rejected</h1><p>Attack ID: 20000051</p>"
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    result = await handle_browser_open(
        {
            "session_id": "lens",
            "url": "https://www.cocky-kontaktni.cz/",
            "headless": True,
        },
        _context(),
    )

    assert result.is_error is True
    assert result.metadata["category"] == "vendor_waf_block"
    assert result.metadata["final_url"] == "https://www.cocky-kontaktni.cz/"
    assert result.metadata["headed_retry_recommended"] is True
    assert "patchright-init-script-inject.internal" not in result.output
    assert "headless=false" in result.output


@pytest.mark.asyncio
async def test_browser_open_sanitizes_internal_init_url_on_navigation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()

    async def _fail_open(**_kwargs: Any) -> Any:
        raise RuntimeError(
            "navigation failed at https://patchright-init-script-inject.internal/bootstrap"
        )

    manager.open_session = _fail_open  # type: ignore[method-assign]
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    result = await handle_browser_open(
        {
            "session_id": "lens",
            "url": "https://www.cocky-kontaktni.cz/",
            "headless": True,
        },
        _context(),
    )

    assert result.is_error is True
    assert result.metadata["requested_url"] == "https://www.cocky-kontaktni.cz/"
    assert result.metadata["final_url"] == "https://www.cocky-kontaktni.cz/"
    assert result.metadata["attribution"] == "requested_site"
    assert "patchright-init-script-inject.internal" not in result.output


@pytest.mark.asyncio
async def test_browser_open_does_not_attribute_runtime_failure_to_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()

    async def _fail_open(**_kwargs: Any) -> Any:
        raise RuntimeError("Headed browser mode is not enabled on this executor")

    manager.open_session = _fail_open  # type: ignore[method-assign]
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    result = await handle_browser_open(
        {
            "session_id": "lens",
            "url": "https://www.cocky-kontaktni.cz/",
            "headless": False,
        },
        _context(),
    )

    assert result.is_error is True
    assert result.metadata["browser_runtime_error"] is True
    assert result.metadata["attribution"] == "executor_runtime"
    assert "requested_site" not in result.output


@pytest.mark.asyncio
async def test_browser_open_does_not_treat_plain_access_denied_as_waf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    manager.session.navigation_status = 403
    manager.session.page.content_text = "<h1>Access denied</h1>"
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    result = await handle_browser_open(
        {
            "session_id": "account",
            "url": "https://example.com/private",
            "headless": True,
        },
        _context(),
    )

    assert result.is_error is False
    assert result.metadata == {}
    assert '"diagnostic": null' in result.output


@pytest.mark.asyncio
async def test_browser_open_detects_vendor_access_denied_returned_as_http_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    manager.session.navigation_status = 200
    manager.session.page.title_text = "Zugriff verweigert / Access denied"
    manager.session.page.content_text = (
        "<h1>Zugriff verweigert</h1><p>Error Reference: 0.1234.5678</p>"
    )
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    result = await handle_browser_open(
        {
            "session_id": "marketplace",
            "url": "https://example.com/search",
            "headless": False,
        },
        _context(),
    )

    assert result.is_error is True
    assert result.metadata["category"] == "vendor_waf_block"
    assert result.metadata["http_status"] == 200


@pytest.mark.asyncio
async def test_browser_open_does_not_flag_http_200_page_discussing_waf_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    manager.session.navigation_status = 200
    manager.session.page.title_text = "Web application firewall documentation"
    manager.session.page.content_text = "<article>Imperva attack ID reference guide</article>"
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    result = await handle_browser_open(
        {
            "session_id": "docs",
            "url": "https://example.com/waf-guide",
            "headless": False,
        },
        _context(),
    )

    assert result.is_error is False
    assert result.metadata == {}


@pytest.mark.asyncio
async def test_browser_open_rejects_invalid_browser_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    with pytest.raises(ValueError, match="browser_settings must be an object"):
        await handle_browser_open(
            {
                "session_id": "sess-1",
                "url": "https://example.com",
                "browser_settings": "off",
            },
            _context(),
        )
    with pytest.raises(ValueError, match="auto_consent"):
        await handle_browser_open(
            {
                "session_id": "sess-1",
                "url": "https://example.com",
                "browser_settings": {"auto_consent": "maybe"},
            },
            _context(),
        )


@pytest.mark.asyncio
async def test_browser_list_sessions_returns_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_list_sessions({}, _context())
    assert '"session_id": "sess-1"' in result.output
    assert '"profile_id": "github-com"' in result.output


@pytest.mark.asyncio
async def test_browser_inspect_session_returns_safe_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_inspect_session({"session_id": "sess-1"}, _context())
    assert '"session_id": "sess-1"' in result.output
    assert '"user_email"' not in result.output


@pytest.mark.asyncio
async def test_browser_list_sessions_rejects_missing_execution_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    context = _context()
    context.execution_scope_id = None
    result = await handle_browser_list_sessions({}, context)
    assert result.is_error is True
    assert result.metadata["browser_lifecycle_error"] == "browser_unauthenticated"


@pytest.mark.asyncio
async def test_browser_close_forwards_verified_owner_and_descendant_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_close(
        {"session_id": "sess-1", "release_managed_descendant": True},
        _context(),
    )
    assert result.metadata == {"closed": True, "idempotent": False}
    assert manager.close_calls[0]["owner"].execution_scope_id == "scope-1"
    assert manager.close_calls[0]["allow_managed_descendant"] is True


@pytest.mark.asyncio
async def test_browser_list_profiles_returns_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_list_profiles({}, _context())
    assert '"profile_id": "github-com"' in result.output


@pytest.mark.asyncio
async def test_browser_claim_profile_returns_claim_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    result = await handle_browser_claim_profile(
        {
            "profile_id": "www-cocky-kontaktni-cz",
            "confirm_profile_id": "www-cocky-kontaktni-cz",
        },
        _context(),
    )

    assert result.is_error is False
    assert result.metadata == {"browser_profile_claimed": True}
    assert '"ownership_status": "owned"' in result.output


@pytest.mark.asyncio
async def test_browser_claim_profile_rejects_shared_executor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "account"
    profile_dir.mkdir()
    (profile_dir / "Default").mkdir()
    manager = BrowserManager(profile_base_dir=str(tmp_path))
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    context = _context()
    context.runtime_metadata["selected_executor_owner_email"] = "system@cognis.local"

    result = await handle_browser_claim_profile(
        {"profile_id": "account", "confirm_profile_id": "account"},
        context,
    )

    assert result.is_error is True
    assert result.metadata["browser_lifecycle_error"] == "browser_unauthorized"
    assert not (profile_dir / ".cognis-owner.json").exists()


def test_browser_profile_claim_schema_requires_explicit_non_bypassable_confirmation() -> None:
    from cognis.tools.executor.browser.definitions import browser_tool_definitions

    definitions = {tool.name: tool for tool in browser_tool_definitions()}
    claim = definitions["browser_claim_profile"]
    assert claim.read_only is False
    assert claim.non_bypassable is True
    assert claim.parameters["required"] == ["profile_id", "confirm_profile_id"]


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
    assert manager.session.ref_map["e1"]["selector"] == '[data-cognis-query-ref="e1"]'


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
    assert manager.session.ref_map["e1"]["selector"] == '[data-cognis-query-ref="e1"]'


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
async def test_browser_type_accepts_resolved_value(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    visible = _FakeLocator(visible=True, enabled=True, editable=True)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = visible
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_type(
        {"session_id": "sess-1", "ref": "e1", "value": "123456"},
        _context(),
    )
    assert visible.typed == ("123456", None)
    assert "123456" not in result.output


@pytest.mark.asyncio
async def test_browser_press_types_into_focused_element(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_press(
        {"session_id": "sess-1", "value": "secret", "delay_ms": 25},
        _context(),
    )
    assert manager.session.page.keyboard.typed == [("secret", 25)]
    assert "secret" not in result.output
    assert '"source": "focused"' in result.output


@pytest.mark.asyncio
async def test_browser_query_refs_track_iframe_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    child_frame = _FakePage()
    child_frame.url = "https://payments.example/frame"
    target = _FakeLocator(visible=True, enabled=True, editable=True)
    child_frame.locator_map['[data-cognis-query-ref="e1"]'] = target
    manager.session.page.frames = [manager.session.page, child_frame]
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    await handle_browser_query(
        {"session_id": "sess-1", "selector": "input", "mode": "fillable"},
        _context(),
    )

    assert manager.session.ref_map["e2"]["frame_index"] == 1
    result = await handle_browser_fill(
        {"session_id": "sess-1", "ref": "e2", "value": "4242424242424242"},
        _context(),
    )
    assert target.filled == "4242424242424242"
    assert "payments.example" in result.output


@pytest.mark.asyncio
async def test_browser_get_focus_reports_active_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    child_frame = _FakePage()
    child_frame.url = "https://issuer.example/challenge"
    child_frame.active_element = {
        "tag": "input",
        "type": "text",
        "name": "otp",
        "editable": True,
        "value_state": "redacted",
    }
    manager.session.page.frames = [manager.session.page, child_frame]
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_get_focus({"session_id": "sess-1"}, _context())
    assert '"frame_index": 1' in result.output
    assert "issuer.example" in result.output
    assert '"value_state": "redacted"' in result.output


@pytest.mark.asyncio
async def test_browser_wait_for_passes_state(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_wait_for(
        {
            "session_id": "sess-1",
            "selector": "iframe",
            "timeout_ms": 500,
            "state": "attached",
        },
        _context(),
    )
    assert result.output == "Wait completed."
    assert manager.session.page.last_wait_for_selector == ("iframe", 500, "attached")


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
async def test_browser_select_selects_by_value_and_label(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    select = _FakeLocator(visible=True, enabled=True, editable=True)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = select
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_select(
        {"session_id": "sess-1", "ref": "e1", "values": ["cz"], "labels": ["English"]},
        _context(),
    )
    assert select.selected_options == [{"value": "cz"}, {"label": "English"}]
    assert '"action": "select"' in result.output


@pytest.mark.asyncio
async def test_browser_upload_sets_files_on_input_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    upload_file = tmp_path / "note.txt"
    upload_file.write_text("hello", encoding="utf-8")
    manager = _FakeManager()
    file_input = _FakeLocator(visible=False, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = file_input
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_upload(
        {"session_id": "sess-1", "ref": "e1", "file_paths": [str(upload_file)]},
        _context(),
    )
    assert file_input.input_files == [str(upload_file)]
    assert '"file_count": 1' in result.output


@pytest.mark.asyncio
async def test_browser_upload_uses_file_chooser_with_artifact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    button = _FakeLocator(visible=True, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = button
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_upload(
        {
            "session_id": "sess-1",
            "ref": "e1",
            "mode": "file_chooser",
            "source_artifacts": [
                {
                    "filename": "image.png",
                    "mime_type": "image/png",
                    "content_b64": "aGVsbG8=",
                }
            ],
        },
        _context(),
    )
    assert manager.session.page.file_chooser.files == [
        {"name": "image.png", "mimeType": "image/png", "buffer": b"hello"}
    ]
    assert button.clicked is True
    assert '"mode": "file_chooser"' in result.output


@pytest.mark.asyncio
async def test_browser_upload_rejects_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    file_input = _FakeLocator(visible=False, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = file_input
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    monkeypatch.setattr(browser_handlers, "_MAX_BROWSER_UPLOAD_BYTES", 5)
    with pytest.raises(ValueError, match="too large"):
        await handle_browser_upload(
            {
                "session_id": "sess-1",
                "ref": "e1",
                "source_artifacts": [
                    {
                        "filename": "large.bin",
                        "mime_type": "application/octet-stream",
                        "content_b64": "MDEyMzQ1Njc4OQ==",
                    }
                ],
            },
            _context(),
        )


@pytest.mark.asyncio
async def test_browser_download_wait_returns_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    button = _FakeLocator(visible=True, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = button
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    result = await handle_browser_download_wait(
        {"session_id": "sess-1", "ref": "e1", "timeout_ms": 1234},
        _context(),
    )
    assert manager.session.page.last_download_timeout == 1234
    assert result.attachments is not None
    assert result.attachments[0]["filename"] == "report.txt"
    assert result.attachments[0]["content_b64"] == "ZG93bmxvYWRlZA=="


@pytest.mark.asyncio
async def test_browser_download_wait_clamps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    button = _FakeLocator(visible=True, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = button
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    monkeypatch.setattr(browser_handlers, "_MAX_BROWSER_DOWNLOAD_TIMEOUT_MS", 50)
    await handle_browser_download_wait(
        {"session_id": "sess-1", "ref": "e1", "timeout_ms": 5000},
        _context(),
    )
    assert manager.session.page.last_download_timeout == 50


@pytest.mark.asyncio
async def test_browser_download_wait_rejects_oversized_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeManager()
    manager.session.page.download = _LargeFakeDownload()
    button = _FakeLocator(visible=True, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = button
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)
    monkeypatch.setattr(browser_handlers, "_MAX_BROWSER_DOWNLOAD_BYTES", 5)
    with pytest.raises(ValueError, match="too large"):
        await handle_browser_download_wait(
            {"session_id": "sess-1", "ref": "e1"},
            _context(),
        )


@pytest.mark.asyncio
async def test_browser_scroll_hovers_and_drag_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    source = _FakeLocator(visible=True, enabled=True, editable=False)
    target = _FakeLocator(visible=True, enabled=True, editable=False)
    manager.session.page.locator_map['[data-cognis-ref="e1"]'] = source
    manager.session.page.locator_map['[data-cognis-ref="e2"]'] = target
    manager.session.ref_map["e2"] = '[data-cognis-ref="e2"]'
    monkeypatch.setattr(browser_handlers, "_get_manager", lambda _context: manager)

    scroll_result = await handle_browser_scroll(
        {"session_id": "sess-1", "ref": "e1", "delta_y": 250},
        _context(),
    )
    hover_result = await handle_browser_hover({"session_id": "sess-1", "ref": "e1"}, _context())
    drag_result = await handle_browser_drag_drop(
        {"session_id": "sess-1", "source_ref": "e1", "target_ref": "e2"},
        _context(),
    )

    assert source.evaluated == "(el, delta) => el.scrollBy(delta.x, delta.y)"
    assert source.evaluated_args == ({"x": 0, "y": 250},)
    assert source.hovered is True
    assert source.dragged_to is target
    assert '"action": "scroll"' in scroll_result.output
    assert '"action": "hover"' in hover_result.output
    assert '"action": "drag_drop"' in drag_result.output


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
    resolved = await router._resolve_sensitive_refs(  # noqa: SLF001
        {"url": "https://github.com/settings", "auth_state_ref": "$credential:github_state"},
        SimpleNamespace(user_email="user@example.com"),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(allowed_credentials=["github_state"]),
        ),
        ToolCall(call_id="auth-state", name="browser_open", arguments={}),
    )
    assert resolved["auth_state"] == {"cookies": [], "origins": []}


@pytest.mark.asyncio
async def test_tool_router_rejects_cross_origin_auth_state_ref() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_FakeCredentialsProvider("https://github.com"),
    )
    with pytest.raises(CredentialAccessError, match="origin does not match"):
        await router._resolve_sensitive_refs(  # noqa: SLF001
            {"url": "https://evil.example", "auth_state_ref": "$credential:github_state"},
            SimpleNamespace(user_email="user@example.com"),
            AgentDefinition(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
                permissions=AgentPermissions(allowed_credentials=["github_state"]),
            ),
            ToolCall(call_id="auth-state", name="browser_open", arguments={}),
        )


@pytest.mark.asyncio
async def test_tool_router_ignores_blank_auth_state_ref() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_FakeCredentialsProvider("https://github.com"),
    )
    resolved = await router._resolve_sensitive_refs(  # noqa: SLF001
        {"url": "https://github.com/settings", "auth_state_ref": "   "},
        SimpleNamespace(user_email="user@example.com"),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(allowed_credentials=["github_state"]),
        ),
        ToolCall(call_id="auth-state", name="browser_open", arguments={}),
    )
    assert "auth_state" not in resolved
    assert "auth_state_ref" not in resolved


@pytest.mark.asyncio
async def test_tool_router_ignores_blank_value_ref() -> None:
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        credentials_provider=_FakeCredentialsProvider("https://github.com"),
    )
    resolved = await router._resolve_sensitive_refs(  # noqa: SLF001
        {"value_ref": ""},
        SimpleNamespace(user_email="user@example.com"),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(allowed_credentials=["github_state"]),
        ),
        ToolCall(call_id="value-ref", name="browser_fill", arguments={}),
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
        await router._resolve_sensitive_refs(  # noqa: SLF001
            {"url": "https://github.com/settings", "auth_state_ref": "$credential:github_state"},
            SimpleNamespace(user_email="user@example.com"),
            AgentDefinition(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
                permissions=AgentPermissions(allowed_credentials=["github_state"]),
            ),
            ToolCall(call_id="auth-state", name="browser_open", arguments={}),
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
