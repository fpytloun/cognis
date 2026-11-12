"""Deliver durable task cancellation to previously accepted executor calls."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.models import StepRun, Task

type _CancelledDispatchSnapshot = tuple[str, int, dict[str, Any], set[tuple[str, str]]]


async def _cancelled_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str | None,
) -> AsyncIterator[_CancelledDispatchSnapshot]:
    # Generic PostgreSQL plans can evaluate JSON predicates before status.
    # Fence extraction so unrelated terminal history is never parsed by this poll.
    recovery = case(
        (Task.status == "cancelled", Task.result_data["execution_recovery"].as_string()),
        else_=None,
    )
    after: str | None = None
    while True:
        async with session_factory() as db:
            query = select(Task).where(
                Task.status == "cancelled",
                recovery.is_not(None),
                recovery != "{}",
            )
            if task_id is not None:
                query = query.where(Task.task_id == task_id)
            if after is not None:
                query = query.where(Task.task_id > after)
            rows = list((await db.scalars(query.order_by(Task.task_id).limit(50))).all())
            if not rows:
                return
            after = rows[-1].task_id
            snapshots: list[_CancelledDispatchSnapshot] = []
            for row in rows:
                steps = list(
                    (
                        await db.scalars(
                            select(StepRun).where(
                                StepRun.task_id == row.task_id,
                                StepRun.attempt_number == row.attempt_number,
                            )
                        )
                    ).all()
                )
                scopes = {
                    (sid, turn_id)
                    for step in steps
                    for sid in (
                        step.session_id,
                        step.intaris_session_id,
                        (step.runtime_info or {}).get("session_id"),
                        (step.runtime_info or {}).get("intaris_session_id"),
                    )
                    for turn_id in (
                        step.step_run_id,
                        (step.runtime_info or {}).get("call_identity"),
                    )
                    if sid and turn_id
                }
                snapshots.append(
                    (
                        row.task_id,
                        row.attempt_number,
                        dict((row.result_data or {}).get("execution_recovery") or {}),
                        scopes,
                    )
                )
        for snapshot in snapshots:
            yield snapshot
        if task_id is not None:
            return


async def reconcile_cancelled_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
    websocket_provider: Any,
    *,
    task_id: str | None = None,
) -> int:
    """Retry idempotent cancellation without an execution slot or open DB I/O.

    The cancelled task/attempt and its StepRun scope authorize each cancellation.
    Lost replies and offline executors leave the dispatch metadata for retry.
    """
    delivered = 0
    async for identifier, attempt, batches, scopes in _cancelled_dispatches(
        session_factory, task_id
    ):
        completed: dict[str, set[str]] = {}
        for key, batch in batches.items():
            session_id, turn_id = batch.get("session_id"), batch.get("turn_id")
            if (session_id, turn_id) not in scopes or batch.get("attempt_number") != attempt:
                continue
            scope = {"session_id": session_id, "turn_id": turn_id}
            for descriptor in batch.get("tool_calls", []):
                executor_id = descriptor.get("executor_id")
                call_id = descriptor.get("call_id")
                accepted_instance = descriptor.get("executor_instance_id")
                if not executor_id or not call_id:
                    continue
                if descriptor.get("dispatch_state") == "not_sent":
                    completed.setdefault(key, set()).add(call_id)
                    continue
                if websocket_provider is None or not accepted_instance:
                    continue
                try:
                    connection = websocket_provider.get_ready_connection(executor_id)
                    if connection is None:
                        continue
                    durable = (
                        getattr(
                            getattr(connection, "capabilities", None),
                            "durable_tool_receipts",
                            False,
                        )
                        is True
                    )
                    if not durable and connection.executor_instance_id != accepted_instance:
                        continue
                    report = await connection.fetch_tool_result(
                        call_id, timeout=5.0, **({"scope": scope} if durable else {})
                    )
                    origin = (
                        report.get("accepted_executor_instance_id")
                        if durable
                        else report.get("executor_instance_id")
                    )
                    if origin != accepted_instance:
                        continue
                    state = report.get("state")
                    done = state in {"terminal", "terminal_result_unavailable"}
                    if state == "active":
                        await connection.rpc_call(
                            "tool.cancel", {"call_id": call_id, "scope": scope}, timeout=5.0
                        )
                    if done:
                        if durable:
                            await connection.rpc_call(
                                "tool.result_ack", {"call_id": call_id, "scope": scope}, timeout=5.0
                            )
                        completed.setdefault(key, set()).add(call_id)
                except Exception:
                    # Cancellation is idempotent; retain its durable request
                    # through disconnect, HA handoff, or uncertain delivery.
                    continue
        if not completed:
            continue
        async with session_factory() as db:
            current_task = await db.get(Task, identifier, with_for_update=True)
            if (
                current_task is None
                or current_task.status != "cancelled"
                or current_task.attempt_number != attempt
            ):
                continue
            data = dict(current_task.result_data or {})
            current = dict(data.get("execution_recovery") or {})
            for key, call_ids in completed.items():
                batch = dict(current.get(key) or {})
                remaining = [
                    entry
                    for entry in batch.get("tool_calls", [])
                    if entry["call_id"] not in call_ids
                ]
                if remaining:
                    current[key] = {**batch, "tool_calls": remaining}
                else:
                    current.pop(key, None)
                delivered += len(call_ids)
            current_task.result_data = {**data, "execution_recovery": current}
            await db.commit()
    return delivered
