"""Stage 36: non-primary active executor reminder builder."""

from __future__ import annotations

from types import SimpleNamespace

from cognis.core.agent_loop import AgentLoop
from cognis.core.executor_pool import (
    ExecutorAvailability,
    ExecutorPool,
    ResolvedExecutorTarget,
)


def _target(
    executor_id: str,
    *,
    is_primary: bool,
    state: ExecutorAvailability = ExecutorAvailability.USABLE,
) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="websocket",
        is_primary=is_primary,
        selection_source="explicit",
        description=None,
        state=state,
    )


def _make_loop() -> AgentLoop:
    return AgentLoop.__new__(AgentLoop)


def _ctx(
    pool: ExecutorPool | None,
    *,
    active_executor_id: str | None,
) -> object:
    conv = SimpleNamespace(active_executor_id=active_executor_id, conversation_id="conv-1")
    return SimpleNamespace(
        executor_pool=pool,
        active_executor_id=active_executor_id,
        conversation=conv,
    )


def test_no_pool_no_reminder() -> None:
    loop = _make_loop()
    ctx = _ctx(None, active_executor_id="exec-1")
    assert loop._build_non_primary_active_reminder(ctx) is None


def test_no_active_no_reminder() -> None:
    loop = _make_loop()
    pool = ExecutorPool(primary=[_target("exec-1", is_primary=True)])
    ctx = _ctx(pool, active_executor_id=None)
    assert loop._build_non_primary_active_reminder(ctx) is None


def test_primary_active_no_reminder() -> None:
    loop = _make_loop()
    pool = ExecutorPool(primary=[_target("exec-1", is_primary=True)])
    ctx = _ctx(pool, active_executor_id="exec-1")
    assert loop._build_non_primary_active_reminder(ctx) is None


def test_unassigned_active_no_reminder() -> None:
    """Unassigned active is a config-drift case; tool errors handle it, not the reminder."""

    loop = _make_loop()
    pool = ExecutorPool(primary=[_target("exec-1", is_primary=True)])
    ctx = _ctx(pool, active_executor_id="exec-ghost")
    assert loop._build_non_primary_active_reminder(ctx) is None


def test_additional_active_emits_reminder() -> None:
    loop = _make_loop()
    pool = ExecutorPool(
        primary=[_target("exec-primary", is_primary=True)],
        additional=[_target("exec-add", is_primary=False)],
    )
    ctx = _ctx(pool, active_executor_id="exec-add")
    msg = loop._build_non_primary_active_reminder(ctx)
    assert msg is not None
    assert msg["role"] == "system"
    assert msg["_executor_reminder"] is True
    assert "exec-add" in msg["content"]
    assert "non-primary" in msg["content"].lower()
    # Mentions the primary as a hint to switch back
    assert "exec-primary" in msg["content"]
