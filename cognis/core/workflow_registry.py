"""Workflow registry — system workflows and DB-backed user workflows."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.gate_conditions import validate_gate_conditions
from cognis.logging import get_logger
from cognis.models.workflow import (
    CompletionConfig,
    GateConfig,
    GateOption,
    InteractionMode,
    OutcomeRoute,
    StepCompletionContract,
    StepCompletionMetadataField,
    StepDefinition,
    StepInputConfig,
    StepProfileConfig,
    StepProfileMode,
    Workflow,
    WorkflowDefaults,
    resolve_source_names,
)
from cognis.providers.llm.reasoning import normalize_reasoning_effort
from cognis.store.queries import (
    create_workflow,
    get_system_workflow_override,
    get_workflow,
    list_bound_workflow_ids,
    list_project_workflow_ids,
    list_workflows,
)

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
    allow_user_disable=False,
    steps=[
        StepDefinition(
            name="execute",
            type="run",
            prompt="{user_message}",
            reasoning_effort="default",
            step_profile_id="system:direct-default",
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=False),
            require_deliverable=False,
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
    allow_user_override=True,
    allow_user_disable=True,
    editable_fields=[
        "steps.*.reasoning_effort",
        "steps.*.completion.max_attempts",
        "steps.*.step_profile_id",
        "steps.*.step_profile_mode",
        "steps.*.step_profile",
    ],
    steps=[
        StepDefinition(
            name="execute",
            type="run",
            prompt=(
                "Execute the requested task directly. Inspect the relevant context "
                "first, keep the work focused, and verify the result before "
                "completing the step. For coding work, prefer the smallest correct "
                "change, preserve existing patterns, and update directly affected "
                "docs only when needed. Write a deliverable that captures the final "
                "result, not just the work you attempted."
            ),
            reasoning_effort="low",
            step_profile_id="system:general-task",
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
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
    allow_user_override=True,
    allow_user_disable=True,
    editable_fields=[
        "steps.*.reasoning_effort",
        "steps.*.completion.max_attempts",
        "steps.*.step_profile_id",
        "steps.*.step_profile_mode",
        "steps.*.step_profile",
    ],
    steps=[
        StepDefinition(
            name="plan",
            type="run",
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt=(
                "Create a research plan for this task. Identify:\n"
                "- Key questions to answer\n"
                "- Sources and methodology (web search, codebase, documentation)\n"
                "- Expected deliverables and format\n\n"
                "Write the plan itself as the step deliverable."
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(name="confidence", type="number", required=True),
                    StepCompletionMetadataField(
                        name="risk", type="string", required=True, enum=["low", "medium", "high"]
                    ),
                    StepCompletionMetadataField(
                        name="source_strategy", type="array", required=False
                    ),
                    StepCompletionMetadataField(name="open_questions", type="array", required=True),
                ]
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=5),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="pre_research_gate",
            type="gate",
            gate=GateConfig(
                message="Research plan confidence is low or risk is high. Approve to continue.",
                input=["plan"],
                options=[
                    GateOption(label="Continue", action="continue"),
                    GateOption(label="Revise plan", action="revise(plan)"),
                    GateOption(label="Cancel", action="cancel"),
                ],
                thresholds={"min_confidence": 0.6},
                conditions=[
                    {
                        "expression": "metadata.plan.confidence < thresholds.min_confidence or metadata.plan.risk == 'high'"
                    }
                ],
            ),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
        ),
        StepDefinition(
            name="research",
            type="run",
            agent_override="system:research",
            reasoning_effort="low",
            step_profile_id="system:research",
            prompt=(
                "Execute the research plan. Gather information from available "
                "sources. Cross-reference findings for accuracy. Note any gaps "
                "or conflicting information. Write a deliverable that preserves "
                "the gathered evidence and conclusions."
            ),
            input=StepInputConfig(type="last", source="plan"),
            completion=CompletionConfig(evaluate=True, max_attempts=5),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="synthesize",
            type="run",
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt=(
                "Synthesize the research findings into a coherent report with:\n"
                "- Key findings and insights\n"
                "- Areas of consensus and disagreement\n"
                "- Actionable recommendations\n"
                "- Gaps in available information"
            ),
            input=StepInputConfig(type="last", source=["plan", "research"]),
            completion=CompletionConfig(evaluate=True),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
    ],
    is_system=True,
)

SOFTWARE_DEVELOPMENT_WORKFLOW = Workflow(
    workflow_id="system:software-development",
    name="Software Development",
    description="Full development pipeline: plan, architect review, implement, docs, code review, commit, remember, final summary.",
    criteria="Implementation tasks, feature development, bug fixes requiring structured quality pipeline.",
    tags=["code", "development"],
    interaction=InteractionMode(mode="explicit_gates"),
    allow_user_override=True,
    allow_user_disable=True,
    editable_fields=[
        "steps.*.reasoning_effort",
        "steps.*.completion.max_attempts",
        "steps.*.step_profile_id",
        "steps.*.step_profile_mode",
        "steps.*.step_profile",
    ],
    steps=[
        StepDefinition(
            name="plan",
            type="run",
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt=(
                "Explore the codebase only as needed to understand the relevant "
                "areas. Use focused exploration first, and parallelize only when "
                "the task is broad enough to justify it.\n\n"
                "Then produce a detailed implementation plan covering:\n"
                "- Files to create/modify (with rationale)\n"
                "- Specific changes per file\n"
                "- Edge cases and error handling\n"
                "- Testing strategy\n"
                "- Migration or compatibility concerns\n\n"
                "Write the plan itself as the step deliverable."
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(name="confidence", type="number", required=True),
                    StepCompletionMetadataField(
                        name="risk", type="string", required=True, enum=["low", "medium", "high"]
                    ),
                    StepCompletionMetadataField(name="decisions", type="array", required=True),
                    StepCompletionMetadataField(name="open_questions", type="array", required=True),
                ]
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(
                evaluate=True,
                max_attempts=5,
            ),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
            # Primary agent runs this — has memory, personality, project context
        ),
        StepDefinition(
            name="architect_review",
            type="run",
            agent_override="system:architect",
            reasoning_effort="medium",
            step_profile_id="system:review",
            prompt=(
                "Review this implementation plan as a proportional architecture and "
                "risk check. Focus on missing security, reliability, testability, "
                "data, dependency, and failure-mode considerations. Catch important "
                "omissions and overengineering, but do not block on nitpicks. If the "
                "plan is sound and ready, complete the step normally with success. If the "
                "review is complete and the plan needs revision, report that via "
                "step_complete.outcome.status='rejected' with a concise reason. If the "
                "review itself could not be completed, use outcome.status='failed'. Put "
                "the outcome only in step_complete, not as a trailing JSON object in the "
                "written review. The deliverable should be the actual review output."
            ),
            input=StepInputConfig(type="full", source="plan"),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            outcome_routes=[
                OutcomeRoute(
                    status="rejected",
                    action="revise(plan)",
                    max_loop_iterations=3,
                    on_exhausted="gate",
                ),
                OutcomeRoute(status="failed", action="gate"),
            ],
            require_deliverable=True,
        ),
        StepDefinition(
            name="pre_implement_gate",
            type="gate",
            gate=GateConfig(
                message="Implementation plan confidence is low or risk is high. Approve to continue.",
                input=["plan", "architect_review"],
                options=[
                    GateOption(label="Continue", action="continue"),
                    GateOption(label="Revise plan", action="revise(plan)"),
                    GateOption(label="Cancel", action="cancel"),
                ],
                thresholds={"min_confidence": 0.6},
                conditions=[
                    {
                        "expression": "metadata.plan.confidence < thresholds.min_confidence or metadata.plan.risk == 'high'"
                    }
                ],
            ),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
        ),
        StepDefinition(
            name="implement",
            type="run",
            agent_override="system:implement",
            reasoning_effort="medium",
            step_profile_id="system:coding",
            prompt=(
                "Implement the approved plan. Follow the plan step by step. "
                "After implementation, run relevant tests and linters to "
                "verify correctness. The deliverable should summarize the concrete "
                "changes made and the validation that was run."
            ),
            input=StepInputConfig(type="summary", source=["plan", "architect_review"]),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="update_docs",
            type="run",
            agent_override="system:implement",
            reasoning_effort="low",
            step_profile_id="system:coding",
            prompt=(
                "Update only the documentation directly affected by the changes, "
                "such as README sections, guides, specs, API docs, configuration "
                "examples, migration notes, or inline comments. If no documentation "
                "updates are needed, explicitly say so instead of forcing changes. "
                "The deliverable should state what documentation changed or why none was needed."
            ),
            input=StepInputConfig(type="summary", source="implement"),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="code_review",
            type="run",
            agent_override="system:code-review",
            reasoning_effort="medium",
            step_profile_id="system:review",
            prompt=(
                "Review all changes made during implementation. If the review is complete "
                "and the changes are acceptable, complete the step normally with success. "
                "If the review is complete but fixes are required before approval, report that via "
                "step_complete.outcome.status='rejected' with a concise reason. If the "
                "review itself could not be completed, use outcome.status='failed'. Put "
                "the outcome only in step_complete, not as a trailing JSON object in the "
                "written review. The deliverable should contain the actual review findings."
            ),
            input=StepInputConfig(
                type="summary",
                source=["plan", "implement", "update_docs"],
            ),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            outcome_routes=[
                OutcomeRoute(
                    status="rejected",
                    action="revise(implement)",
                    max_loop_iterations=3,
                    on_exhausted="gate",
                ),
                OutcomeRoute(status="failed", action="gate"),
            ],
            require_deliverable=True,
        ),
        StepDefinition(
            name="commit",
            type="run",
            agent_override="system:committer",
            reasoning_effort="low",
            step_profile_id="system:coding",
            prompt=(
                "Create a conventional commit for all changes. If the commit cannot be "
                "created due to an operational problem such as missing git identity or a "
                "hook failure, report that via step_complete.outcome.status='failed' with "
                "a concise reason instead of pretending success. Write a short deliverable "
                "summarizing the commit result and commit message."
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="remember",
            type="run",
            reasoning_effort="low",
            step_profile_id="system:direct-default",
            prompt=(
                "Store key findings, decisions, and implementation details "
                "as memories for future reference. Attach a detailed summary "
                "as an artifact. Write a deliverable summarizing what was remembered."
            ),
            input=StepInputConfig(
                type="last",
                source=["plan", "implement", "code_review"],
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
            # Primary agent — has memory tools
        ),
        StepDefinition(
            name="final_summary",
            type="run",
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt=(
                "Produce the final user-facing implementation report for this workflow. "
                "Synthesize the approved plan, implementation summary, documentation status, "
                "code review findings, commit result, and memory summary into one polished "
                "deliverable. Focus on: what changed, what was verified, any remaining risks, "
                "and any important follow-up notes."
            ),
            input=StepInputConfig(
                type="summary",
                source=[
                    "plan",
                    "architect_review",
                    "implement",
                    "update_docs",
                    "code_review",
                    "commit",
                    "remember",
                ],
            ),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
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
    allow_user_override=True,
    allow_user_disable=True,
    editable_fields=[
        "steps.*.reasoning_effort",
        "steps.*.completion.max_attempts",
        "steps.*.step_profile_id",
        "steps.*.step_profile_mode",
        "steps.*.step_profile",
    ],
    steps=[
        StepDefinition(
            name="generate",
            type="run",
            reasoning_effort="low",
            step_profile_id="system:general-task",
            prompt=(
                "Create the requested content. Focus on quality, originality, and meeting the stated requirements. "
                "Write the content itself as the deliverable for this step."
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=5, on_exhausted="continue"),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
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

    async def get(
        self,
        workflow_id: str,
        *,
        owner_email: str | None = None,
        include_disabled: bool = False,
        project_id: str | None = None,
    ) -> Workflow | None:
        """Resolve a workflow by ID — checks system workflows first, then DB."""
        if workflow_id in SYSTEM_WORKFLOWS:
            if not await self._workflow_eligible_for_project(workflow_id, project_id):
                return None
            return await self._resolve_system_workflow(
                SYSTEM_WORKFLOWS[workflow_id],
                owner_email=owner_email,
                include_disabled=include_disabled,
            )

        async with self._session_factory() as db_session:
            row = await get_workflow(db_session, workflow_id)
        if row is None:
            return None
        if not await self._workflow_eligible_for_project(workflow_id, project_id):
            return None
        return _row_to_workflow(row)

    async def list_all(
        self,
        *,
        owner_email: str | None = None,
        include_disabled: bool = False,
        include_ephemeral: bool = False,
        project_id: str | None = None,
    ) -> list[Workflow]:
        """List all available workflows (system + user)."""
        result: list[Workflow] = []
        for workflow in SYSTEM_WORKFLOWS.values():
            effective = await self._resolve_system_workflow(
                workflow, owner_email=owner_email, include_disabled=include_disabled
            )
            if effective is not None:
                result.append(effective)
        async with self._session_factory() as db_session:
            rows = await list_workflows(
                db_session,
                owner_email=owner_email,
                include_system=False,
                include_ephemeral=include_ephemeral,
            )
            bound_ids = await list_bound_workflow_ids(db_session)
            project_ids = (
                await list_project_workflow_ids(db_session, project_id) if project_id else []
            )
        project_id_set = set(project_ids)
        result = [
            workflow
            for workflow in result
            if workflow.workflow_id not in bound_ids
            or (project_id is not None and workflow.workflow_id in project_id_set)
        ]
        rows = [
            row
            for row in rows
            if row.workflow_id not in bound_ids
            or (project_id is not None and row.workflow_id in project_id_set)
        ]
        result.extend(_row_to_workflow(r) for r in rows)
        return result

    async def _workflow_eligible_for_project(
        self,
        workflow_id: str,
        project_id: str | None,
    ) -> bool:
        async with self._session_factory() as db_session:
            bound_ids = await list_bound_workflow_ids(db_session)
            if workflow_id not in bound_ids:
                return True
            if project_id is None:
                return False
            project_ids = await list_project_workflow_ids(db_session, project_id)
        return workflow_id in set(project_ids)

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
                lifecycle=str(workflow.lifecycle),
                archived_at=workflow.archived_at,
            )
            await db_session.commit()
        return workflow

    def get_direct_workflow(self) -> Workflow:
        """Return the system Direct workflow (single-step, no evaluation)."""
        return DIRECT_WORKFLOW

    def get_system_workflow(self, workflow_id: str) -> Workflow | None:
        """Return the raw shipped system workflow."""

        return SYSTEM_WORKFLOWS.get(workflow_id)

    async def get_effective(self, workflow_id: str, *, owner_email: str | None) -> Workflow | None:
        """Resolve a workflow with user-scoped system overrides applied."""

        return await self.get(workflow_id, owner_email=owner_email)

    async def _resolve_system_workflow(
        self,
        base: Workflow,
        *,
        owner_email: str | None,
        include_disabled: bool,
    ) -> Workflow | None:
        effective = base.model_copy(deep=True)
        if not owner_email or not base.allow_user_override:
            return effective
        async with self._session_factory() as db_session:
            row = await get_system_workflow_override(
                db_session, owner_email=owner_email, workflow_id=base.workflow_id
            )
        if row is None:
            return effective
        effective.has_overrides = True
        effective.disabled = bool(row.disabled)
        if row.disabled and not include_disabled:
            return None
        raw_step_overrides = row.step_overrides if isinstance(row.step_overrides, dict) else {}
        steps_by_name = {step.name: step for step in effective.steps}
        warnings: list[str] = []
        for step_name, raw_override in raw_step_overrides.items():
            if not isinstance(raw_override, dict):
                continue
            step = steps_by_name.get(step_name)
            if step is None:
                warnings.append(f"Override for missing step '{step_name}' is ignored.")
                continue
            reasoning_effort = raw_override.get("reasoning_effort")
            if isinstance(reasoning_effort, str):
                normalized_effort = normalize_reasoning_effort(reasoning_effort)
                if normalized_effort is None:
                    warnings.append(
                        f"Override for step '{step_name}' has unsupported reasoning_effort "
                        f"{reasoning_effort!r}; override is ignored."
                    )
                    normalized_effort = None
                if normalized_effort is not None:
                    step.reasoning_effort = normalized_effort
            step_profile_id = raw_override.get("step_profile_id")
            if isinstance(step_profile_id, str):
                step.step_profile_id = step_profile_id or None
            step_profile_mode = raw_override.get("step_profile_mode")
            if isinstance(step_profile_mode, str) and step_profile_mode:
                step.step_profile_mode = StepProfileMode(step_profile_mode)
            step_profile = raw_override.get("step_profile")
            if isinstance(step_profile, dict):
                step.step_profile = StepProfileConfig.model_validate(step_profile)
            completion_override = raw_override.get("completion")
            if isinstance(completion_override, dict):
                if step.completion is None:
                    step.completion = CompletionConfig()
                max_attempts = completion_override.get("max_attempts")
                if isinstance(max_attempts, int):
                    step.completion.max_attempts = max_attempts
        effective.override_warnings = warnings
        return effective


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
        for ref in resolve_source_names(step, i, workflow.steps):
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

    validate_gate_conditions(workflow)


def _row_to_workflow(row: Any) -> Workflow:
    """Convert a DB WorkflowRow to a Workflow domain model."""
    return Workflow.model_validate(row.definition)
