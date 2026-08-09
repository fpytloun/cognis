"""Lifecycle policy for persisted active executor pins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cognis.core.executor_pool import ExecutorPool, ResolvedExecutorTarget
from cognis.logging import get_logger

logger = get_logger(__name__)

DEFAULT_SECONDARY_ASSIGNMENT_TTL_SECONDS = 3600
DEFAULT_SECONDARY_DISCONNECT_RETRY_SECONDS = 15
DEFAULT_SECONDARY_DISCONNECT_RETRY_INTERVAL_SECONDS = 3
CANONICAL_PIN_SOURCES = frozenset({"selector_primary", "explicit_primary", "additional"})


def normalize_active_executor_source(
    source: str | None,
    *,
    expires_at: datetime | None,
    execution: dict[str, Any] | None,
) -> str | None:
    """Normalize persisted pin provenance without consulting the resolved pool."""
    if source in CANONICAL_PIN_SOURCES:
        return source
    if source in {"additional_explicit", "additional"}:
        return "additional"
    if source in {"selector", "auto_failover"}:
        return "selector_primary"
    if source == "explicit":
        return "explicit_primary"
    if source in {None, "initial", "default"}:
        if expires_at is not None:
            return "additional"
        execution = execution or {}
        if execution.get("executor_id"):
            return "explicit_primary"
        selector = execution.get("executor_selector")
        if isinstance(selector, dict) and selector:
            return "selector_primary"
        return None
    if source in {"user_switch", "agent_switch"}:
        if expires_at is not None:
            return "additional"
        return "explicit_primary"
    return source


FALLBACK_UI_MESSAGE_TEMPLATE = (
    "Executor '{previous_executor_id}' is no longer available ({reason}). "
    "Switched active executor to selector-matching primary '{new_executor_id}'. "
    "No in-flight, accepted-unknown, or partially delivered operation was replayed."
)
FALLBACK_LLM_MESSAGE_TEMPLATE = (
    "The previously active non-primary executor '{previous_executor_id}' is no longer "
    "available ({reason}). The controller switched this session back to primary executor "
    "'{new_executor_id}'. Do not assume files, tools, working directory, or local state "
    "from '{previous_executor_id}' are available unless explicitly switched again. "
    "No in-flight, accepted-unknown, or partially delivered operation was replayed."
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
    transient_unavailable: bool = False
    retry_after_seconds: int | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _coerce_positive_int(value: object, default: int) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        coerced = int(value)
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


def _target_ready(target: ResolvedExecutorTarget, ws_provider: Any | None) -> bool:
    """Return whether a usable target also has a ready transport."""

    if not target.usable:
        return False
    if target.executor_type not in {"websocket", "subprocess"} or ws_provider is None:
        return True
    connection = ws_provider.get_connection(target.executor_id)
    if connection is None or not getattr(
        connection, "connected", getattr(connection, "present", True)
    ):
        return False
    handle = getattr(ws_provider, "get_handle", lambda _id: None)(target.executor_id)
    return handle is None or getattr(handle, "status", "ready") == "ready"


async def ensure_active_executor_pin(
    *,
    session_factory: Any,
    conversation_id: str | None,
    task_id: str | None,
    pool: ExecutorPool,
    active_executor_id: str | None,
    active_executor_expires_at: datetime | None,
    active_executor_generation: int = 0,
    active_executor_unavailable_since: datetime | None = None,
    active_executor_source: str | None = None,
    execution: dict[str, Any] | None = None,
    ws_provider: Any = None,
    retry_seconds: int = DEFAULT_SECONDARY_DISCONNECT_RETRY_SECONDS,
    retry_interval_seconds: int = DEFAULT_SECONDARY_DISCONNECT_RETRY_INTERVAL_SECONDS,
    notice_dispatcher: Any = None,
    canonicalization_session: Any = None,
    now: datetime | None = None,
) -> ExecutorPinLifecycleResult:
    """Perform admission-only selector-primary failover.

    Explicit primary pins are immutable here. Existing additional-to-primary
    expiry/disconnect fallback remains admission-only. In-flight runtimes never
    call this function, so an accepted/partial operation cannot be replayed.
    """

    source = normalize_active_executor_source(
        active_executor_source, expires_at=active_executor_expires_at, execution=execution
    )
    if not active_executor_id:
        return ExecutorPinLifecycleResult(active_executor_id=None)
    if source in CANONICAL_PIN_SOURCES and active_executor_source in {None, "initial"}:
        from cognis.store.queries import canonicalize_executor_pin_source

        if canonicalization_session is not None:
            await canonicalize_executor_pin_source(
                canonicalization_session,
                conversation_id=conversation_id,
                task_id=task_id,
                expected_executor_id=active_executor_id,
                expected_generation=active_executor_generation,
                source=source,
            )
            await canonicalization_session.commit()
        else:
            async with session_factory() as session:
                await canonicalize_executor_pin_source(
                    session,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    expected_executor_id=active_executor_id,
                    expected_generation=active_executor_generation,
                    source=source,
                )
                await session.commit()
    if source not in {"selector_primary", "additional"}:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)

    target = pool.by_id(active_executor_id)
    if target is None:
        current_time = now or _now()
        additional_expired = source == "additional" and (
            active_executor_expires_at is None
            or _pin_expired(active_executor_expires_at, now=current_time)
        )
        reason = (
            "secondary assignment has no expiry"
            if source == "additional" and active_executor_expires_at is None
            else "secondary assignment expired"
            if additional_expired
            else "executor is missing from the current assigned pool"
        )
        if not additional_expired and active_executor_unavailable_since is None:
            from cognis.store.queries import mark_executor_unavailable

            async with session_factory() as session:
                await mark_executor_unavailable(
                    session,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    expected_executor_id=active_executor_id,
                    expected_generation=active_executor_generation,
                    observed_at=current_time,
                )
                await session.commit()
            return ExecutorPinLifecycleResult(
                active_executor_id=active_executor_id,
                transient_unavailable=True,
                retry_after_seconds=retry_interval_seconds,
            )
        if (
            not additional_expired
            and active_executor_unavailable_since is not None
            and (current_time - active_executor_unavailable_since).total_seconds() < retry_seconds
        ):
            return ExecutorPinLifecycleResult(
                active_executor_id=active_executor_id,
                transient_unavailable=True,
                retry_after_seconds=retry_interval_seconds,
            )
    elif source == "explicit_primary":
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)

    current_time = now or _now()
    connection_ready = target is not None and _target_ready(target, ws_provider)
    transport_unavailable = target is not None and target.usable and not connection_ready
    additional_expired = source == "additional" and (
        active_executor_expires_at is None
        or _pin_expired(active_executor_expires_at, now=current_time)
    )
    if connection_ready and not additional_expired:
        if active_executor_unavailable_since is not None:
            from cognis.store.queries import clear_executor_unavailable

            async with session_factory() as session:
                await clear_executor_unavailable(
                    session,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    expected_executor_id=active_executor_id,
                    expected_generation=active_executor_generation,
                )
                await session.commit()
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    if source == "additional" and active_executor_expires_at is None:
        reason = "secondary assignment has no expiry"
    elif active_executor_expires_at is not None and _pin_expired(
        active_executor_expires_at, now=current_time
    ):
        reason = "secondary assignment expired"
    elif target is None:
        reason = "executor is missing from the current assigned pool"
    elif transport_unavailable:
        reason = "executor transport disconnected or not ready"
    else:
        reason = f"executor state is {target.state.value}"
    if not additional_expired and active_executor_unavailable_since is None:
        from cognis.store.queries import mark_executor_unavailable

        async with session_factory() as session:
            await mark_executor_unavailable(
                session,
                conversation_id=conversation_id,
                task_id=task_id,
                expected_executor_id=active_executor_id,
                expected_generation=active_executor_generation,
                observed_at=current_time,
            )
            await session.commit()
        return ExecutorPinLifecycleResult(
            active_executor_id=active_executor_id,
            transient_unavailable=True,
            retry_after_seconds=retry_interval_seconds,
        )
    if (
        not additional_expired
        and active_executor_unavailable_since is not None
        and (current_time - active_executor_unavailable_since).total_seconds() < retry_seconds
    ):
        return ExecutorPinLifecycleResult(
            active_executor_id=active_executor_id,
            transient_unavailable=True,
            retry_after_seconds=retry_interval_seconds,
        )
    current_selector = execution is None or (
        isinstance(execution.get("executor_selector"), dict)
        and bool(execution.get("executor_selector"))
    )
    fallback_source = (
        "selector_primary"
        if source == "selector_primary" and current_selector
        else "explicit_primary"
        if source == "additional" and execution and execution.get("executor_id")
        else "selector_primary"
        if source == "additional"
        and execution
        and isinstance(execution.get("executor_selector"), dict)
        and execution.get("executor_selector")
        else None
    )
    if source == "selector_primary" and not current_selector:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    if source == "additional" and fallback_source is None:
        return ExecutorPinLifecycleResult(active_executor_id=active_executor_id)
    candidates = [
        candidate
        for candidate in pool.usable_primaries()
        if candidate.executor_id != active_executor_id
        and _target_ready(candidate, ws_provider)
        and (
            (source == "selector_primary" and candidate.selection_source == "selector")
            or (fallback_source == "selector_primary" and candidate.selection_source == "selector")
            or (fallback_source == "explicit_primary" and candidate.selection_source != "selector")
        )
    ]
    fallback = sorted(candidates, key=lambda item: item.executor_id)[0] if candidates else None
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

    from cognis.store.queries import cas_executor_failover

    async with session_factory() as session:
        persisted, _, _ = await cas_executor_failover(
            session,
            conversation_id=conversation_id,
            task_id=task_id,
            expected_executor_id=active_executor_id,
            new_executor_id=fallback.executor_id,
            expected_generation=active_executor_generation,
            reason=reason,
            failover_source=fallback_source or "selector_primary",
        )
        if persisted:
            await session.commit()
    if not persisted:
        from cognis.store.queries import get_conversation, get_task

        try:
            async with session_factory() as session:
                authoritative = (
                    await get_task(session, task_id)
                    if task_id
                    else (
                        await get_conversation(session, conversation_id)
                        if conversation_id
                        else None
                    )
                )
        except AttributeError:
            authoritative = None
        winner = getattr(authoritative, "active_executor_id", None) or active_executor_id
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
        return ExecutorPinLifecycleResult(active_executor_id=winner)
    if notice_dispatcher is not None:
        await notice_dispatcher.dispatch_pending(limit=10)
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
