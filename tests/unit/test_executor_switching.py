"""Unit tests for the shared switch-executor helper (Stage 36)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest

from cognis.core.executor_pool import (
    ExecutorAvailability,
    ExecutorPool,
    ResolvedExecutorTarget,
)
from cognis.core.executor_switching import perform_executor_switch


@dataclass
class FakeConversation:
    conversation_id: str = "conv-1"
    active_executor_id: str | None = None
    active_executor_assigned_at: datetime | None = None
    active_executor_expires_at: datetime | None = None
    active_executor_source: str | None = None
    updated_at: Any = None


class _FakeSession:
    def __init__(self, conv: FakeConversation) -> None:
        self.conv = conv
        self.committed = False
        self.rolled_back = False
        self._flushed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def flush(self) -> None:
        self._flushed = True


class _FakeSessionFactory:
    def __init__(self, conv: FakeConversation) -> None:
        self.conv = conv
        self.session: _FakeSession | None = None

    def __call__(self) -> _FakeSession:
        self.session = _FakeSession(self.conv)
        return self.session


@pytest.fixture
def factory_and_conv() -> tuple[_FakeSessionFactory, FakeConversation]:
    conv = FakeConversation()
    return _FakeSessionFactory(conv), conv


def _target(
    executor_id: str,
    *,
    is_primary: bool = True,
    state: ExecutorAvailability = ExecutorAvailability.USABLE,
) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="websocket",
        is_primary=is_primary,
        selection_source="explicit",
        description=None,
        state=state,
        observed_tools=[{"name": "bash"}, {"name": "read"}],
    )


@pytest.fixture(autouse=True)
def _patch_set_active_executor(monkeypatch, factory_and_conv):
    """Patch the queries module's set_conversation_active_executor."""

    factory, conv = factory_and_conv

    async def _set(
        _session,
        conversation_id,
        active_executor_id,
        *,
        assigned_at=None,
        expires_at=None,
        source=None,
    ):
        if conversation_id != conv.conversation_id:
            return False
        conv.active_executor_id = active_executor_id
        conv.active_executor_assigned_at = assigned_at
        conv.active_executor_expires_at = expires_at
        conv.active_executor_source = source
        return True

    import cognis.store.queries

    monkeypatch.setattr(cognis.store.queries, "set_conversation_active_executor", _set)


@pytest.mark.asyncio
async def test_switch_to_assigned_primary_succeeds(factory_and_conv) -> None:
    factory, conv = factory_and_conv
    pool = ExecutorPool(primary=[_target("exec-1"), _target("exec-2")])
    outcome = await perform_executor_switch(
        conversation_id=conv.conversation_id,
        pool=pool,
        executor_id="exec-2",
        actor="agent",
        session_factory=factory,
    )
    assert outcome.status == "ok"
    assert outcome.is_primary is True
    assert outcome.target.executor_id == "exec-2"
    assert conv.active_executor_id == "exec-2"
    assert conv.active_executor_expires_at is None
    assert conv.active_executor_source == "agent_switch"
    payload = outcome.to_tool_result()
    assert payload["status"] == "ok"
    assert payload["is_primary"] is True


@pytest.mark.asyncio
async def test_switch_to_assigned_additional_succeeds(factory_and_conv) -> None:
    factory, conv = factory_and_conv
    pool = ExecutorPool(
        primary=[_target("exec-1")],
        additional=[_target("exec-add", is_primary=False)],
    )
    outcome = await perform_executor_switch(
        conversation_id=conv.conversation_id,
        pool=pool,
        executor_id="exec-add",
        actor="user",
        session_factory=factory,
    )
    assert outcome.status == "ok"
    assert outcome.is_primary is False
    assert conv.active_executor_id == "exec-add"
    assert conv.active_executor_expires_at is not None
    assert conv.active_executor_source == "user_switch"
    msg = outcome.to_user_message()
    assert "non-primary" in msg.lower()
    assert "not fallback capacity" in msg
    assert "before unrelated generic work" in msg


@pytest.mark.asyncio
async def test_switch_to_unassigned_fails(factory_and_conv) -> None:
    factory, conv = factory_and_conv
    pool = ExecutorPool(primary=[_target("exec-1")])
    outcome = await perform_executor_switch(
        conversation_id=conv.conversation_id,
        pool=pool,
        executor_id="exec-ghost",
        actor="agent",
        session_factory=factory,
    )
    assert outcome.status == "error"
    assert outcome.error_reason == "not_assigned"
    assert conv.active_executor_id is None  # unchanged
    payload = outcome.to_tool_result()
    assert payload["status"] == "error"
    assert payload["reason"] == "not_assigned"


@pytest.mark.asyncio
async def test_switch_with_task_id_updates_task_pin(monkeypatch, factory_and_conv) -> None:
    """Stage 36: passing task_id updates the task pin alongside the conversation pin."""

    factory, conv = factory_and_conv
    pool = ExecutorPool(primary=[_target("exec-1"), _target("exec-2")])

    task_calls: list[tuple[str, str, str | None]] = []

    async def _set_task(_session, task_id, executor_id, **metadata):
        task_calls.append((task_id, executor_id, metadata.get("source")))
        return True

    import cognis.store.queries as store_queries

    monkeypatch.setattr(store_queries, "set_task_active_executor", _set_task)

    outcome = await perform_executor_switch(
        conversation_id=conv.conversation_id,
        pool=pool,
        executor_id="exec-2",
        actor="agent",
        session_factory=factory,
        task_id="task-99",
    )
    assert outcome.status == "ok"
    assert conv.active_executor_id == "exec-2"
    assert task_calls == [("task-99", "exec-2", "agent_switch")]


@pytest.mark.asyncio
async def test_switch_with_task_id_swallows_task_pin_failure(monkeypatch, factory_and_conv) -> None:
    """Task pin failure must NOT undo a successful conversation switch."""

    factory, conv = factory_and_conv
    pool = ExecutorPool(primary=[_target("exec-1"), _target("exec-2")])

    async def _set_task_boom(_session, _task_id, _executor_id, **_metadata):
        raise RuntimeError("simulated DB failure")

    import cognis.store.queries as store_queries

    monkeypatch.setattr(store_queries, "set_task_active_executor", _set_task_boom)

    outcome = await perform_executor_switch(
        conversation_id=conv.conversation_id,
        pool=pool,
        executor_id="exec-2",
        actor="agent",
        session_factory=factory,
        task_id="task-99",
    )
    # Conversation switch still wins
    assert outcome.status == "ok"
    assert conv.active_executor_id == "exec-2"


@pytest.mark.asyncio
async def test_switch_to_unusable_fails(factory_and_conv) -> None:
    factory, conv = factory_and_conv
    pool = ExecutorPool(
        primary=[
            _target("exec-1"),
            _target("exec-down", state=ExecutorAvailability.OFFLINE),
        ]
    )
    outcome = await perform_executor_switch(
        conversation_id=conv.conversation_id,
        pool=pool,
        executor_id="exec-down",
        actor="agent",
        session_factory=factory,
    )
    assert outcome.status == "error"
    assert outcome.error_reason == "unavailable"
    assert "offline" in outcome.error_detail.lower()
    assert conv.active_executor_id is None
