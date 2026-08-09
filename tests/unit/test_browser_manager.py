from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from cognis.tools.executor.browser.manager import (
    BrowserLifecycleError,
    BrowserManager,
    BrowserSession,
    BrowserSessionOwner,
    BrowserSessionSettings,
)


def _browser_owner(
    scope_id: str,
    *,
    parent_session_id: str | None = None,
    user_email: str = "user@example.com",
) -> BrowserSessionOwner:
    return BrowserSessionOwner(
        execution_scope_id=scope_id,
        session_id=scope_id,
        conversation_id=f"conversation-{scope_id}",
        user_email=user_email,
        agent_id="agent-1",
        parent_session_id=parent_session_id,
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


@pytest.mark.asyncio
async def test_native_bootstrap_warms_new_patchright_chrome_profile_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = BrowserManager(
        runtime="patchright",
        channel="chrome",
        native_bootstrap_seconds=1,
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    terminated = asyncio.Event()
    calls: list[tuple[object, ...]] = []

    class _Process:
        returncode: int | None = None

        async def wait(self) -> int:
            await terminated.wait()
            return 0

        def terminate(self) -> None:
            self.returncode = 0
            terminated.set()

        def kill(self) -> None:
            self.terminate()

    async def _create(*args: object, **kwargs: object) -> _Process:
        assert (profile / ".cognis-native-bootstrap.html").stat().st_mode & 0o777 == 0o600
        calls.append((*args, kwargs))
        return _Process()

    monkeypatch.setattr(manager, "_native_chrome_executable", lambda: "/chrome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)

    await manager._bootstrap_native_profile(  # noqa: SLF001
        user_data_dir=profile,
        url="https://example.com/listings",
        display=":99",
    )
    await manager._bootstrap_native_profile(  # noqa: SLF001
        user_data_dir=profile,
        url="https://example.com/listings",
        display=":99",
    )

    assert len(calls) == 1
    assert calls[0][:5] == (
        "/chrome",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        (profile / ".cognis-native-bootstrap.html").as_uri(),
    )
    assert not (profile / ".cognis-native-bootstrap.html").exists()
    assert (profile / ".cognis-native-bootstrap-v1").is_file()


@pytest.mark.asyncio
async def test_native_bootstrap_does_not_mark_profile_when_chrome_exits_early(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = BrowserManager(runtime="patchright", channel="chrome")
    profile = tmp_path / "profile"
    profile.mkdir()

    class _Process:
        returncode = 1

        async def wait(self) -> int:
            return 1

        def terminate(self) -> None:
            raise AssertionError("already exited")

        def kill(self) -> None:
            raise AssertionError("already exited")

    async def _create(*_args: object, **_kwargs: object) -> _Process:
        return _Process()

    monkeypatch.setattr(manager, "_native_chrome_executable", lambda: "/chrome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)

    await manager._bootstrap_native_profile(  # noqa: SLF001
        user_data_dir=profile,
        url="https://example.com",
        display=":99",
    )

    assert not (profile / ".cognis-native-bootstrap-v1").exists()


@pytest.mark.asyncio
async def test_native_bootstrap_removes_redirect_file_when_chrome_spawn_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = BrowserManager(runtime="patchright", channel="chrome")
    profile = tmp_path / "profile"
    profile.mkdir()

    async def _fail(*_args: object, **_kwargs: object) -> object:
        raise OSError("spawn failed")

    monkeypatch.setattr(manager, "_native_chrome_executable", lambda: "/chrome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

    with pytest.raises(OSError, match="spawn failed"):
        await manager._bootstrap_native_profile(  # noqa: SLF001
            user_data_dir=profile,
            url="https://example.com/?token=secret",
            display=":99",
        )

    assert not (profile / ".cognis-native-bootstrap.html").exists()
    assert not (profile / ".cognis-native-bootstrap-v1").exists()


@pytest.mark.asyncio
async def test_native_bootstrap_is_not_used_for_headless_persistent_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = BrowserManager(runtime="patchright", channel="chrome")
    bootstrap_calls: list[dict[str, object]] = []

    class _Context:
        pages: list[object] = []

        async def new_page(self) -> SimpleNamespace:
            return SimpleNamespace()

    async def _launch(*_args: object, **_kwargs: object) -> _Context:
        return _Context()

    async def _ready(*, headless: bool) -> None:
        manager._playwrights[headless] = SimpleNamespace(  # noqa: SLF001
            chromium=SimpleNamespace(launch_persistent_context=_launch)
        )
        return None

    async def _bootstrap(**kwargs: object) -> None:
        bootstrap_calls.append(kwargs)

    async def _defaults(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(manager, "_ensure_playwright_ready_locked", _ready)
    monkeypatch.setattr(manager, "_bootstrap_native_profile", _bootstrap)
    monkeypatch.setattr(manager, "_apply_context_defaults", _defaults)

    await manager._open_persistent_context(  # noqa: SLF001
        url="https://example.com",
        headless=True,
        auth_state=None,
        profile_id="example",
    )

    assert bootstrap_calls == []


@pytest.mark.asyncio
async def test_browser_manager_scopes_inspect_and_close_to_owner() -> None:
    manager = BrowserManager()
    closed: list[bool] = []

    class _Context:
        async def close(self) -> None:
            closed.append(True)

    owner = _browser_owner("scope-owner")
    unrelated = _browser_owner("scope-other")
    session = BrowserSession(
        session_id="owned-session",
        context=_Context(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )
    manager._sessions[session.session_id] = session  # noqa: SLF001

    assert [item["session_id"] for item in await manager.list_sessions(owner=owner)] == [
        "owned-session"
    ]
    assert await manager.list_sessions(owner=unrelated) == []
    last_used_at = session.last_used_at
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.get_live_session(session.session_id, owner=unrelated)
    assert exc_info.value.code == "browser_unauthorized"
    assert session.last_used_at == last_used_at
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.close_session(session.session_id, owner=unrelated)
    assert exc_info.value.code == "browser_unauthorized"
    assert closed == []

    assert await manager.close_session(session.session_id, owner=owner) is True
    assert await manager.close_session(session.session_id, owner=owner) is False
    assert closed == [True]


@pytest.mark.asyncio
async def test_browser_manager_parent_can_release_only_managed_descendant() -> None:
    manager = BrowserManager()

    class _Context:
        async def close(self) -> None:
            return None

    controller = _browser_owner("controller")
    descendant = BrowserSession(
        session_id="descendant",
        context=_Context(),
        page=SimpleNamespace(url=""),
        owner=_browser_owner("child", parent_session_id="controller"),
    )
    unrelated = BrowserSession(
        session_id="unrelated",
        context=_Context(),
        page=SimpleNamespace(url=""),
        owner=_browser_owner("peer", parent_session_id="different-controller"),
    )
    manager._sessions = {  # noqa: SLF001
        descendant.session_id: descendant,
        unrelated.session_id: unrelated,
    }

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.close_session(
            descendant.session_id,
            owner=controller,
            allow_managed_descendant=True,
        )
    assert exc_info.value.code == "browser_session_active"

    manager._terminal_owners["child"] = descendant.owner  # type: ignore[assignment]  # noqa: SLF001
    assert (
        await manager.close_session(
            descendant.session_id,
            owner=controller,
            allow_managed_descendant=True,
        )
        is True
    )
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.close_session(
            unrelated.session_id,
            owner=controller,
            allow_managed_descendant=True,
        )
    assert exc_info.value.code == "browser_unauthorized"


@pytest.mark.asyncio
async def test_browser_manager_terminal_cleanup_is_automatic_and_idempotent() -> None:
    manager = BrowserManager()
    closed: list[str] = []

    class _Context:
        async def close(self) -> None:
            closed.append("closed")

    owner = _browser_owner("child", parent_session_id="controller")
    manager._sessions["child-browser"] = BrowserSession(  # noqa: SLF001
        session_id="child-browser",
        context=_Context(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )

    assert await manager.mark_owner_terminal(owner) == 1
    assert await manager.mark_owner_terminal(owner) == 0
    assert closed == ["closed"]
    assert manager.active_session_count == 0


@pytest.mark.asyncio
async def test_browser_manager_rejects_registration_after_terminal_notification() -> None:
    manager = BrowserManager()
    owner = _browser_owner("child", parent_session_id="controller")
    manager._terminal_owners[owner.execution_scope_id] = owner  # noqa: SLF001

    class _Context:
        async def close(self) -> None:
            return None

    session = BrowserSession(
        session_id="late-browser",
        context=_Context(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )

    with pytest.raises(BrowserLifecycleError) as exc_info:
        manager._register_session_locked(session)  # noqa: SLF001

    assert exc_info.value.code == "browser_session_terminal"
    assert manager.active_session_count == 0
    await manager._session_close_tasks["late-browser"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_browser_manager_rejects_public_open_after_terminal_notification() -> None:
    manager = BrowserManager()
    owner = _browser_owner("child", parent_session_id="controller")
    await manager.mark_owner_terminal(owner)

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.open_session(
            session_id="late-browser",
            url="https://example.com",
            owner=owner,
        )

    assert exc_info.value.code == "browser_session_terminal"
    assert manager.active_session_count == 0


@pytest.mark.asyncio
async def test_browser_manager_retries_failed_terminal_close() -> None:
    manager = BrowserManager()

    class _Context:
        attempts = 0

        async def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient close failure")

    context = _Context()
    owner = _browser_owner("child", parent_session_id="controller")
    manager._sessions["child-browser"] = BrowserSession(  # noqa: SLF001
        session_id="child-browser",
        context=context,
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.mark_owner_terminal(owner)
    assert exc_info.value.code == "browser_session_close_failed"
    assert (await manager.list_sessions(owner=_browser_owner("controller")))[0]["state"] == (
        "terminal_cleanup_pending"
    )
    assert await manager.mark_owner_terminal(owner) == 1
    assert context.attempts == 2
    assert await manager.list_sessions(owner=_browser_owner("controller")) == []


@pytest.mark.asyncio
async def test_pending_close_retains_capacity_and_profile_lock(tmp_path: Path) -> None:
    manager = BrowserManager(max_sessions=1, profile_base_dir=str(tmp_path))
    owner = _browser_owner("child", parent_session_id="controller")
    manager._ensure_profile_owner("profile", owner)  # noqa: SLF001
    pending = BrowserSession(
        session_id="pending-browser",
        context=SimpleNamespace(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
        profile_mode="persistent_local",
        profile_id="profile",
        idle_timeout_seconds=1,
        last_used_at=datetime.now(UTC) - timedelta(seconds=60),
    )
    manager._closing_sessions[pending.session_id] = pending  # noqa: SLF001

    with pytest.raises(RuntimeError, match="limit exceeded"):
        await manager._reserve_open_slot(  # noqa: SLF001
            headless=True,
            wait_for_slot=False,
            wait_timeout_seconds=0,
        )
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager._reserve_profile_id("profile", owner=owner)  # noqa: SLF001
    assert exc_info.value.code == "browser_profile_locked"
    assert (
        await manager.inspect_session("pending-browser", owner=owner)
        == (await manager.list_sessions(owner=owner))[0]
    )
    profiles = await manager.list_profiles(owner=owner)
    assert profiles[0]["currently_in_use"] is True


@pytest.mark.asyncio
async def test_concurrent_close_calls_close_context_once() -> None:
    manager = BrowserManager()
    owner = _browser_owner("owner")
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    attempts = 0

    class _Context:
        async def close(self) -> None:
            nonlocal attempts
            attempts += 1
            close_started.set()
            await release_close.wait()

    manager._sessions["browser"] = BrowserSession(  # noqa: SLF001
        session_id="browser",
        context=_Context(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )

    first = asyncio.create_task(manager.close_session("browser", owner=owner))
    await close_started.wait()
    second = asyncio.create_task(manager.close_session("browser", owner=owner))
    await asyncio.sleep(0)
    assert attempts == 1

    release_close.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert attempts == 1
    assert manager._closing_sessions == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_concurrent_terminal_cleanup_closes_context_once() -> None:
    manager = BrowserManager()
    owner = _browser_owner("child", parent_session_id="controller")
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    attempts = 0

    class _Context:
        async def close(self) -> None:
            nonlocal attempts
            attempts += 1
            close_started.set()
            await release_close.wait()

    manager._sessions["browser"] = BrowserSession(  # noqa: SLF001
        session_id="browser",
        context=_Context(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )

    first = asyncio.create_task(manager.mark_owner_terminal(owner))
    await close_started.wait()
    second = asyncio.create_task(manager.mark_owner_terminal(owner))
    await asyncio.sleep(0)
    assert attempts == 1

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await second
    assert exc_info.value.code == "browser_session_close_failed"
    release_close.set()
    assert await first == 1
    assert attempts == 1


@pytest.mark.asyncio
async def test_in_progress_terminal_close_does_not_block_sibling_cleanup() -> None:
    manager = BrowserManager()
    owner = _browser_owner("child", parent_session_id="controller")
    blocked_started = asyncio.Event()
    release_blocked = asyncio.Event()
    sibling_closed = asyncio.Event()

    class _BlockedContext:
        async def close(self) -> None:
            blocked_started.set()
            await release_blocked.wait()

    class _SiblingContext:
        async def close(self) -> None:
            sibling_closed.set()

    manager._sessions["blocked"] = BrowserSession(  # noqa: SLF001
        session_id="blocked",
        context=_BlockedContext(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )
    manager._sessions["sibling"] = BrowserSession(  # noqa: SLF001
        session_id="sibling",
        context=_SiblingContext(),
        page=SimpleNamespace(url="https://example.com"),
        owner=owner,
    )
    blocked_close = asyncio.create_task(manager.close_session("blocked", owner=owner))
    await blocked_started.wait()

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.mark_owner_terminal(owner)

    assert exc_info.value.code == "browser_session_close_failed"
    assert sibling_closed.is_set()
    assert "sibling" not in manager._sessions  # noqa: SLF001
    release_blocked.set()
    assert await blocked_close is True


@pytest.mark.asyncio
async def test_manager_cleanup_attempts_all_sessions_after_first_close_failure() -> None:
    manager = BrowserManager()
    calls: list[str] = []

    class _RetryContext:
        attempts = 0

        async def close(self) -> None:
            self.attempts += 1
            calls.append(f"retry-{self.attempts}")
            if self.attempts == 1:
                raise RuntimeError("first close failed")

    class _SiblingContext:
        async def close(self) -> None:
            calls.append("sibling")

    retry_context = _RetryContext()
    manager._sessions["retry"] = BrowserSession(  # noqa: SLF001
        session_id="retry",
        context=retry_context,
        page=SimpleNamespace(url="https://example.com"),
    )
    manager._sessions["sibling"] = BrowserSession(  # noqa: SLF001
        session_id="sibling",
        context=_SiblingContext(),
        page=SimpleNamespace(url="https://example.com"),
    )

    await manager.cleanup()

    assert calls[:2] == ["retry-1", "sibling"]
    assert retry_context.attempts == 2
    assert manager._sessions == {}  # noqa: SLF001
    assert manager._closing_sessions == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_open_race_retains_rejected_context_until_close_succeeds(
    tmp_path: Path,
) -> None:
    manager = BrowserManager(profile_base_dir=str(tmp_path))
    owner = _browser_owner("child", parent_session_id="controller")
    open_started = asyncio.Event()
    release_open = asyncio.Event()

    class _Context:
        attempts = 0

        async def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("late close failed")

    context = _Context()
    page = SimpleNamespace(url="https://example.com")

    async def _open_context(**_: object) -> tuple[object, object, Path, None, int]:
        open_started.set()
        await release_open.wait()
        return context, page, tmp_path / "profile", None, 1

    manager._open_persistent_context = _open_context  # type: ignore[method-assign]  # noqa: SLF001
    open_task = asyncio.create_task(
        manager.open_session(
            session_id="late-browser",
            url="https://example.com",
            profile_mode="persistent_local",
            profile_id="profile",
            owner=owner,
        )
    )
    await open_started.wait()

    with pytest.raises(BrowserLifecycleError) as terminal_exc:
        await manager.mark_owner_terminal(owner)
    assert terminal_exc.value.code == "browser_session_close_failed"
    release_open.set()
    with pytest.raises(BrowserLifecycleError) as open_exc:
        await open_task
    assert open_exc.value.code == "browser_session_terminal"
    assert manager._closing_sessions["late-browser"].context is context  # noqa: SLF001

    assert await manager.mark_owner_terminal(owner) == 1
    assert context.attempts == 2
    assert manager._closing_sessions == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_manager_cleanup_waits_for_and_closes_in_progress_open(tmp_path: Path) -> None:
    manager = BrowserManager(profile_base_dir=str(tmp_path))
    owner = _browser_owner("child", parent_session_id="controller")
    open_started = asyncio.Event()
    release_open = asyncio.Event()
    closed = asyncio.Event()

    class _Context:
        async def close(self) -> None:
            closed.set()

    async def _open_context(**_: object) -> tuple[object, object, Path, None, int]:
        open_started.set()
        await release_open.wait()
        return (
            _Context(),
            SimpleNamespace(url="https://example.com"),
            tmp_path / "profile",
            None,
            1,
        )

    manager._open_persistent_context = _open_context  # type: ignore[method-assign]  # noqa: SLF001
    open_task = asyncio.create_task(
        manager.open_session(
            session_id="late-browser",
            url="https://example.com",
            profile_mode="persistent_local",
            profile_id="profile",
            owner=owner,
        )
    )
    await open_started.wait()
    cleanup_task = asyncio.create_task(manager.cleanup())
    await asyncio.sleep(0)
    release_open.set()

    with pytest.raises(BrowserLifecycleError) as open_exc:
        await open_task
    assert open_exc.value.code == "browser_session_terminal"
    await cleanup_task

    assert closed.is_set()
    assert manager._open_in_flight == 0  # noqa: SLF001
    assert manager._sessions == {}  # noqa: SLF001
    assert manager._closing_sessions == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_browser_manager_inspect_exposes_safe_managed_metadata() -> None:
    manager = BrowserManager()
    parent = _browser_owner("controller")
    child_owner = _browser_owner("child", parent_session_id="controller")
    manager._sessions["child-browser"] = BrowserSession(  # noqa: SLF001
        session_id="child-browser",
        context=SimpleNamespace(),
        page=SimpleNamespace(url="https://example.com"),
        owner=child_owner,
    )
    last_used_at = manager._sessions["child-browser"].last_used_at  # noqa: SLF001

    metadata = await manager.inspect_session("child-browser", owner=parent)

    assert metadata["owner"] == {
        "scope_id": "child",
        "session_id": "child",
        "conversation_id": "conversation-child",
        "agent_id": "agent-1",
        "relationship": "managed_descendant",
    }
    assert "user_email" not in metadata["owner"]
    assert manager._sessions["child-browser"].last_used_at == last_used_at  # noqa: SLF001


@pytest.mark.asyncio
async def test_unauthorized_inspect_is_indistinguishable_from_missing() -> None:
    manager = BrowserManager()
    manager._sessions["hidden"] = BrowserSession(  # noqa: SLF001
        session_id="hidden",
        context=SimpleNamespace(),
        page=SimpleNamespace(url="https://example.com"),
        owner=_browser_owner("owner"),
    )

    errors: list[BrowserLifecycleError] = []
    for session_id in ("hidden", "missing"):
        with pytest.raises(BrowserLifecycleError) as exc_info:
            await manager.inspect_session(session_id, owner=_browser_owner("unrelated"))
        errors.append(exc_info.value)

    assert [(error.code, str(error)) for error in errors] == [
        (errors[1].code, str(errors[1])),
        (errors[1].code, str(errors[1])),
    ]
    assert errors[0].code == "browser_session_missing"


@pytest.mark.asyncio
async def test_browser_manager_distinguishes_missing_expired_and_locked() -> None:
    manager = BrowserManager()
    owner = _browser_owner("scope-owner")

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.get_live_session("missing", owner=owner)
    assert exc_info.value.code == "browser_session_missing"

    manager._expired_session_ids["expired"] = 0  # noqa: SLF001
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.get_live_session("expired", owner=owner)
    assert exc_info.value.code == "browser_session_expired"

    await manager._reserve_profile_id("profile", owner=owner)  # noqa: SLF001
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager._reserve_profile_id("profile", owner=owner)  # noqa: SLF001
    assert exc_info.value.code == "browser_profile_locked"

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.list_sessions(owner=None)
    assert exc_info.value.code == "browser_unauthenticated"


def test_browser_manager_classifies_only_persistent_profile_lock_errors() -> None:
    assert (
        BrowserManager._persistent_profile_is_locked(  # noqa: SLF001
            RuntimeError("Failed to create a ProcessSingleton for your profile directory")
        )
        is True
    )
    assert (
        BrowserManager._persistent_profile_is_locked(  # noqa: SLF001
            RuntimeError("Executable doesn't exist at /missing/chromium")
        )
        is False
    )


@pytest.mark.asyncio
async def test_failed_duplicate_open_does_not_release_existing_profile_reservation() -> None:
    manager = BrowserManager(profile_mode_default="persistent_local")
    first = _browser_owner("first")
    second = _browser_owner("second")
    await manager._reserve_profile_id("shared", owner=first)  # noqa: SLF001

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.open_session(
            session_id="second",
            url="https://example.com",
            profile_mode="persistent_local",
            profile_id="shared",
            owner=second,
        )
    assert exc_info.value.code == "browser_profile_locked"
    assert manager._reserved_profile_ids["shared"] == first  # noqa: SLF001
    await manager.cleanup()


def test_ephemeral_sessions_use_ephemeral_timeout_default() -> None:
    manager = BrowserManager(idle_timeout_seconds=1800)
    session = SimpleNamespace(lifecycle="ephemeral", idle_timeout_seconds=None)
    assert manager._session_idle_timeout_seconds(session) == 60  # noqa: SLF001


@pytest.mark.asyncio
async def test_ephemeral_reaping_works_when_explicit_timeout_is_disabled() -> None:
    manager = BrowserManager(idle_timeout_seconds=0)

    class _Context:
        closed = False

        async def close(self) -> None:
            self.closed = True

    session = BrowserSession(
        session_id="ephemeral",
        context=_Context(),
        page=SimpleNamespace(url=""),
        lifecycle="ephemeral",
        last_used_at=datetime.now(UTC) - timedelta(seconds=61),
    )
    manager._sessions[session.session_id] = session  # noqa: SLF001
    await manager._cleanup_idle_sessions()  # noqa: SLF001
    assert session.context.closed is True


@pytest.mark.asyncio
async def test_idle_cleanup_does_not_close_concurrent_same_id_replacement() -> None:
    manager = BrowserManager(idle_timeout_seconds=1)
    stale_seen = asyncio.Event()

    class _Context:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    stale = BrowserSession(
        session_id="replaceable",
        context=_Context(),
        page=SimpleNamespace(url=""),
        last_used_at=datetime.now(UTC) - timedelta(seconds=2),
    )
    replacement = BrowserSession(
        session_id="replaceable",
        context=_Context(),
        page=SimpleNamespace(url=""),
    )
    manager._sessions[stale.session_id] = stale  # noqa: SLF001
    original_is_idle = manager._session_is_idle  # noqa: SLF001

    def _is_idle(session: BrowserSession) -> bool:
        result = original_is_idle(session)
        if session is stale:
            stale_seen.set()
        return result

    manager._session_is_idle = _is_idle  # type: ignore[method-assign]  # noqa: SLF001

    async def _replace() -> None:
        await stale_seen.wait()
        async with manager._lock:  # noqa: SLF001
            manager._sessions[replacement.session_id] = replacement  # noqa: SLF001

    cleanup_task = asyncio.create_task(manager._cleanup_idle_sessions())  # noqa: SLF001
    replace_task = asyncio.create_task(_replace())
    await asyncio.gather(cleanup_task, replace_task)

    assert manager._sessions[replacement.session_id] is replacement  # noqa: SLF001
    assert replacement.context.closed is False


@pytest.mark.asyncio
async def test_persistent_profile_is_hidden_and_rejected_for_another_user() -> None:
    with TemporaryDirectory() as tmpdir:
        manager = BrowserManager(profile_base_dir=tmpdir)
        owner = _browser_owner("owner", user_email="owner@example.com")
        other = _browser_owner("other", user_email="other@example.com")
        manager._ensure_profile_owner("account", owner)  # noqa: SLF001

        assert [item["profile_id"] for item in await manager.list_profiles(owner=owner)] == [
            "account"
        ]
        assert await manager.list_profiles(owner=other) == []
        with pytest.raises(BrowserLifecycleError) as exc_info:
            manager._ensure_profile_owner("account", other)  # noqa: SLF001
        assert exc_info.value.code == "browser_unauthorized"


@pytest.mark.asyncio
async def test_legacy_profile_requires_explicit_claim_and_preserves_user_isolation(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "www-cocky-kontaktni-cz"
    profile_dir.mkdir()
    (profile_dir / "Default").mkdir()
    (profile_dir / "Default" / "Cookies").write_text("legacy")
    manager = BrowserManager(profile_base_dir=str(tmp_path))
    owner = _browser_owner("owner", user_email="owner@example.com")
    other = _browser_owner("other", user_email="other@example.com")

    assert await manager.list_profiles(owner=owner) == []
    unclaimed = await manager.list_profiles(
        owner=owner,
        include_unclaimed=True,
        executor_owner_email=owner.user_email,
    )
    assert unclaimed == [
        {
            "profile_id": "www-cocky-kontaktni-cz",
            "currently_in_use": False,
            "last_used_at": unclaimed[0]["last_used_at"],
            "ownership_status": "legacy_unclaimed",
            "claimable": True,
        }
    ]

    claimed = await manager.claim_legacy_profile(
        "www-cocky-kontaktni-cz",
        owner=owner,
        confirm_profile_id="www-cocky-kontaktni-cz",
        executor_owner_email=owner.user_email,
    )

    assert claimed["claimed"] is True
    assert [item["profile_id"] for item in await manager.list_profiles(owner=owner)] == [
        "www-cocky-kontaktni-cz"
    ]
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.list_profiles(
            owner=other,
            include_unclaimed=True,
            executor_owner_email=owner.user_email,
        )
    assert exc_info.value.code == "browser_unauthorized"
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.claim_legacy_profile(
            "www-cocky-kontaktni-cz",
            owner=other,
            confirm_profile_id="www-cocky-kontaktni-cz",
            executor_owner_email=owner.user_email,
        )
    assert exc_info.value.code == "browser_unauthorized"


@pytest.mark.asyncio
async def test_legacy_profile_claim_rejects_live_lock_and_confirmation_mismatch(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "account"
    profile_dir.mkdir()
    (profile_dir / "Default").mkdir()
    (profile_dir / "SingletonLock").symlink_to("host-12345")
    manager = BrowserManager(profile_base_dir=str(tmp_path))
    owner = _browser_owner("owner", user_email="owner@example.com")

    with pytest.raises(ValueError, match="exactly match"):
        await manager.claim_legacy_profile(
            "account",
            owner=owner,
            confirm_profile_id="different",
            executor_owner_email=owner.user_email,
        )
    with pytest.raises(BrowserLifecycleError) as exc_info:
        await manager.claim_legacy_profile(
            "account",
            owner=owner,
            confirm_profile_id="account",
            executor_owner_email=owner.user_email,
        )
    assert exc_info.value.code == "browser_profile_locked"
    assert not (profile_dir / ".cognis-owner.json").exists()


def test_profile_lock_parser_uses_trailing_chromium_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as tmpdir:
        manager = BrowserManager(profile_base_dir=tmpdir)
        lock_path = Path(tmpdir) / "SingletonLock"
        lock_path.symlink_to("runner99-12345")
        seen: list[int] = []
        monkeypatch.setattr(
            manager,
            "_pid_is_alive",
            lambda pid: seen.append(pid) is None or True,
        )
        assert manager._profile_lock_looks_orphaned(lock_path) is False  # noqa: SLF001
        assert seen == [12345]


@pytest.mark.parametrize("symlink", [False, True])
def test_orphan_lock_reaper_preserves_replacement(
    monkeypatch: pytest.MonkeyPatch,
    symlink: bool,
) -> None:
    with TemporaryDirectory() as tmpdir:
        manager = BrowserManager(profile_base_dir=tmpdir)
        lock_path = Path(tmpdir) / "SingletonLock"
        if symlink:
            lock_path.symlink_to("stale-99999")
        else:
            lock_path.write_text("stale 99999")
            stale_time = time.time() - 25 * 60 * 60
            os.utime(lock_path, (stale_time, stale_time))
        monkeypatch.setattr(manager, "_pid_is_alive", lambda _pid: False)
        original_rename = Path.rename
        replaced = False

        def _replace_before_claim(path: Path, target: Path):
            nonlocal replaced
            if path == lock_path and not replaced:
                replaced = True
                path.unlink()
                if symlink:
                    path.symlink_to("replacement-12345")
                else:
                    path.write_text("replacement 12345")
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", _replace_before_claim)

        manager._reap_orphan_profile_locks()  # noqa: SLF001

        if symlink:
            assert lock_path.is_symlink()
            assert os.readlink(lock_path) == "replacement-12345"
        else:
            assert lock_path.read_text() == "replacement 12345"


def test_profile_lock_restore_never_overwrites_newer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as tmpdir:
        manager = BrowserManager(profile_base_dir=tmpdir)
        lock_path = Path(tmpdir) / "SingletonLock"
        lock_path.write_text("stale 99999")
        expected = manager._profile_lock_identity(lock_path)  # noqa: SLF001
        assert expected is not None
        lock_path.unlink()
        lock_path.write_text("replacement 12345")
        original_link = os.link

        def _create_newer_before_restore(source, destination, *, follow_symlinks=True):
            Path(destination).write_text("newer 67890")
            return original_link(
                source,
                destination,
                follow_symlinks=follow_symlinks,
            )

        monkeypatch.setattr(os, "link", _create_newer_before_restore)

        assert manager._remove_profile_lock_if_same(lock_path, expected) is False  # noqa: SLF001
        assert lock_path.read_text() == "newer 67890"
        quarantined = list(Path(tmpdir).glob(".SingletonLock.stale-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text() == "replacement 12345"


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
async def test_browser_manager_list_sessions_reaps_idle_sessions() -> None:
    manager = BrowserManager(idle_timeout_seconds=60)
    owner = BrowserSessionOwner(execution_scope_id="scope-1", user_email="user@example.com")

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
        owner=owner,
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
        owner=owner,
    )
    manager._sessions = {"sess-old": stale_session, "sess-new": fresh_session}  # noqa: SLF001

    sessions = await manager.list_sessions(owner=owner)

    assert [session["session_id"] for session in sessions] == ["sess-new"]
    assert sessions[0]["profile_id"] == "www-reddit-com"
    assert stale_session.context.closed is True
    assert "sess-old" not in manager._sessions  # noqa: SLF001


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
        owner = BrowserSessionOwner(execution_scope_id="scope-1", user_email="user@example.com")
        manager._ensure_profile_owner("www-reddit-com", owner)  # noqa: SLF001
        manager._ensure_profile_owner("github-com", owner)  # noqa: SLF001
        manager._sessions = {  # noqa: SLF001
            "sess-1": SimpleNamespace(
                profile_id="www-reddit-com",
                last_used_at=datetime.now(UTC),
                owner=owner,
            ),
            "sess-2": SimpleNamespace(
                profile_id="github-com",
                last_used_at=datetime.now(UTC) - timedelta(days=1),
                owner=owner,
            ),
        }

        profiles = await manager.list_profiles(owner=owner)

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
