"""Controller-owned deterministic step declarations; never user-message evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cognis.core.program_guidance import PYTHON_EXECUTE_TOOL_NAME
from cognis.models.task import TaskModel
from cognis.models.tool import ToolCall
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def canonical_digest(value: Any) -> str:
    """Hash canonical JSON, shared byte-for-byte with the Intaris contract."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


class WorkflowStepDeclaration(BaseModel):
    """Frozen v1 wire contract. No raw argument contents or human claims."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    origin: Literal["controller_workflow_step"] = "controller_workflow_step"
    session_id: Identifier
    task_id: Identifier
    workflow_id: Identifier
    step_name: Identifier
    step_run_id: Identifier
    logical_call_id: Identifier
    workflow_definition_digest: Digest
    step_definition_digest: Digest
    rendered_args_digest: Digest
    tool: Identifier
    evaluated_args_digest: Digest
    executor_id: Identifier
    authorized_by: Literal["owner_configured", "agent_composed"]
    projection: Literal["exact", "unsupported"]

    @property
    def declaration_digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class WorkflowStepReceipt(BaseModel):
    """Validated declaration acknowledgement; not an execution approval."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    ok: Literal[True]
    declaration_digest: Digest
    admission_revision: int = Field(ge=0)


class WorkflowEvaluationBinding(BaseModel):
    """Controller-only identity and declaration retained across admission retries."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    declaration: WorkflowStepDeclaration
    user_email: Identifier
    agent_id: Identifier

    @property
    def controller_context(self) -> dict[str, str]:
        return {
            "governing_execution_id": self.declaration.step_run_id,
            "governing_snapshot_id": self.declaration.workflow_definition_digest,
            "executor_id": self.declaration.executor_id,
        }


class DeclaredWorkflowCall(ToolCall):
    """Prepared controller call; the binding is never serialized as tool data."""

    workflow_binding: WorkflowEvaluationBinding = Field(exclude=True, frozen=True)


def evaluation_binding(tool_call: ToolCall) -> WorkflowEvaluationBinding | None:
    return tool_call.workflow_binding if isinstance(tool_call, DeclaredWorkflowCall) else None


class WorkflowStepDeclarationError(RuntimeError):
    """Safe, content-free failure that must stop dispatch."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Tool not dispatched: {code}")


async def refresh_scope_origin(
    task: TaskModel, session_factory: Any
) -> tuple[TaskModel, str | None]:
    """Recheck revocation after pinning, including a scheduler's detached row."""
    if task.creation_origin != "owner_configured":
        return task, None
    from cognis.store.queries import get_schedule, get_task, get_workflow

    reason = None
    async with session_factory() as db:
        row = await get_task(db, task.task_id)
        owner_configured = row is not None and row.creation_origin == "owner_configured"
        if task.source_type == "scheduler":
            schedule = await get_schedule(db, task.source_ref) if task.source_ref else None
            if schedule is None:
                reason = "schedule_missing"
            else:
                current = (
                    await get_workflow(db, schedule.workflow_id) if schedule.workflow_id else None
                )
                pinned = (
                    task.workflow_state.effective_workflow_definition
                    if task.workflow_state
                    else None
                )
                if (
                    schedule.creation_origin != "owner_configured"
                    or schedule.workflow_id != task.workflow_id
                    or current is None
                    or pinned is None
                    or canonical_digest(
                        Workflow.model_validate(current.definition).model_dump(
                            mode="json", exclude_none=True
                        )
                    )
                    != canonical_digest(pinned)
                ):
                    reason = "schedule_digest_mismatch"
            owner_configured = owner_configured and reason is None
    return (
        task.model_copy(
            update={"creation_origin": "owner_configured" if owner_configured else "agent_composed"}
        ),
        reason,
    )


@dataclass(frozen=True)
class WorkflowStepScope:
    """Per-dispatch immutable binding, passed explicitly through the controller."""

    session_id: str
    task_id: str
    workflow_id: str
    step_name: str
    step_run_id: str
    logical_call_id: str
    workflow_definition_digest: str
    step_definition_digest: str
    rendered_args_digest: str
    routed_args_digest: str
    tool: str
    authorized_by: Literal["owner_configured", "agent_composed"]

    @classmethod
    def build(
        cls,
        *,
        task: TaskModel,
        workflow: Workflow,
        state: WorkflowState,
        step: StepDefinition,
        session_id: str,
        step_run_id: str,
        logical_call_id: str,
        rendered_args: dict[str, Any],
    ) -> WorkflowStepScope:
        """Bind only a stored, pinned step, not task prose or remote outputs."""
        definition = state.effective_workflow_definition
        if definition is None:
            raise WorkflowStepDeclarationError("workflow_step.definition_unverified")
        if step.type == "tool_call" and step.tool_call is not None:
            tool = step.tool_call.tool
            step_contract = {"tool": step.tool_call.tool, "args": step.tool_call.args}
        elif step.type == "program" and step.program is not None:
            tool = PYTHON_EXECUTE_TOOL_NAME
            step_contract = {
                "type": "program",
                "program": step.program.model_dump(mode="json", exclude_none=True),
            }
        else:
            raise WorkflowStepDeclarationError("workflow_step.definition_unverified")
        pinned = Workflow.model_validate(definition)
        candidates = [item for item in pinned.steps if item.name == step.name]
        if (
            pinned.workflow_id != workflow.workflow_id
            or len(candidates) != 1
            or candidates[0].type != step.type
            or (
                candidates[0].tool_call != step.tool_call
                if step.type == "tool_call"
                else candidates[0].program != step.program
            )
        ):
            raise WorkflowStepDeclarationError("workflow_step.definition_unverified")
        return cls(
            session_id=session_id,
            task_id=task.task_id,
            workflow_id=pinned.workflow_id,
            step_name=step.name,
            step_run_id=step_run_id,
            logical_call_id=logical_call_id,
            workflow_definition_digest=canonical_digest(definition),
            step_definition_digest=canonical_digest(step_contract),
            rendered_args_digest=canonical_digest(rendered_args),
            routed_args_digest=canonical_digest(
                {key: value for key, value in rendered_args.items() if key != "target_executor"}
            ),
            tool=tool,
            authorized_by=(
                "owner_configured"
                if task.creation_origin == "owner_configured" and not task.created_by_agent_id
                else "agent_composed"
            ),
        )

    def declaration(
        self, *, session_id: str, call_id: str, tool: str, args: dict[str, Any], executor_id: str
    ) -> WorkflowStepDeclaration:
        """Capture the exact evaluation object after routing transformations."""
        if (session_id, call_id, tool) != (self.session_id, self.logical_call_id, self.tool):
            raise WorkflowStepDeclarationError("workflow_step.declaration_mismatch")
        evaluated_digest = canonical_digest(args)
        return WorkflowStepDeclaration(
            session_id=self.session_id,
            task_id=self.task_id,
            workflow_id=self.workflow_id,
            step_name=self.step_name,
            step_run_id=self.step_run_id,
            logical_call_id=self.logical_call_id,
            workflow_definition_digest=self.workflow_definition_digest,
            step_definition_digest=self.step_definition_digest,
            rendered_args_digest=self.rendered_args_digest,
            tool=tool,
            evaluated_args_digest=evaluated_digest,
            executor_id=executor_id,
            authorized_by=self.authorized_by,
            projection="exact" if evaluated_digest == self.routed_args_digest else "unsupported",
        )
