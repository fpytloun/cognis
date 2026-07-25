from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core import local_model_runtime
from cognis.core.local_model_reconciler import LocalModelReconciler
from cognis.core.local_model_runtime import LocalModelRuntimeManager
from cognis.core.local_model_service import resolve_authorized_deployment_executors
from cognis.executor.ollama_runtime import OllamaRuntimeHandler
from cognis.models.local_models import (
    LocalModelOperationResponse,
    LocalModelTargetStatusResponse,
    OllamaRuntimeConfig,
    OllamaRuntimeOperationStatus,
    OllamaRuntimeStartRequest,
    OllamaRuntimeStatus,
)
from cognis.providers.llm.inference_router import (
    InferenceRouter,
    LocalModelRolloutUnavailableError,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.local_models import lock_local_model_dispatch_guard
from cognis.store.models import (
    Base,
    ExecutorRow,
    LLMProvider,
    LocalModelDeployment,
    LocalModelOperation,
    LocalModelTargetStatus,
    User,
)
from cognis.store.queries import create_executor


class _RuntimeManager:
    def __init__(self) -> None:
        self.statuses: dict[str, OllamaRuntimeStatus] = {}
        self.enabled: dict[str, bool] = {}
        self.dispatched: list[str] = []
        self.status_errors: dict[str, Exception] = {}

    def set_completion_callback(self, callback: Any) -> None:
        self.callback = callback

    def capability(self, executor_id: str) -> Any | None:
        if executor_id not in self.enabled:
            return None
        return SimpleNamespace(management_enabled=self.enabled[executor_id])

    async def status(self, executor_id: str) -> OllamaRuntimeStatus:
        if executor_id in self.status_errors:
            raise self.status_errors[executor_id]
        return self.statuses[executor_id]

    async def show(self, executor_id: str, runtime_name: str) -> dict[str, Any]:
        return {"model": runtime_name, "executor_id": executor_id}

    async def dispatch(self, operation_id: str) -> bool:
        self.dispatched.append(operation_id)
        return True

    async def ensure_observed_provider_upsert(self, **_: Any) -> None:
        return None


async def _database(tmp_path: Path, name: str) -> tuple[Any, Any]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)


def test_local_model_runtime_and_api_byte_counts_are_signed_int64_bounded() -> None:
    operation = {
        "operation_id": "operation",
        "action": "pull",
        "runtime_name": "qwen3:8b",
        "request_hash": "sha256:operation",
        "state": "running",
    }
    assert (
        OllamaRuntimeOperationStatus(**operation, progress_bytes=2**63 - 1).progress_bytes
        == 2**63 - 1
    )
    with pytest.raises(ValueError):
        OllamaRuntimeOperationStatus(**operation, progress_bytes=2**63)
    with pytest.raises(ValueError):
        OllamaRuntimeOperationStatus(**operation, progress_bytes=-1)
    with pytest.raises(ValueError):
        OllamaRuntimeOperationStatus(**operation, progress_bytes=True)

    assert LocalModelReconciler._non_negative_int(2**63 - 1) == 2**63 - 1
    assert LocalModelReconciler._non_negative_int(2**63) is None
    assert LocalModelReconciler._non_negative_int(-1) is None
    now = datetime.now(UTC)
    operation_response = {
        "operation_id": "operation",
        "deployment_id": "deployment",
        "executor_id": "executor",
        "generation": 1,
        "action": "pull",
        "state": "running",
        "progress_seq": 1,
        "phase": "downloading",
        "idempotency_key": "request",
        "request_hash": "sha256:operation",
        "created_at": now,
        "updated_at": now,
    }
    target_response = {
        "target_id": "target",
        "deployment_id": "deployment",
        "executor_id": "executor",
        "generation": 1,
        "observed_generation": 1,
        "state": "ready",
        "created_at": now,
        "updated_at": now,
    }
    assert (
        LocalModelOperationResponse(
            **operation_response,
            progress_bytes=2**63 - 1,
        ).progress_bytes
        == 2**63 - 1
    )
    assert (
        LocalModelTargetStatusResponse(
            **target_response,
            observed_size_bytes=2**63 - 1,
        ).observed_size_bytes
        == 2**63 - 1
    )
    for invalid in (-1, 2**63, True):
        with pytest.raises(ValueError):
            LocalModelOperationResponse(
                **operation_response,
                progress_bytes=invalid,
            )
        with pytest.raises(ValueError):
            LocalModelTargetStatusResponse(
                **target_response,
                observed_size_bytes=invalid,
            )


@pytest.mark.asyncio
async def test_runtime_notification_rejects_out_of_int64_progress_before_database() -> None:
    class _UnexpectedSessionFactory:
        def __call__(self) -> None:
            raise AssertionError("out-of-range progress must not open a database session")

    manager = LocalModelRuntimeManager(_UnexpectedSessionFactory(), object())  # type: ignore[arg-type]
    for invalid in (-1, 2**63, True):
        await manager._handle_progress(  # noqa: SLF001
            "executor",
            {
                "operation_id": "operation",
                "progress_seq": 1,
                "progress_bytes": invalid,
            },
        )


@pytest.mark.asyncio
async def test_executor_ignores_out_of_int64_ollama_progress_frame(tmp_path: Path) -> None:
    class _Adapter:
        async def installed(self) -> list[dict[str, Any]]:
            return []

        async def pull(self, _runtime_name: str, *, on_progress: Any) -> None:
            await on_progress({"status": "invalid", "completed": 2**63})
            await on_progress({"status": "valid", "completed": 5_629_109_111})

        async def close(self) -> None:
            return None

    progress: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []

    async def on_progress(payload: dict[str, Any]) -> None:
        progress.append(payload)

    async def on_complete(payload: dict[str, Any]) -> None:
        completed.append(payload)

    handler = OllamaRuntimeHandler(
        OllamaRuntimeConfig(
            management_enabled=True,
            disk_headroom_bytes=0,
            model_store_path=str(tmp_path),
        ),
        adapter=_Adapter(),  # type: ignore[arg-type]
    )
    request = OllamaRuntimeStartRequest(
        operation_id="operation",
        action="pull",
        runtime_name="qwen3:8b",
        request_hash="sha256:operation",
    )
    await handler.start(
        request,
        on_progress=on_progress,
        on_complete=on_complete,
    )
    await handler._tasks["operation"]  # noqa: SLF001

    assert [item["progress_bytes"] for item in progress] == [0, 5_629_109_111]
    assert completed and completed[0]["state"] == "succeeded"
    assert handler.operation_status("operation").progress_bytes == 5_629_109_111  # type: ignore[union-attr]
    await handler.close()


def _add_provider(
    session: Any,
    *,
    provider_id: str,
    executor_id: str,
) -> None:
    session.add(
        LLMProvider(
            provider_id=provider_id,
            display_name=provider_id,
            location="executor",
            backend="litellm",
            owner_email="owner@example.com",
            config={
                "preset": "ollama",
                "executor_id": executor_id,
                "models": [],
            },
            status="active",
        )
    )


def _mark_executor_local_inference_confirmed(executor: ExecutorRow) -> None:
    executor.runtime_state = "active"
    executor.desired_config_version = 1
    executor.applied_config_version = 1
    executor.runtime_metadata = {
        "local_inference_enabled": True,
        "ollama_runtime": {
            "runtime_type": "ollama",
            "port": 11434,
            "endpoint": "http://127.0.0.1:11434",
            "management_enabled": True,
            "max_concurrent_pulls": 1,
            "disk_headroom_bytes": 5 * 1024**3,
        },
    }


async def _seed_provider_scoped_runtime(
    session: Any,
    *,
    suffix: str,
    target_state: str = "pending",
    include_operation: bool = True,
) -> None:
    session.add(User(email="owner@example.com", name="Owner", role="user"))
    await session.flush()
    executor = await create_executor(
        session,
        executor_id="exec-a",
        name="Executor",
        executor_type="websocket",
        owner_email="owner@example.com",
        labels={"pool": "managed"},
    )
    _mark_executor_local_inference_confirmed(executor)
    session.add_all(
        [
            LLMProvider(
                provider_id=f"provider-{suffix}",
                display_name="Provider",
                location="executor",
                backend="litellm",
                owner_email="owner@example.com",
                config={
                    "preset": "ollama",
                    "executor_labels": {"pool": "managed"},
                    "models": [],
                },
                status="active",
            ),
            LocalModelDeployment(
                deployment_id=f"lmd-{suffix}",
                owner_email="owner@example.com",
                requested_ref="qwen3:8b",
                canonical_name="qwen3:8b",
                runtime_name="qwen3:8b",
                source="ollama",
                revision="8b",
                selector={"executor_ids": ["exec-a"], "match_labels": {}},
                provider_id=f"provider-{suffix}",
            ),
            LocalModelTargetStatus(
                target_id=f"lmt-{suffix}",
                deployment_id=f"lmd-{suffix}",
                executor_id="exec-a",
                generation=1,
                observed_generation=1 if target_state == "ready" else 0,
                state=target_state,
            ),
        ]
    )
    if include_operation:
        session.add(
            LocalModelOperation(
                operation_id=f"lmo-{suffix}",
                deployment_id=f"lmd-{suffix}",
                executor_id="exec-a",
                generation=1,
                action="pull",
                state="queued",
                idempotency_key=suffix,
                request_hash=f"sha256:{suffix}",
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_reconciler_repulls_wiped_ready_model(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "wipe.db")
    runtime = _RuntimeManager()
    runtime.enabled["exec-a"] = True
    runtime.statuses["exec-a"] = OllamaRuntimeStatus(
        management_enabled=True,
        reachable=True,
        installed=[],
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _add_provider(session, provider_id="provider-wipe", executor_id="exec-a")
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-wipe",
                    owner_email="owner@example.com",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                    update_policy="always",
                    provider_id="provider-wipe",
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-wipe",
                    deployment_id="lmd-wipe",
                    executor_id="exec-a",
                    generation=1,
                    observed_generation=1,
                    state="ready",
                )
            )
            await session.commit()

        reconciler = LocalModelReconciler(session_factory, runtime)  # type: ignore[arg-type]
        await reconciler.reconcile_now()

        async with session_factory() as session:
            operations = (
                await session.execute(
                    LocalModelOperation.__table__.select().where(
                        LocalModelOperation.deployment_id == "lmd-wipe"
                    )
                )
            ).all()
            target = await session.get(LocalModelTargetStatus, "lmt-wipe")
            assert target is not None
            assert target.state == "reconciling"
            assert len(operations) == 1
            assert operations[0].action == "pull"
            assert runtime.dispatched == [operations[0].operation_id]
            operation = await session.get(
                LocalModelOperation,
                operations[0].operation_id,
            )
            assert operation is not None
            operation.state = "succeeded"
            target.state = "pending"
            target.current_operation_id = None
            target.observed_generation = 1
            await session.commit()

        runtime.statuses["exec-a"] = OllamaRuntimeStatus(
            management_enabled=True,
            reachable=True,
            installed=[{"name": "llama3.2:latest", "digest": "sha256:model"}],
        )
        await reconciler.reconcile_now()
        async with session_factory() as session:
            target = await session.get(LocalModelTargetStatus, "lmt-wipe")
            assert target is not None and target.state == "ready"
            operations = (
                await session.execute(
                    LocalModelOperation.__table__.select().where(
                        LocalModelOperation.deployment_id == "lmd-wipe"
                    )
                )
            ).all()
            assert len(operations) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_skips_legacy_deployment_needing_provider(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "needs-provider.db")
    runtime = _RuntimeManager()
    runtime.enabled["exec-a"] = True
    runtime.statuses["exec-a"] = OllamaRuntimeStatus(
        management_enabled=True,
        reachable=True,
        installed=[],
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-needs-provider",
                    owner_email="owner@example.com",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-needs-provider",
                    deployment_id="lmd-needs-provider",
                    executor_id="exec-a",
                    generation=1,
                )
            )
            await session.commit()

        reconciler = LocalModelReconciler(session_factory, runtime)  # type: ignore[arg-type]
        await reconciler.reconcile_now()
        async with session_factory() as session:
            target = await session.get(LocalModelTargetStatus, "lmt-needs-provider")
            assert target is not None and target.state == "pending"
        assert runtime.dispatched == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_union_present_reference_blocks_explicit_prune_delete(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "union.db")
    runtime = _RuntimeManager()
    runtime.enabled["exec-a"] = True
    runtime.statuses["exec-a"] = OllamaRuntimeStatus(
        management_enabled=True,
        reachable=True,
        installed=[{"name": "qwen3:8b", "digest": "sha256:model", "size": 42}],
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _add_provider(session, provider_id="provider-union", executor_id="exec-a")
            for deployment_id, desired_state, prune_policy in [
                ("lmd-present", "present", "retain"),
                ("lmd-delete", "absent", "delete"),
            ]:
                session.add(
                    LocalModelDeployment(
                        deployment_id=deployment_id,
                        owner_email="owner@example.com",
                        requested_ref="qwen3:8b",
                        canonical_name="qwen3:8b",
                        runtime_name="qwen3:8b",
                        source="ollama",
                        revision="8b",
                        selector={"executor_ids": ["exec-a"], "match_labels": {}},
                        desired_state=desired_state,
                        prune_policy=prune_policy,
                        provider_id="provider-union",
                    )
                )
                session.add(
                    LocalModelTargetStatus(
                        target_id=f"lmt-{deployment_id}",
                        deployment_id=deployment_id,
                        executor_id="exec-a",
                        generation=1,
                    )
                )
            await session.commit()

        reconciler = LocalModelReconciler(session_factory, runtime)  # type: ignore[arg-type]
        await reconciler.reconcile_now()

        async with session_factory() as session:
            present = await session.get(
                LocalModelTargetStatus,
                "lmt-lmd-present",
            )
            retained = await session.get(
                LocalModelTargetStatus,
                "lmt-lmd-delete",
            )
            assert present is not None and present.state == "ready"
            assert retained is not None and retained.state == "blocked"
            assert "another deployment" in (retained.last_error or "")
            assert runtime.dispatched == []
    finally:
        await engine.dispose()


class _WSProvider:
    def __init__(self, executor_id: str) -> None:
        self.handle = SimpleNamespace(
            executor_id=executor_id,
            metadata={"owner_email": "owner@example.com", "labels": {"gpu": "nvidia"}},
            capabilities=SimpleNamespace(local_inference=True),
        )
        self.connection = object()

    async def list_active(self) -> list[Any]:
        return [self.handle]

    async def get_executor(self, handle: Any) -> Any:
        assert handle is self.handle
        return self.connection


class _ManagedConnection:
    def __init__(self, on_start: Any | None = None) -> None:
        self.capabilities = SimpleNamespace(
            local_inference=True, local_model_runtime=SimpleNamespace(management_enabled=True)
        )
        self.starts = 0
        self.on_start = on_start
        self.cancelled: list[str] = []

    async def local_model_operation_start(self, request: Any) -> Any:
        self.starts += 1
        if self.on_start is not None:
            self.on_start()
        return SimpleNamespace(state="running")

    async def local_model_operation_cancel(self, operation_id: str) -> dict[str, bool]:
        self.cancelled.append(operation_id)
        return {"acknowledged": False}


class _ManagedWSProvider:
    def __init__(self, connection: _ManagedConnection) -> None:
        self.connection = connection

    def get_connection(self, executor_id: str) -> _ManagedConnection | None:
        return self.connection if executor_id == "exec-a" else None

    def register_local_model_callbacks(self, **_: Any) -> None:
        return None


class _MappedWSProvider:
    def __init__(self, connections: dict[str, _ManagedConnection]) -> None:
        self.connections = connections

    def get_connection(self, executor_id: str) -> _ManagedConnection | None:
        return self.connections.get(executor_id)

    def register_local_model_callbacks(self, **_: Any) -> None:
        return None


class _CompletingConnection(_ManagedConnection):
    manager: LocalModelRuntimeManager

    async def local_model_operation_start(self, request: Any) -> Any:
        self.starts += 1
        await self.manager._handle_completed(  # noqa: SLF001
            "exec-a",
            {
                "operation_id": request.operation_id,
                "state": "succeeded",
            },
        )
        return SimpleNamespace(state="running")


@pytest.mark.asyncio
async def test_readiness_upsert_is_idempotent_and_preserves_provider_default(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "readiness-upsert.db")
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(_ManagedConnection()),
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            session.add(
                LLMProvider(
                    provider_id="provider-ready",
                    display_name="Provider",
                    location="executor",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={
                        "preset": "ollama",
                        "executor_id": "exec-a",
                        "models": [],
                        "default_model": "existing:latest",
                    },
                    status="active",
                )
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-ready",
                    owner_email="owner@example.com",
                    requested_ref="qwen3:8b",
                    canonical_name="qwen3:8b",
                    runtime_name="qwen3:8b",
                    source="ollama",
                    revision="8b",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                    provider_id="provider-ready",
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-ready",
                    deployment_id="lmd-ready",
                    executor_id="exec-a",
                    generation=1,
                    observed_generation=0,
                    state="pending",
                )
            )
            await session.commit()

        await manager.ensure_observed_provider_upsert(
            deployment_id="lmd-ready",
            executor_id="exec-a",
            generation=1,
        )
        async with session_factory() as session:
            provider = await session.get(LLMProvider, "provider-ready")
            assert provider is not None and provider.config["models"] == []
            target = await session.get(LocalModelTargetStatus, "lmt-ready")
            assert target is not None
            target.state = "ready"
            target.observed_generation = 1
            await session.commit()
        await manager.ensure_observed_provider_upsert(
            deployment_id="lmd-ready",
            executor_id="exec-a",
            generation=0,
        )

        for _ in range(2):
            await manager.ensure_observed_provider_upsert(
                deployment_id="lmd-ready",
                executor_id="exec-a",
                generation=1,
            )

        async with session_factory() as session:
            provider = await session.get(LLMProvider, "provider-ready")
            assert provider is not None
            assert provider.config["default_model"] == "existing:latest"
            assert provider.config["models"] == [
                {
                    "model_id": "qwen3:8b",
                    "cognis_managed_deployment_ids": ["lmd-ready"],
                }
            ]
    finally:
        await manager.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_manager_never_dispatches_stale_generation(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "stale.db")
    connection = _ManagedConnection()
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(connection),
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            executor = await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _mark_executor_local_inference_confirmed(executor)
            _add_provider(
                session,
                provider_id="provider-stale",
                executor_id="exec-a",
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-stale",
                    owner_email="owner@example.com",
                    requested_ref="new-model",
                    canonical_name="new-model:latest",
                    runtime_name="new-model:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                    generation=2,
                    provider_id="provider-stale",
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-stale",
                    deployment_id="lmd-stale",
                    executor_id="exec-a",
                    generation=2,
                )
            )
            session.add(
                LocalModelOperation(
                    operation_id="lmo-stale",
                    deployment_id="lmd-stale",
                    executor_id="exec-a",
                    generation=1,
                    action="delete",
                    state="queued",
                    idempotency_key="stale-delete",
                    request_hash="sha256:stale-delete",
                )
            )
            await session.commit()

        assert await manager.dispatch("lmo-stale") is False
        assert connection.starts == 0
        async with session_factory() as session:
            operation = await session.get(LocalModelOperation, "lmo-stale")
            assert operation is not None and operation.state == "cancelled"
            session.add(
                LocalModelOperation(
                    operation_id="lmo-cancel-before-start",
                    deployment_id="lmd-stale",
                    executor_id="exec-a",
                    generation=2,
                    action="pull",
                    state="queued",
                    idempotency_key="cancel-before-start",
                    request_hash="sha256:cancel-before-start",
                )
            )
            await session.commit()
        result = await manager.cancel(
            "lmo-cancel-before-start",
            executor_id="exec-a",
        )
        assert result["acknowledged"] is True
        assert await manager.dispatch("lmo-cancel-before-start") is False
        assert connection.starts == 0
        async with session_factory() as session:
            session.add_all(
                [
                    LocalModelOperation(
                        operation_id="lmo-running-reconnect",
                        deployment_id="lmd-stale",
                        executor_id="exec-a",
                        generation=2,
                        action="pull",
                        state="running",
                        idempotency_key="running-reconnect",
                        request_hash="sha256:running-reconnect",
                    ),
                    LocalModelOperation(
                        operation_id="lmo-cancel-reconnect",
                        deployment_id="lmd-stale",
                        executor_id="exec-a",
                        generation=2,
                        action="pull",
                        state="cancel_requested",
                        idempotency_key="cancel-reconnect",
                        request_hash="sha256:cancel-reconnect",
                    ),
                ]
            )
            await session.commit()
        await manager.executor_disconnected("exec-a")
        async with session_factory() as session:
            cancel_operation = await session.get(
                LocalModelOperation,
                "lmo-cancel-reconnect",
            )
            assert cancel_operation is not None
            assert cancel_operation.state == "cancel_requested"
        await manager.executor_connected("exec-a")
        async with session_factory() as session:
            running_operation = await session.get(
                LocalModelOperation,
                "lmo-running-reconnect",
            )
            cancel_operation = await session.get(
                LocalModelOperation,
                "lmo-cancel-reconnect",
            )
            assert running_operation is not None
            assert running_operation.state == "running"
            assert cancel_operation is not None
            assert cancel_operation.state == "cancel_requested"
        assert connection.starts == 1
        assert connection.cancelled == ["lmo-cancel-reconnect"]
        assert len(manager._locks) == 0  # noqa: SLF001
    finally:
        await engine.dispose()


@pytest.mark.parametrize("recovery_method", ["executor_connected", "executor_disconnected"])
@pytest.mark.asyncio
async def test_cancellation_wins_connect_disconnect_snapshot_race(
    recovery_method: str,
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(
        tmp_path,
        f"cancel-{recovery_method}.db",
    )
    connection = _ManagedConnection()
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(connection),
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _add_provider(
                session,
                provider_id="provider-cancel-race",
                executor_id="exec-a",
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-cancel-race",
                    owner_email="owner@example.com",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                    provider_id="provider-cancel-race",
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-cancel-race",
                    deployment_id="lmd-cancel-race",
                    executor_id="exec-a",
                    generation=1,
                    state="reconciling",
                )
            )
            session.add(
                LocalModelOperation(
                    operation_id="lmo-cancel-race",
                    deployment_id="lmd-cancel-race",
                    executor_id="exec-a",
                    generation=1,
                    action="pull",
                    state="running",
                    idempotency_key="cancel-race",
                    request_hash="sha256:cancel-race",
                )
            )
            await session.commit()

        operation_lock = manager._locks.setdefault(  # noqa: SLF001
            "lmo-cancel-race",
            asyncio.Lock(),
        )
        await operation_lock.acquire()
        cancel_task = asyncio.create_task(manager.cancel("lmo-cancel-race", executor_id="exec-a"))
        recovery_task = asyncio.create_task(getattr(manager, recovery_method)("exec-a"))
        await asyncio.sleep(0.05)
        operation_lock.release()
        await asyncio.gather(cancel_task, recovery_task)

        async with session_factory() as session:
            operation = await session.get(LocalModelOperation, "lmo-cancel-race")
            assert operation is not None and operation.state == "cancel_requested"
        assert connection.starts == 0
        assert connection.cancelled
        assert set(connection.cancelled) == {"lmo-cancel-race"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replacement_connection_rebinds_running_dispatch(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "replacement.db")
    replacement = _ManagedConnection()
    provider = _ManagedWSProvider(replacement)
    old = _ManagedConnection(on_start=lambda: setattr(provider, "connection", replacement))
    provider.connection = old
    manager = LocalModelRuntimeManager(session_factory, provider)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            executor = await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _mark_executor_local_inference_confirmed(executor)
            _add_provider(
                session,
                provider_id="provider-replacement",
                executor_id="exec-a",
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-replacement",
                    owner_email="owner@example.com",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                    provider_id="provider-replacement",
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-replacement",
                    deployment_id="lmd-replacement",
                    executor_id="exec-a",
                    generation=1,
                )
            )
            session.add(
                LocalModelOperation(
                    operation_id="lmo-replacement",
                    deployment_id="lmd-replacement",
                    executor_id="exec-a",
                    generation=1,
                    action="pull",
                    state="queued",
                    idempotency_key="replacement",
                    request_hash="sha256:replacement",
                )
            )
            await session.commit()

        assert await manager.dispatch("lmo-replacement") is True
        async with session_factory() as session:
            operation = await session.get(LocalModelOperation, "lmo-replacement")
            assert operation is not None and operation.state == "interrupted"
        await manager.executor_connected("exec-a")
        async with session_factory() as session:
            operation = await session.get(LocalModelOperation, "lmo-replacement")
            assert operation is not None and operation.state == "running"
        assert old.starts == 1
        assert replacement.starts == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completion_racing_dispatch_is_persisted_after_start_commit(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "completion-race.db")
    connection = _CompletingConnection()
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(connection),
    )
    connection.manager = manager
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            executor = await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _mark_executor_local_inference_confirmed(executor)
            _add_provider(
                session,
                provider_id="provider-completion-race-current",
                executor_id="exec-a",
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-completion-race",
                    owner_email="owner@example.com",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                    provider_id="provider-completion-race-current",
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-completion-race",
                    deployment_id="lmd-completion-race",
                    executor_id="exec-a",
                    generation=1,
                )
            )
            session.add(
                LocalModelOperation(
                    operation_id="lmo-completion-race",
                    deployment_id="lmd-completion-race",
                    executor_id="exec-a",
                    generation=1,
                    action="pull",
                    state="queued",
                    idempotency_key="completion-race",
                    request_hash="sha256:completion-race",
                )
            )
            await session.commit()

        assert await manager.dispatch("lmo-completion-race") is True
        for _ in range(100):
            async with session_factory() as session:
                operation = await session.get(
                    LocalModelOperation,
                    "lmo-completion-race",
                )
                if operation is not None and operation.state == "succeeded":
                    break
            await asyncio.sleep(0.01)
        assert operation is not None and operation.state == "succeeded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_records_disabled_and_offline_targets_independently(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "dispatch-isolation.db")
    disabled = _ManagedConnection()
    disabled.capabilities.local_model_runtime.management_enabled = False
    manager = LocalModelRuntimeManager(
        session_factory,
        _MappedWSProvider({"exec-disabled": disabled}),
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            for executor_id in ("exec-disabled", "exec-offline"):
                executor = await create_executor(
                    session,
                    executor_id=executor_id,
                    name=executor_id,
                    executor_type="websocket",
                    owner_email="owner@example.com",
                )
                _mark_executor_local_inference_confirmed(executor)
                _add_provider(
                    session,
                    provider_id=f"provider-{executor_id}",
                    executor_id=executor_id,
                )
                deployment_id = f"lmd-{executor_id}"
                session.add(
                    LocalModelDeployment(
                        deployment_id=deployment_id,
                        owner_email="owner@example.com",
                        requested_ref="llama3.2",
                        canonical_name="llama3.2:latest",
                        runtime_name="llama3.2:latest",
                        source="ollama",
                        revision="latest",
                        selector={"executor_ids": [executor_id], "match_labels": {}},
                        provider_id=f"provider-{executor_id}",
                    )
                )
                session.add(
                    LocalModelTargetStatus(
                        target_id=f"lmt-{executor_id}",
                        deployment_id=deployment_id,
                        executor_id=executor_id,
                        generation=1,
                    )
                )
                session.add(
                    LocalModelOperation(
                        operation_id=f"lmo-{executor_id}",
                        deployment_id=deployment_id,
                        executor_id=executor_id,
                        generation=1,
                        action="pull",
                        state="queued",
                        idempotency_key=f"dispatch-{executor_id}",
                        request_hash=f"sha256:dispatch-{executor_id}",
                    )
                )
            await session.commit()

        assert await manager.dispatch("lmo-exec-disabled") is False
        assert await manager.dispatch("lmo-exec-offline") is False
        async with session_factory() as session:
            disabled_operation = await session.get(
                LocalModelOperation,
                "lmo-exec-disabled",
            )
            disabled_target = await session.get(
                LocalModelTargetStatus,
                "lmt-exec-disabled",
            )
            offline_operation = await session.get(
                LocalModelOperation,
                "lmo-exec-offline",
            )
            offline_target = await session.get(
                LocalModelTargetStatus,
                "lmt-exec-offline",
            )
            assert disabled_operation is not None
            assert disabled_operation.state == "cancelled"
            assert disabled_target is not None and disabled_target.state == "blocked"
            assert offline_operation is not None and offline_operation.state == "queued"
            assert offline_target is not None and offline_target.state == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_fails_closed_while_executor_port_change_is_applying(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "dispatch-applying.db")
    connection = _ManagedConnection()
    manager = LocalModelRuntimeManager(
        session_factory,
        _MappedWSProvider({"exec-a": connection}),
    )
    try:
        async with session_factory() as session:
            await _seed_provider_scoped_runtime(session, suffix="dispatch-applying")
            executor = await session.get(ExecutorRow, "exec-a")
            assert executor is not None
            executor.config = {"ollama_runtime": {"port": 22434}}
            executor.desired_config_version = 2
            executor.runtime_state = "reconfiguring"
            await session.commit()

        assert await manager.dispatch("lmo-dispatch-applying") is False
        assert connection.starts == 0
        async with session_factory() as session:
            target = await session.get(LocalModelTargetStatus, "lmt-dispatch-applying")
            assert target is not None
            assert target.state == "pending"
            assert target.last_error == "executor local inference configuration is still applying"
    finally:
        await manager.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_rejects_provider_label_change_after_queue(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "dispatch-provider-label.db")
    manager = LocalModelRuntimeManager(
        session_factory,
        _MappedWSProvider({}),
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"pool": "managed"},
            )
            session.add_all(
                [
                    LLMProvider(
                        provider_id="provider-label",
                        display_name="Provider",
                        location="executor",
                        backend="litellm",
                        owner_email="owner@example.com",
                        config={
                            "preset": "ollama",
                            "executor_labels": {"pool": "managed"},
                            "models": [],
                        },
                        status="active",
                    ),
                    LocalModelDeployment(
                        deployment_id="lmd-provider-label",
                        owner_email="owner@example.com",
                        requested_ref="qwen3:8b",
                        canonical_name="qwen3:8b",
                        runtime_name="qwen3:8b",
                        source="ollama",
                        revision="8b",
                        selector={"executor_ids": ["exec-a"], "match_labels": {}},
                        provider_id="provider-label",
                    ),
                    LocalModelTargetStatus(
                        target_id="lmt-provider-label",
                        deployment_id="lmd-provider-label",
                        executor_id="exec-a",
                        generation=1,
                    ),
                    LocalModelOperation(
                        operation_id="lmo-provider-label",
                        deployment_id="lmd-provider-label",
                        executor_id="exec-a",
                        generation=1,
                        action="pull",
                        state="queued",
                        idempotency_key="provider-label",
                        request_hash="sha256:provider-label",
                    ),
                ]
            )
            await session.commit()

        async with session_factory() as session:
            executor = await session.get(ExecutorRow, "exec-a")
            assert executor is not None
            executor.labels = {"pool": "other"}
            await session.commit()

        assert await manager.dispatch("lmo-provider-label") is False
        async with session_factory() as session:
            operation = await session.get(LocalModelOperation, "lmo-provider-label")
            target = await session.get(LocalModelTargetStatus, "lmt-provider-label")
            assert operation is not None and operation.state == "cancelled"
            assert target is not None and target.state == "blocked"
            assert "provider scope" in (target.last_error or "")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_upsert_rejects_scope_change_before_completion(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "upsert-provider-scope.db")
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(_ManagedConnection()),
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"pool": "managed"},
            )
            session.add_all(
                [
                    LLMProvider(
                        provider_id="provider-completion-scope",
                        display_name="Provider",
                        location="executor",
                        backend="litellm",
                        owner_email="owner@example.com",
                        config={
                            "preset": "ollama",
                            "executor_labels": {"pool": "managed"},
                            "models": [],
                        },
                        status="active",
                    ),
                    LocalModelDeployment(
                        deployment_id="lmd-completion-scope",
                        owner_email="owner@example.com",
                        requested_ref="qwen3:8b",
                        canonical_name="qwen3:8b",
                        runtime_name="qwen3:8b",
                        source="ollama",
                        revision="8b",
                        selector={"executor_ids": ["exec-a"], "match_labels": {}},
                        provider_id="provider-completion-scope",
                    ),
                    LocalModelTargetStatus(
                        target_id="lmt-completion-scope",
                        deployment_id="lmd-completion-scope",
                        executor_id="exec-a",
                        generation=1,
                        observed_generation=1,
                        state="ready",
                    ),
                ]
            )
            await session.commit()

        async with session_factory() as session:
            executor = await session.get(ExecutorRow, "exec-a")
            assert executor is not None
            executor.labels = {"pool": "other"}
            await session.commit()

        await manager.ensure_observed_provider_upsert(
            deployment_id="lmd-completion-scope",
            executor_id="exec-a",
            generation=1,
        )
        async with session_factory() as session:
            provider = await session.get(LLMProvider, "provider-completion-scope")
            assert provider is not None
            assert provider.config["models"] == []
    finally:
        await manager.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_scope_writer_waits_until_rpc_start_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _database(tmp_path, "dispatch-scope-race.db")
    connection = _ManagedConnection()
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(connection),
    )
    entered_final_scope_check = asyncio.Event()
    release_scope_check = asyncio.Event()
    original_resolver = local_model_runtime.resolve_provider_scoped_deployment_executors
    calls = 0

    async def paused_resolver(session: Any, deployment: Any) -> Any:
        nonlocal calls
        result = await original_resolver(session, deployment)
        calls += 1
        if calls == 2:
            entered_final_scope_check.set()
            await release_scope_check.wait()
        return result

    monkeypatch.setattr(
        local_model_runtime,
        "resolve_provider_scoped_deployment_executors",
        paused_resolver,
    )
    try:
        async with session_factory() as session:
            await _seed_provider_scoped_runtime(session, suffix="dispatch-scope-race")

        dispatch_task = asyncio.create_task(manager.dispatch("lmo-dispatch-scope-race"))
        await entered_final_scope_check.wait()

        async def change_scope() -> None:
            async with session_factory() as session:
                await lock_local_model_dispatch_guard(session)
                executor = await session.get(ExecutorRow, "exec-a")
                assert executor is not None
                executor.labels = {"pool": "other"}
                await session.commit()

        scope_writer = asyncio.create_task(change_scope())
        await asyncio.sleep(0.05)
        assert not scope_writer.done()
        release_scope_check.set()

        assert await dispatch_task is True
        await scope_writer
        assert connection.starts == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completion_scope_writer_waits_until_provider_upsert_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _database(tmp_path, "completion-scope-race.db")
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(_ManagedConnection()),
    )
    entered_scope_check = asyncio.Event()
    release_scope_check = asyncio.Event()
    original_resolver = local_model_runtime.resolve_provider_scoped_deployment_executors

    async def paused_resolver(session: Any, deployment: Any) -> Any:
        result = await original_resolver(session, deployment)
        entered_scope_check.set()
        await release_scope_check.wait()
        return result

    monkeypatch.setattr(
        local_model_runtime,
        "resolve_provider_scoped_deployment_executors",
        paused_resolver,
    )
    try:
        async with session_factory() as session:
            await _seed_provider_scoped_runtime(
                session,
                suffix="completion-scope-race",
                target_state="ready",
                include_operation=False,
            )

        upsert_task = asyncio.create_task(
            manager.ensure_observed_provider_upsert(
                deployment_id="lmd-completion-scope-race",
                executor_id="exec-a",
                generation=1,
            )
        )
        await entered_scope_check.wait()

        async def change_scope() -> None:
            async with session_factory() as session:
                await lock_local_model_dispatch_guard(session)
                executor = await session.get(ExecutorRow, "exec-a")
                assert executor is not None
                executor.labels = {"pool": "other"}
                await session.commit()

        scope_writer = asyncio.create_task(change_scope())
        await asyncio.sleep(0.05)
        assert not scope_writer.done()
        release_scope_check.set()

        await upsert_task
        await scope_writer
        async with session_factory() as session:
            provider = await session.get(LLMProvider, "provider-completion-scope-race")
            assert provider is not None
            assert provider.config["models"] == [
                {
                    "model_id": "qwen3:8b",
                    "cognis_managed_deployment_ids": ["lmd-completion-scope-race"],
                }
            ]
    finally:
        await manager.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_revalidates_current_executor_scope(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "dispatch-scope.db")
    shared_connection = _ManagedConnection()
    inactive_connection = _ManagedConnection()
    manager = LocalModelRuntimeManager(
        session_factory,
        _MappedWSProvider(
            {
                "exec-shared": shared_connection,
                "exec-inactive": inactive_connection,
            }
        ),
    )
    try:
        async with session_factory() as session:
            session.add_all(
                [
                    User(email="owner@example.com", name="Owner", role="user"),
                    User(email="other@example.com", name="Other", role="user"),
                    User(
                        email="system@cognis.local",
                        name="System",
                        role="admin",
                    ),
                ]
            )
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-shared",
                name="Shared",
                executor_type="websocket",
                shared=True,
            )
            await create_executor(
                session,
                executor_id="exec-inactive",
                name="Inactive",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            for executor_id, owner_email in (
                ("exec-shared", "system@cognis.local"),
                ("exec-inactive", "owner@example.com"),
            ):
                deployment_id = f"lmd-scope-{executor_id}"
                session.add(
                    LocalModelDeployment(
                        deployment_id=deployment_id,
                        owner_email=owner_email,
                        requested_ref="llama3.2",
                        canonical_name="llama3.2:latest",
                        runtime_name="llama3.2:latest",
                        source="ollama",
                        revision="latest",
                        selector={"executor_ids": [executor_id], "match_labels": {}},
                    )
                )
                session.add(
                    LocalModelTargetStatus(
                        target_id=f"lmt-scope-{executor_id}",
                        deployment_id=deployment_id,
                        executor_id=executor_id,
                        generation=1,
                    )
                )
                session.add(
                    LocalModelOperation(
                        operation_id=f"lmo-scope-{executor_id}",
                        deployment_id=deployment_id,
                        executor_id=executor_id,
                        generation=1,
                        action="pull",
                        state="queued",
                        idempotency_key=f"scope-{executor_id}",
                        request_hash=f"sha256:scope-{executor_id}",
                    )
                )
            await session.commit()

        async with session_factory() as session:
            shared_executor = await session.get(ExecutorRow, "exec-shared")
            inactive_executor = await session.get(ExecutorRow, "exec-inactive")
            assert shared_executor is not None
            assert inactive_executor is not None
            shared_executor.owner_email = "other@example.com"
            inactive_executor.status = "inactive"
            await session.commit()

        assert await manager.dispatch("lmo-scope-exec-shared") is False
        assert await manager.dispatch("lmo-scope-exec-inactive") is False
        async with session_factory() as session:
            for executor_id in ("exec-shared", "exec-inactive"):
                operation = await session.get(
                    LocalModelOperation,
                    f"lmo-scope-{executor_id}",
                )
                target = await session.get(
                    LocalModelTargetStatus,
                    f"lmt-scope-{executor_id}",
                )
                assert operation is not None and operation.state == "cancelled"
                assert target is not None and target.state == "blocked"
        assert shared_connection.starts == 0
        assert inactive_connection.starts == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_label_change_dependency_blocks_delete_dispatch(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "label-dependency.db")
    connection = _ManagedConnection()
    manager = LocalModelRuntimeManager(
        session_factory,
        _ManagedWSProvider(connection),
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            executor = await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"gpu": "amd"},
            )
            _mark_executor_local_inference_confirmed(executor)
            _add_provider(
                session,
                provider_id="provider-label-delete",
                executor_id="exec-a",
            )
            session.add_all(
                [
                    LocalModelDeployment(
                        deployment_id="lmd-label-delete",
                        owner_email="owner@example.com",
                        requested_ref="qwen3:8b",
                        canonical_name="qwen3:8b",
                        runtime_name="qwen3:8b",
                        source="ollama",
                        revision="8b",
                        selector={"executor_ids": ["exec-a"], "match_labels": {}},
                        desired_state="absent",
                        prune_policy="delete",
                        provider_id="provider-label-delete",
                    ),
                    LocalModelDeployment(
                        deployment_id="lmd-label-present",
                        owner_email="owner@example.com",
                        requested_ref="qwen3:8b",
                        canonical_name="qwen3:8b",
                        runtime_name="qwen3:8b",
                        source="ollama",
                        revision="8b",
                        selector={
                            "executor_ids": [],
                            "match_labels": {"gpu": "nvidia"},
                        },
                    ),
                    LocalModelTargetStatus(
                        target_id="lmt-label-delete",
                        deployment_id="lmd-label-delete",
                        executor_id="exec-a",
                        generation=1,
                    ),
                    LocalModelOperation(
                        operation_id="lmo-label-delete",
                        deployment_id="lmd-label-delete",
                        executor_id="exec-a",
                        generation=1,
                        action="delete",
                        state="queued",
                        idempotency_key="label-delete",
                        request_hash="sha256:label-delete",
                    ),
                ]
            )
            await session.commit()

        async with session_factory() as session:
            executor = await session.get(ExecutorRow, "exec-a")
            assert executor is not None
            executor.labels = {"gpu": "nvidia"}
            await session.commit()

        assert await manager.dispatch("lmo-label-delete") is False
        async with session_factory() as session:
            operation = await session.get(LocalModelOperation, "lmo-label-delete")
            target = await session.get(LocalModelTargetStatus, "lmt-label-delete")
            assert operation is not None and operation.state == "cancelled"
            assert target is not None and target.state == "blocked"
            assert "another deployment" in (target.last_error or "")
        assert connection.starts == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_isolates_target_errors_and_continues(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "isolation.db")
    runtime = _RuntimeManager()
    runtime.enabled.update({"exec-bad": True, "exec-good": True})
    runtime.status_errors["exec-bad"] = RuntimeError("token=secret unavailable")
    runtime.statuses["exec-good"] = OllamaRuntimeStatus(
        management_enabled=True,
        reachable=True,
        installed=[{"name": "llama3.2:latest", "digest": "sha256:ready"}],
    )
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            for executor_id in ("exec-bad", "exec-good"):
                await create_executor(
                    session,
                    executor_id=executor_id,
                    name=executor_id,
                    executor_type="websocket",
                    owner_email="owner@example.com",
                )
                provider_id = f"provider-{executor_id}"
                _add_provider(
                    session,
                    provider_id=provider_id,
                    executor_id=executor_id,
                )
                deployment_id = f"lmd-{executor_id}"
                session.add(
                    LocalModelDeployment(
                        deployment_id=deployment_id,
                        owner_email="owner@example.com",
                        requested_ref="llama3.2",
                        canonical_name="llama3.2:latest",
                        runtime_name="llama3.2:latest",
                        source="ollama",
                        revision="latest",
                        selector={"executor_ids": [executor_id], "match_labels": {}},
                        provider_id=provider_id,
                    )
                )
                session.add(
                    LocalModelTargetStatus(
                        target_id=f"lmt-{executor_id}",
                        deployment_id=deployment_id,
                        executor_id=executor_id,
                        generation=1,
                    )
                )
            await session.commit()

        reconciler = LocalModelReconciler(session_factory, runtime)  # type: ignore[arg-type]
        await reconciler.reconcile_now()
        async with session_factory() as session:
            failed = await session.get(LocalModelTargetStatus, "lmt-exec-bad")
            ready = await session.get(LocalModelTargetStatus, "lmt-exec-good")
            assert failed is not None and failed.state == "error"
            assert "secret" not in (failed.last_error or "")
            assert ready is not None and ready.state == "ready"
        await reconciler.stop()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_keyset_paginates_all_deployments(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path, "pagination.db")
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            session.add_all(
                [
                    LocalModelDeployment(
                        deployment_id=f"lmd-page-{index:04d}",
                        owner_email="owner@example.com",
                        requested_ref="llama3.2",
                        canonical_name="llama3.2:latest",
                        runtime_name="llama3.2:latest",
                        source="ollama",
                        revision="latest",
                        selector={"executor_ids": ["exec-a"], "match_labels": {}},
                    )
                    for index in range(1001)
                ]
            )
            await session.commit()
        async with session_factory() as session:
            deployments = await LocalModelReconciler._list_all_deployments(session)  # noqa: SLF001
        assert len(deployments) == 1001
        assert deployments[-1].deployment_id == "lmd-page-1000"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retry_deadline_schedules_reconcile_wake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "retry.db")
    runtime = _RuntimeManager()
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-retry",
                    owner_email="owner@example.com",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-retry",
                    deployment_id="lmd-retry",
                    executor_id="exec-a",
                    generation=1,
                )
            )
            await session.commit()
        reconciler = LocalModelReconciler(session_factory, runtime)  # type: ignore[arg-type]
        monkeypatch.setattr(
            "cognis.core.local_model_reconciler.random.uniform",
            lambda _low, _high: 0.01,
        )
        reconciler._wake.clear()  # noqa: SLF001
        await reconciler._target_failed(  # noqa: SLF001
            "lmt-retry",
            1,
            RuntimeError("temporary failure"),
        )
        await asyncio.wait_for(reconciler._wake.wait(), timeout=0.5)  # noqa: SLF001
        await reconciler.stop()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inference_routing_requires_managed_readiness_unless_overridden(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "routing.db")
    ws_provider = _WSProvider("exec-a")
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            executor = await session.get(ExecutorRow, "exec-a")
            assert executor is not None
            _mark_executor_local_inference_confirmed(executor)
            session.add(
                LLMProvider(
                    provider_id="ollama-provider",
                    display_name="Ollama",
                    location="executor",
                    backend="litellm",
                    owner_email="system@cognis.local",
                    config={"preset": "ollama"},
                    status="active",
                )
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-routing",
                    owner_email="owner@example.com",
                    provider_id="ollama-provider",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-routing",
                    deployment_id="lmd-routing",
                    executor_id="exec-a",
                    generation=1,
                    observed_generation=0,
                    state="pending",
                )
            )
            await session.commit()

        router = InferenceRouter(ws_provider, session_factory)  # type: ignore[arg-type]
        with pytest.raises(LocalModelRolloutUnavailableError) as exc_info:
            await router._find_executor(  # noqa: SLF001
                "exec-a",
                None,
                model="ollama/llama3.2:latest",
                provider_id="ollama-provider",
                owner_email="owner@example.com",
            )
        assert exc_info.value.summary["state_counts"] == {"pending": 1}
        assert exc_info.value.summary["reason"] == "no_ready_target"

        async with session_factory() as session:
            deployment = await session.get(LocalModelDeployment, "lmd-routing")
            assert deployment is not None
            deployment.capacity_override_acknowledged = True
            await create_executor(
                session,
                executor_id="exec-b",
                name="Ready Executor",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            ready_executor = await session.get(ExecutorRow, "exec-b")
            assert ready_executor is not None
            _mark_executor_local_inference_confirmed(ready_executor)
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-routing-ready",
                    owner_email="owner@example.com",
                    provider_id="ollama-provider",
                    requested_ref="llama3.2",
                    canonical_name="llama3.2:latest",
                    runtime_name="llama3.2:latest",
                    source="ollama",
                    revision="latest",
                    selector={"executor_ids": ["exec-b"], "match_labels": {}},
                )
            )
            session.add(
                LocalModelTargetStatus(
                    target_id="lmt-routing-ready",
                    deployment_id="lmd-routing-ready",
                    executor_id="exec-b",
                    generation=1,
                    observed_generation=1,
                    state="ready",
                )
            )
            await session.commit()
        readiness = await router._managed_readiness(  # noqa: SLF001
            provider_id="ollama-provider",
            model="ollama/llama3.2:latest",
            owner_email="owner@example.com",
        )
        assert readiness is not None
        assert readiness[0] == {"exec-a", "exec-b"}
        selected = await router._find_executor(  # noqa: SLF001
            "exec-a",
            None,
            model="ollama/llama3.2:latest",
            provider_id="ollama-provider",
            owner_email="owner@example.com",
        )
        assert selected is ws_provider.connection

        async with session_factory() as session:
            session.add(User(email="other@example.com", name="Other", role="user"))
            for executor_id in ("exec-a", "exec-b"):
                executor = await session.get(ExecutorRow, executor_id)
                assert executor is not None
                executor.owner_email = "other@example.com"
            await session.commit()
        with pytest.raises(LocalModelRolloutUnavailableError) as stale_target:
            await router._find_executor(  # noqa: SLF001
                "exec-a",
                None,
                model="ollama/llama3.2:latest",
                provider_id="ollama-provider",
                owner_email="owner@example.com",
            )
        assert stale_target.value.summary["state_counts"] == {"unauthorized": 2}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_selector_materialization_excludes_disabled_executor_and_reenables(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "capability-selector.db")
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            executor = await create_executor(
                session,
                executor_id="exec-capability",
                name="Executor",
                executor_type="websocket",
                labels={"gpu": "nvidia"},
                config={"local_inference_enabled": False},
                owner_email="owner@example.com",
            )
            exact = LocalModelDeployment(
                deployment_id="lmd-capability-exact",
                owner_email="owner@example.com",
                requested_ref="llama3.2",
                canonical_name="llama3.2:latest",
                runtime_name="llama3.2:latest",
                source="ollama",
                revision="latest",
                selector={"executor_ids": ["exec-capability"], "match_labels": {}},
            )
            labels = LocalModelDeployment(
                deployment_id="lmd-capability-labels",
                owner_email="owner@example.com",
                requested_ref="qwen3:8b",
                canonical_name="qwen3:8b",
                runtime_name="qwen3:8b",
                source="ollama",
                revision="8b",
                selector={"executor_ids": [], "match_labels": {"gpu": "nvidia"}},
            )
            session.add_all([exact, labels])
            await session.flush()

            assert await resolve_authorized_deployment_executors(session, exact) == []
            assert await resolve_authorized_deployment_executors(session, labels) == []

            executor.config = {"local_inference_enabled": True}
            await session.flush()
            assert [
                row.executor_id
                for row in await resolve_authorized_deployment_executors(session, exact)
            ] == ["exec-capability"]
            assert [
                row.executor_id
                for row in await resolve_authorized_deployment_executors(session, labels)
            ] == ["exec-capability"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inference_router_requires_persisted_and_advertised_capability(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _database(tmp_path, "capability-router.db")
    ws_provider = _WSProvider("exec-a")
    ws_provider.handle.metadata["shared"] = True
    try:
        async with session_factory() as session:
            session.add(
                User(
                    email="system@cognis.local",
                    name="System",
                    role="admin",
                )
            )
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor",
                executor_type="websocket",
                labels={"gpu": "nvidia"},
                config={"local_inference_enabled": False},
                owner_email="owner@example.com",
                shared=True,
            )
            await session.commit()
        router = InferenceRouter(ws_provider, session_factory)  # type: ignore[arg-type]
        assert await router._find_executor("exec-a", None) is None  # noqa: SLF001
        assert await router._find_executor(None, {"gpu": "nvidia"}) is None  # noqa: SLF001

        async with session_factory() as session:
            executor = await session.get(ExecutorRow, "exec-a")
            assert executor is not None
            executor.config = {"local_inference_enabled": True}
            _mark_executor_local_inference_confirmed(executor)
            await session.commit()
        ws_provider.handle.capabilities.local_inference = False
        assert await router._find_executor("exec-a", None) is None  # noqa: SLF001
        del ws_provider.handle.capabilities.local_inference
        assert await router._find_executor("exec-a", None) is None  # noqa: SLF001
        ws_provider.handle.capabilities.local_inference = "false"
        assert await router._find_executor("exec-a", None) is None  # noqa: SLF001

        ws_provider.handle.capabilities.local_inference = True
        assert await router._find_executor("exec-a", None) is ws_provider.connection  # noqa: SLF001
        assert (
            await router._find_executor(None, {"gpu": "nvidia"}) is ws_provider.connection  # noqa: SLF001
        )
    finally:
        await engine.dispose()
