"""Tests for browser install.py — auto-install logic and channel detection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cognis.tools.executor.browser.install import (
    _INSTALLABLE_CHANNELS,
    _SYSTEM_BROWSER_CHANNELS,
    RUNTIME_PLAYWRIGHT,
    _install_target,
    _is_sudo_failure,
    _probe_system_browser,
    ensure_browser_runtime,
)

# ---------------------------------------------------------------------------
# _install_target
# ---------------------------------------------------------------------------


def test_install_target_no_channel_returns_engine() -> None:
    assert _install_target(engine="chromium", channel=None) == "chromium"
    assert _install_target(engine="firefox", channel=None) == "firefox"
    assert _install_target(engine="webkit", channel=None) == "webkit"


def test_install_target_empty_channel_returns_engine() -> None:
    assert _install_target(engine="chromium", channel="") == "chromium"
    assert _install_target(engine="chromium", channel="  ") == "chromium"


@pytest.mark.parametrize("ch", sorted(_INSTALLABLE_CHANNELS))
def test_install_target_installable_channel_returns_channel(ch: str) -> None:
    result = _install_target(engine="chromium", channel=ch)
    assert result == ch.lower()


@pytest.mark.parametrize("ch", sorted(_SYSTEM_BROWSER_CHANNELS))
def test_install_target_system_browser_channels_return_none(ch: str) -> None:
    assert _install_target(engine="chromium", channel=ch) is None


# ---------------------------------------------------------------------------
# _is_sudo_failure
# ---------------------------------------------------------------------------


def test_is_sudo_failure_detects_terminal_required() -> None:
    assert _is_sudo_failure("sudo: a terminal is required to read the password")


def test_is_sudo_failure_detects_password_required() -> None:
    assert _is_sudo_failure("sudo: a password is required")


def test_is_sudo_failure_detects_no_tty() -> None:
    assert _is_sudo_failure("sudo: no tty present and no askpass program specified")


def test_is_sudo_failure_returns_false_for_unrelated_stderr() -> None:
    assert not _is_sudo_failure("")
    assert not _is_sudo_failure("Error: could not find browser")
    assert not _is_sudo_failure("playwright install chromium")


# ---------------------------------------------------------------------------
# _probe_system_browser
# ---------------------------------------------------------------------------


def test_probe_system_browser_returns_true_when_binary_found() -> None:
    import shutil

    with patch.object(shutil, "which", return_value="/usr/bin/google-chrome-stable"):
        assert _probe_system_browser("chrome") is True


def test_probe_system_browser_returns_false_when_binary_absent() -> None:
    import shutil

    with patch.object(shutil, "which", return_value=None):
        assert _probe_system_browser("chrome") is False


def test_probe_system_browser_unknown_channel_returns_false() -> None:
    import shutil

    with patch.object(shutil, "which", return_value=None):
        assert _probe_system_browser("nonexistent-channel") is False


# ---------------------------------------------------------------------------
# ensure_browser_runtime — system-browser channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_browser_runtime_system_channel_found_returns_ok() -> None:
    import shutil

    with (
        patch("cognis.tools.executor.browser.install._import_module_for_runtime"),
        patch.object(shutil, "which", return_value="/usr/bin/google-chrome-stable"),
    ):
        ok, reason = await ensure_browser_runtime(
            runtime=RUNTIME_PLAYWRIGHT,
            engine="chromium",
            channel="chrome",
            auto_install=True,
        )

    assert ok is True
    assert reason == "available"


@pytest.mark.asyncio
async def test_ensure_browser_runtime_system_channel_absent_returns_actionable_error() -> None:
    import shutil

    with (
        patch("cognis.tools.executor.browser.install._import_module_for_runtime"),
        patch.object(shutil, "which", return_value=None),
    ):
        ok, reason = await ensure_browser_runtime(
            runtime=RUNTIME_PLAYWRIGHT,
            engine="chromium",
            channel="chrome",
            auto_install=True,
        )

    assert ok is False
    assert "channel 'chrome'" in reason.lower()
    assert "system browser" in reason.lower() or "os package manager" in reason.lower()
    assert "auto-install is skipped" in reason.lower()


@pytest.mark.asyncio
async def test_ensure_browser_runtime_system_channel_ignores_auto_install_false() -> None:
    """Even when auto_install=False, absence of system channel binary is an error."""
    import shutil

    with (
        patch("cognis.tools.executor.browser.install._import_module_for_runtime"),
        patch.object(shutil, "which", return_value=None),
    ):
        ok, _ = await ensure_browser_runtime(
            runtime=RUNTIME_PLAYWRIGHT,
            engine="chromium",
            channel="msedge",
            auto_install=False,
        )

    assert ok is False


# ---------------------------------------------------------------------------
# ensure_browser_runtime — bundled chromium path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_browser_runtime_no_channel_no_install_returns_available() -> None:
    with patch("cognis.tools.executor.browser.install._import_module_for_runtime"):
        ok, reason = await ensure_browser_runtime(
            runtime=RUNTIME_PLAYWRIGHT,
            engine="chromium",
            channel=None,
            auto_install=False,
        )

    assert ok is True
    assert reason == "available"


@pytest.mark.asyncio
async def test_ensure_browser_runtime_auto_install_runs_subprocess() -> None:
    """auto_install=True runs the install subprocess and sets env vars defensively."""
    calls: list[dict] = []

    async def _fake_proc() -> SimpleNamespace:
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

    async def _fake_create_subprocess(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append({"args": args, "env": kwargs.get("env", {})})
        proc = SimpleNamespace(returncode=0)
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with (
        patch("cognis.tools.executor.browser.install._import_module_for_runtime"),
        patch("cognis.tools.executor.browser.install._PREPARED", set()),
        patch(
            "cognis.tools.executor.browser.install.asyncio.create_subprocess_exec",
            side_effect=_fake_create_subprocess,
        ),
    ):
        ok, reason = await ensure_browser_runtime(
            runtime=RUNTIME_PLAYWRIGHT,
            engine="chromium",
            channel=None,
            auto_install=True,
        )

    assert ok is True
    assert calls, "subprocess should have been called"
    cmd_args = calls[0]["args"]
    assert "install" in cmd_args
    assert "chromium" in cmd_args
    env = calls[0]["env"]
    assert env.get("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS") == "1"
    assert env.get("SUDO_ASKPASS") == "/bin/false"
    assert env.get("DEBIAN_FRONTEND") == "noninteractive"


@pytest.mark.asyncio
async def test_ensure_browser_runtime_sudo_failure_returns_actionable_message() -> None:
    sudo_stderr = b"sudo: a terminal is required to read the password\nsudo: a password is required"

    async def _fake_create_subprocess(*args: object, **kwargs: object) -> SimpleNamespace:
        proc = SimpleNamespace(returncode=1)
        proc.communicate = AsyncMock(return_value=(b"", sudo_stderr))
        return proc

    with (
        patch("cognis.tools.executor.browser.install._import_module_for_runtime"),
        patch("cognis.tools.executor.browser.install._PREPARED", set()),
        patch(
            "cognis.tools.executor.browser.install.asyncio.create_subprocess_exec",
            side_effect=_fake_create_subprocess,
        ),
    ):
        ok, reason = await ensure_browser_runtime(
            runtime=RUNTIME_PLAYWRIGHT,
            engine="chromium",
            channel=None,
            auto_install=True,
        )

    assert ok is False
    assert "root" in reason.lower() or "sudo" in reason.lower()
    assert "daemon" in reason.lower() or "headless" in reason.lower()


@pytest.mark.asyncio
async def test_ensure_browser_runtime_install_failure_returns_stderr() -> None:
    error_stderr = b"Error: Could not download Chromium: network error"

    async def _fake_create_subprocess(*args: object, **kwargs: object) -> SimpleNamespace:
        proc = SimpleNamespace(returncode=1)
        proc.communicate = AsyncMock(return_value=(b"", error_stderr))
        return proc

    with (
        patch("cognis.tools.executor.browser.install._import_module_for_runtime"),
        patch("cognis.tools.executor.browser.install._PREPARED", set()),
        patch(
            "cognis.tools.executor.browser.install.asyncio.create_subprocess_exec",
            side_effect=_fake_create_subprocess,
        ),
    ):
        ok, reason = await ensure_browser_runtime(
            runtime=RUNTIME_PLAYWRIGHT,
            engine="chromium",
            channel=None,
            auto_install=True,
        )

    assert ok is False
    assert "network" in reason.lower()


# ---------------------------------------------------------------------------
# BrowserManager no longer auto-pins channel=chrome for Patchright
# ---------------------------------------------------------------------------


def test_patchright_manager_does_not_auto_pin_chrome_channel() -> None:
    from cognis.tools.executor.browser.manager import BrowserManager

    manager = BrowserManager(runtime="patchright", engine="chromium", channel=None)
    assert manager.channel is None


def test_patchright_manager_respects_explicit_channel() -> None:
    from cognis.tools.executor.browser.manager import BrowserManager

    manager = BrowserManager(runtime="patchright", engine="chromium", channel="chrome-beta")
    assert manager.channel == "chrome-beta"


def test_playwright_manager_with_chrome_channel() -> None:
    from cognis.tools.executor.browser.manager import BrowserManager

    manager = BrowserManager(runtime="playwright", engine="chromium", channel="chrome")
    assert manager.channel == "chrome"
