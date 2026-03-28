"""Workflow registry — system workflows and DB-backed user workflows."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.workflow import (
    CompletionConfig,
    InteractionMode,
    OnRejectConfig,
    StepDefinition,
    Workflow,
    WorkflowDefaults,
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
            completion=CompletionConfig(evaluate=False),
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
            prompt="Create a research plan for this task. Identify key questions, sources, and methodology.",
            completion=CompletionConfig(evaluate=True, max_attempts=2),
        ),
        StepDefinition(
            name="research",
            type="run",
            prompt="Execute the research plan. Gather information from available sources.",
            input=["plan"],
            completion=CompletionConfig(evaluate=True, max_attempts=2),
        ),
        StepDefinition(
            name="synthesize",
            type="run",
            prompt="Synthesize findings into a coherent report with key insights and recommendations.",
            input=["plan", "research"],
            completion=CompletionConfig(evaluate=True),
        ),
    ],
    is_system=True,
)

CODE_WITH_REVIEW_WORKFLOW = Workflow(
    workflow_id="system:code-with-review",
    name="Code with Review",
    description="Structured coding workflow with planning, implementation, testing, and review.",
    criteria="Coding tasks, implementation requests, feature development.",
    tags=["code", "development"],
    interaction=InteractionMode(mode="explicit_gates"),
    steps=[
        StepDefinition(
            name="plan",
            type="run",
            prompt="Break down this task into implementation steps. Include success criteria, test strategy, and documentation plan.",
            completion=CompletionConfig(evaluate=True, max_attempts=2),
        ),
        StepDefinition(
            name="architect_review",
            type="run",
            prompt="Review this plan critically. Check for missing edge cases, security concerns, and architectural issues.",
            input=["plan"],
            completion=CompletionConfig(evaluate=True),
            on_reject=OnRejectConfig(target="plan", max_loop_iterations=2, on_exhausted="gate"),
        ),
        StepDefinition(
            name="implement",
            type="run",
            prompt="Implement the plan with tests and documentation.",
            input=["plan", "architect_review"],
            completion=CompletionConfig(evaluate=True, max_attempts=3),
        ),
        StepDefinition(
            name="run_tests",
            type="run",
            prompt="Run the test suite and fix any failures.",
            input=["implement"],
            completion=CompletionConfig(evaluate=True, max_attempts=2),
            on_reject=OnRejectConfig(
                target="implement",
                max_loop_iterations=2,
                on_exhausted="continue",
            ),
        ),
        StepDefinition(
            name="code_review",
            type="run",
            prompt="Review code quality, test coverage, documentation.",
            input=["plan", "implement", "run_tests"],
            completion=CompletionConfig(evaluate=True),
            on_reject=OnRejectConfig(
                target="implement",
                max_loop_iterations=2,
                on_exhausted="continue",
            ),
        ),
        StepDefinition(
            name="commit",
            type="run",
            prompt="Create a conventional commit with a clear message.",
            input=["implement"],
            completion=CompletionConfig(evaluate=False),
        ),
        StepDefinition(
            name="update_memory",
            type="run",
            prompt="Store key findings and decisions as memories for future reference.",
            input=["plan", "implement", "code_review"],
            completion=CompletionConfig(evaluate=False),
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
            completion=CompletionConfig(evaluate=True, max_attempts=5, on_exhausted="continue"),
        ),
    ],
    is_system=True,
)

SYSTEM_WORKFLOWS: dict[str, Workflow] = {
    w.workflow_id: w
    for w in [DIRECT_WORKFLOW, RESEARCH_WORKFLOW, CODE_WITH_REVIEW_WORKFLOW, CREATIVE_WORKFLOW]
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
    """Validate workflow definition: step references, on_reject targets."""
    step_names = [s.name for s in workflow.steps]
    seen_names: set[str] = set()

    for i, step in enumerate(workflow.steps):
        if step.name in seen_names:
            raise ValueError(f"Duplicate step name: {step.name!r}")
        seen_names.add(step.name)

        # Validate input references
        for ref in step.input:
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

        # Validate gate steps have gate config
        if step.type == "gate" and step.gate is None:
            raise ValueError(f"Gate step {step.name!r} must have gate configuration")


def _row_to_workflow(row: Any) -> Workflow:
    """Convert a DB WorkflowRow to a Workflow domain model."""
    return Workflow.model_validate(row.definition)
