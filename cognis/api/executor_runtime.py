"""Helpers for websocket executor runtime reconciliation."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from cognis.core.executor_connection_ownership import ExecutorConnectionOwner
from cognis.core.mcp_oauth import MCPOAuthError
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.executor_inference import (
    executor_local_inference_config_confirmed,
    resolve_executor_local_inference_config,
)
from cognis.models.executor_resources import normalize_executor_resource_snapshot
from cognis.models.local_models import OllamaRuntimeCapability
from cognis.models.tool import ExecutorCapabilities, ToolDefinition
from cognis.ownership import is_shared_owner_email
from cognis.store.queries import (
    get_executor_row,
    normalize_executor_desired_config_version,
    update_executor_runtime_state,
)
from cognis.tools.skills import resolve_skills_for_agent

_logger = get_logger(__name__)

CONFIGURE_RPC_TIMEOUT_SECONDS = 120.0
RUNTIME_METADATA_SCHEMA_VERSION = 1
CONFIGURE_CAPABILITY_MCP_RUNTIME_STATUS = "mcp_runtime_status_v1"
MAX_SAFE_ERROR_LENGTH = 240
MAX_SAFE_STDERR_LENGTH = 240
RESOURCE_SNAPSHOT_MIN_PERSIST_INTERVAL_SECONDS = 30


def schedule_executor_reconfigure(app: Any, executor_id: str) -> None:
    """Schedule a best-effort background reconcile for a websocket executor."""
    tasks: dict[str, asyncio.Task[None]] = getattr(app.state, "executor_reconcile_tasks", {})
    if not hasattr(app.state, "executor_reconcile_tasks"):
        app.state.executor_reconcile_tasks = tasks
    pending: set[str] = getattr(app.state, "executor_reconcile_pending", set())
    if not hasattr(app.state, "executor_reconcile_pending"):
        app.state.executor_reconcile_pending = pending
    existing = tasks.get(executor_id)
    if existing is not None and not existing.done():
        pending.add(executor_id)
        return

    websocket_provider = app.state.providers.executor.websocket
    connection = websocket_provider.get_connection(executor_id)
    get_local_connection = getattr(websocket_provider, "get_local_connection", None)
    local_connection = (
        get_local_connection(executor_id) if get_local_connection is not None else connection
    )
    forwarded = connection is not None and local_connection is None

    async def _run() -> None:
        cancelled = False
        try:
            try:
                if forwarded:
                    await connection.rpc_call(
                        "executor.reconcile",
                        {"executor_id": executor_id},
                        timeout=CONFIGURE_RPC_TIMEOUT_SECONDS,
                    )
                else:
                    await reconcile_executor(app, executor_id)
            except Exception as exc:
                _logger.warning(
                    "executor_runtime: background reconcile failed for %s: %s",
                    executor_id,
                    _safe_error_message(str(exc)),
                    exc_info=True,
                )
                try:
                    await _mark_reconcile_failed(app, executor_id, exc)
                except Exception:
                    _logger.warning(
                        "executor_runtime: failed to persist reconcile failure for %s",
                        executor_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            current = tasks.get(executor_id)
            if current is asyncio.current_task():
                tasks.pop(executor_id, None)
            if cancelled:
                pending.discard(executor_id)
            elif executor_id in pending:
                pending.discard(executor_id)
                schedule_executor_reconfigure(app, executor_id)

    tasks[executor_id] = asyncio.create_task(_run(), name=f"executor-reconcile-{executor_id}")


async def reconcile_executor(app: Any, executor_id: str, *, connection: Any | None = None) -> bool:
    """Reconcile a connected websocket executor to the desired generation."""
    _logger.info("executor_runtime: reconciling executor %s", executor_id)
    lock = _get_executor_lock(app, executor_id)
    async with lock:
        while True:
            async with app.state.session_factory() as session:
                row = await get_executor_row(session, executor_id)
            if row is None or row.executor_type != "websocket" or row.status != "active":
                _logger.info(
                    "executor_runtime: skipping reconcile for %s (not found, not websocket, or inactive)",
                    executor_id,
                )
                return False

            raw_desired_version = int(getattr(row, "desired_config_version", 0) or 0)
            if raw_desired_version < 1:
                async with app.state.session_factory() as session:
                    normalized = await normalize_executor_desired_config_version(
                        session,
                        executor_id,
                    )
                    await session.commit()
                if normalized:
                    row.desired_config_version = 1
                else:
                    # A concurrent config update won the conditional write. Reload
                    # rather than replacing its newer generation with the legacy floor.
                    connection = None
                    continue

            target_version = max(int(getattr(row, "desired_config_version", 0) or 0), 1)
            applied_version = int(getattr(row, "applied_config_version", 0) or 0)
            current_conn = connection or app.state.providers.executor.websocket.get_connection(
                executor_id
            )
            if current_conn is None or not getattr(current_conn, "connected", False):
                _logger.info(
                    "executor_runtime: executor %s not connected, skipping reconcile",
                    executor_id,
                )
                if getattr(row, "runtime_state", "offline") == "reconfiguring":
                    await _mark_reconcile_unavailable(app, row)
                return False
            if not await _connection_ownership_is_current(app, current_conn):
                _logger.info(
                    "executor_runtime: executor %s connection ownership is stale",
                    executor_id,
                )
                return False

            if applied_version == target_version and getattr(row, "runtime_state", "offline") in {
                "active",
                "degraded",
            }:
                observed_tools = list(getattr(row, "observed_tools", None) or [])
                runtime_metadata = getattr(row, "runtime_metadata", None) or {}
                local_inference_enabled = _fast_path_local_inference_enabled(
                    row,
                    runtime_metadata,
                )
                capabilities = ExecutorCapabilities(
                    tools=[
                        str(tool.get("name", "")) for tool in observed_tools if tool.get("name")
                    ],
                    inference=local_inference_enabled,
                    local_inference=local_inference_enabled,
                    inference_models=[],
                    inference_type="litellm_proxy",
                    channels=True,
                    local_model_runtime=(
                        _ollama_runtime_capability(runtime_metadata.get("ollama_runtime"))
                        if local_inference_enabled
                        else None
                    ),
                )
                runtime_metadata = dict(runtime_metadata)
                runtime_metadata["capabilities"] = capabilities.model_dump(mode="json")
                if getattr(app.state, "executor_connection_ownership", None) is not None:
                    persisted = await _persist_runtime_state(
                        app,
                        executor_id,
                        connection=current_conn,
                        runtime_state=str(getattr(row, "runtime_state", "active")),
                        applied_config_version=applied_version,
                        observed_tools=observed_tools,
                        runtime_metadata=runtime_metadata,
                    )
                    if persisted is None:
                        return False
                app.state.providers.executor.websocket.mark_ready(
                    executor_id,
                    capabilities,
                    metadata=_executor_connection_metadata(
                        labels=row.labels or {},
                        environment=runtime_metadata.get("environment"),
                        platform=runtime_metadata.get("platform") or {},
                        status=row.status,
                        owner_email=row.owner_email,
                        runtime_metadata=runtime_metadata,
                    ),
                )
                _logger.info(
                    "executor_runtime: executor %s already at desired v%d (%s), no reconfigure needed",
                    executor_id,
                    target_version,
                    getattr(row, "runtime_state", "?"),
                )
                return True

            _logger.info(
                "executor_runtime: executor %s needs reconfigure: desired v%d, applied v%d",
                executor_id,
                target_version,
                applied_version,
            )
            async with app.state.session_factory() as session:
                await _update_owned_runtime_state(
                    app,
                    session,
                    current_conn,
                    executor_id=executor_id,
                    runtime_state="reconfiguring",
                )
                await session.commit()
            cluster_signals = getattr(app.state, "cluster_signals", None)
            if cluster_signals is not None:
                await cluster_signals.publish_executor_change(executor_id)

            configure_metadata: dict[str, Any] = getattr(row, "runtime_metadata", None) or {}
            try:
                _logger.info(
                    "executor_runtime: building configure payload for %s v%d",
                    executor_id,
                    target_version,
                )
                payload, configure_metadata = await _build_configure_payload(
                    app, row, target_version
                )
                _logger.info(
                    "executor_runtime: sending executor.configure RPC to %s (timeout=%ds, %d MCP server(s))",
                    executor_id,
                    CONFIGURE_RPC_TIMEOUT_SECONDS,
                    len(payload.get("mcp_servers", [])),
                )
                configure_result = await current_conn.rpc_call(
                    "executor.configure",
                    payload,
                    timeout=CONFIGURE_RPC_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                _logger.warning(
                    "executor_runtime: executor %s configure failed: %s",
                    executor_id,
                    _safe_error_message(str(exc)),
                )
                if not _is_current_connection(app, executor_id, current_conn):
                    _logger.info(
                        "executor_runtime: executor %s connection replaced during configure, retrying",
                        executor_id,
                    )
                    connection = None
                    continue
                runtime_metadata = _merge_runtime_metadata(
                    configure_metadata,
                    {
                        "runtime_state": "blocked",
                        "warnings": [
                            f"Executor reconfigure failed: {_safe_error_message(str(exc))}"
                        ],
                    },
                )
                await _persist_runtime_state(
                    app,
                    executor_id,
                    connection=current_conn,
                    runtime_state="blocked",
                    applied_config_version=int(getattr(row, "applied_config_version", 0) or 0),
                    observed_tools=list(getattr(row, "observed_tools", None) or []),
                    runtime_metadata=runtime_metadata,
                )
                return False

            if not _is_current_connection(app, executor_id, current_conn):
                _logger.info(
                    "executor_runtime: executor %s connection replaced after configure, retrying",
                    executor_id,
                )
                connection = None
                continue

            caps_raw = configure_result.get("capabilities") or {}
            runtime_state = str(configure_result.get("runtime_state") or "active")
            applied_version = int(configure_result.get("applied_version") or target_version)
            observed_tools = list(configure_result.get("observed_tools") or [])
            _logger.info(
                "executor_runtime: executor %s configure result: state=%s, applied v%d, %d tool(s)",
                executor_id,
                runtime_state,
                applied_version,
                len(observed_tools),
            )
            result_runtime_metadata = _sanitize_configure_result_runtime_metadata(
                configure_result.get("runtime_metadata"),
                received_at=datetime.now(UTC),
            )
            runtime_metadata = _merge_runtime_metadata(
                configure_metadata,
                result_runtime_metadata,
            )
            capabilities = ExecutorCapabilities(
                tools=list(caps_raw.get("tools") or []),
                inference=bool(caps_raw.get("inference", False)),
                local_inference=caps_raw.get("local_inference") is True,
                inference_models=list(caps_raw.get("inference_models") or []),
                inference_type=caps_raw.get("inference_type"),
                channels=bool(caps_raw.get("channels", False)),
                local_model_runtime=_ollama_runtime_capability(caps_raw.get("local_model_runtime")),
            )
            if not _live_local_inference_capability_matches(row, capabilities):
                capabilities.inference = False
                capabilities.local_inference = False
                capabilities.local_model_runtime = None
            runtime_metadata["capabilities"] = capabilities.model_dump(mode="json")
            persisted = await _persist_runtime_state(
                app,
                executor_id,
                connection=current_conn,
                runtime_state=runtime_state,
                applied_config_version=applied_version,
                observed_tools=observed_tools,
                runtime_metadata=runtime_metadata,
            )
            if persisted is None:
                _logger.info(
                    "executor_runtime: executor %s ownership changed before configure persisted",
                    executor_id,
                )
                connection = None
                continue
            if not await _connection_ownership_is_current(app, current_conn):
                connection = None
                continue
            marked_ready = app.state.providers.executor.websocket.mark_ready(
                executor_id,
                capabilities,
                metadata=_executor_connection_metadata(
                    labels=row.labels or {},
                    environment=runtime_metadata.get("environment"),
                    platform=runtime_metadata.get("platform") or {},
                    status=row.status,
                    owner_email=row.owner_email,
                    runtime_metadata=runtime_metadata,
                ),
                expected_connection=current_conn,
            )
            if marked_ready is False:
                connection = None
                continue
            await _invalidate_mcp_oauth_tokens_for_runtime_failures(
                app,
                row,
                runtime_metadata=runtime_metadata,
            )

            async with app.state.session_factory() as session:
                refreshed = await get_executor_row(session, executor_id)
            if refreshed is None:
                return False
            if int(getattr(refreshed, "desired_config_version", 0) or 0) > applied_version:
                _logger.info(
                    "executor_runtime: executor %s has newer desired version after apply, re-reconciling",
                    executor_id,
                )
                connection = None
                continue
            _logger.info(
                "executor_runtime: executor %s reconciled to v%d (%s)",
                executor_id,
                applied_version,
                runtime_state,
            )
            return True


async def _persist_runtime_state(
    app: Any,
    executor_id: str,
    *,
    connection: Any | None = None,
    runtime_state: str,
    applied_config_version: int,
    observed_tools: list[dict[str, Any]],
    runtime_metadata: dict[str, Any],
) -> Any | None:
    async with app.state.session_factory() as session:
        if connection is None:
            row = await update_executor_runtime_state(
                session,
                executor_id,
                applied_config_version=applied_config_version,
                observed_tools=observed_tools,
                runtime_metadata=runtime_metadata,
                last_observed_at=datetime.now(UTC),
                runtime_state=runtime_state,
            )
        else:
            row = await _update_owned_runtime_state(
                app,
                session,
                connection,
                executor_id=executor_id,
                applied_config_version=applied_config_version,
                observed_tools=observed_tools,
                runtime_metadata=runtime_metadata,
                last_observed_at=datetime.now(UTC),
                runtime_state=runtime_state,
            )
        await session.commit()
    cluster_signals = getattr(app.state, "cluster_signals", None)
    if cluster_signals is not None:
        await cluster_signals.publish_executor_change(executor_id)
    queue = getattr(app.state, "tool_classification_queue", None)
    if queue is None or row is None or not observed_tools:
        return row
    try:
        tool_defs = [
            ToolDefinition.model_validate(item) for item in observed_tools if isinstance(item, dict)
        ]
        await queue.enqueue_tools(tool_defs, owner_email=getattr(row, "owner_email", None))
    except Exception:
        _logger.warning(
            "executor_runtime: failed to enqueue tool classifications",
            extra={
                "extra_data": {"executor_id": executor_id, "observed_tools": len(observed_tools)}
            },
            exc_info=True,
        )
    return row


async def persist_executor_resource_snapshot(
    app: Any,
    executor_id: str,
    payload: Any,
    *,
    connection: Any | None = None,
) -> bool:
    """Persist one newer current snapshot without creating sample history."""

    snapshot = normalize_executor_resource_snapshot(payload)
    if snapshot is None:
        return False
    lock = _get_executor_lock(app, executor_id)
    async with lock:
        if connection is not None and not _is_current_connection(app, executor_id, connection):
            return False
        async with app.state.session_factory() as session:
            row = await get_executor_row(session, executor_id)
            if row is None:
                return False
            runtime_metadata = dict(getattr(row, "runtime_metadata", None) or {})
            previous = normalize_executor_resource_snapshot(
                runtime_metadata.get("resource_snapshot")
            )
            snapshot_payload = snapshot.model_dump(
                mode="json",
                exclude={"freshness"},
            )
            if previous is not None and snapshot.observed_at < previous.observed_at:
                return False
            if (
                previous is not None
                and previous.observed_at == snapshot.observed_at
                and previous.model_dump(mode="json", exclude={"freshness"}) == snapshot_payload
            ):
                return False
            received_at = datetime.now(UTC)
            previous_received_at = _coerce_utc_datetime(
                runtime_metadata.get("resource_snapshot_received_at")
            )
            elapsed_since_previous = (
                (received_at - previous_received_at).total_seconds()
                if previous_received_at is not None
                else None
            )
            if (
                elapsed_since_previous is not None
                and 0 <= elapsed_since_previous < RESOURCE_SNAPSHOT_MIN_PERSIST_INTERVAL_SECONDS
            ):
                return False
            runtime_metadata["resource_snapshot"] = snapshot_payload
            runtime_metadata["resource_snapshot_received_at"] = received_at.isoformat()
            updated = await _update_owned_runtime_state(
                app,
                session,
                connection,
                executor_id=executor_id,
                runtime_metadata=runtime_metadata,
                last_observed_at=received_at,
            )
            await session.commit()
            if updated is None:
                return False
    return True


async def _update_owned_runtime_state(
    app: Any,
    session: Any,
    connection: Any,
    *,
    executor_id: str | None = None,
    **values: Any,
) -> Any | None:
    """Persist socket-derived state through the exact connection fence."""

    ownership = getattr(app.state, "executor_connection_ownership", None)
    owner: ExecutorConnectionOwner | None = getattr(connection, "connection_owner", None)
    resolved_executor_id = executor_id or getattr(connection, "executor_id", None)
    if ownership is None or connection is None:
        if not resolved_executor_id:
            return None
        return await update_executor_runtime_state(
            session,
            resolved_executor_id,
            **values,
        )
    if owner is None:
        return None
    return await ownership.update_runtime_state(session, owner, **values)


async def _invalidate_mcp_oauth_tokens_for_runtime_failures(
    app: Any,
    row: Any,
    *,
    runtime_metadata: dict[str, Any],
) -> None:
    """Try one controller-owned refresh after an MCP resource rejects authorization."""

    service = getattr(app.state.providers, "mcp_oauth_service", None)
    if service is None:
        return
    failed_server_ids = _authorization_failed_mcp_server_ids(runtime_metadata)
    if not failed_server_ids:
        return
    runtime_servers = {
        item.get("server_id"): item
        for item in runtime_metadata.get("mcp_servers", [])
        if isinstance(item, dict) and isinstance(item.get("server_id"), str)
    }

    for server_id in failed_server_ids:
        try:
            item = runtime_servers.get(server_id, {})
            raw_challenge = item.get("authorization_challenge")
            challenge = (
                {str(key): str(value) for key, value in raw_challenge.items()}
                if isinstance(raw_challenge, dict)
                else None
            )
            if item.get("auth_error") == "insufficient_scope":
                await service.require_reauthorization_for_server(
                    user_email=str(getattr(row, "owner_email", "") or ""),
                    server_id=server_id,
                    reason="insufficient_scope",
                    authorization_challenge=challenge,
                )
            else:
                await service.refresh_token_for_server_id(
                    user_email=str(getattr(row, "owner_email", "") or ""),
                    server_id=server_id,
                    force=True,
                    reason="mcp_resource_authorization_failed",
                )
        except MCPOAuthError as exc:
            _logger.warning(
                "executor_runtime: MCP OAuth recovery did not complete",
                extra={
                    "extra_data": {
                        "executor_id": row.executor_id,
                        "server_id": server_id,
                        "reason": exc.reason or "refresh_failed",
                        "retryable": exc.retryable,
                        "outcome_unknown": exc.outcome_unknown,
                    }
                },
            )
        except Exception:
            _logger.warning(
                "executor_runtime: failed to recover rejected MCP OAuth token",
                extra={
                    "extra_data": {
                        "executor_id": row.executor_id,
                        "server_id": server_id,
                    }
                },
                exc_info=True,
            )


def _authorization_failed_mcp_server_ids(runtime_metadata: dict[str, Any]) -> list[str]:
    servers = runtime_metadata.get("mcp_servers")
    if not isinstance(servers, list):
        return []
    server_ids: list[str] = []
    seen: set[str] = set()
    for item in servers:
        if not isinstance(item, dict):
            continue
        server_id = item.get("server_id")
        if not isinstance(server_id, str) or not server_id or server_id in seen:
            continue
        if item.get("authorization_required") is not True:
            continue
        if item.get("status") != "failed":
            continue
        if item.get("status_code") != 401 and item.get("auth_error") not in {
            "invalid_token",
            "insufficient_scope",
        }:
            continue
        server_ids.append(server_id)
        seen.add(server_id)
    return server_ids


async def _mark_reconcile_unavailable(app: Any, row: Any) -> None:
    """Move an unavailable executor out of transient reconfiguring state."""
    runtime_metadata = _merge_runtime_metadata(
        getattr(row, "runtime_metadata", None) or {},
        {
            "runtime_state": "stale",
            "warnings": [
                "Executor reconfigure could not continue because the connection is unavailable."
            ],
        },
    )
    await _persist_runtime_state(
        app,
        row.executor_id,
        runtime_state="stale",
        applied_config_version=int(getattr(row, "applied_config_version", 0) or 0),
        observed_tools=list(getattr(row, "observed_tools", None) or []),
        runtime_metadata=runtime_metadata,
    )


async def _mark_reconcile_failed(app: Any, executor_id: str, exc: Exception) -> None:
    """Persist a terminal reconcile failure so schedulers do not see a transient state forever."""
    async with app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id)
    if row is None or getattr(row, "runtime_state", "offline") != "reconfiguring":
        return
    runtime_metadata = _merge_runtime_metadata(
        getattr(row, "runtime_metadata", None) or {},
        {
            "runtime_state": "blocked",
            "warnings": [
                f"Executor background reconfigure failed: {_safe_error_message(str(exc))}"
            ],
        },
    )
    await _persist_runtime_state(
        app,
        executor_id,
        runtime_state="blocked",
        applied_config_version=int(getattr(row, "applied_config_version", 0) or 0),
        observed_tools=list(getattr(row, "observed_tools", None) or []),
        runtime_metadata=runtime_metadata,
    )


async def _build_configure_payload(
    app: Any,
    row: Any,
    desired_version: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from cognis.api.executor_ws import _resolve_executor_mcp_payload
    from cognis.api.runtime_support import _resolve_web_config
    from cognis.tools.skills import _qualified_skill_tool_name

    mcp_servers, scoped_secrets, mcp_metadata = await _resolve_executor_mcp_payload(
        row, app.state.providers
    )
    web_config = await _resolve_web_config(app.state.providers, row.owner_email)
    executor_config = row.config if isinstance(row.config, dict) else {}
    inference_config = resolve_executor_local_inference_config(executor_config)
    ollama_runtime = inference_config.ollama_runtime.model_copy(
        update={"management_enabled": inference_config.ollama_management_enabled}
    )
    scoped_secrets.update(web_config.get("web_secrets", {}))
    skill_manifests: list[dict[str, Any]] = []
    try:
        dummy_agent = AgentDefinition(
            agent_id="executor_configure",
            owner_email=row.owner_email,
            name="executor_configure",
        )
        async with app.state.session_factory() as db_session:
            resolved = await resolve_skills_for_agent(
                db_session, dummy_agent, owner_email=row.owner_email
            )
        for skill in resolved.skills:
            if skill.tools or skill.asset_manifest:
                manifest: dict[str, Any] = {
                    "skill_id": skill.skill_id,
                    "version_id": skill.version_id,
                    "content_hash": skill.content_hash,
                    "tools": [
                        {
                            **t.model_dump(mode="json"),
                            "qualified_name": _qualified_skill_tool_name(skill.skill_id, t.name),
                        }
                        for t in skill.tools
                    ],
                    "asset_manifest": [a.model_dump(mode="json") for a in skill.asset_manifest],
                }
                artifact_store = getattr(app.state, "artifact_store", None)
                if artifact_store and skill.asset_manifest:
                    ttl_seconds = getattr(
                        getattr(artifact_store, "_config", None),  # noqa: SLF001
                        "signed_url_ttl_seconds",
                        None,
                    )
                    for asset_entry in manifest["asset_manifest"]:
                        ns = asset_entry.get("artifact_namespace", "skills")
                        oid = asset_entry.get("artifact_object_id", "")
                        filename = asset_entry.get("filename", "")
                        if oid:
                            with contextlib.suppress(Exception):
                                asset_entry["url"] = await artifact_store.async_get_public_url(
                                    ns,
                                    oid,
                                    filename,
                                    ttl_seconds=ttl_seconds,
                                )
                skill_manifests.append(manifest)
    except Exception:
        _logger.warning(
            "executor_runtime: failed to resolve skill manifests",
            extra={"extra_data": {"executor_id": row.executor_id}},
            exc_info=True,
        )

    previous_metadata = getattr(row, "runtime_metadata", None) or {}
    previous_platform = previous_metadata.get("platform")
    previous_environment = previous_metadata.get("environment")
    metadata = {
        "schema_version": RUNTIME_METADATA_SCHEMA_VERSION,
        "configure_capabilities": [CONFIGURE_CAPABILITY_MCP_RUNTIME_STATUS],
        "single_controller_process": True,
        "legacy_metadata": False,
        "mcp_servers": [],
        "warnings": [],
        "config_version": desired_version,
        "platform": dict(previous_platform) if isinstance(previous_platform, dict) else {},
        "environment": (
            dict(previous_environment) if isinstance(previous_environment, dict) else {}
        ),
    }
    previous_snapshot = normalize_executor_resource_snapshot(
        previous_metadata.get("resource_snapshot")
    )
    if previous_snapshot is not None:
        metadata["resource_snapshot"] = previous_snapshot.model_dump(
            mode="json",
            exclude={"freshness"},
        )
        previous_received_at = _coerce_utc_datetime(
            previous_metadata.get("resource_snapshot_received_at")
        )
        if previous_received_at is not None:
            metadata["resource_snapshot_received_at"] = previous_received_at.isoformat()
    if mcp_metadata:
        metadata["mcp_servers"] = list(mcp_metadata.get("mcp_servers") or [])
        metadata["warnings"] = list(mcp_metadata.get("warnings") or [])
    payload = {
        "config_version": desired_version,
        "enabled_tools": row.enabled_tools or [],
        "enabled_tool_groups": row.enabled_tool_groups or [],
        "config": {
            **executor_config,
            "local_inference_enabled": inference_config.local_inference_enabled,
            "ollama_runtime": ollama_runtime.model_dump(
                mode="json",
                exclude={"endpoint"},
            ),
        },
        "mcp_servers": [server.model_dump(mode="json") for server in mcp_servers],
        "secrets": scoped_secrets,
        "web_config": {
            "web_backend": web_config.get("web_backend", "direct"),
            "web_search_backend": web_config.get("web_search_backend", "direct"),
            "web_fetch_backend": web_config.get("web_fetch_backend", "direct"),
            "web_fetch_fallback_browser": web_config.get("web_fetch_fallback_browser", True),
            "web_searxng_url": web_config.get("web_searxng_url", ""),
            "web_searxng_engines": web_config.get("web_searxng_engines", ""),
            "web_searxng_categories": web_config.get("web_searxng_categories", ""),
            "web_searxng_language": web_config.get("web_searxng_language", ""),
            "web_browser_fetch_session_idle_seconds": web_config.get(
                "web_browser_fetch_session_idle_seconds", 60
            ),
            "web_browser_fetch_wait_timeout_seconds": web_config.get(
                "web_browser_fetch_wait_timeout_seconds", 30
            ),
            "web_browser_fetch_navigation_timeout_seconds": web_config.get(
                "web_browser_fetch_navigation_timeout_seconds", 60
            ),
            "web_browser_fetch_wait_until": web_config.get(
                "web_browser_fetch_wait_until", "domcontentloaded"
            ),
            "web_browser_fetch_network_idle_after_dom_seconds": web_config.get(
                "web_browser_fetch_network_idle_after_dom_seconds", 3
            ),
            "web_browser_fetch_headed_fallback_enabled": web_config.get(
                "web_browser_fetch_headed_fallback_enabled", True
            ),
            "web_concurrency": web_config.get("web_concurrency", {}),
            "web_available_backends": web_config.get("web_available_backends", ["direct"]),
            "web_available_search_backends": web_config.get(
                "web_available_search_backends", ["direct"]
            ),
            "web_available_fetch_backends": web_config.get(
                "web_available_fetch_backends", ["direct"]
            ),
        },
        "skill_manifests": skill_manifests,
        "ollama_runtime": ollama_runtime.model_dump(mode="json"),
        "local_inference_enabled": inference_config.local_inference_enabled,
    }
    return payload, metadata


def _merge_runtime_metadata(base: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged.update(result)
    merged.setdefault("schema_version", RUNTIME_METADATA_SCHEMA_VERSION)
    merged.setdefault("configure_capabilities", [CONFIGURE_CAPABILITY_MCP_RUNTIME_STATUS])
    merged.setdefault("legacy_metadata", False)
    merged.setdefault("mcp_servers", [])
    merged.setdefault("warnings", [])
    warnings = merged.get("warnings")
    if isinstance(warnings, list):
        base_warning_value = base.get("warnings")
        result_warning_value = result.get("warnings")
        base_warnings = base_warning_value if isinstance(base_warning_value, list) else []
        result_warnings = result_warning_value if isinstance(result_warning_value, list) else []
        merged["warnings"] = [
            str(item)[:MAX_SAFE_ERROR_LENGTH] for item in [*base_warnings, *result_warnings]
        ][:10]
    else:
        merged["warnings"] = []
    base_mcp_value = base.get("mcp_servers")
    result_mcp_value = result.get("mcp_servers")
    base_mcp_servers = base_mcp_value if isinstance(base_mcp_value, list) else []
    result_mcp_servers = result_mcp_value if isinstance(result_mcp_value, list) else []
    merged["mcp_servers"] = [*base_mcp_servers, *result_mcp_servers]
    return merged


def _sanitize_reported_runtime_metadata(value: Any) -> dict[str, Any]:
    """Allowlist bounded diagnostics returned by an authenticated executor."""

    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, Any] = {}
    configure_capabilities = value.get("configure_capabilities")
    if isinstance(configure_capabilities, list):
        sanitized["configure_capabilities"] = _bounded_string_list(
            configure_capabilities,
            max_items=32,
            max_length=64,
        )
    platform = value.get("platform")
    if isinstance(platform, dict):
        sanitized_platform = _string_mapping(
            platform,
            allowed={"os", "arch", "python"},
            max_value_length=128,
        )
        if sanitized_platform:
            sanitized["platform"] = sanitized_platform
    environment = value.get("environment")
    if isinstance(environment, dict):
        sanitized_environment = _string_mapping(
            environment,
            allowed={"user", "home", "cwd", "hostname", "source", "observed_at"},
            max_value_length=1024,
        )
        if sanitized_environment:
            sanitized["environment"] = sanitized_environment
    ollama_runtime = _ollama_runtime_capability(value.get("ollama_runtime"))
    if ollama_runtime is not None:
        sanitized["ollama_runtime"] = ollama_runtime.model_dump(mode="json")
    if isinstance(value.get("local_inference_enabled"), bool):
        sanitized["local_inference_enabled"] = value["local_inference_enabled"]
    mcp_servers = value.get("mcp_servers")
    if isinstance(mcp_servers, list):
        sanitized["mcp_servers"] = [
            _sanitize_mcp_server_status(item)
            for item in mcp_servers[:128]
            if isinstance(item, dict)
        ]
    warnings = value.get("warnings")
    if isinstance(warnings, list):
        sanitized["warnings"] = [
            item[:MAX_SAFE_ERROR_LENGTH] for item in warnings[:128] if isinstance(item, str)
        ]
    for issue_key in ("runtime_issues", "degraded_issues"):
        issues = value.get(issue_key)
        if isinstance(issues, list):
            sanitized[issue_key] = [
                _bounded_mapping(
                    item,
                    allowed={"source", "kind", "title", "severity", "message"},
                )
                for item in issues[:128]
                if isinstance(item, dict)
            ]
    officecli = value.get("officecli")
    if isinstance(officecli, dict):
        sanitized["officecli"] = _bounded_mapping(
            officecli,
            allowed={"available", "enabled", "version", "platform", "error"},
        )
    for key in (
        "officecli_available",
        "officecli_enabled",
        "officecli_auto_install",
        "officecli_version",
        "officecli_platform",
        "officecli_error",
        "lsp_init_failed",
        "lsp_warning",
    ):
        if key not in value:
            continue
        item = value.get(key)
        if isinstance(item, (bool, int)) or item is None:
            sanitized[key] = item
        elif isinstance(item, str):
            sanitized[key] = item[:MAX_SAFE_ERROR_LENGTH]
    return sanitized


def _sanitize_configure_result_runtime_metadata(
    value: Any,
    *,
    received_at: datetime,
) -> dict[str, Any]:
    sanitized = _sanitize_reported_runtime_metadata(value)
    snapshot = normalize_executor_resource_snapshot(
        value.get("resource_snapshot") if isinstance(value, dict) else None
    )
    if snapshot is not None:
        sanitized["resource_snapshot"] = snapshot.model_dump(
            mode="json",
            exclude={"freshness"},
        )
        sanitized["resource_snapshot_received_at"] = received_at.isoformat()
    return sanitized


def _string_mapping(
    value: dict[str, Any],
    *,
    allowed: set[str],
    max_value_length: int,
) -> dict[str, str]:
    return {
        key: item[:max_value_length]
        for key, item in value.items()
        if key in allowed and isinstance(item, str)
    }


def _bounded_mapping(
    value: dict[str, Any],
    *,
    allowed: set[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if isinstance(item, str):
            result[key] = item[:MAX_SAFE_ERROR_LENGTH]
        elif isinstance(item, (bool, int)) or item is None:
            result[key] = item
    return result


def _bounded_string_list(
    value: list[Any],
    *,
    max_items: int,
    max_length: int,
) -> list[str]:
    return [item[:max_length] for item in value[:max_items] if isinstance(item, str)]


def _sanitize_mcp_server_status(value: dict[str, Any]) -> dict[str, Any]:
    result = _bounded_mapping(
        value,
        allowed={
            "server_id",
            "name",
            "status",
            "phase",
            "error_class",
            "timed_out",
            "message",
            "stderr_summary",
            "tool_count",
            "auth_error",
            "authorization_required",
            "status_code",
            "www_authenticate",
        },
    )
    challenge = value.get("authorization_challenge")
    if isinstance(challenge, dict):
        result["authorization_challenge"] = {
            key[:64]: item[:1024]
            for key, item in list(challenge.items())[:32]
            if isinstance(key, str) and isinstance(item, str)
        }
    return result


def _safe_error_message(message: str) -> str:
    return " ".join(message.split())[:MAX_SAFE_ERROR_LENGTH] or "unknown error"


def _coerce_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _ollama_runtime_capability(value: Any) -> OllamaRuntimeCapability | None:
    if not isinstance(value, dict):
        return None
    try:
        return OllamaRuntimeCapability.model_validate(value)
    except Exception:
        return None


def _fast_path_local_inference_enabled(
    row: Any,
    runtime_metadata: dict[str, Any],
) -> bool:
    """Combine persisted desired intent with the last strict live advertisement."""

    return executor_local_inference_config_confirmed(row)


def _live_local_inference_capability_matches(
    row: Any,
    capabilities: ExecutorCapabilities,
) -> bool:
    """Require live flags and the effective endpoint to match desired config."""

    desired = resolve_executor_local_inference_config(row.config or {})
    advertised = capabilities.local_model_runtime
    return (
        desired.local_inference_enabled
        and capabilities.inference
        and capabilities.local_inference
        and advertised is not None
        and advertised.port == desired.ollama_runtime.port
        and advertised.endpoint == desired.ollama_runtime.endpoint
        and advertised.management_enabled is desired.ollama_management_enabled
    )


def _get_executor_lock(app: Any, executor_id: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = getattr(app.state, "executor_reconcile_locks", {})
    if not hasattr(app.state, "executor_reconcile_locks"):
        app.state.executor_reconcile_locks = locks
    lock = locks.get(executor_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[executor_id] = lock
    return lock


def _is_current_connection(app: Any, executor_id: str, connection: Any) -> bool:
    return app.state.providers.executor.websocket.get_connection(executor_id) is connection


async def _connection_ownership_is_current(app: Any, connection: Any) -> bool:
    ownership = getattr(app.state, "executor_connection_ownership", None)
    if ownership is None:
        return True
    owner = getattr(connection, "connection_owner", None)
    return owner is not None and await ownership.is_current(owner)


def _executor_connection_metadata(
    *,
    labels: dict[str, Any],
    environment: Any,
    platform: dict[str, Any],
    status: str,
    owner_email: str | None,
    runtime_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "labels": labels,
        "platform": platform,
        "status": status,
        "owner_email": owner_email,
        "shared": is_shared_owner_email(owner_email),
    }
    if isinstance(environment, dict):
        metadata["environment"] = environment
    if runtime_metadata is not None:
        metadata["runtime_metadata"] = runtime_metadata
    return metadata
