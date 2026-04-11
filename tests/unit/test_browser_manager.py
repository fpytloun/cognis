from __future__ import annotations

import os

import pytest

from cognis.tools.executor.browser.manager import BrowserManager


def test_browser_manager_derives_persistent_profile_from_origin() -> None:
    manager = BrowserManager(profile_mode_default="persistent_local")
    mode, profile_id = manager._resolve_profile_settings(  # noqa: SLF001
        profile_mode="default",
        profile_id=None,
        url="https://www.reddit.com/r/openwebui/new/",
    )
    assert mode == "persistent_local"
    assert profile_id == "www-reddit-com"


def test_browser_manager_ephemeral_mode_discards_profile_id() -> None:
    manager = BrowserManager(profile_mode_default="persistent_local")
    mode, profile_id = manager._resolve_profile_settings(  # noqa: SLF001
        profile_mode="ephemeral",
        profile_id="reddit-main",
        url="https://www.reddit.com/",
    )
    assert mode == "ephemeral"
    assert profile_id is None


def test_browser_manager_needs_xvfb_for_headed_linux_without_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(xvfb_auto=True)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert manager._needs_virtual_display(headless=False) is True  # noqa: SLF001
    assert manager._needs_virtual_display(headless=True) is False  # noqa: SLF001


def test_browser_manager_skips_xvfb_when_display_present(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = BrowserManager(xvfb_auto=True)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    assert manager._needs_virtual_display(headless=False) is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_browser_manager_requires_xvfb_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = BrowserManager(xvfb_auto=True)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Xvfb"):
        await manager._ensure_virtual_display()  # noqa: SLF001


@pytest.mark.asyncio
async def test_browser_manager_restores_display_after_virtual_display_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Proc:
        returncode = None

        def terminate(self) -> None:
            return None

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    manager = BrowserManager(xvfb_auto=True)
    monkeypatch.setenv("DISPLAY", ":5")
    manager._previous_display = os.environ.get("DISPLAY")  # noqa: SLF001
    manager._xvfb_display = ":99"  # noqa: SLF001
    manager._xvfb_process = _Proc()  # noqa: SLF001
    os.environ["DISPLAY"] = ":99"

    await manager._stop_virtual_display()  # noqa: SLF001

    assert os.environ.get("DISPLAY") == ":5"
