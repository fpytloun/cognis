"""Distributed direct-turn claiming, fencing, renewal, and recovery."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core import turn_latency
from cognis.store.coordination import LEASE_RENEWAL_DELAY_SECONDS, DatabaseLeaseStore, Lease
from cognis.store.direct_turns import (
    DirectTurnStatus,
    DirectTurnStore,
    MaterializedDirectTurnPayload,
    PermanentDirectTurnPayloadError,
    ToolDispatchMergeStatus,
    conversation_lease_key,
)
from cognis.store.models import DirectTurnRequestRow

DIRECT_TURN_LEASE_SECONDS = 30.0
DIRECT_TURN_ACTIVE_POLL_SECONDS = 0.5
DIRECT_TURN_CANCELLATION_POLL_SECONDS = 2.0
DIRECT_TURN_RECOVERY_INTERVAL_SECONDS = 30.0
INTARIS_TAKEOVER_QUARANTINE_SECONDS = 35.0
TOOL_RECOVERY_DEADLINE_SECONDS = 60.0
DIRECT_TURN_CONTROLLER_MAX_ATTEMPTS = 3
logger = logging.getLogger(__name__)


class StaleDirectTurnOwner(RuntimeError):
    """Raised when a turn loses its distributed execution fence."""


class LocalDirectTurnBusy(RuntimeError):
    """Local cancellation cleanup still owns the conversation execution slot."""

    def __init__(self, request_id: str, *, reason: str = "active_turn") -> None:
        super().__init__(request_id)
        self.reason = reason


class UnboundToolDispatch(RuntimeError):
    """A tool reached its executor send without a durable dispatch descriptor.

    The fence is still held, so this is not a lost turn. It means restart
    recovery has no record of the call, which is only safe to tolerate after
    the call was already sent.
    """


class PermanentDirectTurnControllerError(PermanentDirectTurnPayloadError):
    """A deterministic controller failure that must not block FIFO recovery."""


class ToolRecoveryPersistenceError(RuntimeError):
    """Recovery cannot safely reach the next canonical model boundary."""

    def __init__(self, message: str, *, ambiguous: bool) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


@dataclass
class DirectTurnExecutionFence:
    store: DirectTurnStore
    request_id: str
    lease: Lease
    user_append_phase: str | None = None
    user_append_session_id: str | None = None
    last_phase: str = "claimed"
    last_metadata: dict[str, Any] | None = None
    interruption_reason: str | None = None
    retry_after_seconds: float | None = None
    stream_checkpoint_pending: bool = False

    def begin_stream_checkpoint(self) -> None:
        """Prevent terminal settlement until exposed output is accounted for."""
        self.stream_checkpoint_pending = True

    def confirm_stream_checkpoint(self) -> None:
        """Allow settlement after durable output or an explicit failure marker."""
        self.stream_checkpoint_pending = False

    def set_user_append_state(
        self,
        phase: str,
        *,
        session_id: str | None,
    ) -> None:
        self.user_append_phase = phase
        self.user_append_session_id = session_id

    async def assert_current(self, session: AsyncSession | None = None) -> None:
        """Check ownership in the mutation's transaction when one is supplied."""
        if not await self.store.has_fence(self.request_id, lease=self.lease, session=session):
            raise StaleDirectTurnOwner(f"Lost direct-turn fence for {self.request_id}")

    async def checkpoint(self, phase: str, **metadata: Any) -> None:
        self.last_phase = phase
        self.last_metadata = dict(metadata)
        if self.user_append_phase is not None:
            metadata.setdefault("user_append_phase", self.user_append_phase)
        if self.user_append_session_id is not None:
            metadata.setdefault("user_append_session_id", self.user_append_session_id)
        row = await self.store.checkpoint(
            self.request_id,
            lease=self.lease,
            phase=phase,
            metadata=metadata,
        )
        if row is None:
            raise StaleDirectTurnOwner(f"Lost direct-turn fence for {self.request_id}")
        if row.cancel_requested_at is not None:
            raise asyncio.CancelledError

    def _tool_dispatch_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if self.user_append_phase is not None:
            metadata["user_append_phase"] = self.user_append_phase
        if self.user_append_session_id is not None:
            metadata["user_append_session_id"] = self.user_append_session_id
        return metadata

    async def record_tool_dispatch(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        descriptors: list[dict[str, Any]],
    ) -> None:
        """Record this session's in-flight tool batch on the shared turn."""
        self.last_phase = "tool_in_flight"
        metadata = self._tool_dispatch_metadata()
        self.last_metadata = {
            **metadata,
            "session_id": session_id,
            "turn_id": turn_id,
        }
        row = await self.store.record_tool_dispatch(
            self.request_id,
            lease=self.lease,
            session_id=session_id,
            turn_id=turn_id,
            descriptors=descriptors,
            metadata=self.last_metadata,
        )
        if row is None:
            raise StaleDirectTurnOwner(f"Lost direct-turn fence for {self.request_id}")
        if row.cancel_requested_at is not None:
            raise asyncio.CancelledError

    async def complete_tool_dispatch(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        call_ids: list[str],
    ) -> None:
        """Release this session's settled tool batch from the shared turn."""
        metadata = self._tool_dispatch_metadata()
        row = await self.store.complete_tool_dispatch(
            self.request_id,
            lease=self.lease,
            session_id=session_id,
            turn_id=turn_id,
            call_ids=call_ids,
            metadata=metadata,
        )
        if row is None:
            raise StaleDirectTurnOwner(f"Lost direct-turn fence for {self.request_id}")
        outcome = row.outcome if isinstance(row.outcome, dict) else {}
        self.last_phase = str(outcome.get("phase") or "tool_result_persisted")
        self.last_metadata = {**metadata, "call_ids": list(call_ids)}
        if row.cancel_requested_at is not None:
            raise asyncio.CancelledError

    async def bind_tool_dispatch(
        self,
        call_id: str,
        executor_id: str,
        executor_instance_id: str | None,
        *,
        session_id: str,
        turn_id: str | None,
        dispatch_state: Literal["dispatching", "sent"],
    ) -> None:
        if dispatch_state not in {"dispatching", "sent"}:
            raise ValueError("invalid tool dispatch state")
        result = await self.store.merge_tool_dispatch(
            self.request_id,
            lease=self.lease,
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
            executor_id=executor_id,
            executor_instance_id=executor_instance_id,
            dispatch_state=dispatch_state,
        )
        if result.fence_held:
            if result.status is ToolDispatchMergeStatus.UNKNOWN_CALL:
                # The fence is still ours; only the descriptor is missing. Before
                # the send that means the call is about to run with nothing for
                # restart recovery to settle, so it must fail closed. After the
                # send the call already left, and refusing it here would not
                # undo the effect, so only recovery precision is lost.
                logger.warning(
                    "direct turn: tool dispatch binding has no in-flight descriptor",
                    extra={
                        "extra_data": {
                            "request_id": self.request_id,
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "call_id": call_id,
                            "dispatch_state": dispatch_state,
                        }
                    },
                )
                if dispatch_state == "dispatching":
                    raise UnboundToolDispatch(
                        f"Tool call {call_id} has no durable dispatch descriptor"
                    )
            return
        raise StaleDirectTurnOwner(f"Lost direct-turn fence for {self.request_id}")


@dataclass
class BackgroundDirectTurnFence:
    """Fence a child without writing checkpoints into its spawning turn.

    Successful completion by the exact spawning owner permits background work
    to continue. Lease loss, takeover, failure, and cancellation do not transfer
    ownership to that child. Child dispatches are not parent recovery entries.
    """

    parent: DirectTurnExecutionFence

    async def assert_current(self, session: AsyncSession | None = None) -> None:
        """Allow a live parent owner or its successfully completed turn only."""
        if await self.parent.store.has_fence(
            self.parent.request_id, lease=self.parent.lease, session=session
        ):
            return
        row = await self.parent.store.get(self.parent.request_id, session=session)
        if (
            row is not None
            and row.status == DirectTurnStatus.COMPLETED.value
            and row.cancel_requested_at is None
            and row.fencing_token == self.parent.lease.fencing_token
            and f"{row.owner_controller_id}:{row.owner_incarnation_id}"
            == self.parent.lease.owner_id
            and conversation_lease_key(row.conversation_id) == self.parent.lease.resource_key
        ):
            return
        # Use the child cancellation path, not a model-visible tool error or
        # failure follow-up from a controller that no longer owns this work.
        raise asyncio.CancelledError("Background delegate lost its spawning direct-turn owner")

    async def checkpoint(self, _name: str, **_metadata: Any) -> None:
        """Check ownership without modifying the parent's recovery phase."""
        await self.assert_current()

    async def record_tool_dispatch(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        descriptors: list[dict[str, Any]],
    ) -> None:
        """Fence child dispatch without journaling it as a parent tool batch."""
        await self.assert_current()

    async def complete_tool_dispatch(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        call_ids: list[str],
    ) -> None:
        """Fence child settlement without modifying the parent tool journal."""
        await self.assert_current()


ExecuteClaimedTurn = Callable[
    [DirectTurnRequestRow, MaterializedDirectTurnPayload, DirectTurnExecutionFence],
    Awaitable[None],
]
ReconcileCanonicalAppend = Callable[[DirectTurnRequestRow], Awaitable[bool]]
CanClaimTurn = Callable[[DirectTurnRequestRow], bool | Awaitable[bool]]
PermanentFailureHandler = Callable[
    [DirectTurnRequestRow, PermanentDirectTurnPayloadError], Awaitable[None]
]
FencedPermanentFailureHandler = Callable[
    [DirectTurnRequestRow, PermanentDirectTurnPayloadError, Lease], Awaitable[None]
]
TurnStateChanged = Callable[[DirectTurnRequestRow], Awaitable[None]]
RecoverToolCalls = Callable[[DirectTurnRequestRow, Lease], Awaitable[None]]


class DurableDirectTurnRuntime:
    """Per-controller worker for durable ordinary direct turns."""

    def __init__(
        self,
        *,
        store: DirectTurnStore,
        lease_store: DatabaseLeaseStore,
        controller_id: str,
        incarnation_id: str,
        artifact_store: Any,
        execute_claimed_turn: ExecuteClaimedTurn,
        reconcile_canonical_append: ReconcileCanonicalAppend | None = None,
        can_claim_turn: CanClaimTurn | None = None,
        on_permanent_failure: PermanentFailureHandler | None = None,
        on_fenced_permanent_failure: FencedPermanentFailureHandler | None = None,
        on_state_change: TurnStateChanged | None = None,
        recover_tool_calls: RecoverToolCalls | None = None,
        simple_mode: bool,
    ) -> None:
        self.store = store
        self._lease_store = lease_store
        self._controller_id = controller_id
        self._incarnation_id = incarnation_id
        self._owner_id = f"{controller_id}:{incarnation_id}"
        self._artifact_store = artifact_store
        self._execute_claimed_turn = execute_claimed_turn
        self._reconcile_canonical_append = reconcile_canonical_append
        self._can_claim_turn = can_claim_turn
        self._on_permanent_failure = on_permanent_failure
        self._on_fenced_permanent_failure = on_fenced_permanent_failure
        self._on_state_change = on_state_change
        self._recover_tool_calls = recover_tool_calls
        self._simple_mode = simple_mode
        self._accepting_claims = False
        self._wake = asyncio.Event()
        self._wake_generation = 0
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._retry_after: dict[str, float] = {}
        self._run_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._accepting_claims = True
        self._stop.clear()
        self._signal_wake()
        self._worker = asyncio.create_task(self._run(), name="direct-turn-claim-worker")

    async def stop_claiming(self) -> None:
        self._accepting_claims = False
        self._signal_wake()

    async def stop(self) -> None:
        await self.stop_claiming()
        self._stop.set()
        self._signal_wake()
        if self._worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        self._worker = None
        active = [task for task in self._active.values() if not task.done()]
        if active:
            _, pending = await asyncio.wait(active, timeout=5.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def wake(self) -> None:
        self._signal_wake()

    async def expedite(self, request_id: str) -> None:
        """Discard process-local throttling after durable retry invalidation."""

        self._retry_after.pop(request_id, None)
        self._signal_wake()

    def _signal_wake(self) -> None:
        self._wake_generation += 1
        self._wake.set()

    async def run_once(self) -> None:
        if not self._accepting_claims:
            return
        async with self._run_lock:
            await self._recover_failure_visibility()
            await self._recover_stale()
            claimable_heads = await self.store.list_claimable_heads()
            claimable_ids = {row.request_id for row in claimable_heads}
            for request_id in list(self._retry_after):
                if request_id not in claimable_ids:
                    self._retry_after.pop(request_id, None)
            for row in claimable_heads:
                retry_after = self._retry_after.get(row.request_id)
                if retry_after is not None and retry_after > asyncio.get_running_loop().time():
                    continue
                if row.conversation_id in self._active:
                    continue
                if self._can_claim_turn is not None:
                    eligible = self._can_claim_turn(row)
                    if inspect.isawaitable(eligible):
                        eligible = await eligible
                    if not eligible:
                        continue
                if not self._accepting_claims or row.conversation_id in self._active:
                    continue
                lease = await self._lease_store.acquire(
                    conversation_lease_key(row.conversation_id),
                    self._owner_id,
                    ttl_seconds=DIRECT_TURN_LEASE_SECONDS,
                )
                if lease is None:
                    continue
                claimed = await self.store.claim(
                    row.request_id,
                    lease=lease,
                    controller_id=self._controller_id,
                    incarnation_id=self._incarnation_id,
                )
                if claimed is None:
                    await self._lease_store.release(lease)
                    continue
                turn_latency.observe_first_claim(
                    claimed.created_at, claimed.claimed_at, claimed.attempt_count
                )
                with turn_latency.measure("claim_notification"):
                    await self._notify_state_changed(claimed)
                task = asyncio.create_task(
                    self._execute(claimed, lease),
                    name=f"direct-turn:{claimed.request_id}",
                )
                self._active[claimed.conversation_id] = task
                conversation_id = claimed.conversation_id

                def _forget_active(
                    _task: asyncio.Task[None],
                    conversation_id: str = conversation_id,
                ) -> None:
                    self._active.pop(conversation_id, None)

                task.add_done_callback(_forget_active)

    async def _run(self) -> None:
        while not self._stop.is_set():
            generation = self._wake_generation
            if self._accepting_claims:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("direct-turn claim/recovery iteration failed")
            if self._wake_generation != generation:
                continue
            self._wake.clear()
            if self._wake_generation != generation:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=DIRECT_TURN_RECOVERY_INTERVAL_SECONDS,
                )

    async def _execute(self, row: DirectTurnRequestRow, lease: Lease) -> None:
        fence = DirectTurnExecutionFence(self.store, row.request_id, lease)
        execution_task = asyncio.current_task()
        ownership_lost = asyncio.Event()
        renewal = asyncio.create_task(
            self._renew(lease, execution_task, ownership_lost),
            name=f"direct-turn-renew:{row.request_id}",
        )
        cancellation_watch = asyncio.create_task(
            self._watch_cancellation(row.request_id, execution_task),
            name=f"direct-turn-cancel-watch:{row.request_id}",
        )
        retry_scheduled = False
        try:
            payload = await self.store.materialize_claimed_payload(
                row.request_id,
                lease=lease,
                artifact_store=self._artifact_store,
            )
            if payload is None:
                raise StaleDirectTurnOwner(row.request_id)
            prior_outcome = row.outcome if isinstance(row.outcome, dict) else {}
            prior_phase = str(prior_outcome.get("phase") or "claimed")
            prior_append_phase = prior_outcome.get("user_append_phase")
            if not isinstance(prior_append_phase, str) and prior_phase in {
                "user_append_pending",
                "user_append_uncertain",
                "user_appended",
            }:
                prior_append_phase = prior_phase
            prior_append_session_id = prior_outcome.get("user_append_session_id")
            if not isinstance(prior_append_session_id, str):
                prior_append_session_id = prior_outcome.get("session_id")
            if isinstance(prior_append_phase, str):
                fence.set_user_append_state(
                    prior_append_phase,
                    session_id=(
                        prior_append_session_id
                        if isinstance(prior_append_session_id, str)
                        else None
                    ),
                )
            preserve_phase = isinstance(prior_append_phase, str) or prior_phase in {
                "user_append_pending",
                "user_append_uncertain",
                "user_appended",
            }
            await fence.checkpoint(
                prior_phase if preserve_phase else "claimed",
                **(
                    {
                        key: value
                        for key, value in prior_outcome.items()
                        if key not in {"phase", "phase_started_at"}
                    }
                    if preserve_phase
                    else {}
                ),
            )
            await self._execute_claimed_turn(row, payload, fence)
        except StaleDirectTurnOwner:
            return
        except LocalDirectTurnBusy as exc:
            current = await self.store.get(row.request_id)
            current_outcome = (
                current.outcome if current is not None and isinstance(current.outcome, dict) else {}
            )
            await self.store.mark_recoverable(
                row.request_id,
                lease=lease,
                outcome={
                    **current_outcome,
                    "phase": "local_turn_busy",
                    "local_busy_reason": exc.reason,
                },
            )
            attempt = current.attempt_count if current is not None else row.attempt_count
            if attempt > 0 and attempt & (attempt - 1) == 0:
                logger.warning(
                    "direct-turn deferred by local execution owner",
                    extra={
                        "extra_data": {
                            "request_id": row.request_id,
                            "conversation_id": row.conversation_id,
                            "reason": exc.reason,
                            "attempt_count": attempt,
                        }
                    },
                )
            self._retry_after[row.request_id] = (
                asyncio.get_running_loop().time() + DIRECT_TURN_ACTIVE_POLL_SECONDS
            )
            asyncio.get_running_loop().call_later(
                DIRECT_TURN_ACTIVE_POLL_SECONDS,
                self._signal_wake,
            )
            retry_scheduled = True
        except PermanentDirectTurnPayloadError as exc:
            if fence.stream_checkpoint_pending:
                return
            failed = await self.store.mark_terminal(
                row.request_id,
                lease=lease,
                status=DirectTurnStatus.FAILED,
                outcome={
                    "phase": "permanent_payload_visibility_pending",
                    "error": str(exc)[:1000],
                },
            )
            if failed is not None:
                await self._publish_failure_visibility(failed, exc, lease=lease)
        except asyncio.CancelledError:
            # The scheduler can return without settling an unconfirmed stream.
            # Worker teardown must leave that same request lease-reclaimable.
            if not ownership_lost.is_set() and not fence.stream_checkpoint_pending:
                await self.store.mark_terminal(
                    row.request_id,
                    lease=lease,
                    status=DirectTurnStatus.CANCELLED,
                    outcome={"phase": "cancelled_during_controller_drain"},
                )
            raise
        except Exception:
            if fence.stream_checkpoint_pending:
                return
            current = await self.store.get(row.request_id)
            current_outcome = (
                current.outcome if current is not None and isinstance(current.outcome, dict) else {}
            )
            current_phase = str(current_outcome.get("phase") or "controller_error")
            preserve_append_state = isinstance(
                current_outcome.get("user_append_phase"), str
            ) or current_phase in {
                "user_append_pending",
                "user_append_uncertain",
                "user_appended",
            }
            if current is not None and current.attempt_count >= DIRECT_TURN_CONTROLLER_MAX_ATTEMPTS:
                failed = await self.store.mark_terminal(
                    row.request_id,
                    lease=lease,
                    status=DirectTurnStatus.FAILED,
                    outcome={
                        **(current_outcome if preserve_append_state else {}),
                        "phase": "permanent_payload_visibility_pending",
                        "failure_kind": "controller",
                        "error": "Direct turn controller execution failed.",
                    },
                )
                if failed is not None:
                    await self._publish_failure_visibility(
                        failed,
                        PermanentDirectTurnControllerError(
                            "Direct turn controller execution failed."
                        ),
                        lease=lease,
                    )
            else:
                logger.warning(
                    "direct-turn controller execution failed; scheduling bounded retry",
                    extra={
                        "extra_data": {
                            "request_id": row.request_id,
                            "attempt_count": current.attempt_count if current is not None else None,
                            "category": "controller_execution",
                        }
                    },
                )
                await self.store.mark_recoverable(
                    row.request_id,
                    lease=lease,
                    outcome={
                        **(current_outcome if preserve_append_state else {}),
                        "phase": "controller_error",
                    },
                )
                self._retry_after[row.request_id] = (
                    asyncio.get_running_loop().time() + DIRECT_TURN_ACTIVE_POLL_SECONDS
                )
                asyncio.get_running_loop().call_later(
                    DIRECT_TURN_ACTIVE_POLL_SECONDS,
                    self._signal_wake,
                )
                retry_scheduled = True
        finally:
            cancellation_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancellation_watch
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
            await self._lease_store.release(lease)
            updated = await self.store.get(row.request_id)
            if (
                updated is not None
                and updated.status == DirectTurnStatus.RECOVERABLE.value
                and not retry_scheduled
            ):
                outcome = updated.outcome if isinstance(updated.outcome, dict) else {}
                retry_after_seconds = outcome.get("retry_after_seconds")
                retry_delay = (
                    float(retry_after_seconds)
                    if isinstance(retry_after_seconds, (int, float)) and retry_after_seconds > 0
                    else DIRECT_TURN_ACTIVE_POLL_SECONDS
                )
                self._retry_after[row.request_id] = asyncio.get_running_loop().time() + retry_delay
                asyncio.get_running_loop().call_later(retry_delay, self._signal_wake)
                retry_scheduled = True
            if updated is not None and updated.status in {
                DirectTurnStatus.COMPLETED.value,
                DirectTurnStatus.FAILED.value,
                DirectTurnStatus.CANCELLED.value,
                DirectTurnStatus.AMBIGUOUS.value,
            }:
                self._retry_after.pop(row.request_id, None)
            await self._notify_state_changed(updated or row)
            if not retry_scheduled:
                self._signal_wake()

    async def _recover_failure_visibility(self) -> None:
        if self._on_permanent_failure is None:
            return
        for candidate in await self.store.list_pending_failure_visibility():
            # The callback reconstructs user-visible history from the payload.
            # Hydrate only pending work and recheck after discovery's transaction.
            row = await self.store.get(candidate.request_id)
            if row is None or row.status != DirectTurnStatus.FAILED.value:
                continue
            outcome = row.outcome if isinstance(row.outcome, dict) else {}
            if outcome.get("phase") != "permanent_payload_visibility_pending":
                continue
            error_class = (
                PermanentDirectTurnControllerError
                if outcome.get("failure_kind") == "controller"
                else PermanentDirectTurnPayloadError
            )
            error = error_class(
                str(outcome.get("error") or "Permanent direct-turn payload failure")
            )
            await self._publish_failure_visibility(row, error)

    async def _publish_failure_visibility(
        self,
        row: DirectTurnRequestRow,
        error: PermanentDirectTurnPayloadError,
        lease: Lease | None = None,
    ) -> None:
        if self._on_fenced_permanent_failure is not None and lease is not None:
            await self._on_fenced_permanent_failure(row, error, lease)
            await self.store.complete_failure_visibility(row.request_id)
            return
        if self._on_permanent_failure is None:
            return
        try:
            await self._on_permanent_failure(row, error)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "direct-turn permanent failure visibility publish failed",
                extra={"request_id": row.request_id},
            )
            return
        await self.store.complete_failure_visibility(row.request_id)

    async def _renew(
        self,
        lease: Lease,
        execution_task: asyncio.Task[Any] | None,
        ownership_lost: asyncio.Event,
    ) -> None:
        current = lease
        renew_interval = DIRECT_TURN_LEASE_SECONDS / 3
        while True:
            scheduled_at = asyncio.get_running_loop().time()
            await asyncio.sleep(renew_interval)
            # Lag between the intended and the actual wake-up is exactly the
            # loop stall this lease is exposed to; export it for acceptance.
            LEASE_RENEWAL_DELAY_SECONDS.labels(lease="direct_turn").observe(
                max(0.0, asyncio.get_running_loop().time() - scheduled_at - renew_interval)
            )
            try:
                renewed = await self._lease_store.renew(
                    current,
                    ttl_seconds=DIRECT_TURN_LEASE_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("direct-turn lease renewal failed")
                ownership_lost.set()
                if execution_task is not None and not execution_task.done():
                    execution_task.cancel()
                return
            if renewed is None:
                ownership_lost.set()
                if execution_task is not None and not execution_task.done():
                    execution_task.cancel()
                return
            current = renewed

    async def _watch_cancellation(
        self,
        request_id: str,
        execution_task: asyncio.Task[Any] | None,
    ) -> None:
        """Poll durable cancellation so a missed cluster signal still interrupts."""

        while True:
            await asyncio.sleep(DIRECT_TURN_CANCELLATION_POLL_SECONDS)
            try:
                cancel_state = await self.store.get_cancel_state(request_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "direct-turn cancellation watch failed",
                    extra={"request_id": request_id},
                )
                continue
            if cancel_state is None or cancel_state[1] is None:
                continue
            if execution_task is not None and not execution_task.done():
                execution_task.cancel()
            return

    async def _recover_stale(self) -> None:
        for row in await self.store.list_stale_active():
            if row.conversation_id in self._active:
                continue
            lease = await self._lease_store.acquire(
                conversation_lease_key(row.conversation_id),
                self._owner_id,
                ttl_seconds=DIRECT_TURN_LEASE_SECONDS,
            )
            if lease is None:
                continue
            outcome = row.outcome if isinstance(row.outcome, dict) else {}
            phase = str(outcome.get("phase") or "claimed")
            if row.cancel_requested_at is not None:
                cancelled = await self.store.cancel_stale_active(
                    row.request_id,
                    lease=lease,
                )
                await self._lease_store.release(lease)
                await self._notify_state_changed(cancelled or row)
                self._signal_wake()
                continue
            append_recovery_metadata = {
                key: outcome[key]
                for key in ("user_append_phase", "user_append_session_id", "session_id")
                if key in outcome
            }
            if phase == "tool_in_flight":
                recovery_row = await self.store.begin_tool_recovery(
                    row.request_id,
                    lease=lease,
                    controller_id=self._controller_id,
                    incarnation_id=self._incarnation_id,
                    deadline_seconds=TOOL_RECOVERY_DEADLINE_SECONDS,
                )
                if recovery_row is None:
                    await self._lease_store.release(lease)
                    continue
                try:
                    recover_tool_calls = self._recover_tool_calls
                    if recover_tool_calls is None:
                        raise ToolRecoveryPersistenceError(
                            "Tool recovery handler is unavailable.",
                            ambiguous=True,
                        )
                    recovery_outcome = (
                        recovery_row.outcome if isinstance(recovery_row.outcome, dict) else {}
                    )
                    deadline_raw = recovery_outcome.get("recovery_deadline_at")
                    try:
                        deadline = datetime.fromisoformat(str(deadline_raw).replace("Z", "+00:00"))
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=UTC)
                    except ValueError as exc:
                        raise ToolRecoveryPersistenceError(
                            "The durable tool recovery deadline is malformed.",
                            ambiguous=True,
                        ) from exc
                    remaining = max(0.1, (deadline - datetime.now(UTC)).total_seconds())
                    ownership_lost = asyncio.Event()

                    async def _run_recovery(
                        recover: RecoverToolCalls = cast(RecoverToolCalls, recover_tool_calls),
                        row: DirectTurnRequestRow = cast(DirectTurnRequestRow, recovery_row),
                        current_lease: Lease = cast(Lease, lease),
                    ) -> None:
                        await recover(row, current_lease)

                    recovery_task: asyncio.Task[None] = asyncio.create_task(
                        _run_recovery(),
                        name=f"direct-turn-tool-recovery:{row.request_id}",
                    )
                    renewal = asyncio.create_task(
                        self._renew(lease, recovery_task, ownership_lost),
                        name=f"direct-turn-tool-recovery-renew:{row.request_id}",
                    )
                    try:
                        await asyncio.wait_for(
                            recovery_task,
                            timeout=remaining,
                        )
                    except asyncio.CancelledError:
                        if ownership_lost.is_set():
                            raise StaleDirectTurnOwner(row.request_id) from None
                        raise
                    except TimeoutError as exc:
                        raise ToolRecoveryPersistenceError(
                            "The durable tool recovery deadline elapsed.",
                            ambiguous=True,
                        ) from exc
                    finally:
                        renewal.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await renewal
                    checkpointed = await self.store.checkpoint(
                        row.request_id,
                        lease=lease,
                        phase="tool_recovery_result_persisted",
                        metadata={
                            **{
                                key: value
                                for key, value in (
                                    recovery_row.outcome
                                    if isinstance(recovery_row.outcome, dict)
                                    else {}
                                ).items()
                                if key
                                not in {
                                    "phase",
                                    "phase_started_at",
                                    "deferred_phase",
                                    "tool_dispatch_groups",
                                    "tool_calls",
                                }
                            },
                        },
                        # Every group has been settled above, so the turn must
                        # leave tool_in_flight instead of pinning itself there.
                        clear_tool_dispatch=True,
                    )
                    if checkpointed is None:
                        raise StaleDirectTurnOwner(row.request_id)
                    recovered = await self.store.finish_tool_recovery(
                        row.request_id,
                        lease=lease,
                        outcome={
                            **(
                                checkpointed.outcome
                                if isinstance(checkpointed.outcome, dict)
                                else {}
                            ),
                            "phase": "user_appended",
                            "user_append_phase": "user_appended",
                            "interruption_reason": "tool execution was interrupted after restart",
                            "tool_recovery_result_persisted": True,
                        },
                    )
                    if recovered is None:
                        raise StaleDirectTurnOwner(row.request_id)
                    if recovered.status == DirectTurnStatus.CANCELLED.value:
                        await self._lease_store.release(lease)
                        await self._notify_state_changed(recovered)
                        continue
                except StaleDirectTurnOwner:
                    await self._lease_store.release(lease)
                    continue
                except ToolRecoveryPersistenceError as exc:
                    terminal = await self.store.mark_tool_recovery_terminal(
                        row.request_id,
                        lease=lease,
                        status=(
                            DirectTurnStatus.AMBIGUOUS if exc.ambiguous else DirectTurnStatus.FAILED
                        ),
                        outcome={
                            "phase": "ambiguous" if exc.ambiguous else "failed",
                            "code": (
                                "tool_outcome_ambiguous"
                                if exc.ambiguous
                                else "tool_recovery_persistence_failed"
                            ),
                            "reason": str(exc)[:1000],
                            "error": str(exc)[:1000],
                        },
                    )
                    if terminal is None:
                        await self._lease_store.release(lease)
                        continue
                except Exception as exc:
                    logger.exception(
                        "direct-turn tool recovery failed permanently",
                        extra={"request_id": row.request_id},
                    )
                    terminal = await self.store.mark_tool_recovery_terminal(
                        row.request_id,
                        lease=lease,
                        status=DirectTurnStatus.FAILED,
                        outcome={
                            "phase": "failed",
                            "code": "tool_recovery_persistence_failed",
                            "error": str(exc)[:1000],
                        },
                    )
                    if terminal is None:
                        await self._lease_store.release(lease)
                        continue
                await self._lease_store.release(lease)
                updated = await self.store.get(row.request_id)
                await self._notify_state_changed(updated or row)
                self._signal_wake()
                continue
            if not self._quarantine_elapsed(outcome, phase):
                await self._lease_store.release(lease)
                continue
            append_reconciliation = (
                await self._reconcile_append_safely(row)
                if phase in {"intaris_append", "model_response"}
                else None
            )
            if phase == "user_appended":
                recovery_outcome = {
                    "phase": "user_appended",
                    "user_append_phase": "user_appended",
                    **(
                        {"session_id": outcome["session_id"]}
                        if isinstance(outcome.get("session_id"), str)
                        else {}
                    ),
                }
                if row.status == DirectTurnStatus.CLAIMED.value:
                    await self.store.recover_stale_claim(
                        row.request_id,
                        lease=lease,
                        outcome=recovery_outcome,
                    )
                elif row.status == DirectTurnStatus.RUNNING.value:
                    await self.store.recover_stale_running(
                        row.request_id,
                        lease=lease,
                        outcome=recovery_outcome,
                    )
                else:
                    await self.store.mark_stale_ambiguous(
                        row.request_id,
                        lease=lease,
                        outcome={
                            "phase": "ambiguous",
                            "reason": f"stale {phase} in {row.status}",
                        },
                    )
            elif phase in {"canonical_user_append", "user_append_uncertain"}:
                try:
                    reconciled = (
                        self._reconcile_canonical_append is not None
                        and await self._reconcile_canonical_append(row)
                    )
                except Exception:
                    logger.exception(
                        "direct-turn canonical user append reconciliation failed",
                        extra={"request_id": row.request_id},
                    )
                    await self.store.mark_stale_ambiguous(
                        row.request_id,
                        lease=lease,
                        outcome={
                            "phase": "ambiguous",
                            "reason": "canonical user append could not be reconciled",
                        },
                    )
                    await self._lease_store.release(lease)
                    updated = await self.store.get(row.request_id)
                    await self._notify_state_changed(updated or row)
                    continue
                recovery_outcome = {
                    "phase": "user_appended" if reconciled else "user_append_pending",
                    "user_append_phase": ("user_appended" if reconciled else "user_append_pending"),
                    **(
                        {"session_id": outcome["session_id"]}
                        if isinstance(outcome.get("session_id"), str)
                        else {}
                    ),
                }
                if row.status == DirectTurnStatus.ABSORBING.value:
                    if reconciled:
                        await self.store.reconcile_stale_absorbed(
                            row.request_id,
                            lease=lease,
                        )
                    else:
                        await self.store.recover_stale_absorbing(
                            row.request_id,
                            lease=lease,
                        )
                elif row.status == DirectTurnStatus.CLAIMED.value:
                    await self.store.recover_stale_claim(
                        row.request_id,
                        lease=lease,
                        outcome=recovery_outcome,
                    )
                elif row.status == DirectTurnStatus.RUNNING.value:
                    await self.store.recover_stale_running(
                        row.request_id,
                        lease=lease,
                        outcome=recovery_outcome,
                    )
                else:
                    await self.store.mark_stale_ambiguous(
                        row.request_id,
                        lease=lease,
                        outcome={
                            "phase": "ambiguous",
                            "reason": f"stale {phase} in {row.status}",
                        },
                    )
            elif row.status == DirectTurnStatus.CLAIMED.value:
                await self.store.recover_stale_claim(
                    row.request_id,
                    lease=lease,
                    outcome={
                        "phase": "recovered_pre_model",
                        **append_recovery_metadata,
                    },
                )
            elif phase == "intaris_append" and "tool_call" in (outcome.get("event_types") or []):
                await self.store.mark_stale_ambiguous(
                    row.request_id,
                    lease=lease,
                    outcome={
                        "phase": "ambiguous",
                        "reason": "tool call persistence crossed an uncertain dispatch boundary",
                        "call_ids": outcome.get("call_ids") or [],
                    },
                )
            elif phase == "intaris_append" and "tool_result" in (outcome.get("event_types") or []):
                await self.store.mark_stale_ambiguous(
                    row.request_id,
                    lease=lease,
                    outcome={
                        "phase": "ambiguous",
                        "reason": "tool result append outcome is uncertain",
                        "call_ids": outcome.get("call_ids") or [],
                    },
                )
            elif phase == "intaris_append" and append_reconciliation is True:
                if row.status == DirectTurnStatus.ABSORBING.value:
                    await self.store.reconcile_stale_absorbed(
                        row.request_id,
                        lease=lease,
                    )
                elif "assistant_message" in (outcome.get("event_types") or []):
                    await self.store.reconcile_stale_completed(
                        row.request_id,
                        lease=lease,
                        outcome={"phase": "reconciled_terminal_assistant"},
                    )
                else:
                    await self.store.recover_stale_running(
                        row.request_id,
                        lease=lease,
                        outcome={"phase": "user_appended"},
                    )
            elif phase == "intaris_append" and append_reconciliation is None:
                await self.store.mark_stale_ambiguous(
                    row.request_id,
                    lease=lease,
                    outcome={
                        "phase": "ambiguous",
                        "reason": "event append could not be reconciled",
                    },
                )
            elif phase == "model_response":
                if append_reconciliation is True:
                    await self.store.reconcile_stale_completed(
                        row.request_id,
                        lease=lease,
                        outcome={"phase": "reconciled_terminal_assistant"},
                    )
                else:
                    await self.store.mark_stale_ambiguous(
                        row.request_id,
                        lease=lease,
                        outcome={
                            "phase": "ambiguous",
                            "reason": "model response completion could not be reconciled",
                        },
                    )
            elif row.status == DirectTurnStatus.ABSORBING.value:
                await self.store.recover_stale_absorbing(
                    row.request_id,
                    lease=lease,
                )
            elif row.status == DirectTurnStatus.RUNNING.value:
                await self.store.recover_stale_running(
                    row.request_id,
                    lease=lease,
                    outcome={
                        "phase": "recovered_model_boundary",
                        "source_phase": phase,
                        **append_recovery_metadata,
                    },
                )
            else:
                await self.store.mark_stale_ambiguous(
                    row.request_id,
                    lease=lease,
                    outcome={"phase": "ambiguous", "reason": f"stale {phase}"},
                )
            await self._lease_store.release(lease)
            updated = await self.store.get(row.request_id)
            await self._notify_state_changed(updated or row)
            self._signal_wake()

    async def _reconcile_append_safely(self, row: DirectTurnRequestRow) -> bool | None:
        """Reconcile a stale append without letting one poisoned row starve claims."""

        if self._reconcile_canonical_append is None:
            return False
        try:
            return await self._reconcile_canonical_append(row)
        except Exception:
            logger.exception(
                "direct-turn canonical append reconciliation failed",
                extra={"request_id": row.request_id},
            )
            return None

    async def _notify_state_changed(self, row: DirectTurnRequestRow) -> None:
        """Best-effort invalidation after a durable turn transition."""

        if self._on_state_change is None:
            return
        try:
            await self._on_state_change(row)
        except Exception:
            logger.exception(
                "direct-turn state invalidation failed",
                extra={"request_id": row.request_id},
            )

    @staticmethod
    def _quarantine_elapsed(outcome: dict[str, Any], phase: str) -> bool:
        raw_started = outcome.get("phase_started_at")
        if not isinstance(raw_started, str):
            return True
        try:
            started = datetime.fromisoformat(raw_started.replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            return True
        if phase in {"intaris_append", "canonical_user_append"}:
            quarantine = INTARIS_TAKEOVER_QUARANTINE_SECONDS
        else:
            quarantine = 0.0
        return (datetime.now(UTC) - started).total_seconds() >= quarantine
