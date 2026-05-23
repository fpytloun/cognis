"""Lifecycle policy for persisted active executor pins."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cognis.core.executor_pool import ExecutorPool, ResolvedExecutorTarget, pick_initial_active
from cognis.logging import get_logger

logger = get_logger(__name__)

DEFAULT_SECONDARY_ASSIGNMENT_TTL_SECONDS = 3600
DEFAULT_SECONDARY_DISCONNECT_RETRY_SECONDS = 15
DEFAULT_SECONDARY_DISCONNECT_RETRY_INTERVAL_SECONDS = 3

FALLBACK_UI_MESSAGE_TEMPLATE = (
    "Executor '{previous_executor_id}' is no longer available ({reason}). "
    "Switched active executor back to primary '{new_executor_id}'."
)
FALLBACK_LLM_MESSAGE_TEMPLATE = (
    "The previously active non-primary executor '{previous_executor_id}' is no longer "
    "available ({reason}). The controller switched this session back to primary executor "
    "'{new_executor_id}'. Do not assume files, tools, working directory, or local state "
    "from '{previous_executor_id}' are available unless explicitly switched again."
)


@dataclass(frozen=True)
class ExecutorPinFallbackNotice:
    """User- and model-facing notice emitted after automatic fallback."""

    previous_executor_id: str
    new_executor_id: str
    reason: str
    ui_message: str
    llm_message: str


@dataclass(frozen=True)
class ExecutorPinLifecycleResult:
    """Result of validating a persisted active-executor pin."""

    active_executor_id: str | None
    notice: ExecutorPinFallbackNotice | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _coerce_positive_int(value: object, default: int) -> int:
    try:
        coerced = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default


async def load_executor_pin_lifecycle_settings(session_factory: Any) -> dict[str, int]:
    """Load active-executor lifecycle settings from the DB."""

    from cognis.store.queries import get_setting_value

    async with session_factory() as session:
        ttl = await get_setting_value(
            session,
            "executors.secondary_assignment_ttl_seconds",
            DEFAULT_SECONDARY_ASSIGNMENT_TTL_SECONDS,
        )
        retry_seconds = await get_setting_value(
            session,
            "executors.secondary_disconnect_retry_seconds",
            DEFAULT_SECONDARY_DISCONNECT_RETRY_SECONDS,
        )
        retry_interval = await get_setting_value(
            session,
            "executors.secondary_disconnect_retry_interval_seconds",
            DEFAULT_SECONDARY_DISCONNECT_RETRY_INTERVAL_SECONDS,
        )
    return {
        "ttl_seconds": _coerce_positive_int(ttl, DEFAULT_SECONDARY_ASSIGNMENT_TTL_SECONDS),
        "retry_seconds": _coerce_positive_int(
            retry_seconds, DEFAULT_SECONDARY_DISCONNECT_RETRY_SECONDS
        ),
        "retry_interval_seconds": _coerce_positive_int(
            retry_interval, DEFAULT_SECONDARY_DISCONNECT_RETRY_INTERVAL_SECONDS
        ),
    }


def secondary_pin_expires_at(*, ttl_seconds: int, assigned_at: datetime | None = None) -> datetime:
    """Compute expiration time for a non-primary active executor pin."""

    return (assigned_at or _now()) + timedelta(seconds=ttl_seconds)


def _fallback_notice(
    *,
    previous_executor_id: str,
    new_executor_id: str,
    reason: str,
) -> ExecutorPinFallbackNotice:
    return ExecutorPinFallbackNotice(
        previous_executor_id=previous_executor_id,
        new_executor_id=new_executor_id,
        reason=reason,
        ui_message=FALLBACK_UI_MESSAGE_TEMPLATE.format(
            previous_executor_id=previous_executor_id,
            new_executor_id=new_executor_id,
            reason=reason,
        ),
        llm_message=FALLBACK_LLM_MESSAGE_TEMPLATE.format(
            previous_executor_id=previous_executor_id,
            new_executor_id=new_executor_id,
            reason=reason,
        ),
    )


def _pin_expired(expires_at: datetime | None, *, now: datetime) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


async def _wait_for_remote_ready(
    *,
    ws_provider: Any,
    target: ResolvedExecutorTarget,
    retry_seconds: int,
    retry_interval_seconds: int,
) -> bool:
    """Return whether a remote executor has a live connection after bounded retries."""

    if target.executor_type not in {"websocket", "subprocess"}:
        return True
    deadline = asyncio.get_running_loop().time() + retry_seconds
    interval = max(1, retry_interval_seconds)
    while True:
        conn = ws_provider.get_connection(target.executor_id) if ws_provider is not None else None
        if conn is not None:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(min(interval, max(0.0, deadline - asyncio.get_running_loop().time())))


async def _persist_fallback(
    *,
    session_factory: Any,
    conversation_id: str | None,
    task_id: str | None,
    executor_id: str,
    assigned_at: datetime,
) -> bool:
    from cognis.store.queries import set_conversation_active_executor, set_task_active_executor

    async with session_factory() as session:
        if conversation_id:
            ok = await set_conversation_active_executor(
                session,
                conversation_id,
                executor_id,
                assigned_at=assigned_at,
                expires_at=None,
                source="auto_fallback",
            )
            if not ok:
                return False
        if task_id:
            ok = await set_task_active_executor(
                session,
                task_id,
                executor_id,
                assigned_at=assigned_at,
                expires_at=None,
                source="auto_fallback",
            )
            if not ok:
                return False
        commit = getattr(session, "commit", None)
        if callable(commit):
            await commit()
    return True


async def ensure_active_executor_pin(
    *,
    session_factory: Any,
    conversation_id: str | None,
    task_id: str | None,
    pool: ExecutorPool,
    active_executor_id: str | None,
    active_executor_expires_at: datetime | None,
    ws_provider: Any = None,
    retry_seconds: int = DEFAULT_SECONDARY_DISCONNECT_RETRY_SECONDS,
    retry_interval_seconds: int = DEFAULT_SECONDARY_DISCONNECT_RETRY_INTERVAL_SECONDS,
    now: datetime | None = None,
) -> ExecutorPinLifecycleResult:
    """Validate an active executor pin and fall back from expired/broken secondary pins."""

    if not active_executor_id:
        return ExecutorPinLifecycleResult(active_executor_id=None)

    target = pool.by_id(active_executor_id)
    if target is None:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    if target.is_primary:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)

    reason: str | None = None
    current_time = now or _now()
    if active_executor_expires_at is None:
        reason = "secondary assignment has no expiry"
    elif _pin_expired(active_executor_expires_at, now=current_time):
        reason = "secondary assignment expired"
    elif not target.usable:
        reason = f"secondary executor state is {target.state.value}"
    else:
        ready = await _wait_for_remote_ready(
            ws_provider=ws_provider,
            target=target,
            retry_seconds=retry_seconds,
            retry_interval_seconds=retry_interval_seconds,
        )
        if not ready:
            reason = "secondary executor disconnected after retries"

    if reason is None:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)

    fallback = pick_initial_active(pool)
    if fallback is None or fallback.row is None:
        logger.warning(
            "executor_pin_lifecycle: no primary executor available for fallback",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "previous_executor_id": active_executor_id,
                    "reason": reason,
                }
            },
        )
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)

    persisted = await _persist_fallback(
        session_factory=session_factory,
        conversation_id=conversation_id,
        task_id=task_id,
        executor_id=fallback.executor_id,
        assigned_at=current_time,
    )
    if not persisted:
        logger.warning(
            "executor_pin_lifecycle: failed to persist primary fallback",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "previous_executor_id": active_executor_id,
                    "new_executor_id": fallback.executor_id,
                    "reason": reason,
                }
            },
        )
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    notice = _fallback_notice(
        previous_executor_id=active_executor_id,
        new_executor_id=fallback.executor_id,
        reason=reason,
    )
    logger.info(
        "executor_pin_lifecycle: switched back to primary executor",
        extra={
            "extra_data": {
                "conversation_id": conversation_id,
                "task_id": task_id,
                "previous_executor_id": active_executor_id,
                "new_executor_id": fallback.executor_id,
                "reason": reason,
            }
        },
    )
    return ExecutorPinLifecycleResult(active_executor_id=fallback.executor_id, notice=notice)


async def fallback_active_executor_after_remote_failure(
    *,
    session_factory: Any,
    conversation_id: str | None,
    task_id: str | None,
    pool: ExecutorPool | None,
    active_executor_id: str | None,
    reason: str,
    now: datetime | None = None,
) -> ExecutorPinLifecycleResult:
    """Fallback from a non-primary executor after a runtime operation failed."""

    if pool is None or not active_executor_id:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    target = pool.by_id(active_executor_id)
    if target is None or target.is_primary:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    fallback = pick_initial_active(pool)
    if fallback is None or fallback.row is None:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    current_time = now or _now()
    persisted = await _persist_fallback(
        session_factory=session_factory,
        conversation_id=conversation_id,
        task_id=task_id,
        executor_id=fallback.executor_id,
        assigned_at=current_time,
    )
    if not persisted:
        logger.warning(
            "executor_pin_lifecycle: failed to persist primary fallback after remote failure",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "previous_executor_id": active_executor_id,
                    "new_executor_id": fallback.executor_id,
                    "reason": reason,
                }
            },
        )
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    notice = _fallback_notice(
        previous_executor_id=active_executor_id,
        new_executor_id=fallback.executor_id,
        reason=reason,
    )
    return ExecutorPinLifecycleResult(active_executor_id=fallback.executor_id, notice=notice)
