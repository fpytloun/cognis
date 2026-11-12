"""Executor configuration operations shared by HTTP and controller tools."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update

from cognis.api.common import api_exception
from cognis.api.models import (
    ExecutorConfigResponse,
    ExecutorCreateRequest,
    ExecutorTokenResponse,
    ExecutorUpdateRequest,
)
from cognis.core.configuration_dependencies import assert_executor_unreferenced
from cognis.core.executor_availability import (
    is_executor_type_available,
    unavailable_executor_reason,
)
from cognis.core.executor_policy import (
    ensure_executor_type_allowed,
    load_executor_policy,
    validate_executor_mcp_scope,
)
from cognis.core.executor_token_locks import executor_token_lock
from cognis.core.management_access import ManagementActor, check_updated_at
from cognis.logging import get_logger
from cognis.models.executor_inference import (
    executor_local_inference_config_status,
    resolve_executor_local_inference_config,
)
from cognis.models.executor_resources import normalize_executor_resource_snapshot
from cognis.models.runtime_capabilities import normalize_runtime_capability_report
from cognis.ownership import SYSTEM_USER_EMAIL, is_shared_owner_email
from cognis.store.local_models import lock_local_model_dispatch_guard
from cognis.store.models import Agent, CredentialRow, ExecutorRow
from cognis.store.queries import (
    bump_executor_reconfigure_generation,
    create_executor,
    delete_executor,
    get_executor_row,
    list_executors,
    update_agent_if_updated_at,
    update_executor,
)

_logger = get_logger(__name__)
LOCAL_EXECUTOR_TYPES = {"in_process", "subprocess"}


@dataclass(frozen=True)
class ExecutorManagementDependencies:
    """Explicit persistence and post-commit runtime collaborators."""

    session_factory: Any
    websocket: Any
    fence: Callable[[str], Awaitable[None]]
    reconfigure: Callable[[str], None]
    trigger_reconcile: Callable[[str], None]
    sign_token: Callable[..., str]
    credentials: Any | None = None


def _executor_is_shared(row: Any) -> bool:
    return is_shared_owner_email(getattr(row, "owner_email", None))


def _executor_is_local(row: Any) -> bool:
    return getattr(row, "executor_type", None) in LOCAL_EXECUTOR_TYPES


def _resolve_executor_owner(user: Any, shared: bool) -> str:
    return SYSTEM_USER_EMAIL if shared else user.email


def _enforce_executor_creation_rules(user: Any, *, executor_type: str, shared: bool) -> None:
    if shared and user.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can create shared executors")
    if executor_type in LOCAL_EXECUTOR_TYPES and user.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can create local executors")


def _require_executor_mutation_access(actor: ManagementActor, row: Any) -> None:
    actor.require_mutation()
    if _executor_is_local(row) and actor.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can manage local executors")
    if _executor_is_shared(row):
        actor.require_admin()
        return
    if row.owner_email != actor.email:
        raise api_exception(404, "not_found", "Executor not found")


def _executor_to_response(row: Any) -> ExecutorConfigResponse:
    inference_config = resolve_executor_local_inference_config(row.config or {})
    normalized_config = dict(row.config or {})
    normalized_config["local_inference_enabled"] = inference_config.local_inference_enabled
    normalized_config["ollama_runtime"] = inference_config.ollama_runtime.model_dump(
        mode="json",
        exclude={"endpoint"},
    )
    runtime_metadata = dict(getattr(row, "runtime_metadata", None) or {})
    resource_snapshot = normalize_executor_resource_snapshot(
        runtime_metadata.get("resource_snapshot")
    )
    received_at = _resource_snapshot_received_at(runtime_metadata, row)
    runtime_metadata.pop("resource_snapshot", None)
    runtime_metadata.pop("resource_snapshot_received_at", None)
    observed_capabilities = normalize_runtime_capability_report(
        runtime_metadata.get("observed_capabilities")
    )
    return ExecutorConfigResponse(
        executor_id=row.executor_id,
        name=row.name,
        executor_type=row.executor_type,
        available=is_executor_type_available(row.executor_type),
        unavailable_reason=unavailable_executor_reason(row.executor_type),
        labels=row.labels or {},
        enabled_tools=row.enabled_tools or [],
        enabled_tool_groups=row.enabled_tool_groups or [],
        config=normalized_config,
        local_inference_enabled=inference_config.local_inference_enabled,
        ollama_management_enabled=inference_config.ollama_management_enabled,
        ollama_port=inference_config.ollama_runtime.port,
        ollama_endpoint=inference_config.ollama_runtime.endpoint,
        local_inference_config_status=executor_local_inference_config_status(row),
        status=row.status,
        runtime_state=getattr(row, "runtime_state", "offline"),
        desired_config_version=getattr(row, "desired_config_version", 0),
        applied_config_version=getattr(row, "applied_config_version", 0),
        runtime_metadata=runtime_metadata,
        observed_tools=list(getattr(row, "observed_tools", None) or []),
        observed_capabilities=observed_capabilities,
        resource_snapshot=(
            resource_snapshot.with_current_freshness(received_at=received_at)
            if resource_snapshot is not None
            else None
        ),
        last_observed_at=getattr(row, "last_observed_at", None),
        is_default=row.is_default,
        shared=_executor_is_shared(row),
        owner_email=row.owner_email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _resource_snapshot_received_at(
    runtime_metadata: dict[str, Any],
    row: Any,
) -> datetime | None:
    value = runtime_metadata.get("resource_snapshot_received_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    last_observed_at = getattr(row, "last_observed_at", None)
    return last_observed_at if isinstance(last_observed_at, datetime) else None


async def _clear_executor_defaults(session: Any, owner_email: str) -> None:
    owner_filter = ExecutorRow.owner_email == owner_email
    if owner_email == SYSTEM_USER_EMAIL:
        owner_filter = or_(owner_filter, ExecutorRow.owner_email.is_(None))
    await session.execute(update(ExecutorRow).where(owner_filter).values(is_default=False))


async def list_executors_route(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
) -> list[ExecutorConfigResponse]:
    async with deps.session_factory() as session:
        rows = await list_executors(session, owner_email=actor.email, include_shared=True)
    return [_executor_to_response(row) for row in rows]


async def get_executor_route(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
    executor_id: str,
) -> ExecutorConfigResponse:
    async with deps.session_factory() as session:
        row = await get_executor_row(
            session, executor_id, owner_email=actor.email, include_shared=True
        )
    if row is None:
        raise api_exception(404, "not_found", "Executor not found")
    return _executor_to_response(row)


async def create_executor_route(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
    body: ExecutorCreateRequest,
    *,
    preview: bool = False,
) -> ExecutorConfigResponse:
    actor.require_mutation()
    try:
        resolve_executor_local_inference_config(body.config)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    _enforce_executor_creation_rules(actor, executor_type=body.executor_type, shared=body.shared)
    policy = await load_executor_policy(deps.session_factory)
    try:
        ensure_executor_type_allowed(body.executor_type, policy)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    async with deps.session_factory() as session:
        await lock_local_model_dispatch_guard(session)
        executor_owner = _resolve_executor_owner(actor, body.shared)
        try:
            await validate_executor_mcp_scope(
                session,
                owner_email=executor_owner,
                config=body.config or None,
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        if preview:
            return ExecutorConfigResponse(
                executor_id=body.executor_id or "",
                name=body.name,
                executor_type=body.executor_type,
                config=body.config,
                shared=body.shared,
            )
        if body.is_default:
            await _clear_executor_defaults(session, executor_owner)
        row = await create_executor(
            session,
            executor_id=body.executor_id,
            name=body.name,
            executor_type=body.executor_type,
            labels=body.labels or None,
            enabled_tools=body.enabled_tools,
            enabled_tool_groups=body.enabled_tool_groups,
            config=body.config or None,
            is_default=body.is_default,
            owner_email=actor.email,
            shared=body.shared,
        )
        if row.executor_type == "websocket":
            row.desired_config_version = 1
        await session.commit()
    deps.trigger_reconcile(row.executor_id)
    return _executor_to_response(row)


async def generate_executor_token_route(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
    executor_id: str,
) -> ExecutorTokenResponse:
    async with executor_token_lock(executor_id):
        async with deps.session_factory() as session:
            row = await get_executor_row(
                session, executor_id, owner_email=actor.email, include_shared=True
            )
            if row is None:
                raise api_exception(404, "not_found", "Executor not found")
            _require_executor_mutation_access(actor, row)
            if row.executor_type != "websocket":
                raise api_exception(
                    400,
                    "validation_error",
                    "Only WebSocket executors use persistent tokens",
                )
            result = await session.execute(
                update(ExecutorRow)
                .where(ExecutorRow.executor_id == executor_id)
                .values(token_version=ExecutorRow.token_version + 1)
                .returning(ExecutorRow.token_version)
            )
            token_version = int(result.scalar_one())
            await session.commit()
        await deps.fence(executor_id)
    token = deps.sign_token(
        executor_id,
        token_version=token_version,
    )
    return ExecutorTokenResponse(executor_id=executor_id, token=token, expires_in=None)


async def provision_executor_token(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
    executor_id: str,
    *,
    agent_id: str,
    operation_id: str,
    expected_updated_at: str,
) -> dict[str, Any]:
    """Atomically rotate, encrypt and grant a token; reconcile fencing on replay.

    The DB transaction owns the version, encrypted credential and agent grant.
    No external I/O occurs inside it. Cancellation before commit rolls everything
    back; after commit the same operation resumes its pending fence, never signs
    or rotates twice. The existing token lock owns local rotation/fence ordering.
    """
    actor.require_mutation()
    stage = getattr(deps.credentials, "create_credential_in_transaction", None)
    if not callable(stage):
        raise api_exception(503, "unavailable", "Transactional credential storage unavailable")
    if not agent_id or not operation_id or len(operation_id) > 128:
        raise api_exception(
            400, "validation_error", "An agent and stable operation_id are required"
        )
    digest = hashlib.sha256(f"{executor_id}\0{operation_id}".encode()).hexdigest()[:32]
    credential_id = f"executor-token-{digest}"
    async with executor_token_lock(executor_id):
        async with deps.session_factory() as session:
            row = await get_executor_row(session, executor_id, owner_email=actor.email)
            if row is None or _executor_is_shared(row):
                raise api_exception(404, "not_found", "Private executor not found")
            _require_executor_mutation_access(actor, row)
            if row.executor_type != "websocket" or row.status != "active":
                raise api_exception(
                    400, "validation_error", "An active private WebSocket executor is required"
                )
            agent = await session.scalar(
                select(Agent).where(Agent.agent_id == agent_id, Agent.owner_email == actor.email)
            )
            if agent is None or agent.status != "active":
                raise api_exception(403, "forbidden", "An active owner agent is required")
            credential = await session.scalar(
                select(CredentialRow).where(
                    CredentialRow.user_email == actor.email,
                    CredentialRow.credential_id == credential_id,
                )
            )
            if credential is None:
                check_updated_at(row, expected_updated_at)
                previous_version = int(row.token_version or 0)
                result = await session.execute(
                    update(ExecutorRow)
                    .where(
                        ExecutorRow.executor_id == executor_id,
                        ExecutorRow.owner_email == actor.email,
                        ExecutorRow.token_version == previous_version,
                    )
                    .values(token_version=ExecutorRow.token_version + 1)
                    .returning(ExecutorRow.token_version)
                )
                version = result.scalar_one_or_none()
                if version is None:
                    raise api_exception(
                        409, "conflict", "Executor token changed; reconcile before retrying"
                    )
                token = deps.sign_token(executor_id, token_version=int(version))
                credential = await stage(
                    session,
                    credential_id=credential_id,
                    user_email=actor.email,
                    agent_id=agent_id,
                    label=f"Executor provisioning: {executor_id}",
                    payload={"token": token},
                    metadata={
                        "purpose": "executor_provisioning",
                        "executor_id": executor_id,
                        "executor_token_version": int(version),
                        "operation_id": operation_id,
                        "fence_pending": True,
                    },
                )
                permissions = dict(agent.permissions or {})
                permissions["allowed_credentials"] = sorted(
                    set(permissions.get("allowed_credentials", [])) | {credential_id}
                )
                if not await update_agent_if_updated_at(
                    session,
                    agent_id,
                    expected_updated_at=agent.updated_at,
                    updates={"permissions": permissions},
                ):
                    raise api_exception(
                        409, "conflict", "Agent permissions changed; reconcile before retrying"
                    )
                await session.commit()
            else:
                meta = credential.metadata_json or {}
                if (
                    meta.get("purpose") != "executor_provisioning"
                    or meta.get("executor_id") != executor_id
                    or meta.get("operation_id") != operation_id
                    or credential.agent_id != agent_id
                    or credential.status != "active"
                    or meta.get("executor_token_version") != row.token_version
                    or credential_id not in (agent.permissions or {}).get("allowed_credentials", [])
                ):
                    raise api_exception(
                        409, "conflict", "Provisioning operation is stale or no longer authorized"
                    )
            pending = bool((credential.metadata_json or {}).get("fence_pending"))
            version = int(credential.metadata_json["executor_token_version"])
        if pending:
            await deps.fence(executor_id)
            async with deps.session_factory() as session:
                credential = await session.scalar(
                    select(CredentialRow).where(
                        CredentialRow.user_email == actor.email,
                        CredentialRow.credential_id == credential_id,
                    )
                )
                credential.metadata_json = {**credential.metadata_json, "fence_pending": False}
                await session.commit()
    return {
        "executor_id": executor_id,
        "operation_id": operation_id,
        "token_version": version,
        "credential_id": credential_id,
        "token_ref": f"$credential:{credential_id}.token",
        "agent_id": agent_id,
        "persisted": True,
        "fenced": True,
    }


async def update_executor_route(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
    executor_id: str,
    body: ExecutorUpdateRequest,
    *,
    expected_updated_at: str | None = None,
    preview: bool = False,
) -> ExecutorConfigResponse:
    updates = body.model_dump(exclude_none=True)
    expected_config_version = updates.pop("expected_config_version", None)
    if not updates:
        raise api_exception(400, "validation_error", "No fields to update")
    policy = await load_executor_policy(deps.session_factory)
    fence_connection = False
    async with deps.session_factory() as session:
        await lock_local_model_dispatch_guard(session)
        existing = await get_executor_row(
            session, executor_id, owner_email=actor.email, include_shared=True
        )
        if existing is None:
            raise api_exception(404, "not_found", "Executor not found")
        _require_executor_mutation_access(actor, existing)
        check_updated_at(existing, expected_updated_at)
        if (
            expected_config_version is not None
            and int(existing.desired_config_version or 0) != expected_config_version
        ):
            raise api_exception(
                409,
                "executor_config_conflict",
                "Executor configuration changed; reload it before saving",
            )
        previous_inference = resolve_executor_local_inference_config(existing.config or {})
        try:
            resolve_executor_local_inference_config(updates.get("config", existing.config or {}))
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        executor_type = str(updates.get("executor_type", existing.executor_type))
        next_shared = bool(updates.get("shared", _executor_is_shared(existing)))
        executor_owner = _resolve_executor_owner(actor, next_shared)
        _enforce_executor_creation_rules(actor, executor_type=executor_type, shared=next_shared)
        try:
            ensure_executor_type_allowed(executor_type, policy)
            await validate_executor_mcp_scope(
                session,
                owner_email=executor_owner,
                config=updates.get("config", existing.config or {}),
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        if preview:
            return _executor_to_response(existing).model_copy(update=updates)
        if updates.get("is_default") is True:
            await _clear_executor_defaults(session, executor_owner)
        row = await update_executor(
            session,
            executor_id,
            owner_email=actor.email,
            include_shared=True,
            **updates,
        )
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        fence_connection = row.executor_type != "websocket" or (
            "status" in updates and row.status != "active"
        )
        runtime_affecting = (
            any(key in updates for key in {"config", "enabled_tools", "enabled_tool_groups"})
            and row.executor_type == "websocket"
        )
        if runtime_affecting:
            connected = deps.websocket.get_connection(executor_id)
            await bump_executor_reconfigure_generation(
                session,
                executor_id,
                runtime_state="reconfiguring" if connected is not None else "stale",
            )
            await session.refresh(row)
        await session.commit()
        current_inference = resolve_executor_local_inference_config(row.config or {})
    if fence_connection:
        await deps.fence(executor_id)
    if runtime_affecting:
        deps.reconfigure(executor_id)
    inference_changed = previous_inference != current_inference
    if any(key in updates for key in {"labels", "status", "shared"}) or inference_changed:
        deps.trigger_reconcile(executor_id)
    if inference_changed:
        _logger.info(
            "executor local inference capability updated",
            extra={
                "extra_data": {
                    "action": "executor.local_inference.update",
                    "actor_email": actor.email,
                    "actor_role": actor.role,
                    "executor_id": executor_id,
                    "owner_email": row.owner_email,
                    "previous": {
                        "local_inference_enabled": previous_inference.local_inference_enabled,
                        "ollama_management_enabled": previous_inference.ollama_management_enabled,
                    },
                    "current": {
                        "local_inference_enabled": current_inference.local_inference_enabled,
                        "ollama_management_enabled": current_inference.ollama_management_enabled,
                    },
                }
            },
        )
    return _executor_to_response(row)


async def reconfigure_executor_route(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
    executor_id: str,
) -> ExecutorConfigResponse:
    """Retry runtime initialization without replacing the executor configuration."""
    async with deps.session_factory() as session:
        await lock_local_model_dispatch_guard(session)
        row = await get_executor_row(
            session, executor_id, owner_email=actor.email, include_shared=True
        )
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        _require_executor_mutation_access(actor, row)
        if row.executor_type != "websocket" or row.status != "active":
            raise api_exception(
                400, "validation_error", "Only active WebSocket executors can be reconfigured"
            )
        connected = deps.websocket.get_connection(executor_id)
        # Reuse an outstanding generation; repeated clicks must not invalidate
        # an in-flight configure. A converged degraded runtime needs a new one.
        if int(row.desired_config_version or 0) == int(row.applied_config_version or 0):
            await bump_executor_reconfigure_generation(
                session,
                executor_id,
                runtime_state="reconfiguring" if connected is not None else "stale",
            )
            await session.refresh(row)
        await session.commit()
    deps.reconfigure(executor_id)
    return _executor_to_response(row)


async def delete_executor_route(
    deps: ExecutorManagementDependencies,
    actor: ManagementActor,
    executor_id: str,
    *,
    expected_updated_at: str | None = None,
    preview: bool = False,
) -> None:
    async with deps.session_factory() as session:
        await lock_local_model_dispatch_guard(session)
        row = await get_executor_row(
            session, executor_id, owner_email=actor.email, include_shared=True
        )
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        _require_executor_mutation_access(actor, row)
        check_updated_at(row, expected_updated_at)
        if row.is_default:
            raise api_exception(400, "validation_error", "Cannot delete the default executor")
        await assert_executor_unreferenced(session, row)
        if preview:
            return
        deleted = await delete_executor(
            session,
            executor_id,
            owner_email=actor.email,
            include_shared=True,
        )
        if not deleted:
            raise api_exception(404, "not_found", "Executor not found")
        await session.commit()
    await deps.fence(executor_id)


async def fence_executor_connection(
    ownership: Any,
    websocket: Any,
    executor_id: str,
) -> None:
    """Invalidate durable ownership before closing any process-local socket."""

    if ownership is not None:
        await ownership.revoke(executor_id)
    connected = websocket.get_connection(executor_id)
    if connected is not None:
        with contextlib.suppress(Exception):
            await connected.close()
