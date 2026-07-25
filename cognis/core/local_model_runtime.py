"""Controller manager for executor-local managed Ollama operations."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, cast
from weakref import WeakValueDictionary

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.local_model_service import (
    list_current_local_model_delete_dependencies,
    resolve_provider_scoped_deployment_executors,
)
from cognis.core.local_models import sanitize_local_model_error
from cognis.logging import get_logger
from cognis.models.executor_inference import executor_local_inference_config_confirmed
from cognis.models.local_models import (
    LOCAL_MODEL_BYTE_COUNT_MAX,
    LocalModelOperationAction,
    LocalModelOperationState,
    LocalModelTargetState,
    OllamaRuntimeStartRequest,
    OllamaRuntimeStatus,
)
from cognis.providers.executor.websocket import ExecutorRPCError
from cognis.store.local_models import (
    get_local_model_operation,
    interrupt_active_local_model_operations,
    list_active_local_model_operations,
    lock_and_get_llm_provider,
    lock_and_get_local_model_deployment,
    lock_local_model_dispatch_guard,
    record_local_model_operation_progress,
    transition_local_model_operation,
    update_local_model_target_status,
    upsert_llm_provider_model,
)
from cognis.store.models import (
    LocalModelDeployment,
    LocalModelOperation,
    LocalModelTargetStatus,
)

_logger = get_logger(__name__)


class LocalModelRuntimeUnavailable(RuntimeError):
    """The exact executor cannot currently accept managed Ollama work."""


class LocalModelRuntimeManager:
    """Persist operation lifecycle while routing work to exact executors."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ws_provider: Any,
    ) -> None:
        self._session_factory = session_factory
        self._ws_provider = ws_provider
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._on_operation_complete: Callable[[str], None] | None = None
        self._completion_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=64
        )
        self._completion_worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._ws_provider.register_local_model_callbacks(
            on_progress=self._handle_progress,
            on_completed=self._handle_completed,
        )
        self._ensure_completion_worker()
        async with self._session_factory() as session:
            interrupted = await interrupt_active_local_model_operations(
                session,
                reason="controller restarted before operation completion was observed",
            )
            await session.commit()
        if interrupted:
            _logger.info(
                "local_model_runtime: interrupted operations during startup recovery",
                extra={"extra_data": {"operation_ids": interrupted}},
            )

    async def stop(self) -> None:
        self._ws_provider.register_local_model_callbacks(
            on_progress=None,
            on_completed=None,
        )
        if self._completion_worker is not None and not self._completion_worker.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._completion_queue.join(), timeout=5.0)
            self._completion_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._completion_worker
        self._completion_worker = None

    def set_completion_callback(self, callback: Callable[[str], None]) -> None:
        self._on_operation_complete = callback

    async def status(self, executor_id: str) -> OllamaRuntimeStatus:
        connection = self._ws_provider.get_connection(executor_id)
        if connection is None:
            raise LocalModelRuntimeUnavailable("exact executor is offline")
        return cast(OllamaRuntimeStatus, await connection.local_model_status())

    def capability(self, executor_id: str) -> Any | None:
        connection = self._ws_provider.get_connection(executor_id)
        if connection is None:
            return None
        return connection.capabilities.local_model_runtime

    async def show(self, executor_id: str, runtime_name: str) -> dict[str, Any]:
        connection = self._ws_provider.get_connection(executor_id)
        if connection is None:
            raise LocalModelRuntimeUnavailable("exact executor is offline")
        return cast(
            dict[str, Any],
            await connection.local_model_show(runtime_name),
        )

    async def dispatch(self, operation_id: str) -> bool:
        """Start one durable operation on its exact executor."""

        lock = self._locks.setdefault(operation_id, asyncio.Lock())
        async with lock:
            async with self._session_factory() as session:
                await lock_local_model_dispatch_guard(session)
                operation = await get_local_model_operation(session, operation_id)
                if operation is None:
                    raise LookupError("local-model operation not found")
                if operation.state in {
                    LocalModelOperationState.SUCCEEDED.value,
                    LocalModelOperationState.FAILED.value,
                    LocalModelOperationState.CANCELLED.value,
                    LocalModelOperationState.CANCEL_REQUESTED.value,
                }:
                    return False
                deployment = await session.get(
                    LocalModelDeployment,
                    operation.deployment_id,
                )
                target = await self._target_for_operation(session, operation)
                if deployment is None or target is None:
                    raise LookupError("local-model operation target no longer exists")
                if (
                    operation.generation != deployment.generation
                    or target.generation != deployment.generation
                ):
                    if operation.state in {
                        LocalModelOperationState.QUEUED.value,
                        LocalModelOperationState.INTERRUPTED.value,
                    }:
                        await transition_local_model_operation(
                            session,
                            operation.operation_id,
                            LocalModelOperationState.CANCELLED,
                            error="operation generation is stale",
                        )
                        await session.commit()
                    return False
                scoped_executors = await resolve_provider_scoped_deployment_executors(
                    session,
                    deployment,
                )
                scoped_executor = next(
                    (row for row in scoped_executors if row.executor_id == operation.executor_id),
                    None,
                )
                if scoped_executor is None:
                    await self._transition_out_of_scope(session, operation, target)
                    await session.commit()
                    return False
                if not executor_local_inference_config_confirmed(scoped_executor):
                    await update_local_model_target_status(
                        session,
                        target.target_id,
                        expected_generation=target.generation,
                        state=LocalModelTargetState.PENDING.value,
                        last_error="executor local inference configuration is still applying",
                    )
                    await session.commit()
                    return False
                if operation.action == LocalModelOperationAction.DELETE.value:
                    dependencies = await list_current_local_model_delete_dependencies(
                        session,
                        executor_id=operation.executor_id,
                        runtime_name=deployment.runtime_name,
                        exclude_deployment_id=deployment.deployment_id,
                    )
                    if dependencies:
                        await transition_local_model_operation(
                            session,
                            operation.operation_id,
                            LocalModelOperationState.CANCELLED,
                            error="delete blocked by present deployment dependencies",
                        )
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
                        return False
                connection = self._ws_provider.get_connection(operation.executor_id)
                if connection is None:
                    await update_local_model_target_status(
                        session,
                        target.target_id,
                        expected_generation=target.generation,
                        state=LocalModelTargetState.PENDING.value,
                    )
                    await session.commit()
                    return False
                capability = connection.capabilities.local_model_runtime
                if (
                    getattr(connection.capabilities, "local_inference", None) is not True
                    or capability is None
                    or not capability.management_enabled
                ):
                    await transition_local_model_operation(
                        session,
                        operation.operation_id,
                        LocalModelOperationState.CANCELLED,
                        error="managed Ollama mutations are disabled on the exact executor",
                    )
                    await update_local_model_target_status(
                        session,
                        target.target_id,
                        expected_generation=target.generation,
                        state=LocalModelTargetState.BLOCKED.value,
                        last_error="managed Ollama mutations are disabled on the exact executor",
                    )
                    await session.commit()
                    return False
                scoped_executors = await resolve_provider_scoped_deployment_executors(
                    session,
                    deployment,
                )
                if operation.executor_id not in {row.executor_id for row in scoped_executors}:
                    await self._transition_out_of_scope(session, operation, target)
                    await session.commit()
                    return False
                if operation.state == LocalModelOperationState.INTERRUPTED.value:
                    await transition_local_model_operation(
                        session,
                        operation.operation_id,
                        LocalModelOperationState.QUEUED,
                    )
                operation = await transition_local_model_operation(
                    session,
                    operation.operation_id,
                    LocalModelOperationState.RUNNING,
                )
                await update_local_model_target_status(
                    session,
                    target.target_id,
                    expected_generation=target.generation,
                    state=LocalModelTargetState.RECONCILING.value,
                    current_operation_id=operation.operation_id,
                )
                request = OllamaRuntimeStartRequest(
                    operation_id=operation.operation_id,
                    action=operation.action,
                    runtime_name=deployment.runtime_name,
                    request_hash=operation.request_hash,
                    force=(
                        operation.action == LocalModelOperationAction.PULL.value
                        and deployment.update_policy == "always"
                    ),
                )
                executor_id = operation.executor_id
                _logger.info(
                    "local_model_runtime: dispatching operation",
                    extra={
                        "extra_data": {
                            "operation_id": operation_id,
                            "executor_id": executor_id,
                            "action": request.action.value,
                        }
                    },
                )
                try:
                    status = await connection.local_model_operation_start(request)
                except Exception as exc:
                    error = sanitize_local_model_error(str(exc))
                    next_state = (
                        LocalModelOperationState.FAILED
                        if isinstance(exc, ExecutorRPCError)
                        else LocalModelOperationState.INTERRUPTED
                    )
                    await transition_local_model_operation(
                        session,
                        operation_id,
                        next_state,
                        error=error,
                    )
                    await update_local_model_target_status(
                        session,
                        target.target_id,
                        expected_generation=target.generation,
                        state=(
                            LocalModelTargetState.ERROR.value
                            if next_state == LocalModelOperationState.FAILED
                            else LocalModelTargetState.PENDING.value
                        ),
                        last_error=error,
                    )
                    await session.commit()
                    return False
                replaced = self._ws_provider.get_connection(executor_id) is not connection
                if replaced and status.state == "running":
                    await transition_local_model_operation(
                        session,
                        operation_id,
                        LocalModelOperationState.INTERRUPTED,
                        error="executor connection was replaced during dispatch",
                    )
                    await update_local_model_target_status(
                        session,
                        target.target_id,
                        expected_generation=target.generation,
                        state=LocalModelTargetState.PENDING.value,
                    )
                await session.commit()
            if status.state in {"succeeded", "failed", "cancelled"}:
                await self._handle_completed(
                    executor_id,
                    {
                        "operation_id": operation_id,
                        "state": status.state,
                        "error": status.error,
                    },
                )
            return True

    async def cancel(self, operation_id: str, *, executor_id: str) -> dict[str, Any]:
        """Request stream cancellation and acknowledge without rollback claims."""

        lock = self._locks.setdefault(operation_id, asyncio.Lock())
        async with lock:
            async with self._session_factory() as session:
                await lock_local_model_dispatch_guard(session)
                operation = await get_local_model_operation(session, operation_id)
                if operation is None or operation.executor_id != executor_id:
                    raise LookupError("local-model operation not found")
                state = LocalModelOperationState(operation.state)
                if state in {
                    LocalModelOperationState.SUCCEEDED,
                    LocalModelOperationState.FAILED,
                }:
                    return {"acknowledged": False, "rollback_guaranteed": False}
                if state == LocalModelOperationState.CANCELLED:
                    return {"acknowledged": True, "rollback_guaranteed": False}
                if state == LocalModelOperationState.QUEUED:
                    await transition_local_model_operation(
                        session,
                        operation_id,
                        LocalModelOperationState.CANCELLED,
                    )
                    await session.commit()
                    self._notify_completion(operation.deployment_id)
                    return {"acknowledged": True, "rollback_guaranteed": False}
                if state != LocalModelOperationState.CANCEL_REQUESTED:
                    await transition_local_model_operation(
                        session,
                        operation_id,
                        LocalModelOperationState.CANCEL_REQUESTED,
                    )
                await session.commit()

            connection = self._ws_provider.get_connection(executor_id)
            if connection is None:
                return {"acknowledged": True, "rollback_guaranteed": False}
            try:
                result = await connection.local_model_operation_cancel(operation_id)
            except Exception:
                return {"acknowledged": True, "rollback_guaranteed": False}
            acknowledged = bool(result.get("acknowledged", False))
            if not acknowledged:
                try:
                    status = await connection.local_model_operation_status(operation_id)
                except ExecutorRPCError as exc:
                    if exc.code == -32044:
                        async with self._session_factory() as session:
                            current = await get_local_model_operation(session, operation_id)
                            if (
                                current is not None
                                and current.state == LocalModelOperationState.CANCEL_REQUESTED.value
                            ):
                                await transition_local_model_operation(
                                    session,
                                    operation_id,
                                    LocalModelOperationState.CANCELLED,
                                    error=("executor no longer has the operation after restart"),
                                )
                                await session.commit()
                                self._notify_completion(current.deployment_id)
                        return {"acknowledged": True, "rollback_guaranteed": False}
                    status = None
                except Exception:
                    status = None
                if status is not None and status.state in {
                    LocalModelOperationState.SUCCEEDED.value,
                    LocalModelOperationState.FAILED.value,
                    LocalModelOperationState.CANCELLED.value,
                }:
                    await self._handle_completed(
                        executor_id,
                        {
                            "operation_id": operation_id,
                            "state": status.state,
                            "error": status.error,
                        },
                    )
            return {
                "acknowledged": True,
                "rollback_guaranteed": False,
            }

    async def executor_connected(self, executor_id: str) -> None:
        """Recover running and cancellation intent on a replacement connection."""

        async with self._session_factory() as session:
            operations = await list_active_local_model_operations(
                session,
                executor_id=executor_id,
            )
            operation_ids = [operation.operation_id for operation in operations]
        for operation_id in operation_ids:
            operation_lock = self._locks.setdefault(operation_id, asyncio.Lock())
            try:
                async with operation_lock, self._session_factory() as session:
                    await lock_local_model_dispatch_guard(session)
                    operation = await get_local_model_operation(
                        session,
                        operation_id,
                    )
                    if (
                        operation is not None
                        and operation.state == LocalModelOperationState.RUNNING.value
                    ):
                        await transition_local_model_operation(
                            session,
                            operation_id,
                            LocalModelOperationState.INTERRUPTED,
                            error=("executor connection replaced before completion was observed"),
                        )
                        await session.commit()
            except Exception as exc:
                _logger.warning(
                    "local_model_runtime: failed to interrupt operation on reconnect",
                    extra={
                        "extra_data": {
                            "executor_id": executor_id,
                            "operation_id": operation_id,
                            "error": sanitize_local_model_error(str(exc)),
                        }
                    },
                )
        async with self._session_factory() as session:
            operations = await list_active_local_model_operations(
                session,
                executor_id=executor_id,
            )
        for operation in operations:
            try:
                if operation.state == LocalModelOperationState.CANCEL_REQUESTED.value:
                    await self.cancel(operation.operation_id, executor_id=executor_id)
                elif operation.state in {
                    LocalModelOperationState.QUEUED.value,
                    LocalModelOperationState.RUNNING.value,
                    LocalModelOperationState.INTERRUPTED.value,
                }:
                    await self.dispatch(operation.operation_id)
            except Exception as exc:
                _logger.warning(
                    "local_model_runtime: operation recovery failed",
                    extra={
                        "extra_data": {
                            "executor_id": executor_id,
                            "operation_id": operation.operation_id,
                            "error": sanitize_local_model_error(str(exc)),
                        }
                    },
                )

    async def executor_disconnected(self, executor_id: str) -> None:
        async with self._session_factory() as session:
            operations = await list_active_local_model_operations(
                session,
                executor_id=executor_id,
            )
            operation_ids = [operation.operation_id for operation in operations]
        for operation_id in operation_ids:
            operation_lock = self._locks.setdefault(operation_id, asyncio.Lock())
            async with operation_lock, self._session_factory() as session:
                await lock_local_model_dispatch_guard(session)
                operation = await get_local_model_operation(session, operation_id)
                if operation is None or operation.state != LocalModelOperationState.RUNNING.value:
                    continue
                await transition_local_model_operation(
                    session,
                    operation_id,
                    LocalModelOperationState.INTERRUPTED,
                    error="executor disconnected before completion was observed",
                )
                target = await self._target_for_operation(session, operation)
                if target is not None:
                    await update_local_model_target_status(
                        session,
                        target.target_id,
                        expected_generation=target.generation,
                        state=LocalModelTargetState.PENDING.value,
                    )
                await session.commit()

    async def ensure_observed_provider_upsert(
        self,
        *,
        deployment_id: str,
        executor_id: str,
        generation: int,
    ) -> None:
        """Recover the mandatory provider upsert after readiness observation."""

        await self._atomic_provider_upsert(
            deployment_id=deployment_id,
            executor_id=executor_id,
            generation=generation,
        )

    async def _handle_progress(self, executor_id: str, payload: dict[str, Any]) -> None:
        operation_id = str(payload.get("operation_id") or "")
        operation_lock = self._locks.get(operation_id)
        if operation_lock is not None and operation_lock.locked():
            return
        progress_seq_raw = payload.get("progress_seq")
        progress_bytes_raw = payload.get("progress_bytes")
        if (
            isinstance(progress_seq_raw, bool)
            or not isinstance(progress_seq_raw, int | str)
            or isinstance(progress_bytes_raw, bool)
            or not isinstance(progress_bytes_raw, int | str)
        ):
            return
        try:
            progress_seq = int(progress_seq_raw)
            progress_bytes = int(progress_bytes_raw)
        except ValueError:
            return
        if not 0 <= progress_bytes <= LOCAL_MODEL_BYTE_COUNT_MAX:
            return
        phase_raw = payload.get("phase")
        phase = str(phase_raw)[:120] if phase_raw is not None else None
        try:
            async with self._session_factory() as session:
                operation = await get_local_model_operation(session, operation_id)
                if operation is None or operation.executor_id != executor_id:
                    return
                await record_local_model_operation_progress(
                    session,
                    operation_id,
                    progress_seq=progress_seq,
                    progress_bytes=progress_bytes,
                    phase=phase,
                )
                await session.commit()
        except Exception:
            _logger.warning(
                "local_model_runtime: rejected progress notification",
                extra={
                    "extra_data": {
                        "operation_id": operation_id,
                        "executor_id": executor_id,
                    }
                },
                exc_info=True,
            )

    async def _handle_completed(self, executor_id: str, payload: dict[str, Any]) -> None:
        operation_id = str(payload.get("operation_id") or "")
        if not operation_id:
            return
        self._ensure_completion_worker()
        await self._completion_queue.put((executor_id, dict(payload)))

    def _ensure_completion_worker(self) -> None:
        if self._completion_worker is None or self._completion_worker.done():
            self._completion_worker = asyncio.create_task(
                self._completion_worker_loop(),
                name="local-model-completion-worker",
            )

    async def _completion_worker_loop(self) -> None:
        while True:
            executor_id, payload = await self._completion_queue.get()
            try:
                await self._persist_completed_until_terminal(executor_id, payload)
            finally:
                self._completion_queue.task_done()

    async def _persist_completed_until_terminal(
        self,
        executor_id: str,
        payload: dict[str, Any],
    ) -> None:
        operation_id = str(payload.get("operation_id") or "")
        operation_lock = self._locks.setdefault(operation_id, asyncio.Lock())
        delay = 0.25
        while True:
            async with operation_lock:
                persisted = await self._persist_completed(executor_id, payload)
            if persisted:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _persist_completed(self, executor_id: str, payload: dict[str, Any]) -> bool:
        operation_id = str(payload.get("operation_id") or "")
        state_raw = str(payload.get("state") or "")
        target_state = {
            "succeeded": LocalModelOperationState.SUCCEEDED,
            "failed": LocalModelOperationState.FAILED,
            "cancelled": LocalModelOperationState.CANCELLED,
        }.get(state_raw)
        if target_state is None:
            return True
        error = sanitize_local_model_error(
            str(payload.get("error") or payload.get("message") or "")
        )
        deployment_id: str | None = None
        try:
            async with self._session_factory() as session:
                operation = await get_local_model_operation(session, operation_id)
                if operation is None or operation.executor_id != executor_id:
                    return True
                deployment_id = operation.deployment_id

            async with self._session_factory() as session:
                operation = await get_local_model_operation(session, operation_id)
                if operation is None or operation.executor_id != executor_id:
                    return True
                if operation.state in {
                    LocalModelOperationState.SUCCEEDED.value,
                    LocalModelOperationState.FAILED.value,
                    LocalModelOperationState.CANCELLED.value,
                }:
                    return True
                target = await self._target_for_operation(session, operation)
                current_deployment = await session.get(
                    LocalModelDeployment,
                    operation.deployment_id,
                )
                current_generation = (
                    target is not None
                    and target.generation == operation.generation
                    and current_deployment is not None
                    and current_deployment.generation == operation.generation
                )
                await transition_local_model_operation(
                    session,
                    operation_id,
                    target_state,
                    error=error,
                )
                if target is not None and current_generation:
                    await update_local_model_target_status(
                        session,
                        target.target_id,
                        expected_generation=target.generation,
                        state=(
                            LocalModelTargetState.ERROR.value
                            if target_state == LocalModelOperationState.FAILED
                            else LocalModelTargetState.PENDING.value
                        ),
                        observed_generation=(
                            operation.generation
                            if target_state == LocalModelOperationState.SUCCEEDED
                            and operation.action == LocalModelOperationAction.PULL.value
                            else None
                        ),
                        last_error=error,
                    )
                await session.commit()
        except Exception:
            _logger.warning(
                "local_model_runtime: completion persistence failed",
                extra={
                    "extra_data": {
                        "operation_id": operation_id,
                        "executor_id": executor_id,
                    }
                },
                exc_info=True,
            )
            return False
        if deployment_id is not None:
            self._notify_completion(deployment_id)
        return True

    @staticmethod
    async def _transition_out_of_scope(
        session: AsyncSession,
        operation: LocalModelOperation,
        target: LocalModelTargetStatus,
    ) -> None:
        invalid_state = (
            LocalModelOperationState.CANCEL_REQUESTED
            if operation.state == LocalModelOperationState.RUNNING.value
            else LocalModelOperationState.CANCELLED
        )
        scope_error = "exact executor is no longer in the deployment provider scope"
        await transition_local_model_operation(
            session,
            operation.operation_id,
            invalid_state,
            error=scope_error,
        )
        await update_local_model_target_status(
            session,
            target.target_id,
            expected_generation=target.generation,
            state=LocalModelTargetState.BLOCKED.value,
            last_error=scope_error,
        )

    @staticmethod
    async def _target_for_operation(
        session: AsyncSession,
        operation: LocalModelOperation,
    ) -> LocalModelTargetStatus | None:
        result = await session.execute(
            select(LocalModelTargetStatus).where(
                LocalModelTargetStatus.deployment_id == operation.deployment_id,
                LocalModelTargetStatus.executor_id == operation.executor_id,
            )
        )
        return result.scalar_one_or_none()

    async def _atomic_provider_upsert(
        self,
        *,
        deployment_id: str,
        executor_id: str,
        generation: int,
    ) -> None:
        async with self._session_factory() as session:
            await lock_local_model_dispatch_guard(session)
            deployment = await lock_and_get_local_model_deployment(
                session,
                deployment_id,
            )
            if (
                deployment is None
                or deployment.generation != generation
                or deployment.provider_id is None
            ):
                return
            target = (
                await session.execute(
                    select(LocalModelTargetStatus)
                    .where(
                        LocalModelTargetStatus.deployment_id == deployment_id,
                        LocalModelTargetStatus.executor_id == executor_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                target is None
                or target.generation != generation
                or target.observed_generation != generation
                or target.state != LocalModelTargetState.READY.value
            ):
                return
            scoped_executors = await resolve_provider_scoped_deployment_executors(
                session,
                deployment,
            )
            if executor_id not in {row.executor_id for row in scoped_executors}:
                return
            provider = await lock_and_get_llm_provider(
                session,
                deployment.provider_id,
            )
            if provider is None:
                raise LookupError("linked Ollama provider not found")
            config = provider.config if isinstance(provider.config, dict) else {}
            if str(config.get("preset") or "").strip().lower() != "ollama":
                raise ValueError("linked provider is not an Ollama provider")
            await upsert_llm_provider_model(
                session,
                provider,
                model_id=deployment.runtime_name,
                model_config={},
                set_default=True,
                managed_deployment_id=deployment_id,
                set_default_if_missing=True,
            )
            await session.commit()

    def _notify_completion(self, deployment_id: str) -> None:
        if self._on_operation_complete is not None:
            self._on_operation_complete(deployment_id)
