from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_browser_manager_lists_sessions_and_cleans_idle() -> None:
    manager = BrowserManager(idle_timeout_seconds=60)

    class _Context:
        async def close(self) -> None:
            return None

    stale_session = SimpleNamespace(
        session_id="sess-old",
        page=SimpleNamespace(url="https://example.com"),
        context=_Context(),
        profile_mode="ephemeral",
        profile_id=None,
        headless=True,
        display=None,
        last_used_at=datetime.now(UTC) - timedelta(minutes=10),
        auth_origin=None,
    )
    fresh_session = SimpleNamespace(
        session_id="sess-new",
        page=SimpleNamespace(url="https://reddit.com"),
        context=_Context(),
        profile_mode="persistent_local",
        profile_id="www-reddit-com",
        headless=False,
        display=":99",
        last_used_at=datetime.now(UTC),
        auth_origin="https://reddit.com",
    )
    manager._sessions = {"sess-old": stale_session, "sess-new": fresh_session}  # noqa: SLF001

    sessions = await manager.list_sessions()

    assert [session["session_id"] for session in sessions] == ["sess-new"]
    assert sessions[0]["profile_id"] == "www-reddit-com"


@pytest.mark.asyncio
async def test_browser_manager_lists_profiles_from_disk() -> None:
    with TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "www-reddit-com").mkdir()
        (base / "github-com").mkdir()
        manager = BrowserManager(profile_base_dir=str(base))
        manager._sessions = {  # noqa: SLF001
            "sess-1": SimpleNamespace(profile_id="www-reddit-com")
        }

        profiles = await manager.list_profiles()

        assert [profile["profile_id"] for profile in profiles] == ["github-com", "www-reddit-com"]
        assert profiles[1]["currently_in_use"] is True


def test_browser_manager_allocate_display_skips_claimed_values() -> None:
    manager = BrowserManager()
    manager._claimed_displays = {":99", ":100"}  # noqa: SLF001
    display = manager._allocate_display()  # noqa: SLF001
    assert display not in manager._claimed_displays
