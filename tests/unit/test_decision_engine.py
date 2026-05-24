from __future__ import annotations

import pytest

from cognis.core.decision import DecisionEngine, build_routing_reminder, select_workflow
from cognis.models.agent import AgentDefinition, AgentLLMConfig, AgentPermissions


class _LLM:
    """Stub LLM for testing (not used by the rules-only decision engine)."""

    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        return {"choices": [{"message": {"content": "{}"}}]}


class _InvalidWorkflowLLM:
    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"workflow_id": "system:unknown", "confidence": 0.9, "reason": "bad pick"}'
                    }
                }
            ]
        }


class _SequenceWorkflowLLM:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        self.calls += 1
        return self._responses.pop(0)


class _CountingWorkflowLLM(_LLM):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        self.calls += 1
        return await super().generate(messages, model=model, task_type=task_type, **kwargs)


def _agent(can_delegate: bool = True, max_depth: int = 5) -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        llm_config=AgentLLMConfig(model="test-model"),
        permissions=AgentPermissions(can_delegate=can_delegate, max_delegation_depth=max_depth),
    )


def test_build_routing_reminder_matches_implementation_work() -> None:
    reminder = build_routing_reminder("Implement refresh token support across the API.")

    assert reminder is not None
    assert reminder.category == "implementation"
    assert "background task" in reminder.reminder
    assert "inline execution is fine" in reminder.reminder


def test_build_routing_reminder_skips_blank_and_explicit_override_messages() -> None:
    assert build_routing_reminder("") is None
    assert build_routing_reminder("/implement auth") is None
    assert build_routing_reminder("Just answer this directly.") is None


def test_build_routing_reminder_avoids_broad_false_positives() -> None:
    assert build_routing_reminder("What is the current model setting?") is None
    assert build_routing_reminder("Can you help me fix my explanation?") is None
    assert build_routing_reminder("Can you build the argument for this approach?") is None


@pytest.mark.asyncio
async def test_decision_engine_honors_explicit_inline_override() -> None:
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=5,
    )

    result = await engine.decide(user_message="Just answer this directly.", agent=_agent())

    assert result.decision == "inline"
    assert result.override_source == "keyword"


@pytest.mark.asyncio
async def test_decision_engine_defaults_ambiguous_to_inline() -> None:
    """Ambiguous messages default to inline — the agent decides via tools."""
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=20,
        max_delegation_depth=5,
    )

    result = await engine.decide(
        user_message="Please analyze this architecture and plan the implementation steps.",
        agent=_agent(),
    )

    # No LLM classifier — ambiguous messages go inline by default
    assert result.decision == "inline"
    assert result.confidence == 0.9
    assert result.degraded is False


@pytest.mark.asyncio
async def test_decision_engine_leaves_task_slash_commands_inline() -> None:
    """Slash commands are owned by CommandDispatcher, not DecisionEngine."""
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=5,
    )

    result = await engine.decide(
        user_message="/research best practices for async Python",
        agent=_agent(),
    )

    assert result.decision == "inline"
    assert result.override_source is None


@pytest.mark.asyncio
async def test_decision_engine_does_not_parse_delegate_prefix_at_depth_limit() -> None:
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=3,
    )

    result = await engine.decide(
        user_message="/delegate this task",
        agent=_agent(max_depth=3),
        current_depth=3,
    )

    assert result.decision == "inline"
    assert "limit" in result.reason.lower()


@pytest.mark.asyncio
async def test_decision_engine_does_not_delegate_natural_language_task_queries() -> None:
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=5,
    )

    for message in (
        "run in background",
        "background task status",
        "query for task",
        "continue?",
        "/taskfoo create something",
    ):
        result = await engine.decide(user_message=message, agent=_agent())
        assert result.decision == "inline"


@pytest.mark.asyncio
async def test_decision_engine_conversational_is_inline() -> None:
    """Short conversational messages are always inline."""
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=5,
    )

    result = await engine.decide(user_message="hello, how are you?", agent=_agent())

    assert result.decision == "inline"
    assert result.confidence == 0.85


@pytest.mark.asyncio
async def test_select_workflow_uses_general_task_when_no_workflows_available() -> None:
    result = await select_workflow(
        llm=_LLM(),
        task_description="Do something useful",
        available_workflows=[],
        default_workflow_id=None,
    )

    assert result.workflow_id == "system:general-task"


@pytest.mark.asyncio
async def test_select_workflow_invalid_classifier_pick_falls_back_to_default() -> None:
    result = await select_workflow(
        llm=_InvalidWorkflowLLM(),
        task_description="Coordinate slash command rollout",
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic execution",
            },
            {
                "workflow_id": "system:software-development",
                "name": "Software Development",
                "criteria": "Implementation work",
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:general-task"


@pytest.mark.asyncio
async def test_select_workflow_classifier_accepts_plain_json_text() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "system:software-development", "confidence": 0.8, "reason": "implementation work"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Coordinate slash command rollout",
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic execution",
            },
            {
                "workflow_id": "system:software-development",
                "name": "Software Development",
                "criteria": "Implementation work",
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert llm.calls == 1
    assert result.workflow_id == "system:software-development"


@pytest.mark.asyncio
async def test_select_workflow_uses_llm_for_research_selection() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "system:research", "confidence": 0.91, "reason": "research request"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Research best MCP server for Twitter/X",
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic execution",
            },
            {
                "workflow_id": "system:research",
                "name": "Research",
                "criteria": "Research and investigation",
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:research"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_uses_expected_output_for_investigation_report() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "system:research", "confidence": 0.92, "reason": "investigation report"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description=(
            "Title: Investigate cross-user MCP data leakage in scheduled daily brief\n"
            "Expected output: Technical incident report with: confirmed vs rejected hypotheses; "
            "exact root cause if found; affected code paths with file/line references; "
            "security/blast-radius assessment; recommended immediate mitigation; proposed "
            "permanent fix; tests/validation plan; and whether this appears to be direct MCP "
            "credential mix or synthesis/context contamination."
        ),
        available_workflows=[
            {
                "workflow_id": "system:research",
                "name": "Research",
                "description": "Plan, research, synthesize with evaluation.",
                "criteria": "Research tasks, investigation, incident analysis, audits, information gathering, and synthesis reports.",
                "tags": ["research", "analysis", "investigation"],
            },
            {
                "workflow_id": "system:software-development",
                "name": "Software Development",
                "description": "Full development pipeline for code and UI changes.",
                "criteria": "Implementation tasks, feature development, bug fixes, and tests.",
                "tags": ["code", "development", "tests"],
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:research"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_rejects_weak_skill_workflow_classifier_pick() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "skill:contact-lens-shopping", "confidence": 0.9, "reason": "add selected items"}'
                        }
                    }
                ]
            }
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description=(
            "Title: Add selected family movies to Radarr\n"
            "Expected output: Concise Czech completion report listing each requested movie, "
            "resolved title/year/TMDB ID, whether it was added or already existed, and any "
            "errors/blockers."
        ),
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic background tasks that need direct execution.",
            },
            {
                "workflow_id": "skill:contact-lens-shopping",
                "name": "Skill: contact-lens-shopping",
                "description": "Buy contact lenses from a known vendor.",
                "criteria": "Tasks explicitly matching the skill domain: contact-lens-shopping. Tags: shopping, contact-lenses. Do not use for unrelated tasks that only share generic action verbs.",
                "tags": ["shopping", "contact-lenses"],
                "candidate_type": "skill_workflow",
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:general-task"


@pytest.mark.asyncio
async def test_select_workflow_rejects_skill_workflow_without_exact_domain_match() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "skill:contact-lens-shopping", "confidence": 0.99, "reason": "comparison research"}'
                        }
                    }
                ]
            }
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description=(
            "Title: Cognis competitive comparison research\n"
            "Expected output: Compare Cognis with Hermes, OpenClaw, and other agent platforms."
        ),
        available_workflows=[
            {
                "workflow_id": "system:research",
                "name": "Research",
                "description": "Plan, research, and synthesize findings.",
                "criteria": "Information gathering and analysis requests.",
                "tags": ["research", "analysis"],
            },
            {
                "workflow_id": "skill:contact-lens-shopping",
                "name": "Skill: contact-lens-shopping",
                "description": "Buy contact lenses from a known vendor.",
                "criteria": "Tasks explicitly matching the skill domain: contact-lens-shopping. Tags: shopping, contact-lenses. Do not use for unrelated tasks that only share generic action verbs.",
                "tags": ["shopping", "contact-lenses"],
                "candidate_type": "skill_workflow",
            },
        ],
        default_workflow_id="system:research",
    )

    assert result.workflow_id == "system:research"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_accepts_skill_workflow_with_exact_domain_match() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "skill:contact-lens-shopping", "confidence": 0.97, "exact_skill_domain_match": true, "reason": "repeat contact lens order"}'
                        }
                    }
                ]
            }
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Buy my repeat contact lens order from cocky-kontaktni.cz.",
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic background tasks that need direct execution.",
            },
            {
                "workflow_id": "skill:contact-lens-shopping",
                "name": "Skill: contact-lens-shopping",
                "description": "Buy contact lenses from a known vendor.",
                "criteria": "Tasks explicitly matching the skill domain: contact-lens-shopping. Tags: shopping, contact-lenses. Do not use for unrelated tasks that only share generic action verbs.",
                "tags": ["shopping", "contact-lenses"],
                "candidate_type": "skill_workflow",
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "skill:contact-lens-shopping"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_uses_llm_instead_of_candidate_metadata() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "system:software-development", "confidence": 0.9, "reason": "implementation task"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Add expired scheduled tasks filter to the task UI and tests",
        available_workflows=[
            {
                "workflow_id": "system:research",
                "name": "Research",
                "description": "Plan, research, and synthesize findings.",
                "criteria": "Information gathering and analysis requests.",
                "tags": ["research", "analysis"],
            },
            {
                "workflow_id": "system:software-development",
                "name": "Software Development",
                "description": "Full development pipeline for code and UI changes.",
                "criteria": "Implementation tasks, feature development, bug fixes, and tests.",
                "tags": ["code", "development", "ui"],
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:software-development"
    assert result.reason == "implementation task"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_prefers_matching_project_bound_workflow() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "project:onboarding-report", "confidence": 0.94, "reason": "project workflow match"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Create the customer onboarding report from usage metrics",
        available_workflows=[
            {
                "workflow_id": "system:research",
                "name": "Research",
                "description": "Research and report generation.",
                "criteria": "Gather information and write reports.",
                "tags": ["research", "report"],
            },
            {
                "workflow_id": "project:onboarding-report",
                "name": "Onboarding Report",
                "description": "Create customer onboarding reports from usage metrics.",
                "criteria": "Project-specific reporting workflow for onboarding analysis.",
                "tags": ["report", "onboarding", "metrics"],
                "project_bound": True,
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "project:onboarding-report"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_strongly_prefers_meaningful_project_bound_workflow() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "project:onboarding-report", "confidence": 0.94, "reason": "project workflow match"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Create the customer onboarding report from usage metrics",
        available_workflows=[
            {
                "workflow_id": "system:research",
                "name": "Research",
                "description": "Research customer onboarding reports and usage metrics.",
                "criteria": "Gather information and write customer onboarding metric reports.",
                "tags": ["research", "report", "onboarding", "customer", "metrics"],
            },
            {
                "workflow_id": "project:onboarding-report",
                "name": "Onboarding Report",
                "description": "Create onboarding reports.",
                "criteria": "Project-specific reporting workflow.",
                "tags": ["report"],
                "project_bound": True,
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "project:onboarding-report"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_ignores_nonmatching_project_bound_workflow() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "system:research", "confidence": 0.92, "reason": "research task"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Research storage engine tradeoffs for vector search",
        available_workflows=[
            {
                "workflow_id": "project:onboarding-report",
                "name": "Onboarding Report",
                "description": "Create customer onboarding reports from usage metrics.",
                "criteria": "Project-specific reporting workflow for onboarding analysis.",
                "tags": ["report", "onboarding", "metrics"],
                "project_bound": True,
            },
            {
                "workflow_id": "system:research",
                "name": "Research",
                "description": "Research information and compare tradeoffs.",
                "criteria": "Research tasks, information gathering, and analysis requests.",
                "tags": ["research", "analysis"],
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:research"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_select_workflow_respects_llm_software_development_selection() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "system:software-development", "confidence": 0.9, "reason": "implementation with tests"}'
                        }
                    }
                ]
            }
        ]
    )

    result = await select_workflow(
        llm=llm,
        task_description="Implement a frontend feature and add tests for the bug fix",
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic execution",
            },
            {
                "workflow_id": "system:research",
                "name": "Research",
                "description": "Plan, research, and synthesize findings.",
                "criteria": "Research and investigation",
                "tags": ["research", "analysis"],
            },
            {
                "workflow_id": "system:software-development",
                "name": "Software Development",
                "description": "Full development pipeline for frontend, backend, and UI changes.",
                "criteria": "Implementation work, feature development, bug fixes, and tests.",
                "tags": ["code", "development", "tests"],
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert llm.calls == 1
    assert result.workflow_id == "system:software-development"
