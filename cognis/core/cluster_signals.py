"""Best-effort PostgreSQL cluster invalidation signals.

Signals contain only bounded control-plane pointers. Canonical state remains in
PostgreSQL and the configured session event store; delivery is healed by the
periodic subscribed-scope reconciliation loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import random
from collections import OrderedDict
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.runtime_context import scoped_runtime_context
from cognis.store.models import (
    Agent,
    Conversation,
    DirectTurnRequestRow,
    ExecutorRow,
    NotificationRow,
    Session,
    StepRun,
    Task,
    WorkScopeState,
)

logger = get_logger(__name__)

CHANNEL = "cognis_cluster_v1"
MAX_PAYLOAD_BYTES = 2048
MAX_PENDING_SIGNALS = 512
MAX_DEDUP_ENTRIES = 2048
MAX_RECONCILE_SCOPES = 256
MAX_RECONCILE_CONCURRENCY = 8
RECONCILE_SCOPE_TIMEOUT_SECONDS = 3.0
MAX_SIGNAL_REVISION_LENGTH = 160


def _bounded_revision(revision: str | int | datetime) -> str:
    value = revision.isoformat() if isinstance(revision, datetime) else str(revision)
    if len(value) <= MAX_SIGNAL_REVISION_LENGTH:
        return value
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


class ClusterSignalKind(StrEnum):
    CHAT_SCOPE_CHANGED = "chat_scope_changed"
    TASK_PROGRESS_CHANGED = "task_progress_changed"
    NOTIFICATION_STATE_CHANGED = "notification_state_changed"
    EXECUTOR_STATE_CHANGED = "executor_state_changed"
    SIDEBAR_CHANGED = "sidebar_changed"
    EVENT_STORE_SESSION_INVALIDATED = "event_store_session_invalidated"
    WORK_INVALIDATED = "work_invalidated"
    TURN_CANCEL_REQUESTED = "turn_cancel_requested"


class ClusterEventStoreId(StrEnum):
    INTARIS = "intaris"


class ClusterSignalScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, max_length=160)
    session_id: str | None = Field(default=None, max_length=160)
    task_id: str | None = Field(default=None, max_length=160)
    step_run_id: str | None = Field(default=None, max_length=160)
    notification_id: str | None = Field(default=None, max_length=160)
    executor_id: str | None = Field(default=None, max_length=160)
    owner_token: str | None = Field(default=None, min_length=32, max_length=64)
    event_store_id: ClusterEventStoreId | None = None
    event_session_token: str | None = Field(default=None, min_length=64, max_length=64)
    work_scope_key: str | None = Field(default=None, min_length=1, max_length=320)
    direct_request_id: str | None = Field(default=None, min_length=1, max_length=160)


class ClusterSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    kind: ClusterSignalKind
    origin_controller_id: str = Field(min_length=1, max_length=160)
    scope: ClusterSignalScope
    revision: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def _validate_event_store_scope(self) -> ClusterSignal:
        event_scope = (self.scope.event_store_id, self.scope.event_session_token)
        if self.kind == ClusterSignalKind.EVENT_STORE_SESSION_INVALIDATED:
            if any(value is None for value in event_scope):
                raise ValueError("Event-store invalidation requires store and session token")
            if any(
                (
                    self.scope.conversation_id,
                    self.scope.session_id,
                    self.scope.task_id,
                    self.scope.step_run_id,
                    self.scope.notification_id,
                    self.scope.executor_id,
                    self.scope.owner_token,
                    self.scope.work_scope_key,
                    self.scope.direct_request_id,
                )
            ):
                raise ValueError("Event-store invalidation scope must be identity-free")
        elif any(value is not None for value in event_scope):
            raise ValueError("Event-store scope is reserved for event-store invalidation")
        return self

    def encoded(self) -> str:
        payload = self.model_dump_json(exclude_none=True)
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("Cluster signal exceeds 2 KiB")
        return payload


class ClusterSignalTransport(Protocol):
    async def listen(self, channel: str, callback: Any) -> Any: ...

    async def publish(self, channel: str, payload: str) -> None: ...

    async def close(self) -> None: ...


class AsyncpgClusterSignalTransport:
    """Dedicated asyncpg LISTEN connection plus short-lived publishers."""

    def __init__(self, database_url: str) -> None:
        url = make_url(database_url)
        self._dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
        self._connection: Any = None
        self._publisher_connection: Any = None
        self._publisher_lock = asyncio.Lock()

    async def listen(self, channel: str, callback: Any) -> Any:
        import asyncpg

        await self.close()
        self._connection = await asyncpg.connect(self._dsn)

        def _received(_connection: Any, _pid: int, _channel: str, payload: str) -> None:
            callback(payload)

        await self._connection.add_listener(channel, _received)
        return self._connection

    async def publish(self, channel: str, payload: str) -> None:
        import asyncpg

        async with self._publisher_lock:
            if self._publisher_connection is None or self._publisher_connection.is_closed():
                self._publisher_connection = await asyncpg.connect(self._dsn)
            try:
                await self._publisher_connection.execute(
                    "SELECT pg_notify($1, $2)", channel, payload
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await self._publisher_connection.close()
                self._publisher_connection = None
                raise

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()
        publisher, self._publisher_connection = self._publisher_connection, None
        if publisher is not None:
            await publisher.close()


class ClusterSignalService:
    """Publish, receive, coalesce, and reconcile selective cluster invalidations."""

    def __init__(
        self,
        *,
        database_url: str,
        controller_id: str,
        session_factory: Any,
        event_bus: EventBus,
        scope_provider: Any,
        transport: ClusterSignalTransport | None = None,
        reconcile_interval_seconds: float = 15.0,
        enabled: bool = True,
        event_store: Any = None,
        owner_token_secret: str = "",
    ) -> None:
        self.enabled = enabled and database_url.startswith(
            ("postgresql://", "postgresql+asyncpg://")
        )
        self.controller_id = controller_id
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._scope_provider = scope_provider
        self._event_store = event_store
        self._owner_token_secret = owner_token_secret.encode("utf-8")
        self._transport = transport or (
            AsyncpgClusterSignalTransport(database_url) if self.enabled else None
        )
        self._reconcile_interval_seconds = max(1.0, reconcile_interval_seconds)
        self._pending: asyncio.Queue[ClusterSignal] = asyncio.Queue(MAX_PENDING_SIGNALS)
        self._dedup: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._watermarks: dict[str, str] = {}
        self._reconcile_offset = 0
        self._listener_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._reconcile_now = asyncio.Event()

    def owner_token(self, user_email: str) -> str:
        """Return a non-reversible stable routing token for one principal."""
        if not self._owner_token_secret:
            raise RuntimeError("Cluster owner token secret is not configured")
        return hmac.new(
            self._owner_token_secret,
            user_email.strip().lower().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def owner_token_matches(self, user_email: str, token: str) -> bool:
        return hmac.compare_digest(self.owner_token(user_email), token)

    async def start(self) -> None:
        if not self.enabled or self._listener_task is not None:
            return
        self._stopping.clear()
        self._listener_task = asyncio.create_task(
            self._listen_loop(), name="cluster-signal-listener"
        )
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name="cluster-signal-dispatch"
        )
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="cluster-signal-reconcile"
        )

    async def begin_drain(self) -> None:
        self._stopping.set()

    async def stop(self) -> None:
        self._stopping.set()
        tasks = [
            task
            for task in (self._listener_task, self._dispatch_task, self._reconcile_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._listener_task = self._dispatch_task = self._reconcile_task = None
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.close()

    async def publish(
        self,
        kind: ClusterSignalKind,
        *,
        scope: ClusterSignalScope,
        revision: str | int | datetime,
    ) -> bool:
        if not self.enabled or self._transport is None:
            return True
        signal = ClusterSignal(
            kind=kind,
            origin_controller_id=self.controller_id,
            scope=scope,
            revision=_bounded_revision(revision),
        )
        payload = signal.encoded()
        try:
            async with asyncio.timeout(2.0):
                await self._transport.publish(CHANNEL, payload)
            return True
        except Exception:
            logger.warning(
                "cluster_signals: publish failed; reconciliation will heal",
                extra={"extra_data": {"kind": kind}},
                exc_info=True,
            )
            return False

    async def publish_task_change(self, task_id: str, *, step_run_id: str | None = None) -> None:
        """Resolve a committed task pointer and publish its authoritative watermark."""
        if not self.enabled:
            return
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            step = await session.get(StepRun, step_run_id) if step_run_id else None
        if task is None:
            return
        await self.publish(
            ClusterSignalKind.TASK_PROGRESS_CHANGED,
            scope=ClusterSignalScope(
                conversation_id=getattr(step, "conversation_id", None),
                session_id=getattr(step, "session_id", None),
                task_id=task_id,
                step_run_id=step_run_id,
            ),
            revision=step.updated_at if step is not None else task.updated_at,
        )

    async def publish_chat_change(
        self,
        conversation_id: str,
        *,
        session_id: str | None = None,
        revision: str | int | datetime | None = None,
    ) -> None:
        """Publish a committed conversation/session invalidation pointer."""
        if not self.enabled:
            return
        async with self._session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return
        await self.publish(
            ClusterSignalKind.CHAT_SCOPE_CHANGED,
            scope=ClusterSignalScope(
                conversation_id=conversation_id,
                session_id=session_id or conversation.active_session_id,
            ),
            revision=revision or conversation.updated_at,
        )
        await self.publish(
            ClusterSignalKind.SIDEBAR_CHANGED,
            scope=ClusterSignalScope(
                conversation_id=conversation_id,
                session_id=session_id or conversation.active_session_id,
            ),
            revision=revision or conversation.updated_at,
        )

    async def publish_executor_change(self, executor_id: str) -> None:
        """Publish a committed executor state/version invalidation pointer."""
        if not self.enabled:
            return
        async with self._session_factory() as session:
            executor = await session.get(ExecutorRow, executor_id)
        if executor is None:
            return
        await self.publish(
            ClusterSignalKind.EXECUTOR_STATE_CHANGED,
            scope=ClusterSignalScope(
                executor_id=executor_id,
                owner_token=self.owner_token(executor.owner_email),
            ),
            revision=(
                f"{executor.updated_at.isoformat()}:"
                f"{executor.desired_config_version}:{executor.applied_config_version}"
            ),
        )

    async def publish_event_store_invalidation(
        self,
        *,
        store_id: ClusterEventStoreId,
        session_token: str,
        revision: str | int | datetime,
    ) -> bool:
        """Publish one identity-free canonical event-cache invalidation."""
        return await self.publish(
            ClusterSignalKind.EVENT_STORE_SESSION_INVALIDATED,
            scope=ClusterSignalScope(
                event_store_id=store_id,
                event_session_token=session_token,
            ),
            revision=revision,
        )

    async def publish_work_invalidation(
        self,
        *,
        scope_key: str,
        user_email: str,
        revision: int,
    ) -> bool:
        """Publish a durable Work revision locally and across controllers."""

        signal = ClusterSignal(
            kind=ClusterSignalKind.WORK_INVALIDATED,
            origin_controller_id=self.controller_id,
            scope=ClusterSignalScope(
                owner_token=self.owner_token(user_email),
                work_scope_key=scope_key,
            ),
            revision=str(revision),
        )
        await self._emit_local(signal)
        if not self.enabled or self._transport is None:
            return True
        try:
            async with asyncio.timeout(2.0):
                await self._transport.publish(CHANNEL, signal.encoded())
            return True
        except Exception:
            logger.warning(
                "cluster_signals: Work invalidation publish failed; reconciliation will heal",
                exc_info=True,
            )
            return False

    def receive_payload(self, payload: str) -> None:
        """Validate and enqueue an untrusted NOTIFY payload without logging it."""
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            return
        try:
            signal = ClusterSignal.model_validate_json(payload)
        except (ValidationError, ValueError):
            return
        if signal.origin_controller_id == self.controller_id:
            return
        key = (signal.kind, signal.scope.model_dump_json(exclude_none=True), signal.revision)
        if key in self._dedup:
            self._dedup.move_to_end(key)
            return
        self._dedup[key] = None
        while len(self._dedup) > MAX_DEDUP_ENTRIES:
            self._dedup.popitem(last=False)
        try:
            self._pending.put_nowait(signal)
        except asyncio.QueueFull:
            self._reconcile_now.set()

    async def reconcile_once(self) -> None:
        scopes: list[tuple[ClusterSignalScope, str | None]] = []
        for raw in self._scope_provider():
            raw_scope = dict(raw)
            owner_email = raw_scope.pop("user_email", None)
            scopes.append(
                (
                    ClusterSignalScope.model_validate(raw_scope),
                    owner_email if isinstance(owner_email, str) else None,
                )
            )
        all_keys = {scope.model_dump_json(exclude_none=True) for scope, _owner_email in scopes}
        if len(scopes) > MAX_RECONCILE_SCOPES:
            start = self._reconcile_offset % len(scopes)
            end = start + MAX_RECONCILE_SCOPES
            selected = (scopes + scopes)[start:end]
            self._reconcile_offset = end % len(scopes)
        else:
            selected = scopes
            self._reconcile_offset = 0
        semaphore = asyncio.Semaphore(MAX_RECONCILE_CONCURRENCY)

        async def _read(
            scope: ClusterSignalScope, owner_email: str | None
        ) -> tuple[ClusterSignalScope, str | None, str | None]:
            try:
                async with asyncio.timeout(RECONCILE_SCOPE_TIMEOUT_SECONDS):
                    async with semaphore:
                        return scope, owner_email, await self._scope_watermark(scope, owner_email)
            except Exception:
                return scope, owner_email, None

        results = await asyncio.gather(
            *(_read(scope, owner_email) for scope, owner_email in selected)
        )
        failed_reads = sum(watermark is None for _scope, _owner, watermark in results)
        if failed_reads:
            logger.warning(
                "cluster_signals: scope reconciliation reads failed",
                extra={"extra_data": {"failed": failed_reads, "total": len(results)}},
            )
        for scope, _owner_email, watermark in results:
            if watermark is None:
                continue
            key = scope.model_dump_json(exclude_none=True)
            previous = self._watermarks.get(key)
            self._watermarks[key] = watermark
            if previous is None or watermark != previous:
                kind = (
                    ClusterSignalKind.WORK_INVALIDATED
                    if scope.work_scope_key
                    else ClusterSignalKind.SIDEBAR_CHANGED
                    if not any(
                        (
                            scope.conversation_id,
                            scope.session_id,
                            scope.task_id,
                            scope.step_run_id,
                            scope.notification_id,
                            scope.executor_id,
                        )
                    )
                    else ClusterSignalKind.CHAT_SCOPE_CHANGED
                )
                await self._emit_local(
                    ClusterSignal(
                        kind=kind,
                        origin_controller_id="reconciliation",
                        scope=scope,
                        revision=_bounded_revision(watermark),
                    )
                )
        self._watermarks = {
            key: value for key, value in self._watermarks.items() if key in all_keys
        }

    async def _listen_loop(self) -> None:
        assert self._transport is not None
        failures = 0
        while not self._stopping.is_set():
            try:
                connection = await self._transport.listen(CHANNEL, self.receive_payload)
                failures = 0
                self._reconcile_now.set()
                while not self._stopping.is_set() and not connection.is_closed():
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.warning(
                    "cluster_signals: listener disconnected",
                    extra={"extra_data": {"attempt": failures}},
                    exc_info=True,
                )
            if not self._stopping.is_set():
                delay = min(30.0, 0.5 * (2 ** min(failures, 6))) + random.random() * 0.25
                await asyncio.sleep(delay)

    async def _dispatch_loop(self) -> None:
        while not self._stopping.is_set():
            signal = await self._pending.get()
            try:
                await self._emit_local(signal)
            finally:
                self._pending.task_done()

    async def _reconcile_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._reconcile_now.wait(), timeout=self._reconcile_interval_seconds
                )
                self._reconcile_now.clear()
            except TimeoutError:
                pass
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("cluster_signals: reconciliation failed", exc_info=True)

    async def _emit_local(self, signal: ClusterSignal) -> None:
        await self._event_bus.publish(
            Event(
                type=EventType.CLUSTER_SCOPE_INVALIDATED,
                data={
                    "kind": signal.kind,
                    "scope": signal.scope.model_dump(exclude_none=True),
                    "revision": signal.revision,
                },
            )
        )

    async def _scope_watermark(
        self, scope: ClusterSignalScope, owner_email: str | None = None
    ) -> str:
        values: list[str] = []
        event_store_read: tuple[str, str, str, str] | None = None
        trailing_values: list[str] = []
        async with self._session_factory() as session:
            if owner_email and not any(
                (
                    scope.conversation_id,
                    scope.session_id,
                    scope.task_id,
                    scope.step_run_id,
                    scope.notification_id,
                    scope.executor_id,
                )
            ):
                sidebar_mark = await session.execute(
                    select(
                        func.max(Conversation.updated_at),
                        func.count(Conversation.conversation_id),
                    ).where(Conversation.user_email == owner_email)
                )
                executor_mark = await session.execute(
                    select(
                        func.max(ExecutorRow.updated_at),
                        func.count(ExecutorRow.executor_id),
                    ).where(ExecutorRow.owner_email == owner_email)
                )
                values.extend(str(value or "") for value in sidebar_mark.one())
                values.extend(str(value or "") for value in executor_mark.one())
            if scope.conversation_id:
                conversation = await session.get(Conversation, scope.conversation_id)
                if conversation is not None:
                    values.extend(
                        [
                            conversation.updated_at.isoformat(),
                            str(conversation.active_executor_generation),
                            str(conversation.active_session_id or ""),
                        ]
                    )
                notification_mark = await session.execute(
                    select(
                        func.max(NotificationRow.created_at),
                        func.max(NotificationRow.resolved_at),
                        func.count(NotificationRow.notification_id),
                    ).where(NotificationRow.conversation_id == scope.conversation_id)
                )
                values.extend(str(value or "") for value in notification_mark.one())
                direct_turn_mark = await session.execute(
                    select(
                        func.max(DirectTurnRequestRow.updated_at),
                        func.count(DirectTurnRequestRow.request_id),
                    ).where(DirectTurnRequestRow.conversation_id == scope.conversation_id)
                )
                values.extend(str(value or "") for value in direct_turn_mark.one())
            if scope.task_id:
                task = await session.get(Task, scope.task_id)
                if task is not None:
                    values.extend(
                        [
                            task.updated_at.isoformat(),
                            task.status,
                            str(task.active_executor_generation),
                        ]
                    )
            if scope.step_run_id:
                step = await session.get(StepRun, scope.step_run_id)
                if step is not None:
                    values.extend([step.updated_at.isoformat(), step.status])
            if scope.session_id and self._event_store is not None:
                session_row = await session.get(Session, scope.session_id)
                event_store_session_id = (
                    session_row.intaris_session_id if session_row is not None else None
                )
                if event_store_session_id:
                    agent = await session.get(Agent, session_row.agent_id)
                    system_agent = SYSTEM_AGENTS.get(session_row.agent_id)
                    agent_owner_email = (
                        agent.owner_email
                        if agent is not None
                        else system_agent.owner_email
                        if system_agent is not None
                        else None
                    )
                    if agent_owner_email is None:
                        raise RuntimeError("Session agent is unavailable for event-store read")
                    event_store_read = (
                        event_store_session_id,
                        session_row.user_email,
                        session_row.agent_id,
                        agent_owner_email,
                    )
            if scope.work_scope_key:
                work_scope = await session.get(WorkScopeState, scope.work_scope_key)
                if work_scope is not None and (
                    owner_email is None or work_scope.user_email == owner_email
                ):
                    trailing_values.extend(
                        [
                            f"work:{work_scope.work_revision}",
                            f"graph:{work_scope.graph_revision}",
                            f"fingerprint:{work_scope.graph_fingerprint or ''}",
                        ]
                    )
        if event_store_read is not None:
            event_store_session_id, user_email, agent_id, agent_owner_email = event_store_read
            with scoped_runtime_context(
                user_email=user_email,
                agent_id=agent_id,
                agent_owner_email=agent_owner_email,
            ):
                event_store = self._event_store
                bind = getattr(event_store, "bind", None)
                if callable(bind):
                    event_store = bind(
                        EventStoreAuthority(
                            user_email=user_email,
                            agent_id=agent_id,
                            agent_owner_email=agent_owner_email,
                        )
                    )
                watermark = await event_store.read_session_high_watermark(
                    session_id=event_store_session_id
                )
            values.append(f"event_store:{watermark.last_seq}")
        values.extend(trailing_values)
        return json.dumps(values, separators=(",", ":"))
