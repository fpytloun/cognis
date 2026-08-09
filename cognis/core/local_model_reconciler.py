"""Controller reconciliation loop for declarative local-model state."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.local_model_runtime import (
    LocalModelRuntimeManager,
    LocalModelRuntimeUnavailable,
)
from cognis.core.local_model_service import (
    list_current_local_model_delete_dependencies,
    resolve_provider_scoped_deployment_executors,
)
from cognis.core.local_models import parse_local_model_reference, sanitize_local_model_error
from cognis.logging import get_logger
from cognis.models.local_models import (
    LOCAL_MODEL_BYTE_COUNT_MAX,
    LocalModelDesiredState,
    LocalModelOperationAction,
    LocalModelOperationState,
    LocalModelPrunePolicy,
    LocalModelTargetState,
)
from cognis.store.local_models import (
    create_local_model_operation,
    get_local_model_operation,
    list_local_model_deployments,
    list_local_model_operations,
    list_local_model_targets,
    lock_and_get_local_model_deployment,
    lock_local_model_dispatch_guard,
    sync_local_model_targets,
    update_local_model_target_status,
)
from cognis.store.models import (
    LocalModelDeployment,
    LocalModelOperation,
    LocalModelTargetStatus,
)

_logger = get_logger(__name__)

_DEFAULT_RESYNC_SECONDS = 60.0
_MAX_RECONCILE_DEPLOYMENTS = 1000
_BASE_RETRY_SECONDS = 2.0
_MAX_RETRY_SECONDS = 300.0


def _resync_seconds() -> float:
    raw = os.environ.get(
        "COGNIS_LOCAL_MODEL_RECONCILE_INTERVAL_SECONDS",
        str(_DEFAULT_RESYNC_SECONDS),
    )
    try:
        return max(5.0, min(float(raw), 3600.0))
    except ValueError:
        return _DEFAULT_RESYNC_SECONDS


class LocalModelReconciler:
    """Compute union desired state and converge exact executor targets."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_manager: LocalModelRuntimeManager,
    ) -> None:
        self._session_factory = session_factory
        self._runtime_manager = runtime_manager
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._pass_lock = asyncio.Lock()
        self._retry_attempts: dict[str, tuple[int, int]] = {}
        self._retry_deadlines: dict[str, tuple[int, float]] = {}
        self._retry_handles: dict[str, asyncio.TimerHandle] = {}
        self._requested_deployments: set[str] = set()
        self._requested_executors: set[str] = set()

    async def start(self) -> None:
        self._runtime_manager.set_completion_callback(
            lambda deployment_id: self.trigger(deployment_id=deployment_id)
        )
        self._task = asyncio.create_task(
            self._run(),
            name="local-model-reconciler",
        )
        self.trigger()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for handle in self._retry_handles.values():
            handle.cancel()
        self._retry_handles.clear()
        self._retry_attempts.clear()
        self._retry_deadlines.clear()

    def trigger(
        self,
        *,
        deployment_id: str | None = None,
        executor_id: str | None = None,
    ) -> None:
        """Schedule a bounded resync after desired or observed state changes."""

        if deployment_id is not None:
            self._requested_deployments.add(deployment_id)
        if executor_id is not None:
            self._requested_executors.add(executor_id)
        self._wake.set()

    async def reconcile_now(
        self,
        *,
        deployment_id: str | None = None,
        executor_id: str | None = None,
    ) -> None:
        """Run one pass, primarily for exact API requests and tests."""

        async with self._pass_lock:
            await self._reconcile_pass(
                deployment_ids={deployment_id} if deployment_id else set(),
                executor_ids={executor_id} if executor_id else set(),
            )

    async def _run(self) -> None:
        interval = _resync_seconds()
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            self._wake.clear()
            deployments = set(self._requested_deployments)
            executors = set(self._requested_executors)
            self._requested_deployments.clear()
            self._requested_executors.clear()
            try:
                async with self._pass_lock:
                    await self._reconcile_pass(
                        deployment_ids=deployments,
                        executor_ids=executors,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning(
                    "local_model_reconciler: reconciliation pass failed",
                    exc_info=True,
                )

    async def _reconcile_pass(
        self,
        *,
        deployment_ids: set[str],
        executor_ids: set[str],
    ) -> None:
        await self._materialize_authorized_targets()
        async with self._session_factory() as session:
            deployments = await self._list_all_deployments(session)
            snapshots: list[tuple[LocalModelDeployment, list[LocalModelTargetStatus]]] = []
            present_refs: set[tuple[str, str]] = set()
            for deployment in deployments:
                if deployment.provider_id is None:
                    continue
                targets = await list_local_model_targets(
                    session,
                    deployment.deployment_id,
                )
                snapshots.append((deployment, targets))
                if deployment.desired_state == LocalModelDesiredState.PRESENT.value:
                    present_refs.update(
                        (target.executor_id, deployment.runtime_name) for target in targets
                    )
            delete_scan_complete = True
        self._prune_retry_state(
            {target.target_id for _deployment, targets in snapshots for target in targets}
        )

        selected = [
            (deployment, targets)
            for deployment, targets in snapshots
            if (
                not deployment_ids
                and not executor_ids
                or deployment.deployment_id in deployment_ids
                or any(target.executor_id in executor_ids for target in targets)
            )
        ]
        for deployment, targets in selected:
            for target in targets:
                try:
                    await self._reconcile_target(
                        deployment.deployment_id,
                        target.target_id,
                        present_refs=present_refs,
                        delete_scan_complete=delete_scan_complete,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._target_failed(
                        target.target_id,
                        target.generation,
                        exc,
                    )

    async def _materialize_authorized_targets(self) -> None:
        async with self._session_factory() as session:
            deployment_ids = [
                deployment.deployment_id for deployment in await self._list_all_deployments(session)
            ]
        for deployment_id in deployment_ids:
            try:
                async with self._session_factory() as session:
                    await lock_local_model_dispatch_guard(session)
                    deployment = await lock_and_get_local_model_deployment(
                        session,
                        deployment_id,
                    )
                    if deployment is None:
                        continue
                    if deployment.provider_id is None:
                        continue
                    resolved = await resolve_provider_scoped_deployment_executors(
                        session,
                        deployment,
                    )
                    selected_ids = [row.executor_id for row in resolved]
                    existing = await list_local_model_targets(
                        session,
                        deployment.deployment_id,
                    )
                    if {target.executor_id for target in existing} != set(selected_ids):
                        operations = await list_local_model_operations(
                            session,
                            deployment.deployment_id,
                        )
                        if any(
                            operation.state
                            not in {
                                LocalModelOperationState.SUCCEEDED.value,
                                LocalModelOperationState.FAILED.value,
                                LocalModelOperationState.CANCELLED.value,
                            }
                            for operation in operations
                        ):
                            continue
                        deployment.generation += 1
                        deployment.updated_at = datetime.now(UTC)
                        await sync_local_model_targets(
                            session,
                            deployment,
                            selected_ids,
                        )
                        _logger.info(
                            "local_model_reconciler: materialized selector change",
                            extra={
                                "extra_data": {
                                    "deployment_id": deployment.deployment_id,
                                    "generation": deployment.generation,
                                    "executor_ids": selected_ids,
                                }
                            },
                        )
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.warning(
                    "local_model_reconciler: target materialization failed",
                    extra={
                        "extra_data": {
                            "deployment_id": deployment_id,
                            "error": sanitize_local_model_error(str(exc)),
                        }
                    },
                )

    async def _reconcile_target(
        self,
        deployment_id: str,
        target_id: str,
        *,
        present_refs: set[tuple[str, str]],
        delete_scan_complete: bool,
    ) -> None:
        async with self._session_factory() as session:
            await lock_local_model_dispatch_guard(session)
            deployment = await lock_and_get_local_model_deployment(
                session,
                deployment_id,
            )
            target = await session.get(LocalModelTargetStatus, target_id)
            if deployment is None or target is None:
                self._clear_retry_state(target_id)
                return
            generation = deployment.generation
            if target.generation != generation:
                self._clear_retry_state(target_id)
                return
            executor_id = target.executor_id
            runtime_name = deployment.runtime_name
            if not self._retry_ready(target):
                return
            active_operation = await self._active_operation(
                session,
                deployment,
                target,
            )
            if active_operation is not None:
                operation_id = active_operation.operation_id
                operation_state = active_operation.state
                await session.commit()
                if operation_state == LocalModelOperationState.CANCEL_REQUESTED.value:
                    await self._runtime_manager.cancel(
                        operation_id,
                        executor_id=executor_id,
                    )
                    self._clear_retry_state(target_id)
                elif operation_state in {
                    LocalModelOperationState.QUEUED.value,
                    LocalModelOperationState.INTERRUPTED.value,
                }:
                    dispatched = await self._runtime_manager.dispatch(operation_id)
                    if dispatched:
                        self._clear_retry_state(target_id)
                return

        capability = self._runtime_manager.capability(executor_id)
        if capability is None:
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.PENDING,
            )
            return
        if not capability.management_enabled:
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.BLOCKED,
                error="managed Ollama mutations are disabled on the exact executor",
            )
            return
        try:
            status = await self._runtime_manager.status(executor_id)
        except LocalModelRuntimeUnavailable:
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.PENDING,
            )
            return
        except Exception as exc:
            await self._target_failed(target_id, generation, exc)
            return
        if not status.reachable:
            await self._target_failed(
                target_id,
                generation,
                RuntimeError(status.error or "managed Ollama runtime is unreachable"),
            )
            return
        installed = self._find_installed(status.installed, runtime_name)

        if deployment.desired_state == LocalModelDesiredState.PRESENT.value:
            if installed is None:
                await self._start_operation(
                    deployment_id,
                    target_id,
                    LocalModelOperationAction.PULL,
                )
                return
            try:
                await self._runtime_manager.show(executor_id, runtime_name)
            except Exception as exc:
                await self._target_failed(target_id, generation, exc)
                return
            observed_digest = self._string(installed.get("digest"))
            observed_size = self._non_negative_int(installed.get("size"))
            refresh_required = (
                deployment.update_policy == "always" and target.observed_generation < generation
            ) or (
                deployment.update_policy == "if_changed"
                and deployment.digest is not None
                and observed_digest != deployment.digest
            )
            if refresh_required:
                await self._start_operation(
                    deployment_id,
                    target_id,
                    LocalModelOperationAction.PULL,
                )
                return
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.READY,
                observed_generation=generation,
                digest=observed_digest,
                size=observed_size,
            )
            await self._runtime_manager.ensure_observed_provider_upsert(
                deployment_id=deployment_id,
                executor_id=executor_id,
                generation=generation,
            )
            return

        if deployment.prune_policy == LocalModelPrunePolicy.RETAIN.value:
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.ABSENT,
                observed_generation=generation,
            )
            return
        if not delete_scan_complete:
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.BLOCKED,
                error="delete safety scan was truncated; refusing destructive reconciliation",
            )
            return
        if (executor_id, runtime_name) in present_refs:
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.BLOCKED,
                error="model is retained because another deployment references it",
            )
            return
        if installed is None:
            await self._set_target(
                target_id,
                generation,
                LocalModelTargetState.ABSENT,
                observed_generation=generation,
            )
            return
        await self._start_operation(
            deployment_id,
            target_id,
            LocalModelOperationAction.DELETE,
        )

    async def _start_operation(
        self,
        deployment_id: str,
        target_id: str,
        action: LocalModelOperationAction,
    ) -> None:
        async with self._session_factory() as session:
            await lock_local_model_dispatch_guard(session)
            deployment = await lock_and_get_local_model_deployment(
                session,
                deployment_id,
            )
            target = await session.get(LocalModelTargetStatus, target_id)
            if deployment is None or target is None:
                self._clear_retry_state(target_id)
                return
            operations = await list_local_model_operations(session, deployment_id)
            matching = [
                operation
                for operation in operations
                if operation.executor_id == target.executor_id
                and operation.generation == deployment.generation
                and operation.action == action.value
            ]
            active = next(
                (
                    operation
                    for operation in matching
                    if operation.state
                    in {
                        LocalModelOperationState.QUEUED.value,
                        LocalModelOperationState.RUNNING.value,
                        LocalModelOperationState.CANCEL_REQUESTED.value,
                        LocalModelOperationState.INTERRUPTED.value,
                    }
                ),
                None,
            )
            if active is not None:
                operation_id = active.operation_id
            else:
                active_count = sum(
                    operation.state
                    in {
                        LocalModelOperationState.QUEUED.value,
                        LocalModelOperationState.RUNNING.value,
                        LocalModelOperationState.CANCEL_REQUESTED.value,
                        LocalModelOperationState.INTERRUPTED.value,
                    }
                    for operation in operations
                )
                if active_count >= deployment.max_parallel:
                    return
                if action == LocalModelOperationAction.DELETE:
                    dependencies = await list_current_local_model_delete_dependencies(
                        session,
                        executor_id=target.executor_id,
                        runtime_name=deployment.runtime_name,
                        exclude_deployment_id=deployment.deployment_id,
                    )
                    if dependencies:
                        await update_local_model_target_status(
                            session,
                            target.target_id,
                            expected_generation=target.generation,
                            state=LocalModelTargetState.BLOCKED.value,
                            last_error=(
                                "model is retained because another deployment references it"
                            ),
                        )
                        await session.commit()
                        self._clear_retry_state(target_id)
                        return
                attempt = len(matching) + 1
                document = {
                    "action": action.value,
                    "deployment_id": deployment.deployment_id,
                    "executor_id": target.executor_id,
                    "generation": deployment.generation,
                    "runtime_name": deployment.runtime_name,
                }
                request_hash = (
                    "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            document,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                )
                operation, _created = await create_local_model_operation(
                    session,
                    deployment_id=deployment.deployment_id,
                    executor_id=target.executor_id,
                    generation=deployment.generation,
                    action=action.value,
                    idempotency_key=(
                        f"reconcile:{target.executor_id}:{deployment.generation}:"
                        f"{action.value}:{attempt}"
                    ),
                    request_hash=request_hash,
                    post_pull_provider_upsert=action == LocalModelOperationAction.PULL,
                )
                operation_id = operation.operation_id
            await update_local_model_target_status(
                session,
                target.target_id,
                expected_generation=target.generation,
                state=LocalModelTargetState.RECONCILING.value,
                current_operation_id=operation_id,
            )
            await session.commit()
        try:
            dispatched = await self._runtime_manager.dispatch(operation_id)
        except LocalModelRuntimeUnavailable as exc:
            await self._target_failed(target_id, target.generation, exc)
        else:
            if dispatched:
                self._clear_retry_state(target_id)

    async def _set_target(
        self,
        target_id: str,
        generation: int,
        state: LocalModelTargetState,
        *,
        observed_generation: int | None = None,
        digest: str | None = None,
        size: int | None = None,
        error: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await update_local_model_target_status(
                session,
                target_id,
                expected_generation=generation,
                state=state.value,
                observed_generation=observed_generation,
                observed_digest=digest,
                observed_size_bytes=size,
                last_error=error,
            )
            await session.commit()
        if state != LocalModelTargetState.ERROR:
            self._clear_retry_state(target_id)

    async def _target_failed(
        self,
        target_id: str,
        generation: int,
        exc: BaseException,
    ) -> None:
        error = sanitize_local_model_error(str(exc))
        previous = self._retry_attempts.get(target_id)
        if previous is not None and previous[0] != generation:
            self._clear_retry_state(target_id)
            previous = None
        attempt = (previous[1] if previous is not None else 0) + 1
        delay = min(_MAX_RETRY_SECONDS, _BASE_RETRY_SECONDS * 2 ** min(attempt - 1, 8))
        ready_at = monotonic() + random.uniform(delay * 0.75, delay * 1.25)
        self._retry_attempts[target_id] = (generation, attempt)
        self._retry_deadlines[target_id] = (generation, ready_at)
        self._schedule_retry_wake(target_id, ready_at)
        await self._set_target(
            target_id,
            generation,
            LocalModelTargetState.ERROR,
            error=error,
        )
        _logger.warning(
            "local_model_reconciler: target reconciliation failed",
            extra={
                "extra_data": {
                    "target_id": target_id,
                    "generation": generation,
                    "retry_attempt": attempt,
                    "retry_in_seconds": round(ready_at - monotonic(), 3),
                    "error": error,
                }
            },
        )

    def _retry_ready(self, target: LocalModelTargetStatus) -> bool:
        attempt = self._retry_attempts.get(target.target_id)
        deadline = self._retry_deadlines.get(target.target_id)
        attempt_generation_changed = attempt is not None and attempt[0] != target.generation
        deadline_generation_changed = deadline is not None and deadline[0] != target.generation
        if attempt_generation_changed or deadline_generation_changed:
            self._clear_retry_state(target.target_id)
            return True
        if deadline is not None:
            if monotonic() < deadline[1]:
                return False
            self._retry_deadlines.pop(target.target_id, None)
            handle = self._retry_handles.pop(target.target_id, None)
            if handle is not None:
                handle.cancel()
            return True
        if target.state == LocalModelTargetState.ERROR.value:
            delay = random.uniform(_BASE_RETRY_SECONDS * 0.75, _BASE_RETRY_SECONDS * 1.25)
            ready_at = monotonic() + delay
            if attempt is None:
                self._retry_attempts[target.target_id] = (target.generation, 0)
            self._retry_deadlines[target.target_id] = (target.generation, ready_at)
            self._schedule_retry_wake(target.target_id, ready_at)
            return False
        if attempt is not None:
            self._clear_retry_state(target.target_id)
        return True

    def _clear_retry_state(self, target_id: str) -> None:
        self._retry_attempts.pop(target_id, None)
        self._retry_deadlines.pop(target_id, None)
        handle = self._retry_handles.pop(target_id, None)
        if handle is not None:
            handle.cancel()

    def _prune_retry_state(self, active_target_ids: set[str]) -> None:
        known_target_ids = (
            set(self._retry_attempts) | set(self._retry_deadlines) | set(self._retry_handles)
        )
        for target_id in known_target_ids - active_target_ids:
            self._clear_retry_state(target_id)

    def _schedule_retry_wake(self, target_id: str, ready_at: float) -> None:
        previous_handle = self._retry_handles.pop(target_id, None)
        if previous_handle is not None:
            previous_handle.cancel()
        loop = asyncio.get_running_loop()

        def _wake_retry() -> None:
            self._retry_handles.pop(target_id, None)
            self.trigger()

        self._retry_handles[target_id] = loop.call_later(
            max(0.0, ready_at - monotonic()),
            _wake_retry,
        )

    @staticmethod
    async def _list_all_deployments(
        session: AsyncSession,
    ) -> list[LocalModelDeployment]:
        deployments: list[LocalModelDeployment] = []
        after_deployment_id: str | None = None
        while True:
            page = await list_local_model_deployments(
                session,
                limit=_MAX_RECONCILE_DEPLOYMENTS,
                after_deployment_id=after_deployment_id,
            )
            deployments.extend(page)
            if len(page) < _MAX_RECONCILE_DEPLOYMENTS:
                return deployments
            after_deployment_id = page[-1].deployment_id

    @staticmethod
    async def _active_operation(
        session: AsyncSession,
        deployment: LocalModelDeployment,
        target: LocalModelTargetStatus,
    ) -> Any | None:
        if target.current_operation_id:
            operation = await get_local_model_operation(
                session,
                target.current_operation_id,
            )
            if (
                operation is not None
                and operation.generation == deployment.generation
                and operation.state
                in {
                    LocalModelOperationState.QUEUED.value,
                    LocalModelOperationState.RUNNING.value,
                    LocalModelOperationState.CANCEL_REQUESTED.value,
                    LocalModelOperationState.INTERRUPTED.value,
                }
            ):
                return operation
        result = await session.execute(
            select(LocalModelOperation)
            .where(
                LocalModelOperation.deployment_id == deployment.deployment_id,
                LocalModelOperation.executor_id == target.executor_id,
                LocalModelOperation.generation == deployment.generation,
                LocalModelOperation.state.in_(
                    [
                        LocalModelOperationState.QUEUED.value,
                        LocalModelOperationState.RUNNING.value,
                        LocalModelOperationState.CANCEL_REQUESTED.value,
                        LocalModelOperationState.INTERRUPTED.value,
                    ]
                ),
            )
            .order_by(LocalModelOperation.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _find_installed(
        models: list[dict[str, Any]],
        runtime_name: str,
    ) -> dict[str, Any] | None:
        for model in models:
            name = model.get("name") or model.get("model")
            if not isinstance(name, str):
                continue
            try:
                observed = parse_local_model_reference(name).runtime_name
            except ValueError:
                continue
            if observed == runtime_name:
                return model
        return None

    @staticmethod
    def _string(value: Any) -> str | None:
        return str(value)[:255] if isinstance(value, str) and value else None

    @staticmethod
    def _non_negative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if 0 <= parsed <= LOCAL_MODEL_BYTE_COUNT_MAX else None
