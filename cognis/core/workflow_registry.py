"""Workflow registry — system workflows and DB-backed user workflows."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.workflow import (
    CompletionConfig,
    InteractionMode,
    OutcomeRoute,
    StepDefinition,
    StepInputConfig,
    Workflow,
    WorkflowDefaults,
    resolve_effective_input,
)
from cognis.store.queries import create_workflow, get_workflow, list_workflows

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# System workflows (bundled, read-only)
# ---------------------------------------------------------------------------

DIRECT_WORKFLOW = Workflow(
    workflow_id="system:direct",
    name="Direct",
    description="Single-step execution. No planning or evaluation.",
    criteria="Simple questions, quick tasks, conversational messages.",
    tags=["chat", "inline"],
    interaction=InteractionMode(mode="step_requests"),
    defaults=WorkflowDefaults(evaluate=False),
    steps=[
        StepDefinition(
            name="execute",
            type="run",
            prompt="{user_message}",
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=False),
        ),
    ],
    is_system=True,
)

GENERAL_TASK_WORKFLOW = Workflow(
    workflow_id="system:general-task",
    name="General Task",
    description="Single-step task execution with semantic evaluation.",
    criteria="Generic background tasks that need direct execution with evaluation but no specialized pipeline.",
    tags=["task", "general", "evaluated"],
    interaction=InteractionMode(mode="step_requests"),
    defaults=WorkflowDefaults(evaluate=True),
    steps=[
        StepDefinition(
            name="execute",
            type="run",
            prompt=(
                "Execute the requested task directly. Use tools as needed, keep "
                "the work focused, and verify the result before completing the step."
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
        ),
    ],
    is_system=True,
)

RESEARCH_WORKFLOW = Workflow(
    workflow_id="system:research",
    name="Research",
    description="Plan, research, synthesize with evaluation.",
    criteria="Research tasks, information gathering, analysis requests.",
    tags=["research", "analysis"],
    interaction=InteractionMode(mode="explicit_gates"),
    steps=[
        StepDefinition(
            name="plan",
            type="run",
            prompt=(
                "Create a research plan for this task. Identify:\n"
                "- Key questions to answer\n"
                "- Sources and methodology (web search, codebase, documentation)\n"
                "- Expected deliverables and format"
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=5),
        ),
        StepDefinition(
            name="research",
            type="run",
            agent_override="system:research",
            prompt=(
                "Execute the research plan. Gather information from available "
                "sources. Cross-reference findings for accuracy. Note any gaps "
                "or conflicting information."
            ),
            input=StepInputConfig(type="last", source="plan"),
            completion=CompletionConfig(evaluate=True, max_attempts=5),
        ),
        StepDefinition(
            name="synthesize",
            type="run",
            prompt=(
                "Synthesize the research findings into a coherent report with:\n"
                "- Key findings and insights\n"
                "- Areas of consensus and disagreement\n"
                "- Actionable recommendations\n"
                "- Gaps in available information"
            ),
            input=StepInputConfig(type="last", source=["plan", "research"]),
            completion=CompletionConfig(evaluate=True),
        ),
    ],
    is_system=True,
)

SOFTWARE_DEVELOPMENT_WORKFLOW = Workflow(
    workflow_id="system:software-development",
    name="Software Development",
    description="Full development pipeline: plan, architect review, implement, docs, code review, commit, remember.",
    criteria="Implementation tasks, feature development, bug fixes requiring structured quality pipeline.",
    tags=["code", "development"],
    interaction=InteractionMode(mode="explicit_gates"),
    steps=[
        StepDefinition(
            name="plan",
            type="run",
            prompt=(
                "Explore the codebase only as needed to understand the relevant "
                "areas. Use focused exploration first, and parallelize only when "
                "the task is broad enough to justify it.\n\n"
                "Then produce a detailed implementation plan covering:\n"
                "- Files to create/modify (with rationale)\n"
                "- Specific changes per file\n"
                "- Edge cases and error handling\n"
                "- Testing strategy\n"
                "- Migration or compatibility concerns"
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=5),
            # Primary agent runs this — has memory, personality, project context
        ),
        StepDefinition(
            name="architect_review",
            type="run",
            agent_override="system:architect",
            prompt=(
                "Review this implementation plan as an ARB reviewer. If the review is "
                "complete and the plan needs revision, report that via "
                "step_complete.outcome.status='rejected' with a concise reason. If the "
                "review itself could not be completed, use outcome.status='failed'."
            ),
            input=StepInputConfig(type="last", source="plan"),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            outcome_routes=[
                OutcomeRoute(
                    status="rejected",
                    action="revise(plan)",
                    max_loop_iterations=3,
                    on_exhausted="gate",
                )
            ],
        ),
        StepDefinition(
            name="implement",
            type="run",
            agent_override="system:implement",
            prompt=(
                "Implement the approved plan. Follow the plan step by step. "
                "After implementation, run relevant tests and linters to "
                "verify correctness."
            ),
            input=StepInputConfig(type="last", source=["plan", "architect_review"]),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
        ),
        StepDefinition(
            name="update_docs",
            type="run",
            prompt=(
                "Update only the documentation directly affected by the changes, "
                "such as README sections, API docs, configuration examples, or "
                "inline comments. If no documentation updates are needed, "
                "explicitly note that."
            ),
            input=StepInputConfig(type="last", source="implement"),
            completion=CompletionConfig(evaluate=False),
            # Primary agent — knows what changed
        ),
        StepDefinition(
            name="code_review",
            type="run",
            agent_override="system:code-review",
            prompt=(
                "Review all changes made during implementation. If the review is complete "
                "but fixes are required before approval, report that via "
                "step_complete.outcome.status='rejected' with a concise reason. If the "
                "review itself could not be completed, use outcome.status='failed'."
            ),
            input=StepInputConfig(
                type="last",
                source=["plan", "implement", "update_docs"],
            ),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            outcome_routes=[
                OutcomeRoute(
                    status="rejected",
                    action="revise(implement)",
                    max_loop_iterations=3,
                    on_exhausted="gate",
                )
            ],
        ),
        StepDefinition(
            name="commit",
            type="run",
            agent_override="system:committer",
            prompt=(
                "Create a conventional commit for all changes. If the commit cannot be "
                "created due to an operational problem such as missing git identity or a "
                "hook failure, report that via step_complete.outcome.status='failed' with "
                "a concise reason instead of pretending success."
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
        ),
        StepDefinition(
            name="remember",
            type="run",
            prompt=(
                "Store key findings, decisions, and implementation details "
                "as memories for future reference. Attach a detailed summary "
                "as an artifact."
            ),
            input=StepInputConfig(
                type="last",
                source=["plan", "implement", "code_review"],
            ),
            completion=CompletionConfig(evaluate=False),
            # Primary agent — has memory tools
        ),
    ],
    is_system=True,
)

CREATIVE_WORKFLOW = Workflow(
    workflow_id="system:creative",
    name="Creative",
    description="Generate content with evaluation loop.",
    criteria="Creative writing, content generation, copywriting.",
    tags=["creative", "writing"],
    interaction=InteractionMode(mode="explicit_gates"),
    steps=[
        StepDefinition(
            name="generate",
            type="run",
            prompt="Create the requested content. Focus on quality, originality, and meeting the stated requirements.",
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=5, on_exhausted="continue"),
        ),
    ],
    is_system=True,
)

SYSTEM_WORKFLOWS: dict[str, Workflow] = {
    w.workflow_id: w
    for w in [
        DIRECT_WORKFLOW,
        GENERAL_TASK_WORKFLOW,
        RESEARCH_WORKFLOW,
        SOFTWARE_DEVELOPMENT_WORKFLOW,
        CREATIVE_WORKFLOW,
    ]
}


# ---------------------------------------------------------------------------
# Workflow Registry
# ---------------------------------------------------------------------------


class WorkflowRegistry:
    """Manages system and user workflows."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, workflow_id: str) -> Workflow | None:
        """Resolve a workflow by ID — checks system workflows first, then DB."""
        if workflow_id in SYSTEM_WORKFLOWS:
            return SYSTEM_WORKFLOWS[workflow_id]

        async with self._session_factory() as db_session:
            row = await get_workflow(db_session, workflow_id)
        if row is None:
            return None
        return _row_to_workflow(row)

    async def list_all(self, *, owner_email: str | None = None) -> list[Workflow]:
        """List all available workflows (system + user)."""
        result = list(SYSTEM_WORKFLOWS.values())
        async with self._session_factory() as db_session:
            rows = await list_workflows(db_session, owner_email=owner_email, include_system=False)
        result.extend(_row_to_workflow(r) for r in rows)
        return result

    async def create(
        self,
        workflow: Workflow,
    ) -> Workflow:
        """Create a user workflow in the DB."""
        _validate_workflow(workflow)

        async with self._session_factory() as db_session:
            await create_workflow(
                db_session,
                workflow_id=workflow.workflow_id,
                name=workflow.name,
                definition=workflow.model_dump(mode="json"),
                description=workflow.description,
                version=workflow.version,
                is_system=False,
                owner_email=workflow.owner_email,
            )
            await db_session.commit()
        return workflow

    def get_direct_workflow(self) -> Workflow:
        """Return the system Direct workflow (single-step, no evaluation)."""
        return DIRECT_WORKFLOW


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_workflow(workflow: Workflow) -> None:
    """Validate workflow definition: step references, input sources, and routing targets."""
    seen_names: set[str] = set()
    step_names = [s.name for s in workflow.steps]

    for i, step in enumerate(workflow.steps):
        if step.name in seen_names:
            raise ValueError(f"Duplicate step name: {step.name!r}")
        seen_names.add(step.name)

        # Validate input source references point to earlier steps
        effective = resolve_effective_input(step, i, workflow.steps)
        for ref in effective.source_names():
            if ref not in seen_names:
                raise ValueError(f"Step {step.name!r} references unknown/later input: {ref!r}")

        # Validate on_reject.target references an earlier step
        if step.on_reject is not None:
            if step.on_reject.target not in step_names:
                raise ValueError(
                    f"Step {step.name!r} on_reject.target references unknown step: "
                    f"{step.on_reject.target!r}"
                )
            target_idx = step_names.index(step.on_reject.target)
            if target_idx >= i:
                raise ValueError(
                    f"Step {step.name!r} on_reject.target must reference an earlier "
                    f"step, but {step.on_reject.target!r} is at index {target_idx} "
                    f"(current step is at index {i})"
                )

        # Validate outcome routes point to valid actions or earlier steps.
        for route in step.outcome_routes:
            action = route.action
            if action in {"continue", "fail", "gate", "cancel"}:
                continue
            if action.startswith("revise(") and action.endswith(")"):
                target = action[7:-1]
            else:
                raise ValueError(
                    f"Step {step.name!r} has unsupported outcome route action: {action!r}"
                )
            if target not in step_names:
                raise ValueError(
                    f"Step {step.name!r} outcome route references unknown step: {target!r}"
                )
            target_idx = step_names.index(target)
            if target_idx >= i:
                raise ValueError(
                    f"Step {step.name!r} outcome route must reference an earlier step, "
                    f"but {target!r} is at index {target_idx} (current step is at index {i})"
                )

        # Validate gate steps have gate config
        if step.type == "gate" and step.gate is None:
            raise ValueError(f"Gate step {step.name!r} must have gate configuration")


def _row_to_workflow(row: Any) -> Workflow:
    """Convert a DB WorkflowRow to a Workflow domain model."""
    return Workflow.model_validate(row.definition)
