from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cognis.core.local_model_providers import LocalModelProviderService
from cognis.core.local_model_service import (
    LocalModelDeploymentService,
    LocalModelValidationError,
)
from cognis.models.local_models import (
    LocalModelDeploymentCreateRequest,
    LocalModelDeploymentUpdateRequest,
    LocalModelOperationState,
    LocalModelSelector,
    ProviderLocalModelUpsertRequest,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.local_models import (
    create_local_model_operation,
    record_local_model_operation_progress,
    remove_generated_llm_provider_model_reference,
    transition_local_model_operation,
    update_local_model_target_status,
    upsert_llm_provider_model,
)
from cognis.store.models import (
    Base,
    LLMProvider,
    LocalModelDeployment,
    LocalModelOperation,
    LocalModelTargetStatus,
    User,
)
from cognis.store.queries import create_executor


def _add_ollama_provider(
    session: object,
    *,
    provider_id: str,
    owner_email: str,
    executor_id: str | None = None,
    executor_labels: dict[str, str] | None = None,
) -> None:
    config: dict[str, object] = {"preset": "ollama", "models": []}
    if executor_id is not None:
        config["executor_id"] = executor_id
    if executor_labels is not None:
        config["executor_labels"] = executor_labels
    session.add(  # type: ignore[attr-defined]
        LLMProvider(
            provider_id=provider_id,
            display_name=provider_id,
            location="executor",
            backend="litellm",
            owner_email=owner_email,
            config=config,
            status="active",
        )
    )


@pytest.mark.asyncio
async def test_operation_idempotency_progress_and_state_machine(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            executor = await create_executor(
                session,
                executor_id="exec-a",
                name="Executor A",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            deployment = LocalModelDeployment(
                deployment_id="lmd-operation-test",
                owner_email="owner@example.com",
                requested_ref="llama3.2",
                canonical_name="llama3.2:latest",
                runtime_name="llama3.2:latest",
                source="ollama",
                revision="latest",
                selector={"executor_ids": [executor.executor_id], "match_labels": {}},
            )
            session.add(deployment)
            await session.flush()
            target = LocalModelTargetStatus(
                target_id="lmt-operation-test",
                deployment_id=deployment.deployment_id,
                executor_id=executor.executor_id,
                generation=1,
            )
            session.add(target)
            await session.flush()

            operation, created = await create_local_model_operation(
                session,
                deployment_id=deployment.deployment_id,
                executor_id=executor.executor_id,
                generation=1,
                action="pull",
                idempotency_key="request-1",
                request_hash="sha256:first",
            )
            duplicate, duplicate_created = await create_local_model_operation(
                session,
                deployment_id=deployment.deployment_id,
                executor_id=executor.executor_id,
                generation=1,
                action="pull",
                idempotency_key="request-1",
                request_hash="sha256:first",
            )

            assert created is True
            assert duplicate_created is False
            assert duplicate.operation_id == operation.operation_id
            with pytest.raises(ValueError, match="different request"):
                await create_local_model_operation(
                    session,
                    deployment_id=deployment.deployment_id,
                    executor_id=executor.executor_id,
                    generation=1,
                    action="pull",
                    idempotency_key="request-1",
                    request_hash="sha256:second",
                )

            await transition_local_model_operation(
                session,
                operation.operation_id,
                LocalModelOperationState.RUNNING,
            )
            with pytest.raises(ValueError, match="signed int64"):
                await record_local_model_operation_progress(
                    session,
                    operation.operation_id,
                    progress_seq=1,
                    progress_bytes=2**63,
                    phase="downloading",
                )
            with pytest.raises(ValueError, match="signed int64"):
                await record_local_model_operation_progress(
                    session,
                    operation.operation_id,
                    progress_seq=1,
                    progress_bytes=True,
                    phase="downloading",
                )
            with pytest.raises(ValueError, match="signed int64"):
                await record_local_model_operation_progress(
                    session,
                    operation.operation_id,
                    progress_seq=1,
                    progress_bytes=1.5,  # type: ignore[arg-type]
                    phase="downloading",
                )
            with pytest.raises(ValueError, match="signed int64"):
                await record_local_model_operation_progress(
                    session,
                    operation.operation_id,
                    progress_seq=1,
                    progress_bytes=-1,
                    phase="downloading",
                )
            progressed, applied = await record_local_model_operation_progress(
                session,
                operation.operation_id,
                progress_seq=1,
                progress_bytes=5_629_109_111,
                phase="downloading",
            )
            repeated, repeated_applied = await record_local_model_operation_progress(
                session,
                operation.operation_id,
                progress_seq=1,
                progress_bytes=5_629_109_111,
                phase="downloading",
            )
            assert applied is True
            assert repeated_applied is False
            assert repeated.operation_id == progressed.operation_id
            with pytest.raises(ValueError, match="already used"):
                await record_local_model_operation_progress(
                    session,
                    operation.operation_id,
                    progress_seq=1,
                    progress_bytes=5_629_109_112,
                    phase="downloading",
                )
            updated_target = await update_local_model_target_status(
                session,
                target.target_id,
                expected_generation=1,
                state="ready",
                observed_generation=1,
                observed_size_bytes=5_629_109_111,
            )
            assert updated_target is not None
            assert updated_target.observed_size_bytes == 5_629_109_111
            assert progressed.progress_bytes == 5_629_109_111
            with pytest.raises(ValueError, match="signed int64"):
                await update_local_model_target_status(
                    session,
                    target.target_id,
                    expected_generation=1,
                    state="ready",
                    observed_size_bytes=2**63,
                )
            with pytest.raises(ValueError, match="signed int64"):
                await update_local_model_target_status(
                    session,
                    target.target_id,
                    expected_generation=1,
                    state="ready",
                    observed_size_bytes=True,
                )
            with pytest.raises(ValueError, match="signed int64"):
                await update_local_model_target_status(
                    session,
                    target.target_id,
                    expected_generation=1,
                    state="ready",
                    observed_size_bytes=1.5,  # type: ignore[arg-type]
                )

            failed = await transition_local_model_operation(
                session,
                operation.operation_id,
                LocalModelOperationState.FAILED,
                error="secret\nprovider failure\x00" + "x" * 1200,
            )
            assert failed.finished_at is not None
            assert failed.sanitized_error is not None
            assert "\n" not in failed.sanitized_error
            assert "\x00" not in failed.sanitized_error
            assert len(failed.sanitized_error) == 1000
            with pytest.raises(ValueError, match="invalid local-model operation transition"):
                await transition_local_model_operation(
                    session,
                    operation.operation_id,
                    LocalModelOperationState.RUNNING,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_provider_model_upsert_preserves_concurrent_updates(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'provider-upsert.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            session.add(
                LLMProvider(
                    provider_id="ollama-local",
                    display_name="Ollama",
                    location="executor",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={
                        "preset": "ollama",
                        "unknown_top_level": {"preserve": True},
                        "models": [
                            {"model_id": "llama3.2:latest", "existing": 1},
                            {"model_id": "llama3.2:latest", "duplicate": 2},
                        ],
                    },
                    status="active",
                )
            )
            await session.commit()

        async def upsert(reference: str, marker: str, *, set_default: bool = False) -> None:
            async with session_factory() as session:
                service = LocalModelDeploymentService(
                    session,
                    actor_email="owner@example.com",
                    actor_role="user",
                )
                await service.upsert_provider_model(
                    "ollama-local",
                    ProviderLocalModelUpsertRequest(
                        requested_ref=reference,
                        model_config={"marker": marker},
                        set_default=set_default,
                    ),
                )
                await session.commit()

        await asyncio.gather(
            upsert("qwen3:8b", "qwen", set_default=True),
            upsert("gemma3:4b", "gemma"),
        )

        async with session_factory() as session:
            provider = await session.get(LLMProvider, "ollama-local")
            assert provider is not None
            assert provider.config["unknown_top_level"] == {"preserve": True}
            models = provider.config["models"]
            by_id = {entry["model_id"]: entry for entry in models if isinstance(entry, dict)}
            assert set(by_id) == {
                "llama3.2:latest",
                "qwen3:8b",
                "gemma3:4b",
            }
            assert by_id["llama3.2:latest"] == {
                "model_id": "llama3.2:latest",
                "existing": 1,
                "duplicate": 2,
            }
            assert by_id["qwen3:8b"]["marker"] == "qwen"
            assert by_id["gemma3:4b"]["marker"] == "gemma"
            assert provider.config["default_model"] == "qwen3:8b"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_find_or_create_provider_is_concurrency_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'provider-create.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor A",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            await session.commit()

        async def resolve() -> tuple[str, bool]:
            async with session_factory() as session:
                service = LocalModelProviderService(
                    session,
                    actor_email="owner@example.com",
                    actor_role="user",
                )
                provider, created, _reason = await service.find_or_create(
                    runtime_name="qwen3:8b",
                    selector=LocalModelSelector(executor_ids=["exec-a"]),
                    shared=False,
                    force_create=False,
                )
                await session.commit()
                return provider.provider_id, created

        results = await asyncio.gather(resolve(), resolve())
        assert len({provider_id for provider_id, _created in results}) == 1
        assert sum(created for _provider_id, created in results) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generated_provider_model_cleanup_preserves_manual_reference(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'provider-cleanup.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            provider = LLMProvider(
                provider_id="ollama-local",
                display_name="Ollama",
                location="executor",
                backend="litellm",
                owner_email="owner@example.com",
                config={"preset": "ollama", "executor_id": "exec-a", "models": []},
                status="active",
            )
            session.add(provider)
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor A",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            deployment = LocalModelDeployment(
                deployment_id="lmd-cleanup",
                owner_email="owner@example.com",
                requested_ref="qwen3:8b",
                canonical_name="qwen3:8b",
                runtime_name="qwen3:8b",
                source="ollama",
                revision="8b",
                selector={"executor_ids": ["exec-a"], "match_labels": {}},
                provider_id=provider.provider_id,
            )
            session.add(deployment)
            await session.flush()

            await upsert_llm_provider_model(
                session,
                provider,
                model_id="qwen3:8b",
                model_config={},
                set_default=True,
                managed_deployment_id=deployment.deployment_id,
            )
            await upsert_llm_provider_model(
                session,
                provider,
                model_id="qwen3:8b",
                model_config={"context_window": 32768},
                set_default=True,
            )
            await remove_generated_llm_provider_model_reference(
                session,
                provider,
                model_id="qwen3:8b",
                deployment_id=deployment.deployment_id,
            )

            assert provider.config["default_model"] == "qwen3:8b"
            assert provider.config["models"] == [{"model_id": "qwen3:8b", "context_window": 32768}]

            session.add_all(
                [
                    LocalModelDeployment(
                        deployment_id="lmd-generated-ready",
                        owner_email="owner@example.com",
                        requested_ref="gemma3:4b",
                        canonical_name="gemma3:4b",
                        runtime_name="gemma3:4b",
                        source="ollama",
                        revision="4b",
                        selector={"executor_ids": ["exec-a"], "match_labels": {}},
                        provider_id=provider.provider_id,
                    ),
                    LocalModelDeployment(
                        deployment_id="lmd-generated-pending",
                        owner_email="owner@example.com",
                        requested_ref="gemma3:4b",
                        canonical_name="gemma3:4b",
                        runtime_name="gemma3:4b",
                        source="ollama",
                        revision="4b",
                        selector={"executor_ids": ["exec-a"], "match_labels": {}},
                        provider_id=provider.provider_id,
                    ),
                ]
            )
            await session.flush()
            await upsert_llm_provider_model(
                session,
                provider,
                model_id="gemma3:4b",
                model_config={},
                set_default=False,
                managed_deployment_id="lmd-generated-ready",
            )
            await remove_generated_llm_provider_model_reference(
                session,
                provider,
                model_id="gemma3:4b",
                deployment_id="lmd-generated-ready",
            )
            assert provider.config["models"] == [{"model_id": "qwen3:8b", "context_window": 32768}]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_service_materializes_label_selector_and_capacity_override(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'selector.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-match",
                name="Match",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"gpu": "nvidia", "site": "lab"},
            )
            await create_executor(
                session,
                executor_id="exec-other",
                name="Other",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"gpu": "amd", "site": "lab"},
            )
            _add_ollama_provider(
                session,
                provider_id="ollama-nvidia",
                owner_email="owner@example.com",
                executor_labels={"gpu": "nvidia"},
            )
            service = LocalModelDeploymentService(
                session,
                actor_email="owner@example.com",
                actor_role="user",
            )
            deployment = await service.create_deployment(
                LocalModelDeploymentCreateRequest(
                    requested_ref="llama3.2",
                    selector=LocalModelSelector(match_labels={"gpu": "nvidia"}),
                    provider_id="ollama-nvidia",
                    capacity_override_acknowledged=True,
                    capacity_assessment_generation=2**53 - 1,
                )
            )
            targets = await service.list_targets(deployment.deployment_id)

            assert deployment.capacity_override_acknowledged is True
            assert deployment.capacity_assessment_generation == 2**53 - 1
            assert [target.executor_id for target in targets] == ["exec-match"]
            assert targets[0].state == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_label_selector_does_not_match_missing_or_non_string_values(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'selector-types.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-missing",
                name="Missing label",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"site": "lab"},
            )
            await create_executor(
                session,
                executor_id="exec-null",
                name="Null label",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"gpu": None},
            )
            _add_ollama_provider(
                session,
                provider_id="ollama-missing",
                owner_email="owner@example.com",
                executor_id="exec-missing",
            )
            service = LocalModelDeploymentService(
                session,
                actor_email="owner@example.com",
                actor_role="user",
            )

            with pytest.raises(
                LocalModelValidationError,
                match="matched no authorized",
            ):
                await service.create_deployment(
                    LocalModelDeploymentCreateRequest(
                        requested_ref="qwen3:8b",
                        selector=LocalModelSelector(match_labels={"gpu": "None"}),
                        provider_id="ollama-missing",
                    )
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_label_reresolution_advances_generation_when_target_set_changes(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'selector-generation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-first",
                name="First",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"gpu": "nvidia"},
            )
            second = await create_executor(
                session,
                executor_id="exec-second",
                name="Second",
                executor_type="websocket",
                owner_email="owner@example.com",
                labels={"gpu": "amd"},
            )
            _add_ollama_provider(
                session,
                provider_id="ollama-nvidia",
                owner_email="owner@example.com",
                executor_labels={"gpu": "nvidia"},
            )
            service = LocalModelDeploymentService(
                session,
                actor_email="owner@example.com",
                actor_role="user",
            )
            deployment = await service.create_deployment(
                LocalModelDeploymentCreateRequest(
                    requested_ref="qwen3:8b",
                    selector=LocalModelSelector(match_labels={"gpu": "nvidia"}),
                    provider_id="ollama-nvidia",
                )
            )
            assert deployment.generation == 1

            second.labels = {"gpu": "nvidia"}
            await session.flush()
            deployment = await service.update_deployment(
                deployment.deployment_id,
                LocalModelDeploymentUpdateRequest(max_parallel=1),
            )
            targets = await service.list_targets(deployment.deployment_id)

            assert deployment.generation == 2
            assert [target.executor_id for target in targets] == [
                "exec-first",
                "exec-second",
            ]
            assert all(target.generation == 2 for target in targets)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_deployment_updates_serialize_generation_and_fields(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'deployment-concurrency.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor A",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _add_ollama_provider(
                session,
                provider_id="ollama-exec-a",
                owner_email="owner@example.com",
                executor_id="exec-a",
            )
            service = LocalModelDeploymentService(
                session,
                actor_email="owner@example.com",
                actor_role="user",
            )
            deployment = await service.create_deployment(
                LocalModelDeploymentCreateRequest(
                    requested_ref="qwen3:8b",
                    selector=LocalModelSelector(executor_ids=["exec-a"]),
                    provider_id="ollama-exec-a",
                )
            )
            await session.commit()
            deployment_id = deployment.deployment_id

        async def patch_deployment(
            payload: LocalModelDeploymentUpdateRequest,
        ) -> int:
            async with session_factory() as session:
                service = LocalModelDeploymentService(
                    session,
                    actor_email="owner@example.com",
                    actor_role="user",
                )
                deployment = await service.update_deployment(
                    deployment_id,
                    payload,
                )
                await session.commit()
                return deployment.generation

        generations = await asyncio.gather(
            patch_deployment(LocalModelDeploymentUpdateRequest(max_parallel=2)),
            patch_deployment(LocalModelDeploymentUpdateRequest(desired_state="absent")),
        )

        assert sorted(generations) == [2, 3]
        async with session_factory() as session:
            deployment = await session.get(LocalModelDeployment, deployment_id)
            assert deployment is not None
            assert deployment.generation == 3
            assert deployment.max_parallel == 2
            assert deployment.desired_state == "absent"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_noop_patch_preserves_observed_target_state(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'noop-patch.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor A",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            _add_ollama_provider(
                session,
                provider_id="ollama-exec-a",
                owner_email="owner@example.com",
                executor_id="exec-a",
            )
            service = LocalModelDeploymentService(
                session,
                actor_email="owner@example.com",
                actor_role="user",
            )
            deployment = await service.create_deployment(
                LocalModelDeploymentCreateRequest(
                    requested_ref="qwen3:8b",
                    selector=LocalModelSelector(executor_ids=["exec-a"]),
                    provider_id="ollama-exec-a",
                )
            )
            targets = await service.list_targets(deployment.deployment_id)
            targets[0].state = "error"
            targets[0].last_error = "preserve this observation"
            await session.flush()

            unchanged = await service.update_deployment(
                deployment.deployment_id,
                LocalModelDeploymentUpdateRequest(max_parallel=1),
            )
            targets = await service.list_targets(deployment.deployment_id)

            assert unchanged.generation == 1
            assert targets[0].state == "error"
            assert targets[0].last_error == "preserve this observation"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_operation_concurrency_is_atomic(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'operation-concurrency.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await create_executor(
                session,
                executor_id="exec-a",
                name="Executor A",
                executor_type="websocket",
                owner_email="owner@example.com",
            )
            session.add(
                LocalModelDeployment(
                    deployment_id="lmd-concurrency",
                    owner_email="owner@example.com",
                    requested_ref="qwen3:8b",
                    canonical_name="qwen3:8b",
                    runtime_name="qwen3:8b",
                    source="ollama",
                    revision="8b",
                    selector={"executor_ids": ["exec-a"], "match_labels": {}},
                )
            )
            await session.commit()

        async def create_operation() -> tuple[str, bool]:
            async with session_factory() as session:
                operation, created = await create_local_model_operation(
                    session,
                    deployment_id="lmd-concurrency",
                    executor_id="exec-a",
                    generation=1,
                    action="pull",
                    idempotency_key="concurrent-request",
                    request_hash="sha256:concurrent",
                )
                await session.commit()
                return operation.operation_id, created

        created_results = await asyncio.gather(
            create_operation(),
            create_operation(),
        )
        assert len({operation_id for operation_id, _created in created_results}) == 1
        assert sum(created for _operation_id, created in created_results) == 1
        operation_id = created_results[0][0]

        async with session_factory() as session:
            await transition_local_model_operation(
                session,
                operation_id,
                LocalModelOperationState.RUNNING,
            )
            await session.commit()

        async def progress(sequence: int, byte_count: int) -> bool:
            async with session_factory() as session:
                _operation, applied = await record_local_model_operation_progress(
                    session,
                    operation_id,
                    progress_seq=sequence,
                    progress_bytes=byte_count,
                    phase="downloading",
                )
                await session.commit()
                return applied

        await asyncio.gather(
            progress(1, 1024),
            progress(2, 2048),
        )
        async with session_factory() as session:
            operation = await session.get(
                LocalModelOperation,
                operation_id,
            )
            assert operation is not None
            assert operation.progress_seq == 2
            assert operation.progress_bytes == 2048

        async def finish(state: LocalModelOperationState) -> str:
            async with session_factory() as session:
                try:
                    operation = await transition_local_model_operation(
                        session,
                        operation_id,
                        state,
                    )
                    await session.commit()
                    return operation.state
                except ValueError:
                    await session.rollback()
                    return "rejected"

        terminal_results = await asyncio.gather(
            finish(LocalModelOperationState.SUCCEEDED),
            finish(LocalModelOperationState.FAILED),
        )
        assert terminal_results.count("rejected") == 1
        assert set(terminal_results).intersection({"succeeded", "failed"})
    finally:
        await engine.dispose()
