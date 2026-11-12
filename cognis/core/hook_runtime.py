"""Hook matching and durable detached dispatch, outside the parent model history.

Hook runs own only configuration and execution references. The foreground scheduler
owns turn serialization; unique occurrences deduplicate retries. A leased maintenance
pass owns dispatch/recovery, never replays uncertain mutations, and does not cancel
running children when their source conversation resumes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy import and_, exists, func, not_, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from cognis.core.canonical_history import read_complete_history
from cognis.core.events import Event, EventType
from cognis.core.hook_evidence import hook_envelope, turn_evidence, uses_turn_evidence
from cognis.core.hook_handoff import (
    HOOK_CHILD_PREFIX,
    ForkHandoff,
    is_fork,
    pin_claim,
    slot,
)
from cognis.core.hook_subsessions import HookSubsessions, internal_execution
from cognis.core.hooks import BUNDLED_HOOKS, HookError, require_hook, validate_learning_tools
from cognis.core.maintenance_lease import run_periodic_maintenance
from cognis.core.session import _to_conversation_model, _to_session_model
from cognis.core.workflow_rendering import WorkflowRenderer, WorkflowRenderError
from cognis.logging import get_logger
from cognis.models.hooks import HookDefinition
from cognis.models.session import ConversationContext, SessionEvent
from cognis.models.workflow import Workflow
from cognis.runtime_context import scoped_runtime_context
from cognis.store.direct_turns import foreground_admission_clause
from cognis.store.models import (
    AgentHookAssignmentRow,
    AgentHookRow,
    AgentHookRunRow,
    AgentHookTurnRow,
    Conversation,
    DirectTurnRequestRow,
    Task,
)
from cognis.store.queries import get_agent, get_conversation, get_session_row

logger = get_logger(__name__)
RECOVERY_SECONDS = 600
MAX_EVIDENCE_READS = 4
MAX_IDLE_EVIDENCE = 1024
MAX_HOOK_JOBS = 16
IDLE_PAGE_SIZE = 100


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class HookRuntime:
    """Match bounded declarations and dispatch through existing execution owners."""

    def __init__(
        self,
        *,
        session_factory: Any,
        providers: Any,
        session_manager: Any,
        session_cache: Any,
        event_bus: Any,
        lease_store: Any,
        workflow_registry: Any,
        agent_registry: Any,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.session_manager = session_manager
        self.session_cache = session_cache
        self.event_bus = event_bus
        self.lease_store = lease_store
        self.workflow_registry = workflow_registry
        self.agent_registry = agent_registry
        self.scheduler: Any = None
        self.task_queue: Any = None
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._started = False
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._rerun: set[str] = set()
        self._deadlines: dict[str, tuple[datetime, asyncio.TimerHandle]] = {}
        self._evidence_slots = asyncio.Semaphore(MAX_EVIDENCE_READS)
        self.handoffs = ForkHandoff(self)
        self.subsessions = HookSubsessions(self)
        self.agent_loop: Any = None
        self._idle_evidence: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._idle_recovery_cursor = ""
        self._run_recovery_cursors = {"owned": "", "ready": "", "waiting": ""}
        event_bus.subscribe(EventType.TURN_COMPLETED, self._on_completed)
        event_bus.subscribe(EventType.TURN_ERROR, self._on_error)
        event_bus.subscribe(EventType.TURN_STARTED, self._on_activity)
        event_bus.subscribe(EventType.USER_MESSAGE, self._on_activity)
        event_bus.subscribe(EventType.TASK_COMPLETED, self._on_task_terminal)
        event_bus.subscribe(EventType.TASK_FAILED, self._on_task_terminal)
        event_bus.subscribe(EventType.TASK_CANCELLED, self._on_task_terminal)
        event_bus.subscribe(EventType.HOOK_CONFIGURATION_CHANGED, self._on_configuration)
        event_bus.subscribe(EventType.NOTIFICATION_CREATED, self._on_attached_change)
        event_bus.subscribe(EventType.NOTIFICATION_RESOLVED, self._on_attached_change)

    def start(self, *, scheduler: Any, task_queue: Any, agent_loop: Any = None) -> None:
        """Start event-driven dispatch and infrequent missed-event recovery."""
        self.scheduler = scheduler
        self.task_queue = task_queue
        self.agent_loop = agent_loop
        self._started = True
        self._worker = asyncio.create_task(
            self._recover_periodically(), name="agent-hooks-recovery"
        )

    async def _leased(self, key: str, operation: Callable[[], Awaitable[None]]) -> None:
        """Hold a renewable lease only while actual work is in progress."""
        done = asyncio.Event()

        async def once() -> None:
            try:
                await operation()
            finally:
                done.set()

        await run_periodic_maintenance(
            once,
            stop=done,
            interval_seconds=RECOVERY_SECONDS,
            resource_key=key,
            lease_store=self.lease_store,
        )

    async def _recover_periodically(self) -> None:
        while not self._stop.is_set():
            try:
                await self._recover_once()
            except Exception as exc:
                logger.warning("Hook recovery deferred", extra={"error_type": type(exc).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=RECOVERY_SECONDS)

    async def _recover_once(self) -> None:
        """Rate-limit recovery across controllers without an idle renewal loop."""
        if self.lease_store is not None:
            # A non-renewed interval claim, not a work lease. Keeping it until TTL
            # expiry prevents every waiting controller scanning in succession.
            claim = await self.lease_store.acquire(
                "agent-hooks-recovery",
                uuid.uuid4().hex,
                ttl_seconds=RECOVERY_SECONDS,
            )
            if claim is None:
                return
        # Work cannot outlive the claim; a partial scan is safe to recover later.
        async with asyncio.timeout(60):
            await self.scan()

    def _spawn(self, key: str, operation: Callable[[], Awaitable[None]]) -> None:
        if not self._started or self._stop.is_set():
            return
        if key in self._jobs:
            self._rerun.add(key)
            return
        # Durable runs and the periodic scan recover overflow; never queue an
        # unbounded number of waiting tasks, including static/snapshot-only hooks.
        if len(self._jobs) >= MAX_HOOK_JOBS:
            return

        async def work() -> None:
            try:
                while True:
                    self._rerun.discard(key)
                    await operation()
                    if key not in self._rerun or self._stop.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Hook event work deferred to recovery",
                    extra={
                        "hook_job": key,
                        "error_type": type(exc).__name__,
                    },
                )
            finally:
                self._jobs.pop(key, None)
                self._rerun.discard(key)

        self._jobs[key] = asyncio.create_task(work(), name=key)

    def _cancel_deadline(self, key: str) -> None:
        timer = self._deadlines.pop(key, None)
        if timer:
            timer[1].cancel()

    def _deadline(
        self,
        key: str,
        when: datetime,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        if not self._started or self._stop.is_set():
            return
        current = self._deadlines.get(key)
        if current and current[0] == when:
            return
        self._cancel_deadline(key)

        def fire() -> None:
            self._deadlines.pop(key, None)
            self._spawn(key, operation)

        delay = max(0, (_aware(when) - datetime.now(UTC)).total_seconds())
        self._deadlines[key] = (when, asyncio.get_running_loop().call_later(delay, fire))

    def _wake_run(self, run_id: str) -> None:
        self._spawn("hook-run:" + run_id, lambda: self._run(run_id))

    @staticmethod
    def _timeout_origin(run: Any) -> datetime:
        started = run.configuration.get("_execution_started_at")
        return datetime.fromisoformat(started) if started else _aware(run.created_at)

    async def _run(self, run_id: str) -> None:
        async with self.session_factory() as db:
            current = await db.get(AgentHookRunRow, run_id)
        if current is not None and internal_execution(current):
            await self.subsessions.run(run_id)
            return

        async def process() -> None:
            async with self.session_factory() as db:
                run = await db.get(AgentHookRunRow, run_id)
            if run is not None:
                await self._process_run(run)
                async with self.session_factory() as db:
                    current = await db.get(AgentHookRunRow, run_id)
                if current is not None and internal_execution(current) and self._started:
                    # Admission may already occupy the ordinary run job key.
                    # Schedule the independently fenced child outside this job
                    # (and outside the recovery scan's bounded maintenance lease).
                    self._deadline(
                        "hook-child:" + run_id,
                        datetime.now(UTC),
                        partial(self._run, run_id),
                    )
                    return
                if current is not None and current.status in {"dispatching", "running"}:
                    claimed = (
                        (current.handoff or {}).get("claimed_at") if is_fork(current) else None
                    )
                    expires = (
                        datetime.fromisoformat(claimed)
                        if claimed
                        else self._timeout_origin(current)
                    ) + timedelta(
                        seconds=current.configuration["timeout_seconds"],
                    )
                    if expires > datetime.now(UTC):
                        self._deadline("hook-timeout:" + run_id, expires, lambda: self._run(run_id))
                    else:
                        # Admission/publication can cross the deadline. Enforce once now,
                        # without scheduling an expired timer if cancellation also fails.
                        await self._process_run(current)

        await self._leased("hook-run:" + run_id, process)

    async def stop(self) -> None:
        """Stop matching/dispatch; uncertain internal work is recovered as interrupted."""
        for event, handler in (
            (EventType.TURN_COMPLETED, self._on_completed),
            (EventType.TURN_ERROR, self._on_error),
            (EventType.TURN_STARTED, self._on_activity),
            (EventType.USER_MESSAGE, self._on_activity),
            (EventType.TASK_COMPLETED, self._on_task_terminal),
            (EventType.TASK_FAILED, self._on_task_terminal),
            (EventType.TASK_CANCELLED, self._on_task_terminal),
            (EventType.HOOK_CONFIGURATION_CHANGED, self._on_configuration),
            (EventType.NOTIFICATION_CREATED, self._on_attached_change),
            (EventType.NOTIFICATION_RESOLVED, self._on_attached_change),
        ):
            self.event_bus.unsubscribe(event, handler)
        self._stop.set()
        self._started = False
        for _, timer in self._deadlines.values():
            timer.cancel()
        self._deadlines.clear()
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        if self._worker:
            self._worker.cancel()
            jobs.append(self._worker)
        await asyncio.gather(*jobs, return_exceptions=True)
        self._idle_evidence.clear()

    async def before_cycle(self, ctx: Any, cycle_index: int) -> list[dict[str, Any]]:
        """Return ephemeral injection messages; never append them as parent history."""
        conversation = ctx.conversation
        if (
            ctx.system_initiated
            or ctx.task_id
            or conversation.context.platform_data.get("hook_run_id")
            or ctx.agent.owner_email != ctx.session.user_email
        ):
            return []
        async with self.session_factory() as db:
            assignments = (
                await db.scalars(
                    select(AgentHookAssignmentRow).where(
                        AgentHookAssignmentRow.agent_id == ctx.agent.agent_id,
                        AgentHookAssignmentRow.enabled.is_(True),
                    )
                )
            ).all()
        ordinal = await self._turn_ordinal(conversation.conversation_id, ctx.turn_id)
        if not assignments:
            return []
        context = {
            "conversation": {"id": conversation.conversation_id, "type": conversation.context.type},
            "agent": {"id": ctx.agent.agent_id},
            "turn": {"id": ctx.turn_id, "index": ordinal},
            "cycle": {"index": cycle_index},
        }
        triggers = ["before_model_cycle"]
        if cycle_index == 1:
            triggers.append("before_turn")
            if not getattr(ctx, "is_retry", False):
                triggers.append("user_message")
            if ordinal == 1 and not getattr(ctx, "is_retry", False):
                triggers.append("first_user_message")
        return await self.match(
            conversation=conversation,
            session=ctx.session,
            triggers=triggers,
            occurrence=f"turn:{ctx.turn_id}:cycle:{cycle_index}",
            context=context,
        )

    async def _turn_ordinal(self, conversation_id: str, turn_id: str) -> int:
        for _ in range(3):
            async with self.session_factory() as db:
                existing = await db.get(AgentHookTurnRow, (conversation_id, turn_id))
                if existing:
                    return int(existing.ordinal)
                # Foreground admission serializes this conversation; the unique ordinal
                # constraint also protects cross-controller recovery races.
                last = int(
                    await db.scalar(
                        select(func.max(AgentHookTurnRow.ordinal)).where(
                            AgentHookTurnRow.conversation_id == conversation_id
                        )
                    )
                    or 0
                )
                if not last:
                    request = await db.scalar(
                        select(DirectTurnRequestRow).where(
                            DirectTurnRequestRow.conversation_id == conversation_id,
                            DirectTurnRequestRow.turn_id == turn_id,
                        )
                    )
                    if request is not None:
                        last = int(
                            await db.scalar(
                                select(func.count())
                                .select_from(
                                    DirectTurnRequestRow,
                                )
                                .where(
                                    DirectTurnRequestRow.conversation_id == conversation_id,
                                    DirectTurnRequestRow.admission_order < request.admission_order,
                                    foreground_admission_clause(),
                                    DirectTurnRequestRow.status != "absorbed",
                                )
                            )
                            or 0
                        )
                ordinal = last + 1
                db.add(
                    AgentHookTurnRow(
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        ordinal=ordinal,
                    )
                )
                try:
                    await db.commit()
                    return ordinal
                except IntegrityError:
                    await db.rollback()
        raise RuntimeError("Could not allocate hook turn ordinal")

    async def manual_hooks(
        self, conversation: Any, user_email: str
    ) -> list[tuple[str, HookDefinition, bool]]:
        """List assigned manual-compatible hooks, including disabled assignments."""
        async with self.session_factory() as db:
            source = await get_conversation(db, conversation.conversation_id)
            agent = await get_agent(db, conversation.agent_id)
            if (
                source is None
                or agent is None
                or source.user_email != user_email
                or agent.owner_email != user_email
                or source.agent_id != conversation.agent_id
                or source.status != "active"
                or source.context_type != "web"
                or (source.context_data or {}).get("hook_run_id")
            ):
                raise HookError("Manual hooks require an owned, active web conversation.")
            assignments = (
                await db.scalars(
                    select(AgentHookAssignmentRow).where(
                        AgentHookAssignmentRow.agent_id == source.agent_id,
                    )
                )
            ).all()
            result = []
            for assignment in assignments:
                definition, _ = await require_hook(db, user_email, assignment.hook_id)
                if definition.trigger in {"conversation_idle", "turn_completed"}:
                    result.append((assignment.hook_id, definition, assignment.enabled))
            return sorted(result, key=lambda item: (item[1].name.casefold(), item[0]))

    async def run_manual(
        self, conversation: Any, session: Any, user_email: str, selector: str
    ) -> Any:
        """Consume the canonical occurrence; manual and timer admission share one run ID."""
        selector = " ".join(selector.split())
        hooks = await self.manual_hooks(conversation, user_email)
        matches = [item for item in hooks if item[0] == selector]
        if not matches:
            matches = [item for item in hooks if item[1].name.casefold() == selector.casefold()]
        if len(matches) != 1:
            raise HookError("Select one assigned, applicable hook by its exact name or ID.")
        hook_id, definition, _ = matches[0]
        if definition.trigger == "conversation_idle" and await self.has_attached_work(conversation):
            raise HookError("Conversation has pending notifications or background work.")
        if await self.scheduler.durable_running_turn_state(
            conversation.conversation_id
        ) is not None or await self.scheduler.get_queued_messages(conversation.conversation_id):
            raise HookError("Wait for running and queued turns to finish.")
        async with self.session_factory() as db:
            source = await get_conversation(db, conversation.conversation_id)
            if source is None or not source.active_session_id:
                raise HookError("Manual hooks require an active foreground session.")
            source_session = await get_session_row(db, source.active_session_id)
            if source_session is None or source_session.parent_session_id is not None:
                raise HookError("Manual hooks require an active foreground session.")
            origin = await self._idle_origin(db, source)
        if origin is None:
            raise HookError("Send a normal message first; no foreground turn is available.")
        occurrence = origin[0]
        turn_id = occurrence.removeprefix("idle:")
        if definition.trigger == "turn_completed":
            occurrence = "completed:" + turn_id
        async with self.session_factory() as db:
            # Upgrade unstarted automatic admission under the same row lock used
            # by dispatch. A rejected attempt has no executable snapshot; retain
            # its audit record and use one deduplicated manual retry occurrence.
            existing = await db.scalar(
                update(AgentHookRunRow)
                .where(
                    AgentHookRunRow.run_id
                    == self._run_id(hook_id, source.conversation_id, occurrence),
                )
                .values(status=AgentHookRunRow.status)
                .returning(AgentHookRunRow)
            )
            if existing is not None:
                if existing.status in {"invalid_condition", "skipped"}:
                    occurrence = "manual:" + occurrence
                elif existing.status in {"pending", "preparing"}:
                    existing.configuration = {**existing.configuration, "_manual": True}
            await db.commit()
        await self.match(
            conversation=_to_conversation_model(source),
            session=_to_session_model(source_session),
            triggers=[definition.trigger],
            occurrence=occurrence,
            context={
                "conversation": {"id": source.conversation_id, "type": source.context_type},
                "agent": {"id": source.agent_id},
                "turn": {"id": turn_id, "index": 0},
                "cycle": {"index": 0},
                "idle": {"seconds": max(0, int((datetime.now(UTC) - origin[1]).total_seconds()))},
            },
            manual_hook_id=hook_id,
        )
        async with self.session_factory() as db:
            run = await db.get(
                AgentHookRunRow, self._run_id(hook_id, source.conversation_id, occurrence)
            )
        if run is None:
            raise HookError("Hook input could not be prepared; no execution was admitted.")
        if run.status in {"invalid_condition", "skipped"} and not run.configuration.get("_manual"):
            # An automatic rejection may have won the insert race after our read.
            return await self.run_manual(conversation, session, user_email, selector)
        if definition.trigger == "conversation_idle":
            # Timers are conversation-wide. Recalculate the remaining assignments,
            # while the durable run row prevents this occurrence from firing again.
            self._cancel_deadline("hook-idle:" + source.conversation_id)
            await self._match_idle(source.conversation_id)
        return run

    async def has_attached_work(self, conversation: Any) -> bool:
        from cognis.core.hook_readiness import has_attached_work

        return await has_attached_work(self.session_factory, self.scheduler, conversation)

    async def idle_start_ready(self, run: Any) -> bool:
        """Recheck genuine idle at the last boundary before execution, not just matching."""
        if run.configuration.get("trigger") != "conversation_idle":
            return True
        async with self.session_factory() as db:
            source = await get_conversation(db, run.conversation_id)
        if source is None:
            return True  # Let normal missing-source settlement own the failure.
        if not await self._track_idle_readiness(source):
            return False
        if await self.scheduler.durable_running_turn_state(
            run.conversation_id
        ) is not None or await self.scheduler.get_queued_messages(run.conversation_id):
            return False
        async with self.session_factory() as db:
            current = await db.get(AgentHookRunRow, run.run_id)
            if current.configuration.get("_manual") is True:
                return True
            boundary = (source.context_data or {}).get("hook_idle_boundary", {})
            if boundary.get("ready_at"):
                due = datetime.fromisoformat(boundary["ready_at"]) + timedelta(
                    seconds=run.configuration["idle_seconds"]
                )
                if due > datetime.now(UTC):
                    self._deadline("hook-run:" + run.run_id, due, partial(self._run, run.run_id))
                    return False
        return True

    async def match(
        self,
        *,
        conversation: Any,
        session: Any,
        triggers: list[str],
        occurrence: str,
        context: dict[str, Any],
        manual_hook_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pin a definition and occurrence before injection or detached execution."""
        if triggers == ["conversation_idle"] and await self.has_attached_work(conversation):
            return []
        messages: list[dict[str, Any]] = []
        snapshots: list[Any] = []
        ready: list[str] = []
        async with self.session_factory() as db:
            assignments = (
                await db.scalars(
                    select(AgentHookAssignmentRow).where(
                        AgentHookAssignmentRow.agent_id == conversation.agent_id,
                        AgentHookAssignmentRow.hook_id == manual_hook_id
                        if manual_hook_id is not None
                        else AgentHookAssignmentRow.enabled.is_(True),
                    )
                )
            ).all()
            configured = []
            idle_origin = (
                await self._idle_origin(db, conversation)
                if "conversation_idle" in triggers
                else None
            )
            for assignment in sorted(assignments, key=lambda assignment: assignment.hook_id):
                definition, revision = await require_hook(
                    db, conversation.user_email, assignment.hook_id
                )
                if definition.trigger not in triggers:
                    continue
                if manual_hook_id is not None:
                    if definition.trigger not in {"conversation_idle", "turn_completed"}:
                        continue
                    definition = definition.model_copy(update={"condition": None})
                if (
                    manual_hook_id is None
                    and definition.trigger == "conversation_idle"
                    and (
                        idle_origin is None
                        or idle_origin[0] != occurrence
                        or assignment.activated_at is None
                        or idle_origin[2] <= _aware(assignment.activated_at)
                    )
                ):
                    continue
                if (
                    manual_hook_id is None
                    and definition.trigger == "conversation_idle"
                    and (context.get("idle", {}).get("seconds", 0) < definition.idle_seconds)
                ):
                    continue
                if definition.mode != "inject":
                    existing = await db.get(
                        AgentHookRunRow,
                        self._run_id(assignment.hook_id, conversation.conversation_id, occurrence),
                    )
                    if existing is not None:
                        if manual_hook_id is not None:
                            await db.execute(
                                update(AgentHookRunRow)
                                .where(
                                    AgentHookRunRow.run_id == existing.run_id,
                                    AgentHookRunRow.status.in_(["preparing", "pending"]),
                                )
                                .values(configuration={**existing.configuration, "_manual": True})
                            )
                            await db.commit()
                        if existing.status == "pending":
                            ready.append(existing.run_id)
                        continue
                configured.append((assignment, definition, revision))
        # Static reminders must not read history on every cycle.
        evidence_failed = False
        idle_contexts: dict[str, dict[str, Any]] = {}
        idle_failures: set[str] = set()
        if triggers == ["conversation_idle"]:
            for assignment, definition, _ in configured:
                if not uses_turn_evidence(definition.model_dump(mode="json")):
                    continue
                try:
                    async with self.session_factory() as db:
                        previous = await db.scalar(
                            select(AgentHookRunRow)
                            .where(
                                AgentHookRunRow.hook_id == assignment.hook_id,
                                AgentHookRunRow.owner_email == conversation.user_email,
                                AgentHookRunRow.agent_id == conversation.agent_id,
                                AgentHookRunRow.conversation_id == conversation.conversation_id,
                                AgentHookRunRow.status == "completed",
                                # Injected guidance has no processed-history snapshot.
                                # A later detached revision must not treat it as coverage.
                                AgentHookRunRow.configuration["mode"].as_string() != "inject",
                            )
                            .order_by(
                                AgentHookRunRow.created_at.desc(), AgentHookRunRow.run_id.desc()
                            )
                            .limit(1)
                        )
                    key = (
                        conversation.conversation_id,
                        session.session_id,
                        f"{occurrence}:{assignment.hook_id}:{previous.run_id if previous else ''}",
                    )
                    async with self._evidence_slots:
                        evidence = self._idle_evidence.get(key)
                        if evidence is None:
                            boundary = None
                            if previous is not None:
                                async with self.session_factory() as db:
                                    old_source = await get_session_row(
                                        db, previous.source_session_id
                                    )
                                if old_source is None:
                                    raise HookError("Previous hook source unavailable")
                                with scoped_runtime_context(
                                    user_email=conversation.user_email,
                                    agent_id=conversation.agent_id,
                                    agent_owner_email=conversation.user_email,
                                ):
                                    snapshot = await self._source_snapshot(previous, old_source)
                                boundary = (
                                    old_source.intaris_session_id or old_source.session_id,
                                    snapshot["seq"],
                                )
                            evidence = await self._evidence(
                                conversation,
                                session,
                                context.get("turn", {}).get("id") or None,
                                idle_window=True,
                                boundary=boundary,
                            )
                            self._idle_evidence[key] = evidence
                        self._idle_evidence.move_to_end(key)
                        while len(self._idle_evidence) > MAX_IDLE_EVIDENCE:
                            self._idle_evidence.popitem(last=False)
                    idle_contexts[assignment.hook_id] = evidence
                except Exception as exc:
                    logger.warning(
                        "Hook evidence unavailable", extra={"error_type": type(exc).__name__}
                    )
                    idle_failures.add(assignment.hook_id)
        if triggers != ["conversation_idle"] and any(
            definition.trigger in triggers
            and uses_turn_evidence(definition.model_dump(mode="json"))
            for _, definition, _ in configured
        ):
            try:
                # No DB connection is held while waiting or reading Intaris. Idle
                # evidence is stable for a settled foreground occurrence; cache
                # only successful reads, never incomplete history or failures.
                key = (conversation.conversation_id, session.session_id, occurrence)
                idle = triggers == ["conversation_idle"]
                async with self._evidence_slots:
                    evidence = self._idle_evidence.get(key) if idle else None
                    if evidence is None:
                        evidence = await self._evidence(
                            conversation,
                            session,
                            context.get("turn", {}).get("id") or None,
                        )
                        if idle:
                            self._idle_evidence[key] = evidence
                    if idle:
                        self._idle_evidence.move_to_end(key)
                        while len(self._idle_evidence) > MAX_IDLE_EVIDENCE:
                            self._idle_evidence.popitem(last=False)
                context = {**context, **evidence}
            except Exception as exc:
                logger.warning(
                    "Hook evidence unavailable", extra={"error_type": type(exc).__name__}
                )
                evidence_failed = True
        async with self.session_factory() as db:
            for assignment, definition, revision in configured:
                if definition.mode in {"fork", "fresh"}:
                    await db.execute(
                        update(AgentHookAssignmentRow)
                        .where(
                            AgentHookAssignmentRow.agent_id == assignment.agent_id,
                            AgentHookAssignmentRow.hook_id == assignment.hook_id,
                        )
                        .values(enabled=AgentHookAssignmentRow.enabled)
                    )
                    unfinished = await db.scalar(
                        select(AgentHookRunRow)
                        .where(
                            AgentHookRunRow.hook_id == assignment.hook_id,
                            AgentHookRunRow.agent_id == conversation.agent_id,
                            AgentHookRunRow.owner_email == conversation.user_email,
                            AgentHookRunRow.conversation_id == conversation.conversation_id,
                            AgentHookRunRow.status.in_(
                                ["preparing", "pending", "dispatching", "running"]
                            ),
                            AgentHookRunRow.run_id
                            != self._run_id(
                                assignment.hook_id, conversation.conversation_id, occurrence
                            ),
                        )
                        .order_by(AgentHookRunRow.created_at)
                        .limit(1)
                    )
                    if unfinished is not None:
                        self._wake_run(unfinished.run_id)
                if definition.trigger not in triggers:
                    continue
                if assignment.hook_id in idle_failures:
                    continue
                if evidence_failed and uses_turn_evidence(definition.model_dump(mode="json")):
                    continue
                if (
                    manual_hook_id is None
                    and definition.trigger == "conversation_idle"
                    and (context.get("idle", {}).get("seconds", 0) < definition.idle_seconds)
                ):
                    continue
                renderer = WorkflowRenderer()
                render_context = {
                    **context,
                    **idle_contexts.get(assignment.hook_id, {}),
                    "parameters": {
                        **definition.parameters,
                        **assignment.parameters,
                    },
                }
                config = definition.model_dump(mode="json")
                render_failed = False
                try:
                    if definition.condition and not renderer.render_expression(
                        definition.condition, render_context
                    ):
                        continue
                    config["prompt"] = renderer.render_text(definition.prompt, render_context)
                    if definition.tool_call:
                        config["tool_call"]["args"] = renderer.render_native(
                            definition.tool_call.args, render_context
                        )
                except WorkflowRenderError:
                    # Invalid optional automation cannot break a foreground user turn.
                    render_failed = True
                run_id = self._run_id(assignment.hook_id, conversation.conversation_id, occurrence)
                row = await db.get(AgentHookRunRow, run_id)
                created_here = False
                if row is None:
                    row = AgentHookRunRow(
                        run_id=run_id,
                        hook_id=assignment.hook_id,
                        hook_revision=revision,
                        owner_email=conversation.user_email,
                        agent_id=conversation.agent_id,
                        conversation_id=conversation.conversation_id,
                        source_session_id=session.session_id,
                        occurrence=occurrence,
                        configuration={
                            **definition.model_dump(mode="json"),
                            "_input_event": True,
                            **({"_manual": True} if manual_hook_id is not None else {}),
                        },
                        lifecycle_pending=definition.mode != "inject",
                        status="invalid_condition"
                        if render_failed
                        else ("completed" if definition.mode == "inject" else "preparing"),
                        finished_at=datetime.now(UTC)
                        if (definition.mode == "inject" or render_failed)
                        else None,
                    )
                    # One assignment collision must not roll back other matched hooks.
                    try:
                        async with db.begin_nested():
                            db.add(row)
                            await db.flush()
                            created_here = True
                    except IntegrityError:
                        row = await db.get(AgentHookRunRow, run_id)
                if row is not None and manual_hook_id is not None and not created_here:
                    await db.execute(
                        update(AgentHookRunRow)
                        .where(
                            AgentHookRunRow.run_id == row.run_id,
                            AgentHookRunRow.status.in_(["preparing", "pending"]),
                        )
                        .values(configuration={**row.configuration, "_manual": True})
                    )
                if row is not None and definition.mode == "inject" and row.status == "completed":
                    messages.append(
                        {
                            "role": "system",
                            "content": hook_envelope(
                                name=definition.name,
                                hook_id=assignment.hook_id,
                                run_id=row.run_id,
                                prompt=config["prompt"],
                            ),
                            "_audit_source": "agent_hook",
                            "_audit_role": "developer",
                            "_hook_run_id": row.run_id,
                        }
                    )
                elif row is not None and row.status == "preparing" and created_here:
                    snapshots.append((row, config))
                elif row is not None and row.status == "pending":
                    ready.append(row.run_id)
            await db.commit()
        for row, config in snapshots:
            try:
                await self._snapshot(row, config)
                ready.append(row.run_id)
            except Exception as exc:
                logger.warning(
                    "Hook snapshot failed",
                    extra={
                        "hook_run_id": row.run_id,
                        "error_type": type(exc).__name__,
                    },
                )
                await self._finish(row, "failed")
        for run_id in ready:
            self._wake_run(run_id)
        return messages

    @staticmethod
    def _run_id(hook_id: str, conversation_id: str, occurrence: str) -> str:
        digest = hashlib.sha256(f"{hook_id}:{conversation_id}:{occurrence}".encode()).hexdigest()
        return "hrun_" + digest

    async def _evidence(
        self,
        conversation: Any,
        source: Any,
        turn_id: str | None,
        *,
        idle_window: bool = False,
        boundary: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        from cognis.core.hook_evidence import idle_evidence
        from cognis.store.models import Session

        async with self.session_factory() as db:
            sessions = (
                await db.scalars(
                    select(Session)
                    .where(
                        Session.conversation_id == conversation.conversation_id,
                        Session.started_at <= source.started_at,
                        Session.parent_session_id.is_(None),
                        Session.user_email == conversation.user_email,
                        Session.agent_id == conversation.agent_id,
                    )
                    .order_by(Session.started_at.desc())
                    .limit(101 if idle_window else None)
                )
            ).all()
        if idle_window:
            if boundary is not None:
                index = next(
                    (
                        i
                        for i, row in enumerate(sessions)
                        if (row.intaris_session_id or row.session_id) == boundary[0]
                    ),
                    None,
                )
                if index is None:
                    raise HookError("Previous hook boundary is outside source history")
                sessions = sessions[: index + 1]
            if len(sessions) > 100:
                raise HookError("Hook evidence session window exceeds safety limit")
        batches: list[list[dict[str, Any]]] = []
        with scoped_runtime_context(
            user_email=conversation.user_email,
            agent_id=conversation.agent_id,
            agent_owner_email=conversation.user_email,
        ):
            for row in sessions:
                history = await read_complete_history(
                    self.providers.guardrails,
                    row.intaris_session_id or row.session_id,
                    after_seq=boundary[1]
                    if idle_window
                    and boundary is not None
                    and (row.intaris_session_id or row.session_id) == boundary[0]
                    else 0,
                )
                batches.insert(0, history.events)
                evidence = turn_evidence([item for batch in batches for item in batch], turn_id)
                if not idle_window and evidence["last_user_message"]:
                    return evidence
        if idle_window:
            return idle_evidence([item for batch in batches for item in batch], turn_id)
        return turn_evidence([item for batch in batches for item in batch], turn_id)

    async def _snapshot(self, run: Any, rendered: dict[str, Any] | None = None) -> None:
        """Pin the boundary in Intaris rather than duplicating event sequences in Cognis DB."""
        async with self.session_factory() as db:
            source = await get_session_row(db, run.source_session_id)
        if source is None:
            raise HookError("Hook source session not found")
        target_session_id = source.intaris_session_id or source.session_id
        with scoped_runtime_context(
            user_email=run.owner_email,
            agent_id=run.agent_id,
            agent_owner_email=run.owner_email,
        ):
            event = SessionEvent(
                type="lifecycle",
                data={
                    "event": "hook_snapshot",
                    "hook_run_id": run.run_id,
                    "input": rendered,
                },
            )
            result = await self.providers.guardrails.record_events(
                target_session_id,
                [event],
                source="cognis_hooks",
                idempotency_key=f"{target_session_id}:{run.run_id}:snapshot",
                user_email=run.owner_email,
                agent_id=run.agent_id,
            )
            if not result.ok:
                raise HookError("Hook snapshot was not persisted")
            await self.session_cache.append_recorded_events(
                _to_session_model(source), [event], result
            )
        async with self.session_factory() as db:
            await db.execute(
                update(AgentHookRunRow)
                .where(
                    AgentHookRunRow.run_id == run.run_id,
                    AgentHookRunRow.status == "preparing",
                )
                .values(
                    status="pending",
                    **(
                        {
                            "handoff": {
                                "snapshot": {
                                    "session_id": target_session_id,
                                    "seq": result.first_seq,
                                },
                                "boundary": {
                                    "session_id": target_session_id,
                                    "seq": result.first_seq - 1,
                                },
                            },
                        }
                        if is_fork(run) and result.first_seq is not None
                        else {}
                    ),
                )
            )
            await db.commit()

    async def _source_snapshot(self, run: Any, source_session: Any) -> dict[str, Any]:
        if is_fork(run) and (run.handoff or {}).get("snapshot"):
            ref = run.handoff["snapshot"]
            data = await self.handoffs.read(ref, run.run_id, "hook_snapshot")
            if data is None:
                raise HookError("Hook snapshot marker is unavailable")
            return {"seq": ref["seq"], "type": "lifecycle", "data": data}
        history = await read_complete_history(
            self.providers.guardrails,
            source_session.intaris_session_id or source_session.session_id,
        )
        marker = next(
            (
                event
                for event in history.events
                if (
                    event.get("type") == "lifecycle"
                    and event.get("data", {}).get("event") == "hook_snapshot"
                    and event.get("data", {}).get("hook_run_id") == run.run_id
                )
            ),
            None,
        )
        if marker is None:
            raise HookError("Hook snapshot is unavailable")
        return marker

    async def _source_boundary(self, run: Any, source_session: Any) -> int:
        return int((await self._source_snapshot(run, source_session))["seq"]) - 1

    async def _on_completed(self, event: Event) -> None:
        data = event.data
        await self._settle_child(
            str(data.get("conversation_id") or ""),
            "completed",
            data.get("turn_id"),
        )
        if any(
            data.get(key)
            for key in (
                "system_initiated",
                "task_id",
                "delegated",
                "partial",
                "managed_continuation_pending",
            )
        ):
            return
        async with self.session_factory() as db:
            row = await get_conversation(db, str(data.get("conversation_id") or ""))
            if row is None or (row.context_data or {}).get("hook_run_id"):
                return
            session_row = await get_session_row(db, str(data.get("session_id") or ""))
            agent = await get_agent(db, row.agent_id)
            if session_row is None or agent is None or agent.owner_email != row.user_email:
                return
            conversation, session = _to_conversation_model(row), _to_session_model(session_row)
        ordinal = await self._turn_ordinal(conversation.conversation_id, data["turn_id"])
        await self.match(
            conversation=conversation,
            session=session,
            triggers=["turn_completed"],
            occurrence=f"completed:{data['turn_id']}",
            context={
                "turn": {"id": data["turn_id"], "index": ordinal},
                "conversation": {"id": row.conversation_id, "type": row.context_type},
                "agent": {"id": row.agent_id},
                "cycle": {"index": 0},
            },
        )
        await self._match_idle(
            conversation.conversation_id,
            finishing_turn_id=str(data.get("turn_id") or ""),
            finished_at=datetime.now(UTC),
        )

    async def _on_error(self, event: Event) -> None:
        conversation_id = str(event.data.get("conversation_id") or "")
        await self._settle_child(conversation_id, "failed", event.data.get("turn_id"))
        if conversation_id and not event.data.get("system_initiated"):
            await self._match_idle(conversation_id)

    async def _on_activity(self, event: Event) -> None:
        conversation_id = str(event.data.get("conversation_id") or "")
        self._cancel_deadline("hook-idle:" + conversation_id)

    async def _on_task_terminal(self, event: Event) -> None:
        task_id = str(event.data.get("task_id") or "")
        if not task_id:
            return
        async with self.session_factory() as db:
            run_id = await db.scalar(
                select(AgentHookRunRow.run_id).where(
                    AgentHookRunRow.task_id == task_id,
                    AgentHookRunRow.status.in_(["dispatching", "running"]),
                )
            )
        if run_id:
            self._wake_run(run_id)

    async def _on_configuration(self, event: Event) -> None:
        if not event.data.get("agent_id") and not event.data.get("hook_id"):
            return
        # One global dirty bit, not a set of agents/conversations or a task per
        # historical occurrence. Repeated changes coalesce while a pass is blocked.
        self._spawn("hook-config", self._refresh_idle)

    async def _refresh_idle(self) -> None:
        for key in list(self._deadlines):
            if key.startswith("hook-idle:"):
                self._cancel_deadline(key)
        await self._match_idle()

    async def _settle_child(
        self, conversation_id: str, status: str, turn_id: str | None = None
    ) -> None:
        async with self.session_factory() as db:
            rows = (
                await db.scalars(
                    select(AgentHookRunRow).where(
                        AgentHookRunRow.child_conversation_id == conversation_id,
                        AgentHookRunRow.status == "running",
                    )
                )
            ).all()
            settled = []
            for row in rows:
                if is_fork(row):
                    self._deadline(
                        "hook-settle:" + row.run_id,
                        datetime.now(UTC) + timedelta(seconds=1),
                        partial(self._run, row.run_id),
                    )
                    continue
                if row.configuration.get("mode") == "follow_up":
                    request = (
                        await db.scalar(
                            select(DirectTurnRequestRow).where(
                                DirectTurnRequestRow.idempotency_scope == "hook",
                                DirectTurnRequestRow.idempotency_key == row.run_id,
                                DirectTurnRequestRow.turn_id == turn_id,
                            )
                        )
                        if turn_id
                        else None
                    )
                    if request is None:
                        continue
                row.status = status
                row.lifecycle_pending = True
                row.finished_at = datetime.now(UTC)
                settled.append(row)
            await db.commit()
        for row in settled:
            self._cancel_deadline("hook-timeout:" + row.run_id)
            await self.publish(row)

    async def scan(self) -> None:
        """Startup/slow recovery only: rediscover deadlines and missed execution signals."""
        active = aliased(AgentHookRunRow)
        blocked = and_(
            AgentHookRunRow.status == "pending",
            AgentHookRunRow.configuration["mode"].as_string() == "fork",
            exists(
                select(active.run_id).where(
                    active.fork_slot.is_not(None),
                    active.run_id != AgentHookRunRow.run_id,
                    active.owner_email == AgentHookRunRow.owner_email,
                    active.agent_id == AgentHookRunRow.agent_id,
                    active.conversation_id == AgentHookRunRow.conversation_id,
                )
            ),
        )
        ready = and_(
            AgentHookRunRow.status.in_(["preparing", "pending", "dispatching", "running"]),
            func.coalesce(AgentHookRunRow.handoff["execution"]["phase"].as_string(), "")
            != "terminal",
        )
        candidates = select(AgentHookRunRow).where(
            or_(
                AgentHookRunRow.status.in_(["preparing", "pending", "dispatching", "running"]),
                AgentHookRunRow.lifecycle_pending.is_(True),
                AgentHookRunRow.handoff["settlement_pending"].as_boolean().is_(True),
                AgentHookRunRow.fork_slot.is_not(None),
            ),
            not_(blocked),
        )
        # Owned executions must recover before idle evidence can exhaust the
        # maintenance pass. Admit newly matched occurrences in the same pass.
        # Independent rotating pages preserve fairness for all three groups.
        for group, predicate in (
            ("owned", and_(ready, AgentHookRunRow.fork_slot.is_not(None))),
            ("ready", and_(ready, AgentHookRunRow.fork_slot.is_(None))),
            ("waiting", not_(ready)),
        ):
            if group == "ready":
                await self._match_idle(recovery=True)
            async with self.session_factory() as db:
                query = candidates.where(predicate).order_by(AgentHookRunRow.run_id).limit(100)
                cursor = self._run_recovery_cursors[group]
                page = (await db.scalars(query.where(AgentHookRunRow.run_id > cursor))).all()
                if not page and cursor:
                    page = (await db.scalars(query)).all()
                self._run_recovery_cursors[group] = page[-1].run_id if page else ""
            for run in page:
                if self._started and internal_execution(run):
                    self._wake_run(run.run_id)
                else:
                    await self._run(run.run_id)

    async def _process_run(self, run: Any) -> None:
        try:
            timeout_origin = self._timeout_origin(run)
            if run.status in {"preparing", "pending"} and (
                run.configuration.get("trigger") == "conversation_idle"
            ):
                if not await self.idle_start_ready(run):
                    return
                async with self.session_factory() as db:
                    source = await get_conversation(db, run.conversation_id)
                    boundary = (
                        (source.context_data or {}).get("hook_idle_boundary", {}) if source else {}
                    )
                if boundary.get("ready_at") and not run.configuration.get("_manual"):
                    timeout_origin = max(
                        timeout_origin,
                        datetime.fromisoformat(boundary["ready_at"])
                        + timedelta(seconds=run.configuration["idle_seconds"]),
                    )
            if is_fork(run):
                if run.fork_slot and run.status not in {
                    "pending",
                    "preparing",
                    "dispatching",
                    "running",
                }:
                    with scoped_runtime_context(
                        user_email=run.owner_email,
                        agent_id=run.agent_id,
                        agent_owner_email=run.owner_email,
                    ):
                        await self.handoffs.settle(run, run.status)
                    return
                if run.status == "pending":
                    await self._dispatch(run)
                    return
                if run.status == "running":
                    await self._reconcile(run)
                    return
            if (
                run.status in {"preparing", "pending", "dispatching", "running"}
                and not (
                    run.status in {"preparing", "pending"}
                    and run.configuration.get("action", "prompt") == "prompt"
                    and run.configuration.get("mode") in {"fork", "fresh"}
                )
                and (datetime.now(UTC) - timeout_origin).total_seconds()
                >= run.configuration["timeout_seconds"]
            ):
                if run.child_conversation_id or run.task_id:
                    await self._reconcile(run)
                else:
                    await self._finish(run, "timed_out")
                return
            if run.lifecycle_pending and run.status not in {"pending", "preparing"}:
                await self.publish(run)
            if run.status == "pending":
                await self._dispatch(run)
            elif run.status == "preparing":
                async with self.session_factory() as db:
                    source_session = await get_session_row(db, run.source_session_id)
                    if run.configuration.get("_manual") is True:
                        assignment = await db.get(
                            AgentHookAssignmentRow, (run.agent_id, run.hook_id)
                        )
                        eligible = assignment is not None
                    elif run.occurrence.startswith("idle:"):
                        assignment = await db.get(
                            AgentHookAssignmentRow, (run.agent_id, run.hook_id)
                        )
                        source = await get_conversation(db, run.conversation_id)
                        origin = await self._idle_origin(db, source) if source else None
                        eligible = (
                            assignment is not None
                            and assignment.enabled
                            and assignment.activated_at is not None
                            and origin is not None
                            and origin[2] > _aware(assignment.activated_at)
                        )
                    else:
                        eligible = True
                if not eligible:
                    await self._finish(run, "skipped")
                    return
                with scoped_runtime_context(
                    user_email=run.owner_email,
                    agent_id=run.agent_id,
                    agent_owner_email=run.owner_email,
                ):
                    marker = await self._source_snapshot(run, source_session)
                recovered = {}
                if is_fork(run) and source_session is not None:
                    target = source_session.intaris_session_id or source_session.session_id
                    recovered = {
                        "handoff": {
                            **(run.handoff or {}),
                            "snapshot": {"session_id": target, "seq": marker["seq"]},
                            "boundary": {"session_id": target, "seq": marker["seq"] - 1},
                        }
                    }
                async with self.session_factory() as db:
                    await db.execute(
                        update(AgentHookRunRow)
                        .where(AgentHookRunRow.run_id == run.run_id)
                        .values(status="pending", **recovered)
                    )
                    await db.commit()
                run.status = "pending"
                if recovered:
                    run.handoff = recovered["handoff"]
                await self._dispatch(run)
            elif run.status == "dispatching":
                await self._recover_dispatch(run)
            elif run.status == "running":
                await self._reconcile(run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Hook dispatch failed",
                extra={
                    "hook_run_id": run.run_id,
                    "error_type": type(exc).__name__,
                },
            )
            async with self.session_factory() as db:
                persisted = await db.get(AgentHookRunRow, run.run_id)
            if persisted is None:
                return
            # Admission may have committed before its caller raised. References are
            # durable recovery ownership, not evidence that submission failed.
            if persisted.status == "running" or (
                persisted.status == "dispatching"
                and (persisted.task_id or persisted.child_conversation_id)
            ):
                if datetime.now(UTC) - _aware(persisted.created_at) > timedelta(
                    seconds=persisted.configuration["timeout_seconds"]
                ):
                    await self._reconcile(persisted)
                return
            await self._finish(persisted, "failed")

    async def _idle_origin(
        self, db: Any, conversation: Any
    ) -> tuple[str, datetime, datetime] | None:
        """Foreground admission is stable even when automation changes last_message_at."""
        request = await db.scalar(
            select(DirectTurnRequestRow)
            .where(
                DirectTurnRequestRow.conversation_id == conversation.conversation_id,
                foreground_admission_clause(),
                DirectTurnRequestRow.status != "absorbed",
            )
            .order_by(DirectTurnRequestRow.admission_order.desc())
            .limit(1)
        )
        if request is not None:
            activity = _aware(
                request.terminal_at or conversation.last_message_at or request.created_at
            )
            context_data = getattr(conversation, "context_data", None)
            if context_data is None:
                source = await get_conversation(db, conversation.conversation_id)
                context_data = source.context_data if source is not None else {}
            boundary = (context_data or {}).get("hook_idle_boundary", {})
            if boundary.get("turn_id") == request.turn_id and boundary.get("ready_at"):
                activity = max(activity, datetime.fromisoformat(boundary["ready_at"]))
            return (
                "idle:" + request.turn_id,
                activity,
                _aware(request.created_at),
            )
        # last_message_at can be changed by automation; it cannot prove new foreground work.
        return None

    async def _on_attached_change(self, event: Event) -> None:
        cid = event.data.get("conversation_id")
        if not isinstance(cid, str):
            return
        async with self.session_factory() as db:
            source = await get_conversation(db, cid)
        if source is not None:
            self._cancel_deadline("hook-idle:" + cid)
            await self._track_idle_readiness(source, restart=True)
            await self._match_idle(cid)

    async def _track_idle_readiness(self, conversation: Any, *, restart: bool = False) -> bool:
        """Persist the transition from attached work to genuine idle across restarts."""
        busy = await self.has_attached_work(conversation)
        async with self.session_factory() as db:
            # Serialize the context update with other writers without replacing
            # unrelated conversation metadata from our earlier detached snapshot.
            source = await db.scalar(
                update(Conversation)
                .where(
                    Conversation.conversation_id == conversation.conversation_id,
                )
                .values(context_data=Conversation.context_data)
                .returning(Conversation)
            )
            if source is None:
                return False
            origin = await self._idle_origin(db, source)
            if origin is None:
                return False
            turn_id = origin[0].removeprefix("idle:")
            previous = (source.context_data or {}).get("hook_idle_boundary", {})
            boundary = previous
            if busy:
                boundary = {"turn_id": turn_id, "ready_at": None}
            elif restart or (
                previous.get("turn_id") == turn_id and previous.get("ready_at") is None
            ):
                boundary = {"turn_id": turn_id, "ready_at": datetime.now(UTC).isoformat()}
            if boundary != previous:
                source.context_data = {
                    **(source.context_data or {}),
                    "hook_idle_boundary": boundary,
                }
            conversation.context_data = dict(source.context_data or {})
            await db.commit()
        if busy:
            self._cancel_deadline("hook-idle:" + conversation.conversation_id)
        return not busy

    @staticmethod
    def _idle_candidates() -> Select[tuple[Conversation]]:
        latest = (
            select(func.max(DirectTurnRequestRow.admission_order))
            .where(
                DirectTurnRequestRow.conversation_id == Conversation.conversation_id,
                foreground_admission_clause(),
                DirectTurnRequestRow.status != "absorbed",
            )
            .correlate(Conversation)
            .scalar_subquery()
        )
        handled = (
            select(AgentHookRunRow.run_id)
            .where(
                AgentHookRunRow.conversation_id == Conversation.conversation_id,
                AgentHookRunRow.hook_id == AgentHookAssignmentRow.hook_id,
                AgentHookRunRow.occurrence == "idle:" + DirectTurnRequestRow.turn_id,
            )
            .correlate(Conversation, AgentHookAssignmentRow, DirectTurnRequestRow)
            .exists()
        )
        foreground = (
            select(DirectTurnRequestRow.request_id)
            .where(
                DirectTurnRequestRow.conversation_id == Conversation.conversation_id,
                foreground_admission_clause(),
                DirectTurnRequestRow.status != "absorbed",
                DirectTurnRequestRow.created_at > AgentHookAssignmentRow.activated_at,
                DirectTurnRequestRow.admission_order == latest,
                ~handled,
            )
            .correlate(Conversation, AgentHookAssignmentRow)
            .exists()
        )
        assignments = (
            select(AgentHookAssignmentRow.agent_id)
            .outerjoin(AgentHookRow, AgentHookRow.hook_id == AgentHookAssignmentRow.hook_id)
            .where(
                AgentHookAssignmentRow.agent_id == Conversation.agent_id,
                AgentHookAssignmentRow.enabled.is_(True),
                foreground,
                or_(
                    AgentHookAssignmentRow.hook_id.in_(
                        [
                            hook_id
                            for hook_id, definition in BUNDLED_HOOKS.items()
                            if definition.trigger == "conversation_idle"
                        ]
                    ),
                    AgentHookRow.definition["trigger"].as_string() == "conversation_idle",
                ),
            )
            .correlate(Conversation)
            .exists()
        )
        return select(Conversation).where(Conversation.status == "active", assignments)

    async def _match_idle(
        self,
        conversation_id: str | None = None,
        *,
        finishing_turn_id: str | None = None,
        finished_at: datetime | None = None,
        recovery: bool = False,
    ) -> None:
        """Match one due conversation or rebuild future timers during recovery."""
        now = datetime.now(UTC)
        if conversation_id is None:
            cursor = self._idle_recovery_cursor if recovery else ""
            while not self._stop.is_set():
                async with self.session_factory() as db:
                    ids = (
                        await db.scalars(
                            self._idle_candidates()
                            .with_only_columns(Conversation.conversation_id)
                            .where(Conversation.conversation_id > cursor)
                            .order_by(Conversation.conversation_id)
                            .limit(IDLE_PAGE_SIZE)
                        )
                    ).all()
                if not ids:
                    if recovery:
                        self._idle_recovery_cursor = ""
                    return
                for cid in ids:
                    await self._match_idle(cid)
                    cursor = cid
                    if recovery:
                        # Advance only after completion: cancellation retries the
                        # incomplete candidate, not the whole false/busy prefix.
                        self._idle_recovery_cursor = cid
            return
        async with self.session_factory() as db:
            query = self._idle_candidates()
            if conversation_id:
                query = query.where(Conversation.conversation_id == conversation_id)
            candidates = (await db.scalars(query)).all()
        for row in candidates:
            if (row.context_data or {}).get("hook_run_id") or row.context_type == "task":
                continue
            if not await self._track_idle_readiness(row):
                continue
            async with self.session_factory() as db:
                agent = await get_agent(db, row.agent_id)
                session_row = await get_session_row(db, row.active_session_id)
                if agent is None or agent.owner_email != row.user_email or session_row is None:
                    continue
                assignments = (
                    await db.scalars(
                        select(AgentHookAssignmentRow).where(
                            AgentHookAssignmentRow.agent_id == row.agent_id,
                            AgentHookAssignmentRow.enabled.is_(True),
                        )
                    )
                ).all()
                origin = await self._idle_origin(db, row)
                if origin is None:
                    continue
                occurrence, activity, admitted_at = origin
                if finishing_turn_id and occurrence == "idle:" + finishing_turn_id and finished_at:
                    activity = finished_at
                deadlines = []
                for assignment in assignments:
                    definition, _ = await require_hook(db, row.user_email, assignment.hook_id)
                    if definition.trigger != "conversation_idle":
                        continue
                    if assignment.activated_at is None or admitted_at <= _aware(
                        assignment.activated_at
                    ):
                        continue
                    run_id = self._run_id(assignment.hook_id, row.conversation_id, occurrence)
                    if await db.get(AgentHookRunRow, run_id) is None:
                        deadlines.append(activity + timedelta(seconds=definition.idle_seconds))
            future = [deadline for deadline in deadlines if deadline > now]
            if future:
                cid = row.conversation_id
                self._deadline(
                    "hook-idle:" + cid,
                    min(future),
                    partial(self._match_idle, cid),
                )
            due = any(deadline <= now for deadline in deadlines)
            # TURN_COMPLETED is emitted before durable scheduler settlement. Future
            # deadlines must be armed even while that finishing turn still owns the row.
            if due and (
                await self.scheduler.durable_running_turn_state(row.conversation_id) is None
                and not await self.scheduler.get_queued_messages(row.conversation_id)
            ):
                idle_seconds = int((now - activity).total_seconds())
                await self.match(
                    conversation=_to_conversation_model(row),
                    session=_to_session_model(session_row),
                    triggers=["conversation_idle"],
                    occurrence=occurrence,
                    context={
                        "conversation": {"id": row.conversation_id, "type": row.context_type},
                        "agent": {"id": row.agent_id},
                        "idle": {"seconds": idle_seconds},
                        "turn": {"index": 0},
                        "cycle": {"index": 0},
                    },
                )

    async def _dispatch(self, run: Any) -> None:
        with scoped_runtime_context(
            user_email=run.owner_email,
            agent_id=run.agent_id,
            agent_owner_email=run.owner_email,
        ):
            await self._dispatch_scoped(run)

    async def _load_prompt_execution(self, run: Any) -> tuple[Any, Any, Any, HookDefinition]:
        async with self.session_factory() as db:
            source = await get_conversation(db, run.conversation_id)
            source_session = await get_session_row(db, run.source_session_id)
            agent_row = await get_agent(db, run.agent_id)
        if source is None or source_session is None or agent_row is None:
            raise HookError("Hook source is unavailable")
        if (
            agent_row.owner_email != run.owner_email
            or source.user_email != run.owner_email
            or source.agent_id != run.agent_id
            or source_session.conversation_id != run.conversation_id
        ):
            raise HookError("Hook source ownership changed")
        snapshot = await self._source_snapshot(run, source_session)
        definition = HookDefinition.model_validate(snapshot["data"]["input"])
        agent = await self.agent_registry.get(run.agent_id)
        if agent is None:
            raise HookError("Hook agent is unavailable")
        if run.hook_id == "system:self-improvement":
            validate_learning_tools(agent)
        return _to_session_model(source_session), _to_conversation_model(source), agent, definition

    @staticmethod
    def _prompt_envelope(run: Any, definition: HookDefinition, prompt: str) -> str:
        return hook_envelope(
            hook_id=run.hook_id,
            run_id=run.run_id,
            name=definition.name,
            prompt=prompt,
        )

    async def _dispatch_scoped(self, run: Any, *, recovering: bool = False) -> None:
        async with self.session_factory() as db:
            source = await get_conversation(db, run.conversation_id)
        if (
            run.configuration.get("trigger") == "conversation_idle"
            and source is not None
            and await self.has_attached_work(source)
        ):
            return
        busy = run.occurrence.startswith("idle:") and (
            await self.scheduler.durable_running_turn_state(run.conversation_id) is not None
            or bool(await self.scheduler.get_queued_messages(run.conversation_id))
        )
        async with self.session_factory() as db:
            # A write lock works on both SQLite and PostgreSQL. Activation changes
            # and the claim are ordered by this same assignment row; no external
            # I/O is held under the lock. A successful claim owns dispatch thereafter.
            assignment = await db.scalar(
                update(AgentHookAssignmentRow)
                .where(
                    AgentHookAssignmentRow.agent_id == run.agent_id,
                    AgentHookAssignmentRow.hook_id == run.hook_id,
                )
                .values(enabled=AgentHookAssignmentRow.enabled)
                .returning(AgentHookAssignmentRow)
            )
            # The run is the owner of manual intent. Read it under a write lock
            # in the claim transaction, never from the caller's stale snapshot.
            run = await db.scalar(
                update(AgentHookRunRow)
                .where(AgentHookRunRow.run_id == run.run_id)
                .values(status=AgentHookRunRow.status)
                .returning(AgentHookRunRow)
            )
            if run.status != ("dispatching" if recovering else "pending"):
                return
            manual = run.configuration.get("_manual") is True
            idle = run.occurrence.startswith("idle:") and not manual
            source = await get_conversation(db, run.conversation_id)
            idle_origin = (
                await self._idle_origin(db, source) if source is not None and idle else None
            )
            eligible = assignment is not None and (assignment.enabled or manual)
            if idle and idle_origin is not None and source is not None:
                boundary = (source.context_data or {}).get("hook_idle_boundary", {})
                if boundary.get("ready_at") and (
                    datetime.fromisoformat(boundary["ready_at"])
                    + timedelta(seconds=run.configuration["idle_seconds"])
                ) > datetime.now(UTC):
                    return
            if idle and busy:
                return
            if idle:
                eligible = (
                    eligible
                    and not busy
                    and idle_origin is not None
                    and assignment is not None
                    and assignment.activated_at is not None
                    and idle_origin[2] > _aware(assignment.activated_at)
                )
            values: dict[str, Any] = {
                "status": "dispatching" if eligible else "skipped",
                "lifecycle_pending": True,
            }
            boundary = (source.context_data or {}).get("hook_idle_boundary", {}) if source else {}
            if (
                eligible
                and boundary.get("ready_at")
                and (datetime.fromisoformat(boundary["ready_at"]) > _aware(run.created_at))
                and not run.configuration.get("_execution_started_at")
            ):
                # Waiting for genuine idle is not execution time. Persist this
                # once so recovery, timeout scheduling and task cancellation agree.
                run.configuration = {
                    **run.configuration,
                    "_execution_started_at": datetime.now(UTC).isoformat(),
                }
                values["configuration"] = run.configuration
            detached = run.configuration.get(
                "action", "prompt"
            ) == "prompt" and run.configuration.get("mode") in {"fork", "fresh"}
            if eligible and (is_fork(run) or detached):
                # Different assignments share one conversation execution slot.
                # Serialize claims before checking the unique slot on both DBs.
                await db.execute(
                    update(Conversation)
                    .where(Conversation.conversation_id == run.conversation_id)
                    .values(context_data=Conversation.context_data)
                )
                current = await db.get(AgentHookRunRow, run.run_id)
                handoff = await pin_claim(db, current)
                if handoff is None:
                    return
                values.update(fork_slot=slot(run), handoff=handoff)
                # Already-admitted legacy runs keep their original execution lane.
                if detached and not run.child_conversation_id:
                    values["handoff"] = {
                        **handoff,
                        "execution": {
                            "kind": "hook",
                            "version": 2,
                            "phase": "reserved",
                            "session_id": "sess_hook_" + run.run_id.removeprefix("hrun_"),
                            "started_at": datetime.now(UTC).isoformat(),
                            "deadline_at": (
                                datetime.now(UTC)
                                + timedelta(seconds=run.configuration["timeout_seconds"])
                            ).isoformat(),
                        },
                    }
            if not eligible:
                values["finished_at"] = datetime.now(UTC)
            result = await db.execute(
                update(AgentHookRunRow)
                .where(
                    AgentHookRunRow.run_id == run.run_id,
                    AgentHookRunRow.status == ("dispatching" if recovering else "pending"),
                )
                .values(**values)
            )
            await db.commit()
        if result.rowcount != 1:
            return
        run.status = values["status"]
        if "handoff" in values:
            run.handoff, run.fork_slot = values["handoff"], values["fork_slot"]
        if not eligible:
            await self.publish(run)
            return
        if not await self.publish(run):
            return
        if internal_execution(run):
            if self._started:
                self._wake_run(run.run_id)
            else:
                await self.subsessions.run(run.run_id)
            return
        async with self.session_factory() as db:
            source = await get_conversation(db, run.conversation_id)
            source_session = await get_session_row(db, run.source_session_id)
            agent_row = await get_agent(db, run.agent_id)
        if source is None or source_session is None or agent_row is None:
            raise HookError("Hook source is unavailable")
        if agent_row.owner_email != run.owner_email or source.user_email != run.owner_email:
            raise HookError("Hook source ownership changed")
        config = {key: value for key, value in run.configuration.items() if key != "_input_event"}
        if run.configuration.get("_input_event"):
            snapshot = await self._source_snapshot(run, source_session)
            config = snapshot["data"].get("input")
            if not isinstance(config, dict):
                raise HookError("Hook rendered input is unavailable")
        definition = HookDefinition.model_validate(config)
        agent = await self.agent_registry.get(run.agent_id)
        if agent is None:
            raise HookError("Hook agent is unavailable")
        if run.hook_id == "system:self-improvement":
            validate_learning_tools(agent)
        context = ConversationContext(
            type="web",
            platform_data={"hook_run_id": run.run_id},
            memory_labels=dict(_to_conversation_model(source).context.memory_labels),
        )
        if definition.action == "tool_call":
            assert definition.tool_call is not None
            workflow_id = "hook_workflow_" + run.run_id.removeprefix("hrun_")
            workflow = Workflow.model_validate(
                {
                    "workflow_id": workflow_id,
                    "owner_email": run.owner_email,
                    "name": definition.name,
                    "lifecycle": "ephemeral",
                    "steps": [
                        {
                            "name": "execute",
                            "type": "tool_call",
                            "tool_call": definition.tool_call.model_dump(mode="json"),
                        }
                    ],
                }
            )
            if await self.workflow_registry.get(workflow_id) is None:
                await self.workflow_registry.create(workflow)
            from cognis.models.task import TaskDelivery

            run.task_id = "hook_task_" + run.run_id.removeprefix("hrun_")
            async with self.session_factory() as db:
                await db.execute(
                    update(AgentHookRunRow)
                    .where(
                        AgentHookRunRow.run_id == run.run_id,
                    )
                    .values(task_id=run.task_id)
                )
                await db.commit()
            task = await self.task_queue.submit(
                task_id=run.task_id,
                created_by=run.owner_email,
                agent_id=run.agent_id,
                title=definition.name,
                source_type="hook",
                source_ref=run.run_id,
                workflow_id=workflow_id,
                delivery=TaskDelivery(mode="silent"),
            )
            run.task_id = task.task_id
        else:
            run.child_conversation_id = run.child_conversation_id or (
                run.conversation_id
                if definition.mode == "follow_up"
                else HOOK_CHILD_PREFIX + run.run_id.removeprefix("hrun_")
            )
            async with self.session_factory() as db:
                await db.execute(
                    update(AgentHookRunRow)
                    .where(
                        AgentHookRunRow.run_id == run.run_id,
                    )
                    .values(child_conversation_id=run.child_conversation_id)
                )
                await db.commit()
                child = await get_conversation(db, run.child_conversation_id)
                child_session = (
                    await get_session_row(db, child.active_session_id)
                    if child and child.active_session_id
                    else None
                )
            if child is not None:
                conversation = _to_conversation_model(child)
                if definition.mode == "fork":
                    if child_session is None:
                        raise HookError("Incomplete hook fork")
                    history = await read_complete_history(
                        self.providers.guardrails,
                        child_session.intaris_session_id or child_session.session_id,
                    )
                    if not any(
                        event.get("type") == "lifecycle"
                        and (event.get("data", {}).get("event") == "history_copy_completed")
                        for event in history.events
                    ):
                        raise HookError("Incomplete hook fork; refusing execution")
            elif definition.mode == "fork":
                boundary = await self._source_boundary(run, source_session)
                conversation, _, _ = await self.session_manager.fork_into_new_conversation(
                    source_session=_to_session_model(source_session),
                    source_conversation=_to_conversation_model(source),
                    agent=agent,
                    user_email=run.owner_email,
                    title=definition.name,
                    context=context,
                    conversation_id=run.child_conversation_id,
                    max_source_seq=boundary,
                )
            else:
                conversation, _ = await self.session_manager.create_conversation_with_root_session(
                    user_email=run.owner_email,
                    agent_id=run.agent_id,
                    context=context,
                    title=definition.name,
                    conversation_id=run.child_conversation_id,
                )
            run.child_conversation_id = conversation.conversation_id

            async def link_admission(db: Any, request: Any, created: bool) -> None:
                await db.execute(
                    update(AgentHookRunRow)
                    .where(
                        AgentHookRunRow.run_id == run.run_id,
                    )
                    .values(
                        status="running",
                        lifecycle_pending=True,
                        child_conversation_id=run.child_conversation_id,
                    )
                )

            from cognis.core.followups import (
                FollowUpMode,
                FollowUpOriginKind,
                FollowUpRelevanceHint,
                FollowUpRequiredAction,
                FollowUpStatus,
                OtherFollowUp,
            )

            prompt = definition.prompt
            if is_fork(run):
                prompt += await self.handoffs.input(run, source_session)
            error = await self.scheduler.submit_turn(
                conversation.conversation_id,
                hook_envelope(
                    name=definition.name,
                    hook_id=run.hook_id,
                    run_id=run.run_id,
                    prompt=prompt,
                ),
                user_email=run.owner_email,
                system_initiated=True,
                allow_queue=definition.mode == "follow_up",
                follow_up=OtherFollowUp(
                    follow_up_id="hook_" + run.run_id.removeprefix("hrun_")[:48],
                    mode=FollowUpMode.INTEGRATE,
                    origin_kind=FollowUpOriginKind.OTHER,
                    relevance_hint=FollowUpRelevanceHint.SAME_THREAD,
                    required_action=FollowUpRequiredAction.OTHER,
                    status=FollowUpStatus.COMPLETED,
                    title=f"Hook: {definition.name}",
                    summary="Automated hook instructions, not a user request.",
                    metadata={"hook_run_id": run.run_id, "hook_id": run.hook_id},
                ),
                idempotency_scope="hook",
                idempotency_key=run.run_id,
                admission_transaction_participant=link_admission,
            )
            if error is not None:
                raise RuntimeError("Hook child turn could not be admitted")
        async with self.session_factory() as db:
            await db.execute(
                update(AgentHookRunRow)
                .where(
                    AgentHookRunRow.run_id == run.run_id,
                    AgentHookRunRow.status == "dispatching",
                )
                .values(status="running", task_id=run.task_id, lifecycle_pending=True)
            )
            await db.commit()
        run.status = "running"
        # The child may have finished before submit_turn returned.
        async with self.session_factory() as db:
            current = await db.get(AgentHookRunRow, run.run_id)
        await self.publish(current)

    async def _recover_dispatch(self, run: Any) -> None:
        async with self.session_factory() as db:
            task = await db.get(Task, run.task_id) if run.task_id else None
            request = await db.scalar(
                select(DirectTurnRequestRow).where(
                    DirectTurnRequestRow.idempotency_scope == "hook",
                    DirectTurnRequestRow.idempotency_key == run.run_id,
                )
            )
            admitted = task is not None or request is not None
            if admitted:
                if request is not None:
                    run.child_conversation_id = request.conversation_id
                result = await db.execute(
                    update(AgentHookRunRow)
                    .where(
                        AgentHookRunRow.run_id == run.run_id,
                        AgentHookRunRow.status == "dispatching",
                    )
                    .values(
                        status="running",
                        lifecycle_pending=True,
                        child_conversation_id=run.child_conversation_id,
                    )
                )
                await db.commit()
        if admitted:
            if result.rowcount == 1:
                run.status = "running"
                await self.publish(run)
                await self._reconcile(run)
            return
        with scoped_runtime_context(
            user_email=run.owner_email,
            agent_id=run.agent_id,
            agent_owner_email=run.owner_email,
        ):
            await self._dispatch_scoped(run, recovering=True)

    async def _reconcile(self, run: Any) -> None:
        if is_fork(run):
            claimed = (run.handoff or {}).get("claimed_at")
            age = (
                datetime.now(UTC)
                - (datetime.fromisoformat(claimed) if claimed else self._timeout_origin(run))
            ).total_seconds()
            with scoped_runtime_context(
                user_email=run.owner_email, agent_id=run.agent_id, agent_owner_email=run.owner_email
            ):
                await self.handoffs.settle(
                    run,
                    "timed_out" if age > run.configuration["timeout_seconds"] else None,
                )
            return
        async with self.session_factory() as db:
            if run.task_id:
                task = await db.get(Task, run.task_id)
                status = task.status if task else "failed"
            elif run.child_conversation_id:
                request = await db.scalar(
                    select(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.conversation_id == run.child_conversation_id,
                        DirectTurnRequestRow.idempotency_scope == "hook",
                        DirectTurnRequestRow.idempotency_key == run.run_id,
                    )
                    .order_by(DirectTurnRequestRow.created_at.desc())
                    .limit(1)
                )
                status = request.status if request else "running"
            else:
                status = "failed"
        if status in {"completed", "failed", "cancelled", "absorbed"}:
            if status == "absorbed":
                status = "completed"
            await self._finish(run, status)
        elif datetime.now(UTC) - self._timeout_origin(run) > timedelta(
            seconds=run.configuration["timeout_seconds"]
        ):
            if run.task_id:
                await self.task_queue.cancel_task(run.task_id)
            elif run.configuration.get("mode") == "follow_up":
                if request is not None:
                    await self.scheduler.cancel_request(run.conversation_id, request.request_id)
            elif run.child_conversation_id:
                await self.scheduler.cancel_turn(run.child_conversation_id, clear_queue=False)
            await self._finish(run, "timed_out")

    async def _finish(self, run: Any, status: str) -> None:
        if is_fork(run) and run.fork_slot:
            with scoped_runtime_context(
                user_email=run.owner_email, agent_id=run.agent_id, agent_owner_email=run.owner_email
            ):
                await self.handoffs.settle(run, status)
            return
        self._cancel_deadline("hook-timeout:" + run.run_id)
        async with self.session_factory() as db:
            await db.execute(
                update(AgentHookRunRow)
                .where(
                    AgentHookRunRow.run_id == run.run_id,
                )
                .values(status=status, finished_at=datetime.now(UTC), lifecycle_pending=True)
            )
            await db.commit()
        run.status = status
        await self.publish(run)

    async def publish(self, run: Any) -> bool:
        """Publish presentation-only lifecycle state to the source session."""
        try:
            with scoped_runtime_context(
                user_email=run.owner_email,
                agent_id=run.agent_id,
                agent_owner_email=run.owner_email,
            ):
                return await self._publish(run)
        except Exception as exc:
            logger.warning(
                "Hook lifecycle delivery deferred",
                extra={
                    "hook_run_id": run.run_id,
                    "error_type": type(exc).__name__,
                },
            )
            return False

    async def _publish(self, run: Any) -> bool:
        async with self.session_factory() as db:
            conversation = await get_conversation(db, run.conversation_id)
            if (
                conversation is None
                or conversation.user_email != run.owner_email
                or conversation.agent_id != run.agent_id
            ):
                return False
            source = await get_session_row(
                db, conversation.active_session_id or run.source_session_id
            )
        if source is None:
            return False
        if (
            source.conversation_id != run.conversation_id
            or source.user_email != run.owner_email
            or source.agent_id != run.agent_id
            or source.parent_session_id is not None
        ):
            return False
        session = _to_session_model(source)
        target_session_id = session.intaris_session_id or session.session_id
        outcome_data: dict[str, Any] = {}
        execution = internal_execution(run) or {}
        if execution.get("failure_reason"):
            outcome_data = {
                "outcome": (
                    f"Execution failed ({execution['failure_reason']}). "
                    "This execution will not retry. Later eligible hooks can run normally."
                ),
            }
        if (run.handoff or {}).get("outcome"):
            outcome = await self.handoffs.read(run.handoff["outcome"], run.run_id, "hook_outcome")
            if outcome is None:
                return False
            outcome_data = {
                "outcome": outcome["text"],
                "outcome_partial": outcome["partial"],
                "outcome_available": outcome["response_available"],
                "result_anchors": outcome.get("result_anchors", []),
                "result_truncated": outcome.get("result_truncated", False),
                "result_original_length": outcome.get("result_original_length"),
            }
        event = SessionEvent(
            type="lifecycle",
            data={
                **outcome_data,
                "event": "agent_hook",
                "hook_run_id": run.run_id,
                "hook_id": run.hook_id,
                "name": run.configuration["name"],
                "trigger": run.configuration["trigger"],
                "status": (
                    "retrying"
                    if execution.get("phase") == "retry_wait"
                    else "blocked"
                    if execution.get("phase") == "blocked"
                    else run.status
                ),
                "child_conversation_id": run.child_conversation_id,
                "child_session_id": (
                    execution.get("physical_session_id") or execution.get("session_id")
                )
                if execution.get("context_ready")
                else None,
                "task_id": run.task_id,
            },
        )
        result = await self.providers.guardrails.record_events(
            target_session_id,
            [event],
            source="cognis_hooks",
            idempotency_key=(
                f"{target_session_id}:{run.run_id}:{run.status}:"
                f"{run.child_conversation_id or ''}:{run.task_id or ''}"
                + (f":{execution.get('transition', 0)}" if execution else "")
            ),
            user_email=run.owner_email,
            agent_id=run.agent_id,
        )
        if not result.ok:
            return False
        await self.session_cache.append_recorded_events(session, [event], result)
        signals = getattr(self.scheduler, "cluster_signals", None)
        if signals:
            await signals.publish_chat_change(
                run.conversation_id,
                session_id=session.session_id,
                revision=result.last_seq,
            )
        async with self.session_factory() as db:
            await db.execute(
                update(AgentHookRunRow)
                .where(
                    AgentHookRunRow.run_id == run.run_id,
                    AgentHookRunRow.status == run.status,
                    (
                        AgentHookRunRow.handoff["execution"]["transition"].as_integer()
                        == execution["transition"]
                        if "transition" in execution
                        else true()
                    ),
                )
                .values(lifecycle_pending=False)
            )
            await db.commit()
        return True
