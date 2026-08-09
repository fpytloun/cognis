"""Persistence helpers for declarative local-model desired state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.local_models import (
    sanitize_local_model_error,
    validate_local_model_operation_transition,
)
from cognis.models.local_models import LOCAL_MODEL_BYTE_COUNT_MAX, LocalModelOperationState
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.coordination import database_now_expression
from cognis.store.models import (
    CoordinationLeaseRow,
    ExecutorRow,
    LLMProvider,
    LocalModelDeployment,
    LocalModelOperation,
    LocalModelTargetStatus,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def list_visible_local_model_deployments(
    session: AsyncSession,
    *,
    owner_email: str,
) -> list[LocalModelDeployment]:
    """List caller-owned and shared-system deployments."""

    result = await session.execute(
        select(LocalModelDeployment)
        .where(
            or_(
                LocalModelDeployment.owner_email == owner_email,
                LocalModelDeployment.owner_email == SYSTEM_USER_EMAIL,
            )
        )
        .order_by(
            LocalModelDeployment.updated_at.desc(),
            LocalModelDeployment.deployment_id.asc(),
        )
    )
    return list(result.scalars().all())


async def list_local_model_deployments(
    session: AsyncSession,
    *,
    limit: int = 100,
    after_deployment_id: str | None = None,
) -> list[LocalModelDeployment]:
    """List a bounded reconciliation page in stable keyset order."""

    statement = select(LocalModelDeployment)
    if after_deployment_id is not None:
        statement = statement.where(LocalModelDeployment.deployment_id > after_deployment_id)
    result = await session.execute(
        statement.order_by(LocalModelDeployment.deployment_id.asc()).limit(max(1, min(limit, 1000)))
    )
    return list(result.scalars().all())


async def lock_local_model_dispatch_guard(session: AsyncSession) -> None:
    """Serialize desired-state writes with the short remote start handshake."""

    bind = session.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        if not session.in_transaction():
            await session.execute(text("BEGIN IMMEDIATE"))
        return
    if dialect == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('cognis_local_model_dispatch_guard'))")
        )


async def get_local_model_deployment(
    session: AsyncSession,
    deployment_id: str,
) -> LocalModelDeployment | None:
    """Get a deployment without applying caller visibility."""

    return await session.get(LocalModelDeployment, deployment_id)


async def get_visible_local_model_deployment(
    session: AsyncSession,
    deployment_id: str,
    *,
    owner_email: str,
) -> LocalModelDeployment | None:
    """Get a caller-owned or shared-system deployment."""

    result = await session.execute(
        select(LocalModelDeployment).where(
            LocalModelDeployment.deployment_id == deployment_id,
            or_(
                LocalModelDeployment.owner_email == owner_email,
                LocalModelDeployment.owner_email == SYSTEM_USER_EMAIL,
            ),
        )
    )
    return result.scalar_one_or_none()


async def lock_and_get_local_model_deployment(
    session: AsyncSession,
    deployment_id: str,
) -> LocalModelDeployment | None:
    """Serialize desired-state mutations across SQLite and PostgreSQL writers."""

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite" and not session.in_transaction():
        await session.execute(text("BEGIN IMMEDIATE"))
    statement = select(LocalModelDeployment).where(
        LocalModelDeployment.deployment_id == deployment_id
    )
    if dialect_name != "sqlite":
        statement = statement.with_for_update()
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def list_local_model_targets(
    session: AsyncSession,
    deployment_id: str,
) -> list[LocalModelTargetStatus]:
    """List concrete targets for one deployment."""

    result = await session.execute(
        select(LocalModelTargetStatus)
        .where(LocalModelTargetStatus.deployment_id == deployment_id)
        .order_by(LocalModelTargetStatus.executor_id.asc())
    )
    return list(result.scalars().all())


async def list_local_model_operations(
    session: AsyncSession,
    deployment_id: str,
) -> list[LocalModelOperation]:
    """List durable operations for one deployment, newest first."""

    result = await session.execute(
        select(LocalModelOperation)
        .where(LocalModelOperation.deployment_id == deployment_id)
        .order_by(
            LocalModelOperation.created_at.desc(),
            LocalModelOperation.operation_id.asc(),
        )
    )
    return list(result.scalars().all())


async def get_local_model_operation(
    session: AsyncSession,
    operation_id: str,
) -> LocalModelOperation | None:
    """Get one durable operation without applying caller visibility."""

    return await session.get(LocalModelOperation, operation_id)


async def list_active_local_model_operations(
    session: AsyncSession,
    *,
    executor_id: str | None = None,
) -> list[LocalModelOperation]:
    """List queued or in-flight operations for recovery and dispatch."""

    statement = select(LocalModelOperation).where(
        LocalModelOperation.state.in_(
            [
                LocalModelOperationState.QUEUED.value,
                LocalModelOperationState.RUNNING.value,
                LocalModelOperationState.CANCEL_REQUESTED.value,
                LocalModelOperationState.INTERRUPTED.value,
            ]
        )
    )
    if executor_id is not None:
        statement = statement.where(LocalModelOperation.executor_id == executor_id)
    result = await session.execute(
        statement.order_by(
            LocalModelOperation.created_at.asc(),
            LocalModelOperation.operation_id.asc(),
        )
    )
    return list(result.scalars().all())


async def list_active_executor_rows(session: AsyncSession) -> list[ExecutorRow]:
    """List active executor rows for selector authorization and resolution."""

    result = await session.execute(
        select(ExecutorRow)
        .where(ExecutorRow.status == "active")
        .order_by(ExecutorRow.executor_id.asc())
    )
    return list(result.scalars().all())


async def sync_local_model_targets(
    session: AsyncSession,
    deployment: LocalModelDeployment,
    executor_ids: list[str],
    *,
    requested_at: datetime | None = None,
) -> list[LocalModelTargetStatus]:
    """Materialize a selector result as the deployment's concrete target set."""

    existing = {
        row.executor_id: row
        for row in await list_local_model_targets(session, deployment.deployment_id)
    }
    selected = set(executor_ids)
    stale_target_ids = [
        row.target_id for executor_id, row in existing.items() if executor_id not in selected
    ]
    if stale_target_ids:
        await session.execute(
            delete(LocalModelTargetStatus).where(
                LocalModelTargetStatus.target_id.in_(stale_target_ids)
            )
        )

    targets: list[LocalModelTargetStatus] = []
    now = _utcnow()
    for executor_id in executor_ids:
        target = existing.get(executor_id)
        if target is None:
            target = LocalModelTargetStatus(
                target_id=f"lmt_{uuid.uuid4().hex}",
                deployment_id=deployment.deployment_id,
                executor_id=executor_id,
                generation=deployment.generation,
                observed_generation=0,
                state="pending",
                reconcile_requested_at=requested_at,
            )
            session.add(target)
        else:
            target.generation = deployment.generation
            if target.observed_generation < deployment.generation:
                target.state = "pending"
            if requested_at is not None:
                target.reconcile_requested_at = requested_at
            target.updated_at = now
        targets.append(target)
    await session.flush()
    return targets


async def create_local_model_operation(
    session: AsyncSession,
    *,
    deployment_id: str,
    executor_id: str,
    generation: int,
    action: str,
    idempotency_key: str,
    request_hash: str,
    post_pull_provider_upsert: bool = False,
) -> tuple[LocalModelOperation, bool]:
    """Create an idempotent future operation or return the matching existing row."""

    operation_id = f"lmo_{uuid.uuid4().hex}"
    values = {
        "operation_id": operation_id,
        "deployment_id": deployment_id,
        "executor_id": executor_id,
        "generation": generation,
        "action": action,
        "state": LocalModelOperationState.QUEUED.value,
        "progress_seq": 0,
        "progress_bytes": 0,
        "idempotency_key": idempotency_key,
        "request_hash": request_hash,
        "post_pull_provider_upsert": post_pull_provider_upsert,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    dialect_name = session.get_bind().dialect.name
    inserted = False
    if dialect_name in {"sqlite", "postgresql"}:
        insert_factory = sqlite_insert if dialect_name == "sqlite" else postgresql_insert
        statement = (
            insert_factory(LocalModelOperation)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["deployment_id", "idempotency_key"])
            .returning(LocalModelOperation.operation_id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none() is not None
    else:
        try:
            async with session.begin_nested():
                session.add(LocalModelOperation(**values))
                await session.flush()
            inserted = True
        except IntegrityError:
            inserted = False

    result = await session.execute(
        select(LocalModelOperation).where(
            LocalModelOperation.deployment_id == deployment_id,
            LocalModelOperation.idempotency_key == idempotency_key,
        )
    )
    operation = result.scalar_one()
    if operation.request_hash != request_hash:
        raise ValueError("idempotency key was already used with a different request")
    if bool(operation.post_pull_provider_upsert) != post_pull_provider_upsert:
        raise ValueError("idempotency key was already used with a different request")
    return operation, inserted


async def transition_local_model_operation(
    session: AsyncSession,
    operation_id: str,
    target_state: LocalModelOperationState,
    *,
    error: str | None = None,
) -> LocalModelOperation:
    """Apply a validated durable operation state transition."""

    for _attempt in range(3):
        operation = await session.get(
            LocalModelOperation,
            operation_id,
            populate_existing=True,
        )
        if operation is None:
            raise LookupError("local-model operation not found")
        current_state = LocalModelOperationState(operation.state)
        validate_local_model_operation_transition(current_state, target_state)
        if current_state == target_state:
            return operation

        now = _utcnow()
        values: dict[str, Any] = {
            "state": target_state.value,
            "updated_at": now,
            "sanitized_error": (
                sanitize_local_model_error(error)
                if target_state
                in {
                    LocalModelOperationState.FAILED,
                    LocalModelOperationState.INTERRUPTED,
                }
                else None
            ),
        }
        if target_state == LocalModelOperationState.RUNNING and operation.started_at is None:
            values["started_at"] = now
        if target_state == LocalModelOperationState.CANCEL_REQUESTED:
            values["cancel_requested_at"] = now
        if target_state in {
            LocalModelOperationState.SUCCEEDED,
            LocalModelOperationState.FAILED,
            LocalModelOperationState.CANCELLED,
        }:
            values["finished_at"] = now
        result = await session.execute(
            update(LocalModelOperation)
            .where(
                LocalModelOperation.operation_id == operation_id,
                LocalModelOperation.state == current_state.value,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            await session.refresh(operation)
            return operation
        session.expire(operation)
    raise RuntimeError("local-model operation transition lost repeated concurrent races")


async def record_local_model_operation_progress(
    session: AsyncSession,
    operation_id: str,
    *,
    progress_seq: int,
    progress_bytes: int,
    phase: str | None,
) -> tuple[LocalModelOperation, bool]:
    """Persist monotonic operation progress with sequence-level idempotency."""

    if (
        isinstance(progress_bytes, bool)
        or not isinstance(progress_bytes, int)
        or not 0 <= progress_bytes <= LOCAL_MODEL_BYTE_COUNT_MAX
    ):
        raise ValueError("progress_bytes must be between 0 and signed int64 maximum")

    for _attempt in range(3):
        operation = await session.get(
            LocalModelOperation,
            operation_id,
            populate_existing=True,
        )
        if operation is None:
            raise LookupError("local-model operation not found")
        if operation.state not in {
            LocalModelOperationState.RUNNING.value,
            LocalModelOperationState.CANCEL_REQUESTED.value,
        }:
            raise ValueError("progress is accepted only for running operations")
        if progress_seq < operation.progress_seq:
            return operation, False
        if progress_seq == operation.progress_seq:
            if progress_bytes != operation.progress_bytes or phase != operation.phase:
                raise ValueError("progress sequence was already used with different values")
            return operation, False
        if progress_bytes < operation.progress_bytes:
            raise ValueError("progress_bytes must be monotonic")

        result = await session.execute(
            update(LocalModelOperation)
            .where(
                LocalModelOperation.operation_id == operation_id,
                LocalModelOperation.state == operation.state,
                LocalModelOperation.progress_seq == operation.progress_seq,
                LocalModelOperation.progress_bytes == operation.progress_bytes,
            )
            .values(
                progress_seq=progress_seq,
                progress_bytes=progress_bytes,
                phase=phase,
                updated_at=_utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            await session.refresh(operation)
            return operation, True
        session.expire(operation)
    raise RuntimeError("local-model progress update lost repeated concurrent races")


async def update_local_model_target_status(
    session: AsyncSession,
    target_id: str,
    *,
    expected_generation: int,
    state: str,
    observed_generation: int | None = None,
    observed_digest: str | None = None,
    observed_size_bytes: int | None = None,
    current_operation_id: str | None = None,
    last_error: str | None = None,
) -> LocalModelTargetStatus | None:
    """Apply an observed target update behind the deployment generation fence."""

    if observed_size_bytes is not None and (
        isinstance(observed_size_bytes, bool)
        or not isinstance(observed_size_bytes, int)
        or not 0 <= observed_size_bytes <= LOCAL_MODEL_BYTE_COUNT_MAX
    ):
        raise ValueError("observed_size_bytes must be between 0 and signed int64 maximum")

    now = _utcnow()
    values: dict[str, Any] = {
        "state": state,
        "current_operation_id": current_operation_id,
        "last_error": sanitize_local_model_error(last_error),
        "updated_at": now,
    }
    if observed_generation is not None:
        values["observed_generation"] = observed_generation
    if state in {"ready", "absent"}:
        values["observed_digest"] = observed_digest
        values["observed_size_bytes"] = observed_size_bytes
        values["reconciled_at"] = now
    if state == "reconciling":
        values["reconcile_started_at"] = now
    result = await session.execute(
        update(LocalModelTargetStatus)
        .where(
            LocalModelTargetStatus.target_id == target_id,
            LocalModelTargetStatus.generation == expected_generation,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        return None
    return await session.get(
        LocalModelTargetStatus,
        target_id,
        populate_existing=True,
    )


async def list_local_model_delete_dependencies(
    session: AsyncSession,
    *,
    executor_id: str,
    runtime_name: str,
    exclude_deployment_id: str,
) -> list[str]:
    """Return present deployments that still reference a model on an executor."""

    result = await session.execute(
        select(LocalModelDeployment.deployment_id)
        .join(
            LocalModelTargetStatus,
            LocalModelTargetStatus.deployment_id == LocalModelDeployment.deployment_id,
        )
        .where(
            LocalModelTargetStatus.executor_id == executor_id,
            LocalModelDeployment.runtime_name == runtime_name,
            LocalModelDeployment.desired_state == "present",
            LocalModelDeployment.deployment_id != exclude_deployment_id,
        )
        .order_by(LocalModelDeployment.deployment_id.asc())
    )
    return [str(value) for value in result.scalars().all()]


async def interrupt_active_local_model_operations(
    session: AsyncSession,
    *,
    executor_id: str | None = None,
    reason: str,
) -> list[str]:
    """Move in-flight operations to interrupted during disconnect or restart."""

    operations = await list_active_local_model_operations(session, executor_id=executor_id)
    interrupted: list[str] = []
    for operation in operations:
        state = LocalModelOperationState(operation.state)
        if state != LocalModelOperationState.RUNNING:
            continue
        await transition_local_model_operation(
            session,
            operation.operation_id,
            LocalModelOperationState.INTERRUPTED,
            error=reason,
        )
        interrupted.append(operation.operation_id)
    return interrupted


async def interrupt_recoverable_local_model_operations(
    session: AsyncSession,
    *,
    controller_owner_id: str,
    reason: str,
) -> list[str]:
    """Interrupt running operations not protected by another live controller."""

    operations = await list_active_local_model_operations(session)
    interrupted: list[str] = []
    now = database_now_expression(session)
    for operation in operations:
        if operation.state != LocalModelOperationState.RUNNING.value:
            continue
        live_other_owner = exists(
            select(CoordinationLeaseRow.resource_key).where(
                CoordinationLeaseRow.resource_key == f"executor_connection:{operation.executor_id}",
                CoordinationLeaseRow.owner_id != controller_owner_id,
                CoordinationLeaseRow.lease_expires_at > now,
            )
        )
        result = await session.execute(
            update(LocalModelOperation)
            .where(
                LocalModelOperation.operation_id == operation.operation_id,
                LocalModelOperation.state == LocalModelOperationState.RUNNING.value,
                ~live_other_owner,
            )
            .values(
                state=LocalModelOperationState.INTERRUPTED.value,
                sanitized_error=sanitize_local_model_error(reason),
                updated_at=_utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            interrupted.append(operation.operation_id)
    return interrupted


async def lock_and_get_llm_provider(
    session: AsyncSession,
    provider_id: str,
) -> LLMProvider | None:
    """Acquire a provider-row write lock with an SQLite-compatible strategy."""

    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        if not session.in_transaction():
            await session.execute(text("BEGIN IMMEDIATE"))
        return await session.get(LLMProvider, provider_id)
    result = await session.execute(
        select(LLMProvider).where(LLMProvider.provider_id == provider_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def upsert_llm_provider_model(
    session: AsyncSession,
    provider: LLMProvider,
    *,
    model_id: str,
    model_config: dict[str, Any],
    set_default: bool,
    managed_deployment_id: str | None = None,
    set_default_if_missing: bool = False,
) -> LLMProvider:
    """Merge one model atomically while preserving unknown provider/model fields."""

    config = dict(provider.config or {})
    raw_models = config.get("models")
    models = raw_models if isinstance(raw_models, list) else []
    deduplicated: list[Any] = []
    by_model_id: dict[str, dict[str, Any]] = {}

    for raw_entry in models:
        if not isinstance(raw_entry, dict):
            deduplicated.append(raw_entry)
            continue
        entry = dict(raw_entry)
        entry_model_id = entry.get("model_id")
        if not isinstance(entry_model_id, str) or not entry_model_id:
            deduplicated.append(entry)
            continue
        existing = by_model_id.get(entry_model_id)
        if existing is None:
            by_model_id[entry_model_id] = entry
            deduplicated.append(entry)
        else:
            existing.update(entry)

    target = by_model_id.get(model_id)
    if target is None:
        target = {"model_id": model_id}
        deduplicated.append(target)
    elif (managed_deployment_id is not None and "cognis_managed_deployment_ids" not in target) or (
        managed_deployment_id is None and "cognis_managed_deployment_ids" in target
    ):
        target["cognis_manual_reference"] = True
    target.update(model_config)
    target["model_id"] = model_id
    if managed_deployment_id is not None:
        deployment_ids = target.get("cognis_managed_deployment_ids")
        normalized_ids = (
            {str(value) for value in deployment_ids if isinstance(value, str) and value}
            if isinstance(deployment_ids, list)
            else set()
        )
        normalized_ids.add(managed_deployment_id)
        target["cognis_managed_deployment_ids"] = sorted(normalized_ids)
    config["models"] = deduplicated
    if set_default and (not set_default_if_missing or not config.get("default_model")):
        config["default_model"] = model_id
    provider.config = config
    provider.updated_at = _utcnow()
    await session.flush()
    return provider


async def remove_generated_llm_provider_model_reference(
    session: AsyncSession,
    provider: LLMProvider,
    *,
    model_id: str,
    deployment_id: str,
) -> LLMProvider:
    """Remove one generated reference and drop only unreferenced generated entries."""

    config = dict(provider.config or {})
    raw_models = config.get("models")
    if not isinstance(raw_models, list):
        return provider
    models: list[Any] = []
    removed = False
    for raw_entry in raw_models:
        if not isinstance(raw_entry, dict) or raw_entry.get("model_id") != model_id:
            models.append(raw_entry)
            continue
        entry = dict(raw_entry)
        deployment_ids = entry.get("cognis_managed_deployment_ids")
        normalized_ids = (
            [value for value in deployment_ids if isinstance(value, str) and value != deployment_id]
            if isinstance(deployment_ids, list)
            else []
        )
        if normalized_ids:
            entry["cognis_managed_deployment_ids"] = sorted(set(normalized_ids))
            models.append(entry)
        elif entry.get("cognis_manual_reference") is True:
            entry.pop("cognis_managed_deployment_ids", None)
            entry.pop("cognis_manual_reference", None)
            models.append(entry)
        else:
            removed = True
    if not removed and models == raw_models:
        return provider
    config["models"] = models
    if removed and config.get("default_model") == model_id:
        replacement = next(
            (
                entry.get("model_id")
                for entry in models
                if isinstance(entry, dict)
                and isinstance(entry.get("model_id"), str)
                and entry.get("model_id")
            ),
            None,
        )
        if replacement is None:
            config.pop("default_model", None)
        else:
            config["default_model"] = replacement
    provider.config = config
    provider.updated_at = _utcnow()
    await session.flush()
    return provider
