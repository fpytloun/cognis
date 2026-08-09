"""Unit tests for stealth and runtime axis additions to BrowserManager."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from cognis.tools.executor.browser.manager import (
    BROWSER_DEFAULT_ACCEPT_LANGUAGE,
    BROWSER_DEFAULT_TIMEZONE_ID,
    BrowserManager,
    _coherent_chromium_user_agent,
)

# ---------------------------------------------------------------------------
# context_kwargs: stealth-aware defaults
# ---------------------------------------------------------------------------


def test_context_kwargs_includes_stealth_defaults_when_enabled() -> None:
    manager = BrowserManager()  # defaults: playwright runtime, stealth on
    manager._browser_user_agents[False] = _coherent_chromium_user_agent(  # noqa: SLF001
        "145.0.7632.6"
    )
    kwargs = manager._context_kwargs(headless=False)  # noqa: SLF001
    assert kwargs["user_agent"].endswith("Chrome/145.0.7632.6 Safari/537.36")
    expected_platform = (
        "Macintosh"
        if sys.platform == "darwin"
        else "Windows NT"
        if sys.platform == "win32"
        else "X11; Linux x86_64"
    )
    assert expected_platform in kwargs["user_agent"]
    assert kwargs["extra_http_headers"] == {"Accept-Language": BROWSER_DEFAULT_ACCEPT_LANGUAGE}
    assert kwargs["timezone_id"] == BROWSER_DEFAULT_TIMEZONE_ID
    assert kwargs["locale"] == "en-US"


def test_context_kwargs_skips_stealth_defaults_when_disabled() -> None:
    manager = BrowserManager(stealth_enabled=False)
    kwargs = manager._context_kwargs()  # noqa: SLF001
    assert "user_agent" not in kwargs
    assert "extra_http_headers" not in kwargs
    assert "timezone_id" not in kwargs


def test_patchright_keeps_coherent_locale_defaults_without_stealth_stack() -> None:
    manager = BrowserManager(runtime="patchright")
    kwargs = manager._context_kwargs(headless=False)  # noqa: SLF001
    assert kwargs["timezone_id"] == BROWSER_DEFAULT_TIMEZONE_ID
    assert kwargs["extra_http_headers"] == {"Accept-Language": BROWSER_DEFAULT_ACCEPT_LANGUAGE}


def test_context_kwargs_respects_explicit_timezone_override() -> None:
    manager = BrowserManager(timezone_id="Europe/Prague")
    kwargs = manager._context_kwargs()  # noqa: SLF001
    assert kwargs["timezone_id"] == "Europe/Prague"


def test_context_kwargs_skips_user_agent_when_realistic_ua_disabled() -> None:
    manager = BrowserManager(realistic_user_agent=False)
    manager._browser_user_agents[False] = "unused"  # noqa: SLF001
    kwargs = manager._context_kwargs(headless=False)  # noqa: SLF001
    assert "user_agent" not in kwargs
    # Accept-Language and timezone still applied because stealth is on.
    assert kwargs["extra_http_headers"] == {"Accept-Language": BROWSER_DEFAULT_ACCEPT_LANGUAGE}
    assert kwargs["timezone_id"] == BROWSER_DEFAULT_TIMEZONE_ID


def test_context_kwargs_preserves_native_ua_when_version_is_not_probed() -> None:
    manager = BrowserManager(channel="chrome")
    manager._browser_user_agents[False] = "bundled-browser-ua"  # noqa: SLF001

    kwargs = manager._context_kwargs(headless=None)  # noqa: SLF001

    assert "user_agent" not in kwargs


def test_shared_browser_channel_does_not_cache_chrome_ua_override() -> None:
    manager = BrowserManager(channel="msedge")

    manager._cache_browser_user_agent(  # noqa: SLF001
        headless=False,
        browser=SimpleNamespace(version="145.0.7632.6"),
    )

    assert False not in manager._browser_user_agents  # noqa: SLF001


@pytest.mark.asyncio
async def test_probe_browser_user_agent_uses_runtime_version_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"Chromium 145.0.7632.6\n", b""

    async def _create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _Process:
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    user_agent = await BrowserManager._probe_browser_user_agent("/browser")  # noqa: SLF001

    assert user_agent is not None
    assert "Chrome/145.0.7632.6" in user_agent
    assert "Windows NT" not in user_agent or sys.platform == "win32"


# ---------------------------------------------------------------------------
# Stealth instantiation + apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_context_defaults_invokes_stealth(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = BrowserManager()

    applied: list[Any] = []

    class _Stealth:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def apply_stealth_async(self, ctx: Any) -> None:
            applied.append(ctx)

    fake_module = ModuleType("playwright_stealth")
    fake_module.Stealth = _Stealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright_stealth", fake_module)

    context = SimpleNamespace()
    await manager._apply_context_defaults(context)  # noqa: SLF001
    assert applied == [context]


@pytest.mark.asyncio
async def test_apply_context_defaults_noop_when_stealth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(stealth_enabled=False)

    instantiated: list[Any] = []

    class _Stealth:
        def __init__(self, **kwargs: Any) -> None:
            instantiated.append(kwargs)

        async def apply_stealth_async(self, ctx: Any) -> None:
            raise AssertionError("should not be called when stealth disabled")

    fake_module = ModuleType("playwright_stealth")
    fake_module.Stealth = _Stealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright_stealth", fake_module)

    await manager._apply_context_defaults(SimpleNamespace())  # noqa: SLF001
    assert instantiated == []


def test_patchright_never_stacks_playwright_stealth_when_explicitly_enabled() -> None:
    manager = BrowserManager(runtime="patchright", stealth_enabled=True)

    assert manager._build_stealth() is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_stealth_evasion_exclusions_passed_to_stealth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(stealth_evasions=["navigator_languages", " webgl_vendor "])

    captured: list[dict[str, Any]] = []

    class _Stealth:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

        async def apply_stealth_async(self, ctx: Any) -> None:
            return None

    fake_module = ModuleType("playwright_stealth")
    fake_module.Stealth = _Stealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright_stealth", fake_module)

    await manager._apply_context_defaults(SimpleNamespace())  # noqa: SLF001
    assert captured[0].get("navigator_languages") is False
    assert captured[0].get("webgl_vendor") is False


@pytest.mark.asyncio
async def test_apply_context_defaults_swallows_stealth_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager()

    class _Stealth:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def apply_stealth_async(self, _ctx: Any) -> None:
            raise RuntimeError("stealth blew up")

    fake_module = ModuleType("playwright_stealth")
    fake_module.Stealth = _Stealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright_stealth", fake_module)

    # Should not propagate; defensive logging only.
    await manager._apply_context_defaults(SimpleNamespace())  # noqa: SLF001


# ---------------------------------------------------------------------------
# Runtime axis (Patchright)
# ---------------------------------------------------------------------------


def test_patchright_runtime_defaults_channel_to_none_and_disables_stealth() -> None:
    # Channel is no longer auto-pinned to "chrome" for Patchright; auto-install
    # of system-browser channels requires sudo which is unavailable in daemon mode.
    manager = BrowserManager(runtime="patchright")
    assert manager.runtime == "patchright"
    assert manager.channel is None
    # Patchright already covers stealth's evasions; default off avoids double-up.
    assert manager.stealth_enabled is False


def test_patchright_runtime_does_not_override_explicit_channel() -> None:
    manager = BrowserManager(runtime="patchright", channel="msedge")
    assert manager.channel == "msedge"


def test_patchright_runtime_explicit_stealth_enabled_overrides_default() -> None:
    manager = BrowserManager(runtime="patchright", stealth_enabled=True)
    assert manager.stealth_enabled is True


def test_unknown_runtime_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported browser runtime"):
        BrowserManager(runtime="bogus-engine")


def test_resolve_async_playwright_imports_runtime_specific_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub the patchright module so we can verify import routing without
    # actually starting a browser.
    fake_async_playwright = object()
    fake_module = ModuleType("patchright.async_api")
    fake_module.async_playwright = fake_async_playwright  # type: ignore[attr-defined]
    parent = ModuleType("patchright")
    parent.async_api = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "patchright", parent)
    monkeypatch.setitem(sys.modules, "patchright.async_api", fake_module)

    manager = BrowserManager(runtime="patchright")
    resolved = manager._resolve_async_playwright()  # noqa: SLF001
    assert resolved is fake_async_playwright

    manager_pw = BrowserManager(runtime="playwright")
    pw_resolved = manager_pw._resolve_async_playwright()  # noqa: SLF001
    # The real playwright module is installed in the venv; just sanity check
    # that we got *something* and that it differs from the patchright stub.
    assert pw_resolved is not fake_async_playwright


def test_launch_kwargs_includes_channel_when_explicitly_set() -> None:
    # Channel must be explicitly provided; Patchright no longer auto-pins "chrome".
    manager = BrowserManager(runtime="patchright", channel="chrome")
    kwargs = manager._launch_kwargs(headless=True)  # noqa: SLF001
    assert kwargs["channel"] == "chrome"


def test_launch_kwargs_omits_channel_when_unset() -> None:
    manager = BrowserManager()
    kwargs = manager._launch_kwargs(headless=True)  # noqa: SLF001
    assert "channel" not in kwargs


# ---------------------------------------------------------------------------
# Patchright persistent-profile warning
# ---------------------------------------------------------------------------


def test_patchright_persistent_warning_emitted_once_for_ephemeral_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = BrowserManager(runtime="patchright")
    import logging

    with caplog.at_level(logging.WARNING):
        manager._maybe_emit_patchright_persistent_warning(profile_mode="ephemeral")  # noqa: SLF001
        manager._maybe_emit_patchright_persistent_warning(profile_mode="ephemeral")  # noqa: SLF001
    warnings = [record for record in caplog.records if "patchright" in record.getMessage()]
    assert len(warnings) == 1


def test_patchright_persistent_warning_silent_for_persistent_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = BrowserManager(runtime="patchright")
    import logging

    with caplog.at_level(logging.WARNING):
        manager._maybe_emit_patchright_persistent_warning(  # noqa: SLF001
            profile_mode="persistent_local"
        )
    assert not [record for record in caplog.records if "patchright" in record.getMessage()]


def test_persistent_warning_silent_for_playwright_runtime(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = BrowserManager()
    import logging

    with caplog.at_level(logging.WARNING):
        manager._maybe_emit_patchright_persistent_warning(profile_mode="ephemeral")  # noqa: SLF001
    assert not [record for record in caplog.records if "patchright" in record.getMessage()]


# ---------------------------------------------------------------------------
# Integration: ensure_playwright_browser back-compat, ensure_browser_runtime call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ready_uses_back_compat_for_default_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager()
    legacy_calls: list[dict[str, Any]] = []
    new_calls: list[dict[str, Any]] = []

    async def _fake_legacy(**kwargs: Any) -> tuple[bool, str]:
        legacy_calls.append(kwargs)
        return True, "available"

    async def _fake_new(**kwargs: Any) -> tuple[bool, str]:
        new_calls.append(kwargs)
        return True, "available"

    monkeypatch.setattr(
        "cognis.tools.executor.browser.manager.ensure_playwright_browser", _fake_legacy
    )
    monkeypatch.setattr("cognis.tools.executor.browser.manager.ensure_browser_runtime", _fake_new)
    monkeypatch.setattr(manager, "_resolve_async_playwright", lambda: _StarterFactory)

    await manager._ensure_playwright_ready_locked(headless=True)  # noqa: SLF001

    assert legacy_calls and not new_calls


@pytest.mark.asyncio
async def test_ensure_ready_uses_new_helper_for_patchright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(runtime="patchright")
    legacy_calls: list[dict[str, Any]] = []
    new_calls: list[dict[str, Any]] = []

    async def _fake_legacy(**kwargs: Any) -> tuple[bool, str]:
        legacy_calls.append(kwargs)
        return True, "available"

    async def _fake_new(**kwargs: Any) -> tuple[bool, str]:
        new_calls.append(kwargs)
        return True, "available"

    monkeypatch.setattr(
        "cognis.tools.executor.browser.manager.ensure_playwright_browser", _fake_legacy
    )
    monkeypatch.setattr("cognis.tools.executor.browser.manager.ensure_browser_runtime", _fake_new)
    monkeypatch.setattr(manager, "_resolve_async_playwright", lambda: _StarterFactory)

    await manager._ensure_playwright_ready_locked(headless=True)  # noqa: SLF001

    assert new_calls and not legacy_calls
    assert new_calls[0]["runtime"] == "patchright"
    # Channel is no longer auto-pinned; manager.channel is None.
    assert new_calls[0]["channel"] is None


@pytest.mark.asyncio
async def test_ensure_ready_uses_new_helper_when_channel_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(channel="chrome")
    legacy_calls: list[dict[str, Any]] = []
    new_calls: list[dict[str, Any]] = []

    async def _fake_legacy(**kwargs: Any) -> tuple[bool, str]:
        legacy_calls.append(kwargs)
        return True, "available"

    async def _fake_new(**kwargs: Any) -> tuple[bool, str]:
        new_calls.append(kwargs)
        return True, "available"

    monkeypatch.setattr(
        "cognis.tools.executor.browser.manager.ensure_playwright_browser", _fake_legacy
    )
    monkeypatch.setattr("cognis.tools.executor.browser.manager.ensure_browser_runtime", _fake_new)
    monkeypatch.setattr(manager, "_resolve_async_playwright", lambda: _StarterFactory)

    await manager._ensure_playwright_ready_locked(headless=True)  # noqa: SLF001

    assert new_calls and not legacy_calls
    assert new_calls[0]["runtime"] == "playwright"
    assert new_calls[0]["channel"] == "chrome"


# ---------------------------------------------------------------------------
# install.py: ensure_browser_runtime install command selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_browser_runtime_invokes_correct_install_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    from cognis.tools.executor.browser import install as install_mod

    monkeypatch.setattr(install_mod, "_PREPARED", set())  # reset cache
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))

    captured_argv: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _fake_create_subprocess_exec(*argv: str, **_kwargs: Any) -> _FakeProc:
        captured_argv.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_create_subprocess_exec)

    # "chrome" is now a system-browser channel: install is skipped, binary is probed.
    # Simulate the binary being present so the call succeeds.
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/google-chrome-stable")
    ok, _reason = await install_mod.ensure_browser_runtime(
        runtime="patchright",
        engine="chromium",
        channel="chrome",
        auto_install=True,
    )
    assert ok is True
    assert not captured_argv, "system-browser channel must NOT trigger subprocess install"

    # Bundled engine install (no channel) still invokes the subprocess.
    monkeypatch.setattr(install_mod, "_PREPARED", set())
    ok, _reason = await install_mod.ensure_browser_runtime(
        runtime="playwright",
        engine="firefox",
        channel=None,
        auto_install=True,
    )
    assert ok is True
    assert captured_argv[-1][1:] == ["-m", "playwright", "install", "firefox"]


@pytest.mark.asyncio
async def test_ensure_browser_runtime_caches_prepared_combinations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.tools.executor.browser import install as install_mod

    monkeypatch.setattr(install_mod, "_PREPARED", set())
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    calls = {"count": 0}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _fake_create_subprocess_exec(*_argv: str, **_kwargs: Any) -> _FakeProc:
        calls["count"] += 1
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_create_subprocess_exec)

    await install_mod.ensure_browser_runtime(
        runtime="playwright", engine="chromium", channel=None, auto_install=True
    )
    await install_mod.ensure_browser_runtime(
        runtime="playwright", engine="chromium", channel=None, auto_install=True
    )
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_ensure_browser_runtime_validates_inputs() -> None:
    from cognis.tools.executor.browser import install as install_mod

    with pytest.raises(ValueError, match="Unsupported browser runtime"):
        await install_mod.ensure_browser_runtime(
            runtime="kameleo", engine="chromium", channel=None, auto_install=False
        )
    with pytest.raises(ValueError, match="Unsupported browser engine"):
        await install_mod.ensure_browser_runtime(
            runtime="playwright", engine="lynx", channel=None, auto_install=False
        )


# ---------------------------------------------------------------------------
# handlers._browser_config maps the new fields
# ---------------------------------------------------------------------------


def test_handler_browser_config_passthrough_dict() -> None:
    from cognis.tools.executor.browser import handlers

    metadata = {
        "browser": {
            "runtime": "patchright",
            "channel": "chrome",
            "stealth_enabled": False,
            "stealth_evasions": ["navigator_webdriver"],
            "realistic_user_agent": False,
            "default_timezone_id": "Europe/Prague",
            "default_accept_language": "cs-CZ,cs;q=0.9",
        }
    }
    cfg = handlers._browser_config(metadata)  # noqa: SLF001
    assert cfg["runtime"] == "patchright"
    assert cfg["channel"] == "chrome"
    assert cfg["stealth_enabled"] is False
    assert cfg["stealth_evasions"] == ["navigator_webdriver"]
    assert cfg["realistic_user_agent"] is False
    assert cfg["default_timezone_id"] == "Europe/Prague"


def test_handler_browser_config_legacy_keys_default_runtime_to_playwright() -> None:
    from cognis.tools.executor.browser import handlers

    metadata = {
        "browser_engine": "chromium",
    }
    cfg = handlers._browser_config(metadata)  # noqa: SLF001
    assert cfg["runtime"] == "playwright"
    assert cfg["channel"] is None
    assert cfg["stealth_enabled"] is None  # left for manager default


def test_handler_get_manager_translates_config_to_constructor_arguments() -> None:
    from cognis.tools.executor.browser.handlers import build_manager_from_config

    runtime_metadata = {
        "browser": {
            "runtime": "patchright",
            "channel": "chrome-beta",
            "stealth_enabled": True,
            "stealth_evasions": "navigator_webdriver, webgl_vendor",
            "default_timezone_id": "Europe/Prague",
        }
    }
    manager = build_manager_from_config(runtime_metadata)
    assert manager.runtime == "patchright"
    assert manager.channel == "chrome-beta"
    assert manager.stealth_enabled is True
    assert manager.stealth_evasions == ["navigator_webdriver", "webgl_vendor"]
    assert manager.default_timezone_id == "Europe/Prague"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StarterFactory:
    """Stand-in for ``async_playwright()`` returning a fake starter."""

    def __new__(cls) -> Any:  # type: ignore[override]
        return _Starter()


class _Starter:
    async def start(self) -> Any:
        return SimpleNamespace()
