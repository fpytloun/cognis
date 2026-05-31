from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from cognis.tools.executor.browser.manager import (
    BrowserManager,
    BrowserSession,
    BrowserSessionSettings,
)


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


def test_browser_manager_defaults_match_explicit_session_lifecycle() -> None:
    manager = BrowserManager()

    assert manager.max_sessions == 8
    assert manager.idle_timeout_seconds == 1800
    assert manager.navigation_timeout_seconds == 60
    assert manager.wait_until == "domcontentloaded"
    assert manager.network_idle_after_dom_seconds == 3


def test_browser_manager_uses_per_session_idle_timeout() -> None:
    manager = BrowserManager(idle_timeout_seconds=1800)
    session = SimpleNamespace(
        idle_timeout_seconds=60,
        last_used_at=datetime.now(UTC) - timedelta(seconds=90),
    )

    assert manager._session_is_idle(session) is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_browser_manager_goto_uses_domcontentloaded_and_soft_networkidle() -> None:
    manager = BrowserManager()
    calls: list[tuple[str, dict[str, object]]] = []

    class _Page:
        async def goto(self, url: str, **kwargs: object) -> None:
            calls.append((url, dict(kwargs)))

        async def wait_for_load_state(self, state: str, **kwargs: object) -> None:
            calls.append((state, dict(kwargs)))

    await manager._goto(_Page(), "https://example.com")  # noqa: SLF001

    assert calls[0] == (
        "https://example.com",
        {"timeout": 60000, "wait_until": "domcontentloaded"},
    )
    assert calls[1] == ("networkidle", {"timeout": 3000})


def test_browser_manager_response_activity_bump_is_throttled() -> None:
    manager = BrowserManager()
    session = BrowserSession(
        session_id="s",
        context=SimpleNamespace(),
        page=SimpleNamespace(),
        last_used_at=datetime.now(UTC) - timedelta(seconds=10),
    )

    manager._bump_session_activity(session)  # noqa: SLF001
    first = session.last_used_at
    manager._bump_session_activity(session)  # noqa: SLF001

    assert session.last_used_at == first


def test_browser_manager_session_settings_override_without_mutating_defaults() -> None:
    manager = BrowserManager(
        auto_consent="accept",
        stealth_enabled=True,
        fingerprint_hardening=True,
        humanize_input=True,
    )

    settings = manager._resolve_session_settings(  # noqa: SLF001
        {
            "auto_consent": "off",
            "stealth_enabled": False,
            "fingerprint_hardening": False,
            "humanize_input": False,
        }
    )

    assert settings.as_dict() == {
        "auto_consent": "off",
        "stealth_enabled": False,
        "fingerprint_hardening": False,
        "humanize_input": False,
    }
    assert manager.auto_consent == "accept"
    assert manager.stealth_enabled is True
    assert manager.fingerprint_hardening is True
    assert manager.humanize_input is True


def test_browser_manager_rejects_conflicting_existing_session_settings() -> None:
    manager = BrowserManager(auto_consent="accept")
    session = BrowserSession(
        session_id="s",
        context=SimpleNamespace(),
        page=SimpleNamespace(),
        browser_settings=BrowserSessionSettings(
            auto_consent="accept",
            stealth_enabled=True,
            fingerprint_hardening=True,
            humanize_input=True,
        ),
    )
    requested = {"auto_consent": "off"}
    resolved = manager._resolve_session_settings(requested)  # noqa: SLF001

    with pytest.raises(ValueError, match="cannot be changed"):
        manager._ensure_session_settings_compatible(  # noqa: SLF001
            session, requested=requested, resolved=resolved
        )


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
async def test_browser_manager_stops_virtual_display_without_mutating_process_display(
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
    manager._xvfb_display = ":99"  # noqa: SLF001
    manager._xvfb_process = _Proc()  # noqa: SLF001

    await manager._stop_virtual_display()  # noqa: SLF001

    assert os.environ.get("DISPLAY") == ":5"


@pytest.mark.asyncio
async def test_browser_manager_waits_for_virtual_display_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Proc:
        returncode = None

    manager = BrowserManager(xvfb_auto=True)
    checks = {"count": 0}

    def _fake_exists(self: Path) -> bool:
        if str(self) != "/tmp/.X11-unix/X99":
            return False
        checks["count"] += 1
        return checks["count"] >= 3

    monkeypatch.setattr(Path, "exists", _fake_exists)

    await manager._wait_for_virtual_display_ready(display=":99", proc=_Proc())  # noqa: SLF001

    assert checks["count"] == 3


@pytest.mark.asyncio
async def test_browser_manager_list_sessions_hides_idle_without_closing() -> None:
    manager = BrowserManager(idle_timeout_seconds=60)

    class _Context:
        closed = False

        async def close(self) -> None:
            self.closed = True
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
    assert stale_session.context.closed is False


@pytest.mark.asyncio
async def test_browser_manager_cleanup_idle_sessions_closes_stale() -> None:
    manager = BrowserManager(idle_timeout_seconds=60)

    class _Context:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    stale_context = _Context()
    manager._sessions = {  # noqa: SLF001
        "sess-old": SimpleNamespace(
            session_id="sess-old",
            page=SimpleNamespace(url="https://example.com"),
            context=stale_context,
            profile_mode="ephemeral",
            profile_id=None,
            headless=True,
            display=None,
            last_used_at=datetime.now(UTC) - timedelta(minutes=10),
            auth_origin=None,
        )
    }

    await manager._cleanup_idle_sessions()  # noqa: SLF001

    assert stale_context.closed is True
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_browser_manager_lists_profiles_from_disk() -> None:
    with TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "www-reddit-com").mkdir()
        (base / "github-com").mkdir()
        manager = BrowserManager(profile_base_dir=str(base))
        manager._sessions = {  # noqa: SLF001
            "sess-1": SimpleNamespace(
                profile_id="www-reddit-com",
                last_used_at=datetime.now(UTC),
            ),
            "sess-2": SimpleNamespace(
                profile_id="github-com",
                last_used_at=datetime.now(UTC) - timedelta(days=1),
            ),
        }

        profiles = await manager.list_profiles()

        assert [profile["profile_id"] for profile in profiles] == ["github-com", "www-reddit-com"]
        assert profiles[0]["currently_in_use"] is False
        assert profiles[1]["currently_in_use"] is True


def test_browser_manager_allocate_display_skips_claimed_values() -> None:
    manager = BrowserManager()
    manager._claimed_displays = {":99", ":100"}  # noqa: SLF001
    display = manager._allocate_display()  # noqa: SLF001
    assert display not in manager._claimed_displays


@pytest.mark.asyncio
async def test_open_session_records_returned_display(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = BrowserManager(profile_mode_default="persistent_local")

    async def _goto(*_a: object, **_k: object) -> None:
        return None

    async def _fake_open_persistent_context(**_: object):
        return (
            SimpleNamespace(),
            SimpleNamespace(url="https://reddit.com", goto=_goto),
            Path("/tmp/p"),
            ":101",
            1,
        )

    monkeypatch.setattr(manager, "_open_persistent_context", _fake_open_persistent_context)  # type: ignore[arg-type]
    session = await manager.open_session(
        session_id="sess-1",
        url="https://reddit.com/login",
        headless=False,
        profile_mode="persistent_local",
    )
    assert session.display == ":101"


@pytest.mark.asyncio
async def test_browser_manager_reserve_profile_blocks_duplicate_use() -> None:
    manager = BrowserManager()
    await manager._reserve_profile_id("reddit")  # noqa: SLF001
    with pytest.raises(RuntimeError, match="already in use"):
        await manager._reserve_profile_id("reddit")  # noqa: SLF001


@pytest.mark.asyncio
async def test_open_session_rolls_back_when_initial_navigation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(profile_mode_default="persistent_local")

    class _Context:
        async def close(self) -> None:
            return None

    async def _goto(*_a: object, **_k: object) -> None:
        raise RuntimeError("nav failed")

    async def _fake_open_persistent_context(**_: object):
        return (
            _Context(),
            SimpleNamespace(url="https://reddit.com", goto=_goto),
            Path("/tmp/p"),
            ":101",
            1,
        )

    monkeypatch.setattr(manager, "_open_persistent_context", _fake_open_persistent_context)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="nav failed"):
        await manager.open_session(
            session_id="sess-1",
            url="https://reddit.com/login",
            headless=False,
            profile_mode="persistent_local",
        )
    assert manager._sessions == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_open_session_closes_persistent_context_when_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(profile_mode_default="persistent_local")
    closed = {"count": 0}

    class _Context:
        async def close(self) -> None:
            closed["count"] += 1

    async def _goto(*_a: object, **_k: object) -> None:
        return None

    async def _fake_open_persistent_context(**_: object):
        return (
            _Context(),
            SimpleNamespace(url="https://reddit.com", goto=_goto),
            Path("/tmp/p"),
            ":101",
            1,
        )

    def _fake_register_session_locked(_session: object) -> None:
        raise RuntimeError("duplicate session")

    monkeypatch.setattr(manager, "_open_persistent_context", _fake_open_persistent_context)  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "_register_session_locked", _fake_register_session_locked)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="duplicate session"):
        await manager.open_session(
            session_id="sess-1",
            url="https://reddit.com/login",
            headless=False,
            profile_mode="persistent_local",
        )

    assert closed["count"] == 1


@pytest.mark.asyncio
async def test_open_session_closes_shared_browser_after_failed_navigation_when_no_other_open_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(profile_mode_default="ephemeral")
    browser_closed: list[str] = []

    class _Browser:
        async def new_context(self, **_kwargs: object) -> object:
            return _Context()

        async def close(self) -> None:
            browser_closed.append("closed")

    class _Context:
        async def close(self) -> None:
            return None

        async def new_page(self) -> object:
            return SimpleNamespace(url="https://reddit.com", goto=_goto)

        async def add_init_script(self, _script: str) -> None:
            return None

    async def _goto(*_a: object, **_k: object) -> None:
        raise RuntimeError("nav failed")

    async def _fake_ensure_runtime(*, headless: bool) -> None:
        b = _Browser()
        manager._browsers[headless] = b  # noqa: SLF001
        manager._browser = b  # noqa: SLF001
        manager._browser_generations[headless] = 1  # noqa: SLF001
        manager._runtime_generation = 1  # noqa: SLF001

    monkeypatch.setattr(manager, "ensure_runtime", _fake_ensure_runtime)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="nav failed"):
        await manager.open_session(
            session_id="sess-1",
            url="https://reddit.com/login",
            headless=True,
            profile_mode="ephemeral",
        )

    assert browser_closed == ["closed"]


@pytest.mark.asyncio
async def test_open_session_rolls_back_headed_open_counter_when_profile_reserve_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(profile_mode_default="persistent_local")

    async def _fake_reserve_profile_id(_profile_id: str) -> None:
        raise RuntimeError("already in use")

    monkeypatch.setattr(manager, "_reserve_profile_id", _fake_reserve_profile_id)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="already in use"):
        await manager.open_session(
            session_id="sess-1",
            url="https://reddit.com/login",
            headless=False,
            profile_mode="persistent_local",
        )

    assert manager._headed_open_in_flight == 0  # noqa: SLF001


def test_launch_env_does_not_mutate_process_display(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = BrowserManager()
    monkeypatch.setenv("DISPLAY", ":5")

    env = manager._launch_env(":99")  # noqa: SLF001

    assert env["DISPLAY"] == ":99"
    assert os.environ.get("DISPLAY") == ":5"


@pytest.mark.asyncio
async def test_ensure_playwright_ready_restarts_for_new_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(headed_allowed=True)
    stops: list[str] = []

    async def _fake_stop(*, headless: bool) -> None:
        del headless
        stops.append("stop")
        manager._playwrights.clear()  # noqa: SLF001
        manager._playwright_displays.clear()  # noqa: SLF001
        manager._playwright = None  # noqa: SLF001
        manager._playwright_display = None  # noqa: SLF001

    async def _fake_ensure_virtual_display_locked() -> None:
        manager._xvfb_display = ":99"  # noqa: SLF001

    class _Starter:
        async def start(self) -> object:
            return object()

    async def _fake_ensure_playwright_browser(**_: object) -> tuple[bool, str]:
        return True, "available"

    monkeypatch.setattr(
        "cognis.tools.executor.browser.manager.ensure_playwright_browser",
        _fake_ensure_playwright_browser,
    )
    monkeypatch.setattr(
        manager, "_ensure_virtual_display_locked", _fake_ensure_virtual_display_locked
    )  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "_stop_playwright_locked", _fake_stop)  # type: ignore[arg-type]
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _Starter())
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("sys.platform", "linux")

    manager._playwrights[False] = object()  # noqa: SLF001
    manager._playwright_displays[False] = None  # noqa: SLF001
    manager._playwright = manager._playwrights[False]  # noqa: SLF001
    manager._playwright_display = None  # noqa: SLF001

    display = await manager._ensure_playwright_ready_locked(headless=False)  # noqa: SLF001

    assert stops == ["stop"]
    assert display == ":99"
    assert manager._playwright_display == ":99"  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_playwright_ready_blocks_restart_with_live_generation_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(headed_allowed=True)

    async def _fake_ensure_playwright_browser(**_: object) -> tuple[bool, str]:
        return True, "available"

    monkeypatch.setattr(
        "cognis.tools.executor.browser.manager.ensure_playwright_browser",
        _fake_ensure_playwright_browser,
    )
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    async def _fake_ensure_virtual_display_locked() -> None:
        manager._xvfb_display = ":99"  # noqa: SLF001

    monkeypatch.setattr(
        manager, "_ensure_virtual_display_locked", _fake_ensure_virtual_display_locked
    )  # type: ignore[arg-type]
    manager._playwrights[False] = object()  # noqa: SLF001
    manager._playwright_displays[False] = None  # noqa: SLF001
    manager._playwright = manager._playwrights[False]  # noqa: SLF001
    manager._playwright_display = None  # noqa: SLF001
    manager._runtime_generation = 7  # noqa: SLF001
    manager._sessions = {
        "sess-1": SimpleNamespace(
            runtime_generation=7, headless=False, last_used_at=datetime.now(UTC)
        )
    }  # noqa: SLF001

    with pytest.raises(RuntimeError, match="different display"):
        await manager._ensure_playwright_ready_locked(headless=False)  # noqa: SLF001


@pytest.mark.asyncio
async def test_headed_runtime_can_start_while_headless_session_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(headed_allowed=True)

    async def _fake_ensure_playwright_browser(**_: object) -> tuple[bool, str]:
        return True, "available"

    async def _fake_ensure_virtual_display_locked() -> None:
        manager._xvfb_display = ":99"  # noqa: SLF001

    class _Starter:
        async def start(self) -> object:
            return object()

    monkeypatch.setattr(
        "cognis.tools.executor.browser.manager.ensure_playwright_browser",
        _fake_ensure_playwright_browser,
    )
    monkeypatch.setattr(
        manager, "_ensure_virtual_display_locked", _fake_ensure_virtual_display_locked
    )  # type: ignore[arg-type]
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _Starter())
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    headless_runtime = object()
    manager._playwrights[True] = headless_runtime  # noqa: SLF001
    manager._playwright_displays[True] = None  # noqa: SLF001
    manager._sessions = {  # noqa: SLF001
        "headless-1": SimpleNamespace(
            runtime_generation=1,
            headless=True,
            last_used_at=datetime.now(UTC),
        )
    }

    display = await manager._ensure_playwright_ready_locked(headless=False)  # noqa: SLF001

    assert display == ":99"
    assert manager._playwrights[True] is headless_runtime  # noqa: SLF001
    assert manager._playwrights[False] is not None  # noqa: SLF001
    assert manager._playwright_displays[False] == ":99"  # noqa: SLF001


@pytest.mark.asyncio
async def test_open_persistent_context_passes_display_env_without_mutating_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(headed_allowed=True)
    monkeypatch.setenv("DISPLAY", ":5")

    class _Context:
        pages: list[object] = []

        async def add_init_script(self, _script: str) -> None:
            return None

        async def new_page(self) -> object:
            return SimpleNamespace()

    launch_calls: list[dict[str, object]] = []

    async def _fake_ensure_playwright_ready_locked(*, headless: bool) -> str | None:
        manager._playwrights[headless] = SimpleNamespace(  # noqa: SLF001
            chromium=SimpleNamespace(
                launch_persistent_context=_fake_launch_persistent_context,
            )
        )
        manager._playwright = manager._playwrights[headless]  # noqa: SLF001
        manager._runtime_generation = 3  # noqa: SLF001
        return ":99"

    async def _fake_launch_persistent_context(user_data_dir: str, **kwargs: object) -> _Context:
        launch_calls.append({"user_data_dir": user_data_dir, **kwargs})
        return _Context()

    monkeypatch.setattr(
        manager, "_ensure_playwright_ready_locked", _fake_ensure_playwright_ready_locked
    )  # type: ignore[arg-type]

    _context, _page, _dir, display, runtime_generation = await manager._open_persistent_context(  # noqa: SLF001
        url="https://example.com",
        headless=False,
        auth_state=None,
        profile_id="example",
    )

    assert display == ":99"
    assert runtime_generation == 3
    assert launch_calls[0]["env"]["DISPLAY"] == ":99"
    assert os.environ.get("DISPLAY") == ":5"


@pytest.mark.asyncio
async def test_launch_shared_browser_retries_only_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(headed_allowed=True)
    attempts = {"count": 0}
    recoveries: list[int] = []

    async def _fake_ensure_playwright_ready_locked(*, headless: bool) -> str | None:
        manager._playwrights[headless] = SimpleNamespace(  # noqa: SLF001
            chromium=SimpleNamespace(launch=_fake_launch)
        )
        manager._playwright = manager._playwrights[headless]  # noqa: SLF001
        return ":99"

    async def _fake_launch(**_kwargs: object) -> object:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("x server missing")
        return object()

    async def _fake_recover(
        *, failure_category: str, headless: bool | None = None, retry_count: int
    ) -> None:
        del failure_category, headless
        recoveries.append(retry_count)

    monkeypatch.setattr(
        manager, "_ensure_playwright_ready_locked", _fake_ensure_playwright_ready_locked
    )  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "_recover_retryable_launch_failure_locked", _fake_recover)  # type: ignore[arg-type]

    await manager._launch_shared_browser_locked(headless=False)  # noqa: SLF001

    assert attempts["count"] == 2
    assert recoveries == [1]


@pytest.mark.asyncio
async def test_launch_shared_browser_does_not_retry_non_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserManager(headed_allowed=True)
    attempts = {"count": 0}
    recoveries: list[int] = []

    async def _fake_ensure_playwright_ready_locked(*, headless: bool) -> str | None:
        manager._playwrights[headless] = SimpleNamespace(  # noqa: SLF001
            chromium=SimpleNamespace(launch=_fake_launch)
        )
        manager._playwright = manager._playwrights[headless]  # noqa: SLF001
        return ":99"

    async def _fake_launch(**_kwargs: object) -> object:
        attempts["count"] += 1
        raise ValueError("bad args")

    async def _fake_recover(*, failure_category: str, retry_count: int) -> None:
        recoveries.append(retry_count)

    monkeypatch.setattr(
        manager, "_ensure_playwright_ready_locked", _fake_ensure_playwright_ready_locked
    )  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "_recover_retryable_launch_failure_locked", _fake_recover)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="bad args"):
        await manager._launch_shared_browser_locked(headless=False)  # noqa: SLF001

    assert attempts["count"] == 1
    assert recoveries == []


@pytest.mark.asyncio
async def test_recover_retryable_launch_failure_blocks_when_live_session_exists() -> None:
    manager = BrowserManager()
    manager._runtime_generation = 4  # noqa: SLF001
    manager._sessions = {  # noqa: SLF001
        "sess-1": SimpleNamespace(
            runtime_generation=4, headless=False, last_used_at=datetime.now(UTC)
        )
    }

    with pytest.raises(RuntimeError, match="live sessions exist"):
        await manager._recover_retryable_launch_failure_locked(  # noqa: SLF001
            failure_category="display_bootstrap",
            retry_count=1,
        )


@pytest.mark.asyncio
async def test_recover_retryable_launch_failure_blocks_when_other_headed_open_exists() -> None:
    manager = BrowserManager()
    manager._open_in_flight = 2  # noqa: SLF001

    with pytest.raises(RuntimeError, match="live sessions exist"):
        await manager._recover_retryable_launch_failure_locked(  # noqa: SLF001
            failure_category="display_bootstrap",
            retry_count=1,
        )


@pytest.mark.asyncio
async def test_recover_retryable_headed_launch_preserves_live_headless_session() -> None:
    manager = BrowserManager()
    stopped: list[str] = []
    xvfb_stopped: list[str] = []

    class _Playwright:
        def __init__(self, label: str) -> None:
            self.label = label

        async def stop(self) -> None:
            stopped.append(self.label)

    async def _fake_stop_virtual_display_locked() -> None:
        xvfb_stopped.append("stop")

    headless_runtime = _Playwright("headless")
    headed_runtime = _Playwright("headed")
    manager._sessions = {  # noqa: SLF001
        "headless-1": SimpleNamespace(
            runtime_generation=1,
            headless=True,
            last_used_at=datetime.now(UTC),
        )
    }
    manager._playwrights = {True: headless_runtime, False: headed_runtime}  # noqa: SLF001
    manager._playwright_displays = {True: None, False: ":99"}  # noqa: SLF001
    manager._open_in_flight = 1  # noqa: SLF001
    manager._open_in_flight_by_mode = {True: 0, False: 1}  # noqa: SLF001
    manager._xvfb_display = ":99"  # noqa: SLF001
    manager._xvfb_process = SimpleNamespace()  # noqa: SLF001
    manager._stop_virtual_display_locked = _fake_stop_virtual_display_locked  # type: ignore[method-assign]

    await manager._recover_retryable_launch_failure_locked(  # noqa: SLF001
        failure_category="display_bootstrap",
        headless=False,
        retry_count=1,
    )

    assert stopped == ["headed"]
    assert xvfb_stopped == ["stop"]
    assert manager._playwrights == {True: headless_runtime}  # noqa: SLF001
    assert manager._sessions["headless-1"].headless is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_close_session_keeps_xvfb_while_headed_open_in_flight() -> None:
    manager = BrowserManager()
    stopped: list[str] = []

    class _Context:
        async def close(self) -> None:
            return None

    async def _fake_stop_virtual_display_locked() -> None:
        stopped.append("stop")

    manager._sessions = {  # noqa: SLF001
        "sess-1": SimpleNamespace(
            session_id="sess-1",
            context=_Context(),
            headless=False,
            profile_mode="ephemeral",
            profile_id=None,
            display=":99",
            runtime_generation=1,
        )
    }
    manager._headed_open_in_flight = 1  # noqa: SLF001
    manager._xvfb_display = ":99"  # noqa: SLF001
    manager._xvfb_process = SimpleNamespace()  # noqa: SLF001
    manager._stop_virtual_display_locked = _fake_stop_virtual_display_locked  # type: ignore[method-assign]

    await manager.close_session("sess-1")

    assert stopped == []


@pytest.mark.asyncio
async def test_close_last_headed_session_cleans_up_while_headless_open_in_flight() -> None:
    manager = BrowserManager()
    browser_closed: list[str] = []
    playwright_stopped: list[str] = []
    xvfb_stopped: list[str] = []

    class _Context:
        async def close(self) -> None:
            return None

    class _Browser:
        async def close(self) -> None:
            browser_closed.append("headed")

    class _Playwright:
        async def stop(self) -> None:
            playwright_stopped.append("headed")

    async def _fake_stop_virtual_display_locked() -> None:
        xvfb_stopped.append("stop")

    manager._sessions = {  # noqa: SLF001
        "sess-1": SimpleNamespace(
            session_id="sess-1",
            context=_Context(),
            headless=False,
            profile_mode="ephemeral",
            profile_id=None,
            display=":99",
            runtime_generation=1,
        )
    }
    manager._open_in_flight = 1  # noqa: SLF001
    manager._open_in_flight_by_mode = {True: 1, False: 0}  # noqa: SLF001
    manager._browsers[False] = _Browser()  # noqa: SLF001
    manager._playwrights[False] = _Playwright()  # noqa: SLF001
    manager._playwright_displays[False] = ":99"  # noqa: SLF001
    manager._xvfb_display = ":99"  # noqa: SLF001
    manager._xvfb_process = SimpleNamespace()  # noqa: SLF001
    manager._stop_virtual_display_locked = _fake_stop_virtual_display_locked  # type: ignore[method-assign]

    await manager.close_session("sess-1")

    assert browser_closed == ["headed"]
    assert playwright_stopped == ["headed"]
    assert xvfb_stopped == ["stop"]
    assert False not in manager._playwrights  # noqa: SLF001


@pytest.mark.asyncio
async def test_close_headless_session_keeps_xvfb_for_sessionless_headed_runtime() -> None:
    manager = BrowserManager()
    xvfb_stopped: list[str] = []

    class _Context:
        async def close(self) -> None:
            return None

    class _Playwright:
        async def stop(self) -> None:
            return None

    async def _fake_stop_virtual_display_locked() -> None:
        xvfb_stopped.append("stop")

    manager._sessions = {  # noqa: SLF001
        "headless-1": SimpleNamespace(
            session_id="headless-1",
            context=_Context(),
            headless=True,
            profile_mode="ephemeral",
            profile_id=None,
            display=None,
            runtime_generation=1,
        )
    }
    manager._playwrights[False] = _Playwright()  # noqa: SLF001
    manager._playwright_displays[False] = ":99"  # noqa: SLF001
    manager._xvfb_display = ":99"  # noqa: SLF001
    manager._xvfb_process = SimpleNamespace()  # noqa: SLF001
    manager._stop_virtual_display_locked = _fake_stop_virtual_display_locked  # type: ignore[method-assign]

    await manager.close_session("headless-1")

    assert xvfb_stopped == []
    assert manager._playwrights.get(False) is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_close_session_keeps_shared_browser_while_open_in_flight() -> None:
    manager = BrowserManager()
    browser_closed: list[str] = []

    class _Context:
        async def close(self) -> None:
            return None

    async def _fake_close_shared_browser_locked(*, headless: bool | None = None) -> None:
        del headless
        browser_closed.append("closed")

    manager._sessions = {  # noqa: SLF001
        "sess-1": SimpleNamespace(
            session_id="sess-1",
            context=_Context(),
            headless=True,
            profile_mode="ephemeral",
            profile_id=None,
            display=None,
            runtime_generation=1,
        )
    }
    manager._browser = object()  # noqa: SLF001
    manager._open_in_flight = 1  # noqa: SLF001
    manager._open_in_flight_by_mode = {True: 1, False: 0}  # noqa: SLF001
    manager._close_shared_browser_locked = _fake_close_shared_browser_locked  # type: ignore[method-assign]

    await manager.close_session("sess-1")

    assert browser_closed == []


def test_log_lifecycle_emits_safe_fields_only(caplog: pytest.LogCaptureFixture) -> None:
    manager = BrowserManager()
    session = SimpleNamespace(
        session_id="sess-1",
        profile_mode="persistent_local",
        profile_id="example",
        headless=False,
        display=":99",
    )

    with caplog.at_level(logging.INFO):
        manager._log_lifecycle("browser_session_open", outcome="success", session=session)  # noqa: SLF001

    message = caplog.records[-1].getMessage()
    extra = caplog.records[-1].extra_data
    assert message == "browser: lifecycle"
    assert extra["session_id"] == "sess-1"
    assert "profile_id" not in extra
    assert "failure_category" not in extra
    assert "arguments" not in extra
    assert "output" not in extra


# ---------------------------------------------------------------------------
# Stage B: parallel headless + headed modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headless_and_headed_browsers_can_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two browsers (headless and headed) can coexist in the same manager."""
    manager = BrowserManager(headed_allowed=True, profile_mode_default="ephemeral")
    launched: list[bool] = []  # records headless argument for each launch

    class _FakeBrowser:
        def __init__(self, headless: bool) -> None:
            self._headless = headless
            self.closed = False

        async def new_context(self, **_: object) -> object:
            return _FakeContext()

        async def close(self) -> None:
            self.closed = True

    class _FakeContext:
        async def close(self) -> None:
            pass

        async def new_page(self) -> object:
            return SimpleNamespace(url="https://example.com", goto=_noop_goto)

        async def add_init_script(self, _script: str) -> None:
            pass

    async def _noop_goto(*_a: object, **_k: object) -> None:
        pass

    async def _fake_launch_shared_browser_locked(*, headless: bool) -> None:
        b = _FakeBrowser(headless)
        manager._browsers[headless] = b  # noqa: SLF001
        manager._browser = b  # noqa: SLF001
        manager._runtime_generation += 1  # noqa: SLF001
        manager._browser_generations[headless] = manager._runtime_generation  # noqa: SLF001
        launched.append(headless)

    monkeypatch.setattr(
        manager, "_launch_shared_browser_locked", _fake_launch_shared_browser_locked
    )

    await manager.open_session(
        session_id="headless-1",
        url="https://example.com",
        headless=True,
        profile_mode="ephemeral",
    )
    await manager.open_session(
        session_id="headed-1",
        url="https://example.com",
        headless=False,
        profile_mode="ephemeral",
    )

    assert True in manager._browsers  # noqa: SLF001
    assert False in manager._browsers  # noqa: SLF001
    assert len(manager._sessions) == 2  # noqa: SLF001
    assert True in launched and False in launched


@pytest.mark.asyncio
async def test_close_headless_session_does_not_affect_headed_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the headless session tears down only the headless browser."""
    manager = BrowserManager(headed_allowed=True, profile_mode_default="ephemeral")
    headless_closed: list[bool] = []
    headed_closed: list[bool] = []

    class _FakeBrowser:
        def __init__(self, closed_recorder: list[bool]) -> None:
            self._closed_recorder = closed_recorder

        async def new_context(self, **_: object) -> object:
            return _FakeContext()

        async def close(self) -> None:
            self._closed_recorder.append(True)

    class _FakeContext:
        async def close(self) -> None:
            pass

        async def new_page(self) -> object:
            return SimpleNamespace(url="https://example.com", goto=_noop_goto)

        async def add_init_script(self, _script: str) -> None:
            pass

    async def _noop_goto(*_a: object, **_k: object) -> None:
        pass

    async def _fake_launch_shared_browser_locked(*, headless: bool) -> None:
        b = _FakeBrowser(headless_closed if headless else headed_closed)
        manager._browsers[headless] = b  # noqa: SLF001
        manager._browser = b  # noqa: SLF001
        manager._runtime_generation += 1  # noqa: SLF001
        manager._browser_generations[headless] = manager._runtime_generation  # noqa: SLF001

    monkeypatch.setattr(
        manager, "_launch_shared_browser_locked", _fake_launch_shared_browser_locked
    )

    await manager.open_session(
        session_id="headless-1",
        url="https://example.com",
        headless=True,
        profile_mode="ephemeral",
    )
    await manager.open_session(
        session_id="headed-1",
        url="https://example.com",
        headless=False,
        profile_mode="ephemeral",
    )

    await manager.close_session("headless-1")

    assert headless_closed == [True]
    assert headed_closed == []
    assert len(manager._sessions) == 1  # noqa: SLF001
    assert "headed-1" in manager._sessions  # noqa: SLF001
