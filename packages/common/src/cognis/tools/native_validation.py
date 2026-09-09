"""Pure native-validation contracts with optional controller delegation."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast

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


def _controller_validators() -> ModuleType | None:
    """Load controller validators only when the controller distribution is installed."""

    try:
        return importlib.import_module("cognis.tools.controller_native_validation")
    except ModuleNotFoundError as exc:
        if exc.name == "cognis.tools.controller_native_validation":
            return None
        raise


async def validate_native_operation_domains(
    operation: NativeToolOperation,
    arguments: dict[str, Any],
    context: NativeValidationContext | None,
) -> list[NativeValidationIssue]:
    """Run controller validators, failing closed when they are unavailable."""

    validators = _controller_validators()
    if validators is not None:
        return cast(
            list[NativeValidationIssue],
            await validators.validate_native_operation_domains(operation, arguments, context),
        )
    return [
        NativeValidationIssue(
            code="unknown_native_validator",
            path="<root>",
            message=f"Unknown native validator: {validator_id}",
        )
        for validator_id in operation.validator_ids
    ]


def registered_native_validator_ids() -> set[str]:
    validators = _controller_validators()
    if validators is None:
        return set()
    return cast(set[str], validators.registered_native_validator_ids())


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
