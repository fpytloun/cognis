from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cognis.api.runtime_support import (
    _browser_cleanup_executor_ids,
    _terminal_browser_cleanup,
)
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.websocket import WebSocketExecutorProvider
from cognis.runtime_context import RuntimeAccessContext


async def test_terminal_browser_cleanup_notifies_executor_once() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    after_calls: list[bool] = []

    class _Connection:
        async def rpc_call(self, method: str, params: dict[str, object]) -> dict[str, int]:
            calls.append((method, params))
            return {"closed": 1}

    async def _after() -> None:
        after_calls.append(True)

    cleanup = _terminal_browser_cleanup(
        _Connection(),
        RuntimeAccessContext(
            user_email="user@example.com",
            agent_id="agent-1",
            session_id="child-session",
            conversation_id="conversation-1",
            parent_session_id="parent-session",
            delegation_mode="delegate",
        ),
        after=_after,
    )

    await cleanup()

    assert calls == [
        (
            "browser.session_terminal",
            {
                "owner": {
                    "execution_scope_id": "child-session",
                    "session_id": "child-session",
                    "conversation_id": "conversation-1",
                    "user_email": "user@example.com",
                    "agent_id": "agent-1",
                    "parent_session_id": "parent-session",
                    "delegation_mode": "delegate",
                }
            },
        )
    ]
    assert after_calls == [True]


async def test_terminal_browser_cleanup_skips_active_root_session() -> None:
    called = False

    class _Connection:
        async def rpc_call(self, method: str, params: dict[str, object]) -> dict[str, int]:
            del method, params
            nonlocal called
            called = True
            return {"closed": 0}

    cleanup = _terminal_browser_cleanup(
        _Connection(),
        RuntimeAccessContext(
            user_email="user@example.com",
            session_id="root-session",
        ),
    )

    await cleanup()

    assert called is False


async def test_terminal_browser_cleanup_resolves_reconnected_executor_at_cleanup() -> None:
    calls: list[str] = []

    class _Connection:
        def __init__(self, name: str) -> None:
            self.name = name

        async def rpc_call(self, method: str, params: dict[str, object]) -> dict[str, int]:
            del method, params
            calls.append(self.name)
            return {"closed": 0}

    current = [_Connection("old")]
    cleanup = _terminal_browser_cleanup(
        lambda: list(current),
        RuntimeAccessContext(
            user_email="user@example.com",
            session_id="child-session",
            parent_session_id="parent-session",
        ),
    )
    current[:] = [_Connection("new")]

    await cleanup()

    assert calls == ["new"]


async def test_terminal_runtime_finalization_notifies_parallel_executor_pool() -> None:
    provider = WebSocketExecutorProvider()
    executor_pool = SimpleNamespace(
        all=[
            SimpleNamespace(executor_id="executor-1"),
            SimpleNamespace(executor_id="executor-2"),
            SimpleNamespace(executor_id="executor-3"),
        ]
    )
    executor_ids = _browser_cleanup_executor_ids("executor-1", executor_pool)
    calls: list[str] = []

    class _Connection:
        connected = True

        def __init__(self, executor_id: str) -> None:
            self.executor_id = executor_id
            self.capabilities = ExecutorCapabilities()

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            del method, params, timeout
            calls.append(self.executor_id)
            return {"closed": 0, "complete": True}

    for executor_id in executor_ids:
        provider._connections[executor_id] = _Connection(executor_id)  # type: ignore[assignment]  # noqa: SLF001

    cleanup = _terminal_browser_cleanup(
        [],
        RuntimeAccessContext(
            user_email="user@example.com",
            session_id="child-session",
            parent_session_id="parent-session",
        ),
        notifier=lambda owner: provider.notify_browser_session_terminal(
            executor_ids,
            owner,
        ),
    )

    await cleanup()
    await asyncio.gather(*provider._browser_terminal_flush_tasks.values())  # noqa: SLF001

    assert sorted(calls) == executor_ids
    assert provider._pending_browser_terminal == {}  # noqa: SLF001
    await provider.cleanup()
