"""Service layer for declarative local-model desired state."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.local_model_providers import (
    LocalModelProviderResolver,
    LocalModelProviderService,
)
from cognis.core.local_models import parse_local_model_reference
from cognis.models.executor_inference import (
    executor_local_inference_configured,
    executor_local_inference_routable,
)
from cognis.models.local_models import (
    LocalModelDeploymentCreateRequest,
    LocalModelDeploymentStatusResponse,
    LocalModelDeploymentUpdateRequest,
    LocalModelDesiredState,
    LocalModelManagedDeploymentCreateRequest,
    LocalModelManagedProviderAttachRequest,
    LocalModelOperationAction,
    LocalModelRuntimeOperationCreateRequest,
    LocalModelSelector,
    LocalModelTargetState,
    LocalModelTargetStatusResponse,
    ParsedLocalModelReference,
    ProviderLocalModelUpsertRequest,
)
from cognis.ownership import SYSTEM_USER_EMAIL, is_shared_owner_email
from cognis.store.local_models import (
    create_local_model_operation,
    get_visible_local_model_deployment,
    list_active_executor_rows,
    list_local_model_operations,
    list_local_model_targets,
    list_visible_local_model_deployments,
    lock_and_get_llm_provider,
    lock_and_get_local_model_deployment,
    lock_local_model_dispatch_guard,
    remove_generated_llm_provider_model_reference,
    sync_local_model_targets,
    update_local_model_target_status,
    upsert_llm_provider_model,
)
from cognis.store.models import (
    ExecutorRow,
    LLMProvider,
    LocalModelDeployment,
    LocalModelOperation,
    LocalModelTargetStatus,
    User,
)
from cognis.store.queries import get_executor_row, get_visible_llm_provider


class LocalModelAccessError(PermissionError):
    """The caller cannot access or mutate a local-model resource."""


class LocalModelNotFoundError(LookupError):
    """A requested local-model resource is not visible."""


class LocalModelValidationError(ValueError):
    """A local-model desired-state request is invalid."""


class LocalModelDependencyError(LocalModelValidationError):
    """A destructive operation is blocked by explicit deployment dependencies."""

    def __init__(self, message: str, *, deployment_ids: list[str]) -> None:
        super().__init__(message)
        self.deployment_ids = deployment_ids


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _can_manage_owner(*, actor_email: str, actor_role: str, owner_email: str) -> bool:
    if owner_email == SYSTEM_USER_EMAIL:
        return actor_role == "admin"
    return owner_email == actor_email


def _labels_match(row: ExecutorRow, labels: dict[str, str]) -> bool:
    row_labels = row.labels if isinstance(row.labels, dict) else {}
    return all(
        isinstance(row_labels.get(key), str) and row_labels[key] == value
        for key, value in labels.items()
    )


async def resolve_authorized_deployment_executors(
    session: AsyncSession,
    deployment: LocalModelDeployment,
) -> list[ExecutorRow]:
    """Resolve a persisted declaration only across currently authorized rows."""

    selector = LocalModelSelector.model_validate(deployment.selector)
    rows = [
        row
        for row in await list_active_executor_rows(session)
        if executor_local_inference_configured(row)
    ]
    by_id = {row.executor_id: row for row in rows}
    shared = is_shared_owner_email(deployment.owner_email)
    owner = await session.get(User, deployment.owner_email)
    owner_is_admin = owner is not None and owner.role == "admin"
    authorized = [
        row
        for row in rows
        if (
            is_shared_owner_email(row.owner_email)
            if shared
            else row.owner_email == deployment.owner_email
            or (owner_is_admin and is_shared_owner_email(row.owner_email))
        )
    ]
    authorized_ids = {row.executor_id for row in authorized}
    selected_ids = {
        executor_id
        for executor_id in selector.executor_ids
        if executor_id in by_id and executor_id in authorized_ids
    }
    if selector.match_labels:
        selected_ids.update(
            row.executor_id for row in authorized if _labels_match(row, selector.match_labels)
        )
    return [by_id[executor_id] for executor_id in sorted(selected_ids)]


async def resolve_provider_scoped_deployment_executors(
    session: AsyncSession,
    deployment: LocalModelDeployment,
) -> list[ExecutorRow]:
    """Resolve deployment targets within the linked provider's host scope."""

    if deployment.provider_id is None:
        return []
    provider = await session.get(LLMProvider, deployment.provider_id)
    if provider is None:
        return []
    shared = is_shared_owner_email(deployment.owner_email)
    owner = await session.get(User, deployment.owner_email)
    owner_role = owner.role if owner is not None else ("admin" if shared else "user")
    resolved_provider = await LocalModelProviderResolver().resolve(
        session,
        provider,
        actor_email=deployment.owner_email,
        actor_role=owner_role,
        shared=shared,
    )
    if resolved_provider is None:
        return []
    provider_host_ids = {row.executor_id for row in resolved_provider.hosts}
    return [
        row
        for row in await resolve_authorized_deployment_executors(session, deployment)
        if row.executor_id in provider_host_ids
    ]


async def list_current_local_model_delete_dependencies(
    session: AsyncSession,
    *,
    executor_id: str,
    runtime_name: str,
    exclude_deployment_id: str,
) -> list[str]:
    """Resolve current declarations instead of trusting stale materialized targets."""

    deployments = (
        await session.execute(
            select(LocalModelDeployment)
            .where(
                LocalModelDeployment.runtime_name == runtime_name,
                LocalModelDeployment.desired_state == LocalModelDesiredState.PRESENT.value,
                LocalModelDeployment.deployment_id != exclude_deployment_id,
            )
            .order_by(LocalModelDeployment.deployment_id.asc())
        )
    ).scalars()
    dependencies: list[str] = []
    for deployment in deployments:
        resolved = await resolve_authorized_deployment_executors(
            session,
            deployment,
        )
        if any(row.executor_id == executor_id for row in resolved):
            dependencies.append(deployment.deployment_id)
    return dependencies


class LocalModelDeploymentService:
    """Transactional desired-state operations bound to one DB session and caller."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_email: str,
        actor_role: str,
    ) -> None:
        self.session = session
        self.actor_email = actor_email
        self.actor_role = actor_role

    async def list_deployments(self) -> list[LocalModelDeployment]:
        """List deployments visible to the caller."""

        return await list_visible_local_model_deployments(
            self.session,
            owner_email=self.actor_email,
        )

    async def get_deployment(self, deployment_id: str) -> LocalModelDeployment:
        """Get a visible deployment."""

        row = await get_visible_local_model_deployment(
            self.session,
            deployment_id,
            owner_email=self.actor_email,
        )
        if row is None:
            raise LocalModelNotFoundError("local-model deployment not found")
        return row

    async def list_targets(self, deployment_id: str) -> list[LocalModelTargetStatus]:
        """List concrete targets for a visible deployment."""

        await self.get_deployment(deployment_id)
        return await list_local_model_targets(self.session, deployment_id)

    async def list_operations(self, deployment_id: str) -> list[LocalModelOperation]:
        """List durable operations for a visible deployment."""

        await self.get_deployment(deployment_id)
        return await list_local_model_operations(self.session, deployment_id)

    async def deployment_status(
        self,
        deployment_id: str,
    ) -> LocalModelDeploymentStatusResponse:
        """Build a generation-aware rollout summary for one deployment."""

        deployment = await self.get_deployment(deployment_id)
        targets = await self.list_targets(deployment_id)
        state_counts: dict[str, int] = {}
        for target in targets:
            state_counts[target.state] = state_counts.get(target.state, 0) + 1
        expected_state = (
            LocalModelTargetState.READY.value
            if deployment.desired_state == LocalModelDesiredState.PRESENT.value
            else LocalModelTargetState.ABSENT.value
        )
        ready = bool(targets) and all(
            target.state == expected_state and target.observed_generation == deployment.generation
            for target in targets
        )
        return LocalModelDeploymentStatusResponse(
            deployment_id=deployment.deployment_id,
            generation=deployment.generation,
            desired_state=deployment.desired_state,
            total_targets=len(targets),
            state_counts=state_counts,
            ready=ready,
            targets=[
                LocalModelTargetStatusResponse.model_validate(
                    {
                        column: getattr(target, column)
                        for column in LocalModelTargetStatusResponse.model_fields
                    }
                )
                for target in targets
            ],
        )

    async def authorize_executor(
        self,
        executor_id: str,
        *,
        mutation: bool,
    ) -> ExecutorRow:
        """Authorize one concrete executor; selectors never authorize an operation."""

        row = await get_executor_row(
            self.session,
            executor_id,
            owner_email=self.actor_email,
            include_shared=True,
        )
        if row is None or row.status != "active":
            raise LocalModelNotFoundError("executor not found")
        if mutation and not executor_local_inference_routable(row):
            raise LocalModelValidationError("local inference is disabled on the selected executor")
        shared = is_shared_owner_email(row.owner_email)
        if shared:
            if mutation and self.actor_role != "admin":
                raise LocalModelAccessError("shared executor mutations require an admin")
            return row
        if row.owner_email != self.actor_email and self.actor_role != "admin":
            raise LocalModelNotFoundError("executor not found")
        return row

    async def create_runtime_operation(
        self,
        executor_id: str,
        payload: LocalModelRuntimeOperationCreateRequest,
    ) -> LocalModelOperation:
        """Create one durable operation bound to an authorized concrete target."""

        await lock_local_model_dispatch_guard(self.session)
        await self.authorize_executor(executor_id, mutation=True)
        deployment = await self._get_managed_deployment(
            payload.deployment_id,
            for_update=False,
        )
        if deployment.provider_id is None:
            raise LocalModelValidationError(
                "deployment requires an Ollama provider before runtime operations"
            )
        selected = await resolve_provider_scoped_deployment_executors(
            self.session,
            deployment,
        )
        if executor_id not in {row.executor_id for row in selected}:
            raise LocalModelNotFoundError("local-model deployment target not found")
        target = next(
            (
                item
                for item in await list_local_model_targets(
                    self.session,
                    deployment.deployment_id,
                )
                if item.executor_id == executor_id
            ),
            None,
        )
        if target is None:
            raise LocalModelNotFoundError("local-model deployment target not found")
        if payload.action == LocalModelOperationAction.PULL:
            if deployment.desired_state != LocalModelDesiredState.PRESENT.value:
                raise LocalModelValidationError("pull requires deployment desired_state=present")
        else:
            if (
                deployment.desired_state != LocalModelDesiredState.ABSENT.value
                or deployment.prune_policy != "delete"
            ):
                raise LocalModelValidationError(
                    "delete requires desired_state=absent and prune_policy=delete"
                )
            dependencies = await list_current_local_model_delete_dependencies(
                self.session,
                executor_id=executor_id,
                runtime_name=deployment.runtime_name,
                exclude_deployment_id=deployment.deployment_id,
            )
            if dependencies:
                raise LocalModelDependencyError(
                    "model is still referenced by present deployments",
                    deployment_ids=dependencies,
                )
        provider_upsert = payload.action == LocalModelOperationAction.PULL
        request_document = {
            "action": payload.action.value,
            "deployment_id": deployment.deployment_id,
            "executor_id": executor_id,
            "generation": deployment.generation,
            "post_pull_provider_upsert": provider_upsert,
            "runtime_name": deployment.runtime_name,
        }
        request_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    request_document,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        try:
            operation, _created = await create_local_model_operation(
                self.session,
                deployment_id=deployment.deployment_id,
                executor_id=executor_id,
                generation=deployment.generation,
                action=payload.action.value,
                idempotency_key=payload.idempotency_key,
                request_hash=request_hash,
                post_pull_provider_upsert=provider_upsert,
            )
        except ValueError as exc:
            raise LocalModelValidationError(str(exc)) from exc
        if _created:
            await update_local_model_target_status(
                self.session,
                target.target_id,
                expected_generation=target.generation,
                state=LocalModelTargetState.PENDING.value,
                current_operation_id=operation.operation_id,
            )
        return operation

    async def get_managed_runtime_operation(
        self,
        executor_id: str,
        operation_id: str,
    ) -> LocalModelOperation:
        """Authorize one operation through both its deployment and exact executor."""

        await self.authorize_executor(executor_id, mutation=True)
        operation = await self.session.get(LocalModelOperation, operation_id)
        if operation is None or operation.executor_id != executor_id:
            raise LocalModelNotFoundError("local-model operation not found")
        await self._get_managed_deployment(operation.deployment_id)
        return operation

    async def resolve_selector(
        self,
        selector: LocalModelSelector,
        *,
        shared: bool = False,
    ) -> list[ExecutorRow]:
        """Resolve a selector with the same authorization rules as deployment creation."""

        return await self._resolve_selector(selector, shared=shared)

    async def resolve_provider_hosts(
        self,
        provider_id: str,
        *,
        shared: bool = False,
    ) -> list[ExecutorRow]:
        """Resolve the authorized host scope for a preselected provider."""

        provider = await self._resolve_provider(provider_id, shared=shared)
        resolved = await LocalModelProviderResolver().resolve(
            self.session,
            provider,
            actor_email=self.actor_email,
            actor_role=self.actor_role,
            shared=shared,
        )
        assert resolved is not None
        return list(resolved.hosts)

    async def create_deployment(
        self,
        payload: LocalModelDeploymentCreateRequest,
    ) -> LocalModelDeployment:
        """Create desired state and materialize its authorized target set."""

        await lock_local_model_dispatch_guard(self.session)
        if payload.shared and self.actor_role != "admin":
            raise LocalModelAccessError("only admins can create shared deployments")
        parsed = self._parse_reference(payload.requested_ref)
        owner_email = SYSTEM_USER_EMAIL if payload.shared else self.actor_email
        provider = await self._resolve_provider(payload.provider_id, shared=payload.shared)
        executor_rows = await self._resolve_selector(payload.selector, shared=payload.shared)
        await self._validate_provider_target_subset(
            provider,
            executor_rows,
            shared=payload.shared,
        )
        row = LocalModelDeployment(
            deployment_id=f"lmd_{uuid.uuid4().hex}",
            owner_email=owner_email,
            runtime_type=payload.runtime_type.value,
            requested_ref=parsed.requested_ref,
            canonical_name=parsed.canonical_name,
            runtime_name=parsed.runtime_name,
            source=parsed.source.value,
            digest=payload.digest,
            revision=parsed.revision,
            selector=payload.selector.model_dump(mode="json"),
            desired_state=payload.desired_state.value,
            update_policy=payload.update_policy.value,
            prune_policy=payload.prune_policy.value,
            max_parallel=payload.max_parallel,
            generation=1,
            provider_id=provider.provider_id,
            capacity_override_acknowledged=payload.capacity_override_acknowledged,
            capacity_assessment_generation=payload.capacity_assessment_generation,
        )
        self.session.add(row)
        await self.session.flush()
        await sync_local_model_targets(
            self.session,
            row,
            [executor.executor_id for executor in executor_rows],
        )
        return row

    async def create_managed_deployment(
        self,
        payload: LocalModelManagedDeploymentCreateRequest,
    ) -> tuple[LocalModelDeployment, LLMProvider, bool, str]:
        """Atomically reuse/create a compatible provider and create a deployment."""

        parsed = self._parse_reference(payload.requested_ref)
        provider_service = LocalModelProviderService(
            self.session,
            actor_email=self.actor_email,
            actor_role=self.actor_role,
        )
        try:
            provider, created, reason = await provider_service.find_or_create(
                runtime_name=parsed.runtime_name,
                selector=payload.selector,
                shared=payload.shared,
                force_create=payload.force_create_provider,
            )
        except PermissionError as exc:
            raise LocalModelAccessError(str(exc)) from exc
        except LookupError as exc:
            raise LocalModelNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise LocalModelValidationError(str(exc)) from exc
        deployment = await self.create_deployment(
            LocalModelDeploymentCreateRequest(
                requested_ref=payload.requested_ref,
                digest=payload.digest,
                selector=payload.selector,
                desired_state=payload.desired_state,
                update_policy=payload.update_policy,
                prune_policy=payload.prune_policy,
                max_parallel=payload.max_parallel,
                provider_id=provider.provider_id,
                capacity_override_acknowledged=payload.capacity_override_acknowledged,
                capacity_assessment_generation=payload.capacity_assessment_generation,
                shared=payload.shared,
            )
        )
        return deployment, provider, created, reason

    async def attach_managed_provider(
        self,
        deployment_id: str,
        payload: LocalModelManagedProviderAttachRequest,
    ) -> tuple[LocalModelDeployment, LLMProvider, bool, str]:
        """Atomically reuse/create and attach a provider to a legacy deployment."""

        await lock_local_model_dispatch_guard(self.session)
        deployment = await self._get_managed_deployment(deployment_id, for_update=True)
        if deployment.provider_id is not None:
            raise LocalModelValidationError("deployment already has a provider")
        selector = LocalModelSelector.model_validate(deployment.selector)
        shared = is_shared_owner_email(deployment.owner_email)
        provider_service = LocalModelProviderService(
            self.session,
            actor_email=self.actor_email,
            actor_role=self.actor_role,
        )
        try:
            provider, created, reason = await provider_service.find_or_create(
                runtime_name=deployment.runtime_name,
                selector=selector,
                shared=shared,
                force_create=payload.force_create_provider,
            )
        except PermissionError as exc:
            raise LocalModelAccessError(str(exc)) from exc
        except LookupError as exc:
            raise LocalModelNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise LocalModelValidationError(str(exc)) from exc
        updated = await self.update_deployment(
            deployment_id,
            LocalModelDeploymentUpdateRequest(provider_id=provider.provider_id),
        )
        return updated, provider, created, reason

    async def update_deployment(
        self,
        deployment_id: str,
        payload: LocalModelDeploymentUpdateRequest,
    ) -> LocalModelDeployment:
        """Patch desired state and advance generation only for effective changes."""

        await lock_local_model_dispatch_guard(self.session)
        row = await self._get_managed_deployment(deployment_id, for_update=True)
        updates = payload.model_dump(mode="json", exclude_unset=True)
        if not updates:
            raise LocalModelValidationError("no deployment fields to update")

        selector = (
            LocalModelSelector.model_validate(updates["selector"])
            if "selector" in updates
            else LocalModelSelector.model_validate(row.selector)
        )
        shared = is_shared_owner_email(row.owner_email)
        executor_rows = await self._resolve_selector(selector, shared=shared)
        selected_executor_ids = {executor.executor_id for executor in executor_rows}
        existing_target_ids = {
            target.executor_id
            for target in await list_local_model_targets(
                self.session,
                row.deployment_id,
            )
        }
        if "provider_id" in updates:
            provider = await self._resolve_provider(updates["provider_id"], shared=shared)
            updates["provider_id"] = provider.provider_id
        else:
            provider = await self._resolve_provider(row.provider_id, shared=shared)
        await self._validate_provider_target_subset(
            provider,
            executor_rows,
            shared=shared,
        )
        if "requested_ref" in updates:
            parsed = self._parse_reference(str(updates.pop("requested_ref")))
            updates.update(
                {
                    "requested_ref": parsed.requested_ref,
                    "canonical_name": parsed.canonical_name,
                    "runtime_name": parsed.runtime_name,
                    "source": parsed.source.value,
                    "revision": parsed.revision,
                }
            )
        if "selector" in updates:
            updates["selector"] = selector.model_dump(mode="json")

        changed = (
            any(getattr(row, key) != value for key, value in updates.items())
            or selected_executor_ids != existing_target_ids
        )
        if changed:
            if (
                selected_executor_ids != existing_target_ids
                or updates.get("provider_id", row.provider_id) != row.provider_id
            ):
                operations = await list_local_model_operations(
                    self.session,
                    row.deployment_id,
                )
                if any(
                    operation.state not in {"succeeded", "failed", "cancelled"}
                    for operation in operations
                ):
                    raise LocalModelDependencyError(
                        "cannot change provider or executor targets while local-model operations are active",
                        deployment_ids=[row.deployment_id],
                    )
            next_provider_id = str(updates.get("provider_id", row.provider_id))
            next_runtime_name = str(updates.get("runtime_name", row.runtime_name))
            if (
                next_provider_id != row.provider_id or next_runtime_name != row.runtime_name
            ) and row.provider_id is not None:
                old_provider = await lock_and_get_llm_provider(
                    self.session,
                    row.provider_id,
                )
                if old_provider is not None:
                    await remove_generated_llm_provider_model_reference(
                        self.session,
                        old_provider,
                        model_id=row.runtime_name,
                        deployment_id=row.deployment_id,
                    )
            for key, value in updates.items():
                setattr(row, key, value)
            row.generation += 1
            row.updated_at = _utcnow()
            await sync_local_model_targets(
                self.session,
                row,
                sorted(selected_executor_ids),
            )
        return row

    async def request_reconciliation(self, deployment_id: str) -> LocalModelDeployment:
        """Persist a reconciliation request marker without creating executor work."""

        await lock_local_model_dispatch_guard(self.session)
        row = await self._get_managed_deployment(deployment_id, for_update=True)
        if row.provider_id is None:
            raise LocalModelValidationError(
                "deployment needs a provider before reconciliation can be requested"
            )
        selector = LocalModelSelector.model_validate(row.selector)
        executor_rows = await self._resolve_selector(
            selector,
            shared=is_shared_owner_email(row.owner_email),
        )
        provider = await self._resolve_provider(
            row.provider_id,
            shared=is_shared_owner_email(row.owner_email),
        )
        await self._validate_provider_target_subset(
            provider,
            executor_rows,
            shared=is_shared_owner_email(row.owner_email),
        )
        selected_executor_ids = {executor.executor_id for executor in executor_rows}
        existing_executor_ids = {
            target.executor_id
            for target in await list_local_model_targets(
                self.session,
                row.deployment_id,
            )
        }
        if selected_executor_ids != existing_executor_ids:
            operations = await list_local_model_operations(
                self.session,
                row.deployment_id,
            )
            if any(
                operation.state not in {"succeeded", "failed", "cancelled"}
                for operation in operations
            ):
                raise LocalModelDependencyError(
                    "cannot rematerialize executor targets while local-model operations are active",
                    deployment_ids=[row.deployment_id],
                )
        requested_at = _utcnow()
        row.generation += 1
        row.reconcile_requested_at = requested_at
        row.updated_at = requested_at
        await sync_local_model_targets(
            self.session,
            row,
            [executor.executor_id for executor in executor_rows],
            requested_at=requested_at,
        )
        return row

    async def delete_deployment(self, deployment_id: str) -> None:
        """Delete control-plane desired state without mutating executor resources."""

        await lock_local_model_dispatch_guard(self.session)
        row = await self._get_managed_deployment(deployment_id, for_update=True)
        operations = await list_local_model_operations(self.session, row.deployment_id)
        if any(
            operation.state not in {"succeeded", "failed", "cancelled"} for operation in operations
        ):
            raise LocalModelDependencyError(
                "cannot delete a deployment while local-model operations are active",
                deployment_ids=[row.deployment_id],
            )
        provider = (
            await lock_and_get_llm_provider(self.session, row.provider_id)
            if row.provider_id is not None
            else None
        )
        if provider is not None:
            await remove_generated_llm_provider_model_reference(
                self.session,
                provider,
                model_id=row.runtime_name,
                deployment_id=row.deployment_id,
            )
        await self.session.delete(row)
        await self.session.flush()

    async def upsert_provider_model(
        self,
        provider_id: str,
        payload: ProviderLocalModelUpsertRequest,
    ) -> LLMProvider:
        """Atomically merge one validated local model into a managed provider."""

        provider = await lock_and_get_llm_provider(self.session, provider_id)
        if provider is None:
            raise LocalModelNotFoundError("LLM provider not found")
        provider_owner = provider.owner_email or SYSTEM_USER_EMAIL
        if provider_owner not in {self.actor_email, SYSTEM_USER_EMAIL}:
            raise LocalModelNotFoundError("LLM provider not found")
        if not _can_manage_owner(
            actor_email=self.actor_email,
            actor_role=self.actor_role,
            owner_email=provider_owner,
        ):
            raise LocalModelAccessError("resource access denied")
        config = provider.config if isinstance(provider.config, dict) else {}
        if str(config.get("preset") or "").strip().lower() != "ollama":
            raise LocalModelValidationError("provider model upsert requires an Ollama provider")
        parsed = self._parse_reference(payload.requested_ref)
        return await upsert_llm_provider_model(
            self.session,
            provider,
            model_id=parsed.runtime_name,
            model_config=payload.model_options,
            set_default=payload.set_default,
        )

    async def _get_managed_deployment(
        self,
        deployment_id: str,
        *,
        for_update: bool = False,
    ) -> LocalModelDeployment:
        if for_update:
            row = await lock_and_get_local_model_deployment(
                self.session,
                deployment_id,
            )
            if row is None or row.owner_email not in {
                self.actor_email,
                SYSTEM_USER_EMAIL,
            }:
                raise LocalModelNotFoundError("local-model deployment not found")
        else:
            row = await self.get_deployment(deployment_id)
        if not _can_manage_owner(
            actor_email=self.actor_email,
            actor_role=self.actor_role,
            owner_email=row.owner_email,
        ):
            raise LocalModelAccessError("resource access denied")
        return row

    async def _resolve_provider(
        self,
        provider_id: str | None,
        *,
        shared: bool,
    ) -> LLMProvider:
        if provider_id is None:
            raise LocalModelValidationError("provider_id is required")
        provider = await get_visible_llm_provider(
            self.session,
            provider_id,
            self.actor_email,
        )
        if provider is None:
            raise LocalModelNotFoundError("LLM provider not found")
        provider_owner = provider.owner_email or SYSTEM_USER_EMAIL
        if shared and provider_owner != SYSTEM_USER_EMAIL:
            raise LocalModelAccessError("shared deployments require a shared provider")
        if not _can_manage_owner(
            actor_email=self.actor_email,
            actor_role=self.actor_role,
            owner_email=provider_owner,
        ):
            raise LocalModelAccessError("resource access denied")
        config = provider.config if isinstance(provider.config, dict) else {}
        if str(config.get("preset") or "").strip().lower() != "ollama":
            raise LocalModelValidationError("local-model deployments require an Ollama provider")
        if provider.location != "executor":
            raise LocalModelValidationError(
                "local-model deployments require an executor-backed provider"
            )
        resolved = await LocalModelProviderResolver().resolve(
            self.session,
            provider,
            actor_email=self.actor_email,
            actor_role=self.actor_role,
            shared=shared,
        )
        if resolved is None:
            raise LocalModelValidationError("provider resolves no eligible managed hosts")
        return provider

    async def _validate_provider_target_subset(
        self,
        provider: LLMProvider,
        executor_rows: list[ExecutorRow],
        *,
        shared: bool,
    ) -> None:
        resolved = await LocalModelProviderResolver().resolve(
            self.session,
            provider,
            actor_email=self.actor_email,
            actor_role=self.actor_role,
            shared=shared,
        )
        if resolved is None:
            raise LocalModelValidationError("provider resolves no eligible managed hosts")
        provider_ids = {row.executor_id for row in resolved.hosts}
        target_ids = {row.executor_id for row in executor_rows}
        if not target_ids.issubset(provider_ids):
            raise LocalModelValidationError("deployment targets must be a subset of provider hosts")

    async def _resolve_selector(
        self,
        selector: LocalModelSelector,
        *,
        shared: bool,
    ) -> list[ExecutorRow]:
        rows = [
            row
            for row in await list_active_executor_rows(self.session)
            if executor_local_inference_configured(row)
        ]
        by_id = {row.executor_id: row for row in rows}
        authorized = [
            row
            for row in rows
            if (
                is_shared_owner_email(row.owner_email)
                if shared
                else row.owner_email == self.actor_email
                or (self.actor_role == "admin" and is_shared_owner_email(row.owner_email))
            )
        ]
        authorized_ids = {row.executor_id for row in authorized}
        selected_ids: set[str] = set()
        for executor_id in selector.executor_ids:
            row = by_id.get(executor_id)
            if row is None:
                raise LocalModelNotFoundError(f"executor {executor_id!r} not found")
            if executor_id not in authorized_ids:
                if not is_shared_owner_email(row.owner_email):
                    raise LocalModelNotFoundError(f"executor {executor_id!r} not found")
                raise LocalModelAccessError(f"executor {executor_id!r} is not mutable by caller")
            selected_ids.add(executor_id)
        if selector.match_labels:
            selected_ids.update(
                row.executor_id for row in authorized if _labels_match(row, selector.match_labels)
            )
        if not selected_ids:
            raise LocalModelValidationError("selector matched no authorized active executors")
        return [by_id[executor_id] for executor_id in sorted(selected_ids)]

    @staticmethod
    def _parse_reference(reference: str) -> ParsedLocalModelReference:
        try:
            return parse_local_model_reference(reference)
        except ValueError as exc:
            raise LocalModelValidationError(str(exc)) from exc
