"""Workflow registry — system workflows and DB-backed user workflows."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.gate_conditions import validate_gate_conditions
from cognis.logging import get_logger
from cognis.models.workflow import (
    CompletionConfig,
    ConditionStepConfig,
    DeterministicOutputConfig,
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
    WorkflowPhaseDefinition,
    WorkflowPresentation,
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
    presentation=WorkflowPresentation(
        phases=[WorkflowPhaseDefinition(id="execute", title="Execute", step_names=["execute"])]
    ),
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
    presentation=WorkflowPresentation(
        phases=[WorkflowPhaseDefinition(id="execute", title="Execute", step_names=["execute"])]
    ),
    description="Single-session task execution with deterministic completion requirements.",
    criteria="Generic background tasks that need direct execution but no specialized pipeline.",
    tags=["task", "general"],
    interaction=InteractionMode(mode="step_requests"),
    defaults=WorkflowDefaults(evaluate=False),
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
            objective="Complete the requested task and return the verified result.",
            responsibilities=[
                "Inspect only the context needed for the task.",
                "Perform the requested work.",
                "Verify the result.",
                "Produce the canonical final task deliverable.",
            ],
            prompt="Use one targeted question only when a material ambiguity has no safe default.",
            allow_questions=True,
            reasoning_effort="low",
            step_profile_id="system:general-task",
            input=StepInputConfig(type="null"),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="result_status",
                        type="string",
                        required=True,
                        enum=["completed", "blocked"],
                    ),
                    StepCompletionMetadataField(
                        name="verification",
                        type="object",
                        required=True,
                    ),
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
    ],
    is_system=True,
)

RESEARCH_WORKFLOW = Workflow(
    workflow_id="system:research",
    name="Research",
    presentation=WorkflowPresentation(
        phases=[
            WorkflowPhaseDefinition(
                id="plan",
                title="Plan",
                step_names=["plan", "pre_research_gate"],
            ),
            WorkflowPhaseDefinition(id="investigate", title="Investigate", step_names=["research"]),
            WorkflowPhaseDefinition(id="deliver", title="Deliver", step_names=["synthesize"]),
        ]
    ),
    description="One primary-agent research session with deterministic phase contracts.",
    criteria="Research tasks, investigation, incident analysis, audits, information gathering, and synthesis reports.",
    tags=["research", "analysis", "investigation"],
    interaction=InteractionMode(mode="step_requests"),
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
            objective="Create an evidence-based research plan for the requested task.",
            responsibilities=[
                "Define the questions, source strategy, research depth, and output format.",
                "Resolve only ambiguities that materially affect the research plan.",
                "Produce the research plan deliverable.",
            ],
            defer_to=["research", "synthesize"],
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt=(
                "Plan questions, depth, source selection, freshness checks, contradiction checks, "
                "and the final output shape. Do not collect evidence or write the final narrative."
            ),
            allow_questions=True,
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(name="confidence", type="number", required=True),
                    StepCompletionMetadataField(
                        name="risk", type="string", required=True, enum=["low", "medium", "high"]
                    ),
                    StepCompletionMetadataField(
                        name="source_strategy", type="array", required=False
                    ),
                    StepCompletionMetadataField(
                        name="research_depth", type="string", required=True
                    ),
                    StepCompletionMetadataField(
                        name="media_strategy", type="string", required=False
                    ),
                    StepCompletionMetadataField(name="open_questions", type="array", required=True),
                ]
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=False),
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
            objective="Execute the approved research plan and preserve auditable evidence.",
            responsibilities=[
                "Gather and cross-check evidence at the planned depth.",
                "Use managed primary-agent workstreams for substantial independent research when useful.",
                "Record sources, dates, confidence, gaps, and artifact references.",
                "Produce the research evidence deliverable.",
            ],
            defer_to=["synthesize"],
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt=(
                "Execute the approved source strategy. Prefer primary sources, preserve provenance, "
                "and separate evidence from inference. Do not write the final user narrative."
            ),
            input=StepInputConfig(type="last", source="plan", reuse_session_from="plan"),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(name="sources", type="array", required=True),
                    StepCompletionMetadataField(name="evidence", type="array", required=True),
                    StepCompletionMetadataField(name="gaps", type="array", required=True),
                    StepCompletionMetadataField(name="confidence", type="number", required=True),
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="synthesize",
            type="run",
            objective="Synthesize the gathered evidence into the final research report.",
            responsibilities=[
                "Explain findings, disagreements, recommendations, gaps, and confidence.",
                "Cite the supplied evidence and include useful artifact references.",
                "Produce the final user-facing research deliverable.",
            ],
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt="Synthesize only from the collected evidence. Make gaps and confidence explicit.",
            input=StepInputConfig(
                type="last",
                source=["plan", "research"],
                reuse_session_from="research",
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(name="source_count", type="number", required=True),
                    StepCompletionMetadataField(name="confidence", type="number", required=True),
                    StepCompletionMetadataField(name="open_gaps", type="array", required=True),
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
    ],
    is_system=True,
)

SOFTWARE_DEVELOPMENT_WORKFLOW = Workflow(
    workflow_id="system:software-development",
    name="Software Development",
    presentation=WorkflowPresentation(
        phases=[
            WorkflowPhaseDefinition(
                id="plan",
                title="Plan",
                step_names=[
                    "plan",
                    "architect_review",
                    "architect_review_route",
                    "pre_implement_gate",
                ],
            ),
            WorkflowPhaseDefinition(
                id="build", title="Build", step_names=["implement", "update_docs"]
            ),
            WorkflowPhaseDefinition(
                id="verify",
                title="Verify",
                step_names=["code_review", "code_review_route", "post_review_gate"],
            ),
            WorkflowPhaseDefinition(
                id="deliver",
                title="Deliver",
                step_names=["commit", "remember", "final_summary"],
            ),
        ]
    ),
    description="Full development pipeline: plan, architect review, implement, docs, code review, commit, remember, final summary.",
    criteria="Implementation tasks, feature development, bug fixes requiring structured quality pipeline.",
    tags=["code", "development"],
    interaction=InteractionMode(mode="step_requests"),
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
            objective="Produce an implementation brief that later workflow steps can execute.",
            responsibilities=[
                "Inspect the codebase only enough to define an implementable scope contract.",
                "Define acceptance criteria, validation, workspace, and lifecycle strategy.",
                "Produce the implementation-plan deliverable without changing the repository.",
            ],
            defer_to=[
                "architect_review",
                "implement",
                "update_docs",
                "code_review",
                "commit",
                "remember",
                "final_summary",
            ],
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt=(
                "Keep this step read-only. Define a proportional scope contract, acceptance "
                "criteria, validation plan, workspace strategy, assumptions, and lifecycle plan. "
                "Use one targeted question only when a material ambiguity has no safe default."
            ),
            allow_questions=True,
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(name="confidence", type="number", required=True),
                    StepCompletionMetadataField(
                        name="risk", type="string", required=True, enum=["low", "medium", "high"]
                    ),
                    StepCompletionMetadataField(name="decisions", type="array", required=True),
                    StepCompletionMetadataField(
                        name="lifecycle_strategy", type="object", required=True
                    ),
                    StepCompletionMetadataField(
                        name="implementation_intent",
                        type="object",
                        required=True,
                        description="What is being implemented and why it is needed.",
                    ),
                    StepCompletionMetadataField(
                        name="scope_contract",
                        type="array",
                        required=True,
                        description=(
                            "Array of objects. Proportional required/not-applicable "
                            "scope items with ids, areas, descriptions, and "
                            "acceptance/evidence guidance."
                        ),
                    ),
                    StepCompletionMetadataField(
                        name="acceptance_criteria", type="array", required=True
                    ),
                    StepCompletionMetadataField(
                        name="validation_plan", type="array", required=True
                    ),
                    StepCompletionMetadataField(name="open_questions", type="array", required=True),
                ]
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
            # Primary agent runs this — has memory, personality, project context
        ),
        StepDefinition(
            name="architect_review",
            type="run",
            objective="Review the implementation brief for material architecture and delivery risks.",
            responsibilities=[
                "Assess the plan against material correctness and delivery risks.",
                "Approve it or report concise required revisions.",
                "Produce the architecture-review deliverable without implementing changes.",
            ],
            defer_to=[
                "implement",
                "update_docs",
                "code_review",
                "commit",
                "remember",
                "final_summary",
            ],
            agent_override="system:architect",
            reasoning_effort="medium",
            step_profile_id="system:review",
            prompt=(
                "Review material architecture, security, persistence, compatibility, and delivery "
                "risks. Return decision=approved or decision=revise. Do not implement changes."
            ),
            input=StepInputConfig(type="full", source="plan"),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="decision",
                        type="string",
                        required=True,
                        enum=["approved", "revise"],
                    ),
                    StepCompletionMetadataField(
                        name="must_fix_count", type="number", required=True
                    ),
                ]
            ),
            completion=CompletionConfig(evaluate=False, max_attempts=3),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="architect_review_route",
            type="condition",
            condition=ConditionStepConfig(
                if_=(
                    "steps.architect_review.metadata.decision == 'revise' or "
                    "steps.architect_review.metadata.must_fix_count > 0"
                ),
                then="plan",
                else_="pre_implement_gate",
                revision_source="architect_review",
                max_loop_iterations=5,
                on_exhausted="gate",
                output=DeterministicOutputConfig(
                    summary="Routed the architecture review decision.",
                ),
            ),
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
            objective="Implement and verify the approved scope contract.",
            responsibilities=[
                "Make the smallest correct code and test changes required by the approved scope.",
                "Run the focused validation owned by implementation and fix caused failures.",
                "Report scope completion, validation evidence, blockers, and residual risk.",
            ],
            defer_to=["update_docs", "code_review", "commit", "remember", "final_summary"],
            reasoning_effort="medium",
            step_profile_id="system:coding",
            prompt=(
                "Execute the approved scope contract. Own code, tests, integration, focused "
                "validation, and managed implementation workstreams. Preserve valid prior work "
                "during revisions. Report blockers instead of silently reducing scope."
            ),
            input=StepInputConfig(
                type="last",
                source=["plan", "architect_review"],
                reuse_session_from="plan",
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="scope_status",
                        type="array",
                        required=True,
                        description=(
                            "Array of objects. Status for every required plan "
                            "scope_contract item, with evidence or blocker details."
                        ),
                    ),
                    StepCompletionMetadataField(
                        name="incomplete_required_scope_count",
                        type="number",
                        required=True,
                    ),
                    StepCompletionMetadataField(
                        name="validation_summary", type="object", required=True
                    ),
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="update_docs",
            type="run",
            objective="Update only documentation directly affected by the implementation.",
            responsibilities=[
                "Change directly affected documentation or explain why no change is needed.",
                "Produce the documentation-status deliverable.",
            ],
            defer_to=["code_review", "commit", "remember", "final_summary"],
            reasoning_effort="low",
            step_profile_id="system:coding",
            prompt=(
                "Verify the documentation boundary. Update only directly affected documentation, "
                "or report a no-op or an implementation boundary violation."
            ),
            input=StepInputConfig(
                type="last",
                source="implement",
                reuse_session_from="implement",
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="docs_status",
                        type="string",
                        required=True,
                        enum=["updated", "no_change", "boundary_violation"],
                    ),
                    StepCompletionMetadataField(name="changed_files", type="array", required=True),
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="code_review",
            type="run",
            objective="Review the implementation against the approved scope and validation plan.",
            responsibilities=[
                "Check required scope completion before code-quality findings.",
                "Report must-fix and should-fix findings with evidence.",
                "Approve or reject the implementation without applying fixes.",
            ],
            defer_to=["commit", "remember", "final_summary"],
            agent_override="system:code-review",
            reasoning_effort="medium",
            step_profile_id="system:review",
            prompt=(
                "Review the current diff against scope and validation. Verify prior fixes on "
                "repeated review. Return decision=approved or decision=revise. Do not apply fixes."
            ),
            input=StepInputConfig(
                type="last",
                source=["plan", "implement", "update_docs"],
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="decision",
                        type="string",
                        required=True,
                        enum=["approved", "revise"],
                    ),
                    StepCompletionMetadataField(
                        name="required_scope_complete", type="boolean", required=True
                    ),
                    StepCompletionMetadataField(
                        name="missing_scope_count", type="number", required=True
                    ),
                    StepCompletionMetadataField(
                        name="must_fix_count", type="number", required=True
                    ),
                    StepCompletionMetadataField(
                        name="should_fix_count", type="number", required=True
                    ),
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="code_review_route",
            type="condition",
            condition=ConditionStepConfig(
                if_=(
                    "steps.code_review.metadata.decision == 'revise' or "
                    "not steps.code_review.metadata.required_scope_complete or "
                    "steps.code_review.metadata.missing_scope_count > 0 or "
                    "steps.code_review.metadata.must_fix_count > 0"
                ),
                then="implement",
                else_="post_review_gate",
                revision_source="code_review",
                max_loop_iterations=5,
                on_exhausted="gate",
                output=DeterministicOutputConfig(
                    summary="Routed the code review decision.",
                ),
            ),
        ),
        StepDefinition(
            name="post_review_gate",
            type="gate",
            gate=GateConfig(
                message=(
                    "Code review reported unresolved scope or review findings. "
                    "Revise implementation, continue explicitly, or cancel."
                ),
                input=["plan", "implement", "code_review"],
                options=[
                    GateOption(label="Revise implementation", action="revise(implement)"),
                    GateOption(label="Continue explicitly", action="continue"),
                    GateOption(label="Cancel", action="cancel"),
                ],
                conditions=[
                    {
                        "expression": (
                            "metadata.code_review.decision != 'approved' or "
                            "metadata.code_review.required_scope_complete == false or "
                            "metadata.code_review.missing_scope_count > 0 or "
                            "metadata.code_review.must_fix_count > 0 or "
                            "metadata.code_review.should_fix_count > 0"
                        )
                    }
                ],
            ),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
        ),
        StepDefinition(
            name="commit",
            type="run",
            objective="Create the authorized conventional commit and report publication status.",
            responsibilities=[
                "Commit only the approved task changes.",
                "Publish or open a pull request only when explicitly authorized.",
                "Produce the commit-result deliverable.",
            ],
            defer_to=["remember", "final_summary"],
            reasoning_effort="low",
            step_profile_id="system:coding",
            prompt=(
                "Commit only approved task-owned changes with the repository convention. "
                "Do not push, publish, or open a pull request unless explicitly authorized."
            ),
            input=StepInputConfig(
                type="last",
                source=["update_docs", "code_review"],
                reuse_session_from="update_docs",
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="commit_status",
                        type="string",
                        required=True,
                        enum=["committed", "no_changes"],
                    ),
                    StepCompletionMetadataField(name="commit", type="object", required=True),
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
        StepDefinition(
            name="remember",
            type="run",
            objective="Store concise reusable memory for the completed implementation.",
            responsibilities=[
                "Store durable implementation, validation, commit, and decision context.",
                "Exclude secrets, transient progress, and excessive detail.",
                "Produce the memory-status deliverable.",
            ],
            defer_to=["final_summary"],
            reasoning_effort="low",
            step_profile_id="system:direct-default",
            prompt="Store only concise reusable decisions and implementation facts. Exclude secrets and transient progress.",
            input=StepInputConfig(
                type="last",
                source=["plan", "implement", "code_review", "commit"],
                reuse_session_from="commit",
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="memory_status",
                        type="string",
                        required=True,
                        enum=["stored", "no_durable_memory"],
                    )
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
            # Primary agent — has memory tools
        ),
        StepDefinition(
            name="final_summary",
            type="run",
            objective="Produce the final evidence-backed implementation report.",
            responsibilities=[
                "Synthesize the approved plan and all completed downstream results.",
                "Report changes, validation, commit status, risks, and follow-ups accurately.",
                "Produce the final user-facing workflow deliverable.",
            ],
            reasoning_effort="medium",
            step_profile_id="system:research",
            prompt="Deliver only the evidence-backed result. Do not perform more implementation work.",
            input=StepInputConfig(
                type="last",
                source=[
                    "plan",
                    "architect_review",
                    "implement",
                    "update_docs",
                    "code_review",
                    "commit",
                    "remember",
                ],
                reuse_session_from="remember",
            ),
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="delivery_status",
                        type="string",
                        required=True,
                        enum=["complete", "incomplete"],
                    )
                ]
            ),
            completion=CompletionConfig(evaluate=False),
            outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            require_deliverable=True,
        ),
    ],
    is_system=True,
)

CREATIVE_WORKFLOW = Workflow(
    workflow_id="system:creative",
    name="Creative",
    presentation=WorkflowPresentation(
        phases=[WorkflowPhaseDefinition(id="create", title="Create", step_names=["generate"])]
    ),
    description="Generate content with evaluation loop.",
    criteria="Creative writing, content generation, copywriting.",
    tags=["creative", "writing"],
    interaction=InteractionMode(mode="step_requests"),
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
                "If audience, tone, format, constraints, or direction are ambiguous and the answer would materially affect the content, ask a small targeted question set with step_request_questions first. Do not ask when a safe default is obvious or the task explicitly requests fully autonomous execution. "
                "Write the content itself as the deliverable for this step."
            ),
            allow_questions=True,
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
                definition=workflow.model_dump(mode="json", exclude_none=True),
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
        resolved_sources = resolve_source_names(step, i, workflow.steps)
        for ref in resolved_sources:
            if ref not in seen_names:
                raise ValueError(f"Step {step.name!r} references unknown/later input: {ref!r}")

        reuse_source = step.input.reuse_session_from if step.input is not None else None
        if reuse_source is not None:
            if step.type != "run":
                raise ValueError(
                    f"Step {step.name!r} can reuse a session only for an executable run step"
                )
            if reuse_source == step.name:
                raise ValueError(f"Step {step.name!r} cannot reuse its own session")
            if reuse_source not in step_names:
                raise ValueError(
                    f"Step {step.name!r} reuses unknown step session: {reuse_source!r}"
                )
            source_index = step_names.index(reuse_source)
            if source_index >= i:
                raise ValueError(
                    f"Step {step.name!r} reuse_session_from must reference an earlier step: "
                    f"{reuse_source!r}"
                )
            source_step = workflow.steps[source_index]
            if source_step.type != "run":
                raise ValueError(
                    f"Step {step.name!r} cannot reuse non-run step session: {reuse_source!r}"
                )
            if reuse_source not in resolved_sources:
                raise ValueError(
                    f"Step {step.name!r} reuse_session_from must also be an input source: "
                    f"{reuse_source!r}"
                )
            if (
                source_step.agent_override is not None
                and step.agent_override is not None
                and source_step.agent_override != step.agent_override
            ):
                raise ValueError(
                    f"Step {step.name!r} cannot reuse a session across different agents"
                )
            if (
                source_step.agent_profile_id is not None
                and step.agent_profile_id is not None
                and source_step.agent_profile_id != step.agent_profile_id
            ):
                raise ValueError(
                    f"Step {step.name!r} cannot reuse a session across different runtime profiles"
                )

        # Responsibility boundaries can defer work only to later workflow steps.
        for target in step.defer_to:
            if target not in step_names:
                raise ValueError(f"Step {step.name!r} defer_to references unknown step: {target!r}")
            target_idx = step_names.index(target)
            if target_idx <= i:
                relation = "itself" if target_idx == i else "an earlier step"
                raise ValueError(
                    f"Step {step.name!r} defer_to cannot reference {relation}: {target!r}"
                )

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

        deterministic_targets = [step.next]
        if step.condition is not None:
            deterministic_targets.extend([step.condition.then, step.condition.else_])
            revision_source = step.condition.revision_source
            if revision_source is not None:
                if revision_source not in step_names:
                    raise ValueError(
                        f"Step {step.name!r} references unknown revision source: "
                        f"{revision_source!r}"
                    )
                source_index = step_names.index(revision_source)
                if source_index >= i:
                    raise ValueError(
                        f"Step {step.name!r} revision_source must reference an earlier step, "
                        f"but {revision_source!r} is at index {source_index}"
                    )
        for target in (target for target in deterministic_targets if target is not None):
            if target not in step_names:
                raise ValueError(
                    f"Step {step.name!r} references unknown deterministic target: {target!r}"
                )
            if target == step.name:
                raise ValueError(f"Step {step.name!r} cannot jump to itself")

    validate_gate_conditions(workflow)


def _row_to_workflow(row: Any) -> Workflow:
    """Convert a DB WorkflowRow to a Workflow domain model."""
    return Workflow.model_validate(row.definition)
