"""Reconcile interrupted task tools through the normal executor result pipeline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cognis.core.harness_guards import tool_call_argument_fingerprint
from cognis.core.runtime import TransientExecutorUnavailable
from cognis.core.task_execution import delegation_arguments_binding
from cognis.core.task_recovery_errors import TaskRecoveryBlocked
from cognis.core.tool_result_settlement import (
    append_tool_result_once,
    read_canonical_tool_pairs,
    uncertain_executor_tool_result,
)
from cognis.models.session import SessionEvent, SessionModel
from cognis.models.tool import ToolCall, ToolResult
from cognis.providers.guardrails.errors import IntarisDependencyError

if TYPE_CHECKING:
    from cognis.core.agent_loop import AgentLoop, StepContext


@dataclass(frozen=True)
class RecoveredTaskTools:
    """Real results and canonical settlement evidence, never tool replay."""

    results: dict[str, ToolResult | None]
    canonical_calls: set[str]
    requires_delegation_continuation: bool = False


async def acknowledge_task_receipts(loop: AgentLoop, descriptors: list[dict[str, Any]]) -> None:
    """Best-effort idempotent settlement hints; they never reclaim receipts."""
    for descriptor in descriptors:
        executor_id = descriptor.get("executor_id")
        if not executor_id:
            continue
        connection = loop._resolve_target_connection(
            target_executor_id=executor_id, target_executor_type="websocket"
        )
        if (
            connection is None
            or getattr(getattr(connection, "capabilities", None), "durable_tool_receipts", False)
            is not True
        ):
            continue
        try:
            await connection.rpc_call(
                "tool.result_ack",
                {
                    "call_id": descriptor["call_id"],
                    "scope": {
                        "session_id": descriptor.get("session_id"),
                        "turn_id": descriptor.get("turn_id"),
                    },
                },
                timeout=5.0,
            )
        except Exception:
            # Lost hints never authorize receipt deletion or replay.
            continue


async def _recover_reserved_child(
    loop: AgentLoop,
    ctx: StepContext,
    descriptor: dict[str, Any],
    *,
    session_id: str,
    turn_id: str,
    settlement_only: bool,
) -> tuple[SessionModel, bool]:
    from cognis.core.session import _to_session_model
    from cognis.store.models import Session

    fence = ctx.execution_fence
    assert fence is not None
    async with loop._session_factory() as db:
        await fence.assert_current(db)
        row = await db.get(Session, descriptor["child_session_id"])
        if row is None:
            raise TaskRecoveryBlocked(
                "The reserved delegated session is missing; cancel this task "
                "after reconciling existing effects."
            )
        child = _to_session_model(row)
    child = await loop.session_manager.resolve_reserved_child_continuation(child)
    child_workspace_root = ctx.workspace_root if ctx.workspace_root_explicit else None
    child_working_directory = (
        ctx.working_directory if ctx.working_directory_explicit else child_workspace_root
    )
    recovery = await loop._run_child_session(
        child_session=child,
        child_turn_id=descriptor["child_turn_id"],
        conversation=ctx.conversation,
        agent=ctx.executor_agent or ctx.agent,
        task_description="",
        parent_intaris_session_id=session_id,
        parent_session=ctx.session,
        parent_tool_call_id=descriptor["call_id"],
        parent_turn_id=turn_id,
        workspace_root=child_workspace_root,
        working_directory=child_working_directory,
        workspace_root_explicit=ctx.workspace_root_explicit,
        working_directory_explicit=ctx.working_directory_explicit,
        deliverable_step_run_id=ctx.deliverable_step_run_id or ctx.step_run_id,
        allow_questions=(
            ctx.interaction_mode == "step_requests" and ctx.step_definition.allow_questions
        ),
        controller_tool_surface=ctx.controller_tool_surface,
        cancel_event=ctx.cancel_event,
        execution_fence=fence,
        settlement_only=settlement_only,
        recovery_only=True,
    )
    assert recovery is None or isinstance(recovery, RecoveredTaskTools)
    return child, bool(recovery and recovery.requires_delegation_continuation)


async def recover_task_tools(
    loop: AgentLoop,
    ctx: StepContext,
    batches: dict[str, Any],
    *,
    collect_results: bool = False,
    complete_descriptors: bool = True,
    record_results: bool = True,
    settlement_only: bool = False,
) -> RecoveredTaskTools:
    """Settle this context's interrupted calls before it may ask the model.

    Descriptors contain identities only. Arguments and controller audit evidence
    are read from Intaris; untrusted executor output always goes through the
    same finalizer used by live reconnect. Delegation rejoins a reserved child
    through its normal controller admission; executor mutations are never replayed.
    """
    fence = ctx.execution_fence
    if fence is None:
        raise RuntimeError("Task tool recovery requires current execution ownership")
    from sqlalchemy import or_, select

    from cognis.core.task_execution import TaskExecutionFence
    from cognis.core.tool_result_settlement import (
        CanonicalToolHistoryError,
        read_canonical_tool_pair,
    )
    from cognis.store.models import Session

    if isinstance(fence, TaskExecutionFence):
        for batch in tuple(batches.values()):
            child_id = batch.get("session_id")
            if not child_id or child_id in {ctx.session.session_id, ctx.session.intaris_session_id}:
                continue
            async with loop._session_factory() as db:
                await fence.assert_current(db)
                child = (
                    await db.execute(
                        select(Session).where(
                            or_(
                                Session.session_id == child_id,
                                Session.intaris_session_id == child_id,
                            )
                        )
                    )
                ).scalar_one_or_none()
            if child is None or child.parent_session_id != ctx.session.session_id:
                continue
            proof = (child.delegation_metadata or {}).get("task_call") or {}
            parent_batch = batches.get(f"{proof.get('session_id')}:{proof.get('turn_id')}") or {}
            if any(
                d.get("child_session_id")
                and d["child_session_id"] in {child.session_id, proof.get("child_session_id")}
                for d in parent_batch.get("tool_calls", [])
            ):
                continue
            if not all(
                proof.get(k)
                for k in (
                    "task_id",
                    "attempt_number",
                    "step_run_id",
                    "session_id",
                    "turn_id",
                    "call_id",
                    "binding",
                    "child_session_id",
                    "child_turn_id",
                )
            ):
                raise TaskRecoveryBlocked(
                    f"Delegated child {child.session_id} lacks task/attempt/StepRun provenance "
                    "for its original canonical call. Legacy cross-turn attachment cannot be "
                    "proven. Cancel this task and start a fresh run after reconciling existing effects."
                )
            try:
                parent_call, _ = await read_canonical_tool_pair(
                    loop.providers.guardrails,
                    session_id=proof["session_id"],
                    turn_id=proof["turn_id"],
                    call_id=proof["call_id"],
                    tool_name="delegate",
                )
            except CanonicalToolHistoryError as exc:
                raise TaskRecoveryBlocked(
                    f"Delegated child {child.session_id} has no canonical parent call under "
                    f"reserved turn {proof['turn_id']}. Cross-turn reconstruction is unsupported; "
                    "cancel this task and start a fresh run after reconciling existing effects."
                ) from exc
            await fence.restore_child_reservation(
                child_session_id=proof["child_session_id"],
                parent_session_id=ctx.session.session_id,
                call=parent_call["data"],
            )
        batches = await fence.recovery_batches()
    from cognis.core.agent_loop import _record_tool_call_ledger_events

    recovered_results: dict[str, ToolResult | None] = {}
    canonical_calls: set[str] = set()
    requires_delegation_continuation = False
    for batch in batches.values():
        session_id = batch["session_id"]
        turn_id = batch["turn_id"]
        descriptors = batch["tool_calls"]
        if not descriptors:
            continue
        if session_id != (ctx.session.intaris_session_id or ctx.session.session_id):
            # Descendant calls are recovered only inside their own runtime.
            continue
        if turn_id != ctx.turn_id:
            raise TaskRecoveryBlocked("Interrupted task tools require their original turn.")
        try:
            pairs = await read_canonical_tool_pairs(
                loop.providers.guardrails,
                session_id=session_id,
                turn_id=turn_id,
                tool_names={d["call_id"]: d["tool_name"] for d in descriptors},
            )
        except IntarisDependencyError:
            raise
        except CanonicalToolHistoryError as exc:
            raise TaskRecoveryBlocked(
                f"Task canonical call proof for session {session_id}, reserved turn {turn_id} "
                "is missing or conflicting. Cross-turn reconstruction is unsupported; cancel "
                "this task and start a fresh run after reconciling existing effects."
            ) from exc
        except Exception as exc:
            raise TransientExecutorUnavailable(
                "Task is waiting for authoritative tool history.",
                retry_after_seconds=30,
            ) from exc
        settled: list[str] = []
        for descriptor in descriptors:
            call_id = descriptor["call_id"]
            call, result = pairs[call_id]
            if result is not None:
                if descriptor.get("child_session_id") and any(
                    pending["session_id"] != session_id and pending.get("tool_calls")
                    for pending in (await fence.recovery_batches()).values()
                ):
                    _, child_requires_continuation = await _recover_reserved_child(
                        loop,
                        ctx,
                        descriptor,
                        session_id=session_id,
                        turn_id=turn_id,
                        settlement_only=settlement_only,
                    )
                    requires_delegation_continuation |= child_requires_continuation
                canonical_calls.add(call_id)
            if result is not None and not collect_results:
                assert call is not None
                _record_tool_call_ledger_events(
                    ctx,
                    [SessionEvent.model_validate(call), SessionEvent.model_validate(result)],
                )
                settled.append(call_id)
                continue
            if result is not None:
                recovered_results[call_id] = ToolResult.model_validate(
                    {k: v for k, v in result["data"].items() if k in ToolResult.model_fields}
                )
                settled.append(call_id)
                continue
            assert call is not None
            data = call["data"]
            registered = (
                ctx.tool_registry.get(descriptor["tool_name"]) if ctx.tool_registry else None
            )
            if registered is None:
                raise RuntimeError(
                    "Interrupted task tool is no longer available; it was not replayed"
                )
            arguments = data.get("recovery_arguments", data.get("arguments", {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "Canonical tool arguments are unavailable; tool was not replayed"
                    ) from exc
            if not isinstance(arguments, dict) or any(
                key in arguments for key in ("_truncated", "_raw_arguments")
            ):
                raise RuntimeError("Canonical tool arguments are incomplete; tool was not replayed")
            if (
                descriptor["tool_name"] == "delegate"
                and "recovery_arguments" not in data
                and "_bounded" in arguments
            ):
                # Pre-fix events added a title and preview marker. Remove only
                # presentation fields, and accept nothing without the original
                # reservation's exact hash. Truncated input cannot be recovered.
                original = {key: value for key, value in arguments.items() if key != "_bounded"}
                candidates = [original, {k: v for k, v in original.items() if k != "title"}]
                arguments = next(
                    (
                        candidate
                        for candidate in candidates
                        if delegation_arguments_binding(candidate)
                        == descriptor.get("delegate_binding")
                    ),
                    None,
                )
                if arguments is None:
                    raise TaskRecoveryBlocked(
                        "Canonical delegation arguments are incomplete or changed; "
                        "the reserved child was not replayed."
                    )
            tc = ToolCall(
                call_id=call_id,
                name=descriptor["tool_name"],
                arguments=arguments,
            )
            executor_id = descriptor.get("executor_id")
            instance_id = descriptor.get("executor_instance_id")
            if tc.name == "delegate":
                if not descriptor.get("child_session_id") or not descriptor.get("child_turn_id"):
                    raise TaskRecoveryBlocked(
                        "Legacy delegation has no unambiguous child reservation. "
                        "Reconcile the existing child before explicitly resuming."
                    )
                await fence.assert_current()
                if settlement_only:
                    child, child_requires_continuation = await _recover_reserved_child(
                        loop,
                        ctx,
                        descriptor,
                        session_id=session_id,
                        turn_id=turn_id,
                        settlement_only=True,
                    )
                    if child_requires_continuation or child.status not in {
                        "completed",
                        "failed",
                        "cancelled",
                        "terminated",
                    }:
                        requires_delegation_continuation = True
                        continue
                recovery_events: list[SessionEvent] = []
                recovered = await loop._handle_orchestration_tool(
                    tc, ctx=ctx, events_to_record=recovery_events
                )
                state, reason = "terminal", "reserved_delegation"
            elif descriptor.get("dispatch_state") == "not_sent":
                # Affirmative transport evidence committed by the prior owner,
                # not an inference from an absent receipt or dispatch identity.
                state, recovered, reason = "not_sent", None, "not_sent"
            elif executor_id and instance_id:
                state, recovered, reason = await loop._reconcile_accepted_tool_call(
                    ctx,
                    tc=tc,
                    registered=registered,
                    executor_id=executor_id,
                    failed_connection=None,
                    original_result=ToolResult(
                        output="",
                        metadata={"transport": {"executor_instance_id": instance_id}},
                    ),
                    deadline=asyncio.get_running_loop().time() + 5,
                    single_attempt=True,
                    receipt_scope={"session_id": session_id, "turn_id": turn_id},
                )
                if state == "unresolved" and reason in {
                    "active",
                    "reconnect_timeout",
                    "fetch_failed",
                    "deadline_exceeded",
                }:
                    raise TransientExecutorUnavailable(
                        "Task is waiting to reconcile an accepted executor call.",
                        executor_id=executor_id,
                        retry_after_seconds=30,
                    )
            else:
                state, recovered, reason = "unresolved", None, "missing_dispatch_identity"
            if state == "not_sent" and collect_results:
                # A deterministic caller may admit the original call only
                # after this affirmative transport proof of non-execution.
                recovered_results[call_id] = None
                continue
            if state != "terminal":
                if state != "not_sent" and loop._tool_safe_for_same_executor_retry(registered):
                    await fence.assert_current()
                    recovered = await loop.execute_controller_tool(ctx, tc)
                elif state == "not_sent":
                    recovered = ToolResult(
                        output=(
                            "The interrupted call has no result. It was not replayed. "
                            "The model may issue a new call after checking current conditions."
                        ),
                        is_error=True,
                        metadata={"recovery": reason, "retryable": True},
                    )
                else:
                    if collect_results:
                        raise TaskRecoveryBlocked(
                            f"Outcome of tool '{tc.name}' is uncertain ({reason}); "
                            "it was not replayed. Reconcile its external effects before retrying."
                        )
                    from cognis.core.agent_loop import (
                        _canonical_registered_tool_name,
                        _canonical_tool_arguments,
                    )

                    canonical_name = _canonical_registered_tool_name(tc.name, ctx.tool_registry)
                    fingerprint = tool_call_argument_fingerprint(
                        canonical_name, _canonical_tool_arguments(tc.arguments)
                    )
                    recovered = uncertain_executor_tool_result(
                        tc,
                        canonical_name=canonical_name,
                        argument_fingerprint=fingerprint,
                        executor_id=executor_id,
                        reason=reason,
                        original_result=ToolResult(
                            output="",
                            is_error=True,
                            metadata={"transport": {"executor_instance_id": instance_id}},
                        ),
                    )
            assert recovered is not None
            recovered_results[call_id] = recovered
            if result is not None or not record_results:
                settled.append(call_id)
                continue
            await fence.assert_current()
            settlement = await append_tool_result_once(
                loop.providers.guardrails,
                session_id=session_id,
                turn_id=turn_id,
                call_id=call_id,
                tool_name=tc.name,
                event=SessionEvent(
                    type="tool_result",
                    data={
                        "call_id": call_id,
                        "name": tc.name,
                        "turn_id": turn_id,
                        "output": recovered.output,
                        "is_error": recovered.is_error,
                        "duration_ms": recovered.duration_ms,
                        "metadata": recovered.metadata,
                        "attachments": recovered.attachments,
                        **(
                            {"uncertain": True, "ambiguity": recovered.metadata["ambiguity"]}
                            if recovered.metadata and recovered.metadata.get("uncertain")
                            else {}
                        ),
                    },
                ),
            )
            await loop.session_cache.append_recorded_events(
                ctx.session,
                [SessionEvent.model_validate(settlement.event)],
                settlement.append_result,
            )
            if not collect_results:
                _record_tool_call_ledger_events(
                    ctx,
                    [
                        SessionEvent.model_validate(call),
                        SessionEvent.model_validate(settlement.event),
                    ],
                )
            settled.append(call_id)
        if settled and complete_descriptors:
            completed = await fence.complete_tool_dispatch(
                session_id=session_id,
                turn_id=turn_id,
                call_ids=settled,
            )
            if isinstance(completed, list):
                await acknowledge_task_receipts(loop, completed)
    if settlement_only and requires_delegation_continuation:
        return RecoveredTaskTools(recovered_results, canonical_calls, True)
    if (
        complete_descriptors
        and getattr(ctx, "task_id", None) is not None
        and await fence.recovery_batches()
    ):
        raise TaskRecoveryBlocked(
            "Interrupted child execution lacks a consistent same-turn task/attempt/StepRun "
            "delegation proof. Cancel this task and start a fresh run after reconciling existing effects."
        )
    return RecoveredTaskTools(recovered_results, canonical_calls)
