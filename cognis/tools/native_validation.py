"""Shared domain validation for declared native tool operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from cognis.models.tool import NativeToolOperation


@dataclass(frozen=True, slots=True)
class NativeValidationContext:
    """State available to domain validators without granting mutation access."""

    actor_email: str | None = None
    current_agent_id: str | None = None
    agent_management_deps: Any | None = None
    session_factory: Any | None = None
    artifact_store: Any | None = None
    conversation_id: str | None = None
    conversation_agent_id: str | None = None
    write_deliverable_available: bool = True
    write_deliverable_validation_phase: str = "preflight"
    write_deliverable_exact_validation_present: bool = True
    task_title: str = ""
    task_description: str = ""
    task_expected_output: str | None = None
    loaded_skill_names: frozenset[str] = frozenset()
    loaded_skill_snapshots: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...] = ()
    executed_tool_names: tuple[str, ...] = ()
    materialized_artifact_evidence: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class NativeValidationIssue:
    code: str
    path: str
    message: str


type NativeValidator = Callable[
    [dict[str, Any], NativeValidationContext],
    Awaitable[list[NativeValidationIssue]],
]


async def validate_native_operation_domains(
    operation: NativeToolOperation,
    arguments: dict[str, Any],
    context: NativeValidationContext | None,
) -> list[NativeValidationIssue]:
    """Run every declared validator, failing closed on unknown validator IDs."""

    issues: list[NativeValidationIssue] = []
    for validator_id in operation.validator_ids:
        validator = _VALIDATORS.get(validator_id)
        if validator is None:
            issues.append(
                NativeValidationIssue(
                    code="unknown_native_validator",
                    path="<root>",
                    message=f"Unknown native validator: {validator_id}",
                )
            )
            continue
        issues.extend(await validator(arguments, context or NativeValidationContext()))
    return issues


def registered_native_validator_ids() -> set[str]:
    return set(_VALIDATORS)


def write_deliverable_validation_state_fingerprint(
    context: NativeValidationContext,
    *,
    schema_hash: str | None,
) -> str:
    """Bind preflight to every deterministic session-known validation input."""

    payload = {
        "schema_hash": schema_hash,
        "task": [
            context.task_title,
            context.task_description,
            context.task_expected_output,
        ],
        "loaded_skill_names": sorted(context.loaded_skill_names),
        "loaded_skill_snapshots": context.loaded_skill_snapshots,
        "executed_tool_names": context.executed_tool_names,
        "materialized_artifact_evidence": context.materialized_artifact_evidence,
        "conversation_id": context.conversation_id,
        "conversation_agent_id": context.conversation_agent_id,
        "write_deliverable_available": context.write_deliverable_available,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


async def _validate_agent_settings(
    arguments: dict[str, Any],
    context: NativeValidationContext,
) -> list[NativeValidationIssue]:
    from cognis.core.agent_management import (
        AgentManagementError,
        _require_owned_target,
        _settings_updates,
    )

    if (
        context.agent_management_deps is None
        or not context.actor_email
        or not context.current_agent_id
    ):
        return [
            NativeValidationIssue(
                code="native_validation_context_unavailable",
                path="<root>",
                message="Agent settings validation requires caller runtime context.",
            )
        ]
    settings = arguments.get("settings")
    if not isinstance(settings, dict):
        return []
    try:
        row = await _require_owned_target(
            context.agent_management_deps,
            context.actor_email,
            context.current_agent_id,
            arguments,
        )
        updates = await _settings_updates(
            context.agent_management_deps,
            context.actor_email,
            row,
            settings,
        )
        if not updates:
            raise AgentManagementError("No settings update fields provided")
    except AgentManagementError as exc:
        return [
            NativeValidationIssue(
                code="invalid_agent_settings",
                path="settings",
                message=str(exc),
            )
        ]
    return []


async def _validate_tool_assignment(
    arguments: dict[str, Any],
    context: NativeValidationContext,
) -> list[NativeValidationIssue]:
    from cognis.core.agent_management import (
        AgentManagementError,
        _apply_assignment_delta,
        _configured_tool_assignment,
        _require_owned_target,
        _tool_assignment_from_arguments,
        _validate_tool_assignment,
    )

    deps = context.agent_management_deps
    if deps is None or not context.actor_email or not context.current_agent_id:
        return [
            NativeValidationIssue(
                code="native_validation_context_unavailable",
                path="<root>",
                message="Tool assignment validation requires caller runtime context.",
            )
        ]
    try:
        row = await _require_owned_target(
            deps,
            context.actor_email,
            context.current_agent_id,
            arguments,
        )
        action = arguments.get("action")
        if action == "tools_set":
            proposed = _tool_assignment_from_arguments(arguments, row)
        else:
            current = _configured_tool_assignment(row)
            delta = _tool_assignment_from_arguments(arguments, row, default_empty=True)
            proposed = {
                key: _apply_assignment_delta(
                    current.get(key, []),
                    delta.get(key, []),
                    remove=action == "tools_remove",
                )
                for key in ("tool_groups", "allow_tools", "deny_tools")
            }
        result = _validate_tool_assignment(row, proposed, deps.assignable_tools)
    except AgentManagementError as exc:
        return [
            NativeValidationIssue(
                code="invalid_tool_assignment",
                path="<root>",
                message=str(exc),
            )
        ]
    return [
        NativeValidationIssue(
            code="invalid_tool_assignment",
            path=str(error.get("field") or "<root>"),
            message=str(error.get("reason") or "Invalid tool assignment"),
        )
        for error in result["errors"]
    ]


async def _validate_agent_create(
    arguments: dict[str, Any],
    _context: NativeValidationContext,
) -> list[NativeValidationIssue]:
    name = arguments.get("name")
    if isinstance(name, str) and name.strip():
        return []
    return [
        NativeValidationIssue(
            code="invalid_agent_name",
            path="name",
            message="name must contain non-whitespace characters",
        )
    ]


async def _validate_agent_update(
    arguments: dict[str, Any],
    _context: NativeValidationContext,
) -> list[NativeValidationIssue]:
    from cognis.core.agent_management import _agent_updates

    if _agent_updates(arguments) or arguments.get("generate_avatar") is True:
        return []
    return [
        NativeValidationIssue(
            code="empty_agent_update",
            path="<root>",
            message="No agent update fields provided",
        )
    ]


async def _validate_schedule_definition(
    arguments: dict[str, Any],
    context: NativeValidationContext,
) -> list[NativeValidationIssue]:
    from cognis.core.agent_profiles import normalize_agent_profile_id
    from cognis.tools.builtin.schedule import (
        _resolve_delivery,
        _resolve_timing_fields,
        _validate_agent_and_workflow,
        _validate_agent_profile,
        _validate_delivery_target,
    )

    if context.session_factory is None or not context.actor_email:
        return [
            NativeValidationIssue(
                code="missing_schedule_validation_context",
                path="<root>",
                message="Schedule validation requires caller-scoped persisted state",
            )
        ]
    existing = None
    if arguments.get("action") == "update":
        from cognis.store.queries import get_schedule

        async with context.session_factory() as session:
            existing = await get_schedule(session, str(arguments.get("schedule_id", "")))
        if existing is None or existing.created_by != context.actor_email:
            return [
                NativeValidationIssue(
                    code="schedule_not_found",
                    path="$.schedule_id",
                    message="Schedule not found for the current caller",
                )
            ]
    timing_keys = {"schedule_type", "cron_expr", "interval_seconds", "one_shot_at"}
    if existing is None or timing_keys & arguments.keys():
        _fields, error = _resolve_timing_fields(arguments, existing=existing)
        if error is not None:
            return [
                NativeValidationIssue(
                    code="invalid_schedule_definition",
                    path="<root>",
                    message=error.output,
                )
            ]

    if existing is None:
        target_agent = arguments.get("agent_id") or context.current_agent_id
        workflow_id = arguments.get("workflow_id")
        profile_value = arguments.get("agent_profile_id")
        existing_delivery: dict[str, Any] | None = None
    else:
        target_agent = arguments.get("agent_id")
        workflow_id = arguments.get("workflow_id")
        profile_value = arguments.get("agent_profile_id")
        template = existing.task_template if isinstance(existing.task_template, dict) else {}
        raw_delivery = template.get("delivery")
        existing_delivery = raw_delivery if isinstance(raw_delivery, dict) else {}

    try:
        agent_profile_id = normalize_agent_profile_id(profile_value)
    except ValueError as exc:
        return [
            NativeValidationIssue(
                code="invalid_agent_profile_id",
                path="$.agent_profile_id",
                message=str(exc),
            )
        ]

    validate_delivery = (
        existing is None
        or bool(arguments.get("delivery_mode"))
        or arguments.get("delivery_target") is not None
    )
    delivery: dict[str, Any] | None = None
    if validate_delivery:
        delivery, delivery_error = _resolve_delivery(arguments, existing=existing_delivery)
        if delivery_error is not None:
            return [
                NativeValidationIssue(
                    code="invalid_delivery",
                    path="$.delivery_target",
                    message=delivery_error.output,
                )
            ]

    async with context.session_factory() as session:
        domain_error = await _validate_agent_and_workflow(
            session,
            context.actor_email,
            target_agent=target_agent,
            workflow_id=workflow_id,
        )
        if domain_error is None:
            domain_error = await _validate_agent_profile(
                session,
                context.actor_email,
                target_agent=target_agent or (existing.agent_id if existing is not None else None),
                agent_profile_id=agent_profile_id,
            )
        if (
            domain_error is None
            and delivery is not None
            and delivery["mode"] == "specific_conversation"
        ):
            domain_error = await _validate_delivery_target(
                session,
                context.actor_email,
                str(delivery["target"]),
            )
    if domain_error is None:
        return []
    return [
        NativeValidationIssue(
            code="invalid_schedule_domain",
            path="<root>",
            message=domain_error.output,
        )
    ]


async def _validate_write_deliverable_rich(
    arguments: dict[str, Any],
    context: NativeValidationContext,
) -> list[NativeValidationIssue]:
    from cognis.core.daily_brief_contract import (
        CURRENT_DAILY_BRIEF_CONTRACT_VERSION,
        resolve_daily_brief_contract,
        validate_daily_brief_deliverable,
    )
    from cognis.core.deliverable_media import authorize_rich_media, rich_payload_has_media
    from cognis.models.deliverable import (
        RichPayloadValidationError,
        normalize_required_rich_payload,
    )

    issues: list[NativeValidationIssue] = []
    if not context.write_deliverable_available:
        issues.append(
            NativeValidationIssue(
                code="not_in_workflow",
                path="<root>",
                message="write_deliverable is unavailable outside workflow or direct-chat scope.",
            )
        )

    format_name = str(arguments.get("format") or "markdown")
    normalized_rich: dict[str, Any] | None = None
    if format_name == "rich":
        try:
            normalized_rich, _warnings = normalize_required_rich_payload(arguments.get("rich"))
        except RichPayloadValidationError as exc:
            issues.extend(_rich_payload_validation_issues(exc))

    snapshots = {skill_id: dict(items) for skill_id, items in context.loaded_skill_snapshots}
    activation = resolve_daily_brief_contract(
        task_title=context.task_title,
        task_description=context.task_description,
        task_expected_output=context.task_expected_output,
        loaded_skill_names=context.loaded_skill_names,
        loaded_skill_snapshots=snapshots,
    )
    if activation is not None and activation.version >= CURRENT_DAILY_BRIEF_CONTRACT_VERSION:
        daily_brief_issues = validate_daily_brief_deliverable(
            action=arguments.get("action"),
            format_name=format_name,
            rich=arguments.get("rich"),
            validation_fingerprint_present=context.write_deliverable_exact_validation_present,
            executed_tool_names=context.executed_tool_names,
            materialized_artifact_evidence=dict(context.materialized_artifact_evidence),
        )
        issues.extend(
            NativeValidationIssue(code="invalid_daily_brief", path="$", message=issue)
            for issue in daily_brief_issues
        )

    if normalized_rich is None or not rich_payload_has_media(normalized_rich):
        return issues
    if (
        context.session_factory is None
        or context.artifact_store is None
        or not context.actor_email
        or not context.conversation_id
    ):
        issues.append(
            NativeValidationIssue(
                code="missing_rich_media_access_context",
                path="$.rich.blocks[*].media.ref",
                message="Rich media validation requires caller-scoped artifact access context.",
            )
        )
        return issues
    try:
        async with context.session_factory() as session:
            await authorize_rich_media(
                session,
                context.artifact_store,
                deepcopy(normalized_rich),
                owner_email=context.actor_email,
                accessor_conversation_id=context.conversation_id,
                accessor_agent_id=context.conversation_agent_id,
                retain=False,
            )
    except RichPayloadValidationError as exc:
        issues.extend(_rich_payload_validation_issues(exc))
    return issues


def _rich_payload_validation_issues(exc: Any) -> list[NativeValidationIssue]:
    issues: list[NativeValidationIssue] = []
    for issue in exc.issues:
        rich_path = str(issue["path"])
        if rich_path == "$.rich" or rich_path.startswith("$.rich."):
            retry_path = rich_path
        else:
            retry_path = f"$.rich{rich_path[1:]}" if rich_path.startswith("$") else "$.rich"
        issues.append(
            NativeValidationIssue(
                code=str(issue["reason"]),
                path=retry_path,
                message=(
                    f"At {retry_path}, expected {issue['expected']}. "
                    "Correct that path and retry write_deliverable with format='rich'."
                ),
            )
        )
    return issues


_VALIDATORS: dict[str, NativeValidator] = {
    "manage_agents.settings_update": _validate_agent_settings,
    "manage_agents.tool_assignment": _validate_tool_assignment,
    "manage_agents.create": _validate_agent_create,
    "manage_agents.update": _validate_agent_update,
    "schedule.definition": _validate_schedule_definition,
    "write_deliverable.rich": _validate_write_deliverable_rich,
}
