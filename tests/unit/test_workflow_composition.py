from __future__ import annotations

import asyncio
import json

import pytest

from cognis.core.workflow_composition import (
    COMPOSED_WORKFLOW_PREVIEW_ID,
    SkillMaterial,
    compose_workflow_plan,
    decompose_skill_material,
    validate_composed_workflow,
    workflow_payload_for_persistence,
    workflow_preview_payload,
)
from cognis.tools.builtin.orchestration import (
    COMPOSE_AND_RUN_WORKFLOW_TOOL,
    CREATE_WORKFLOW_TOOL,
    UPDATE_WORKFLOW_TOOL,
)


class _FallbackLLM:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    async def generate(
        self, messages: list[dict[str, object]], **kwargs: object
    ) -> dict[str, object]:
        self.calls.append({"messages": messages, **kwargs})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self.payload),
                    }
                }
            ]
        }


class _SlowLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate(
        self, messages: list[dict[str, object]], **kwargs: object
    ) -> dict[str, object]:
        self.calls.append({"messages": messages, **kwargs})
        await asyncio.sleep(1)
        return {"choices": [{"message": {"content": json.dumps({"rationale": "", "steps": []})}}]}


def test_composition_tool_directs_ordinary_work_to_standard_tools() -> None:
    description = COMPOSE_AND_RUN_WORKFLOW_TOOL.description
    properties = COMPOSE_AND_RUN_WORKFLOW_TOOL.parameters["properties"]

    assert "Advanced, rare operation" in description
    assert "Use create_task" in description
    assert "manage_schedules" in description
    assert "immediately trigger a schedule" in description
    assert "force_compose" not in properties
    assert "ordinary schedule creation" in properties["schedule"]["description"]


def test_preview_workflow_id_is_never_persisted() -> None:
    workflow = validate_composed_workflow(
        {
            "workflow_id": COMPOSED_WORKFLOW_PREVIEW_ID,
            "name": "Preview",
            "description": "",
            "lifecycle": "persistent",
            "steps": [{"name": "run", "type": "run", "prompt": "Do the work."}],
        },
        skill_materials=[],
    )

    payload = workflow_payload_for_persistence(workflow)

    assert payload["workflow_id"] is None
    assert workflow.workflow_id == COMPOSED_WORKFLOW_PREVIEW_ID


def test_composer_supplied_workflow_id_is_never_persisted() -> None:
    workflow = validate_composed_workflow(
        {
            "workflow_id": "wf_stable_composer_guess",
            "name": "Composed",
            "description": "",
            "lifecycle": "persistent",
            "steps": [{"name": "run", "type": "run", "prompt": "Do the work."}],
        },
        skill_materials=[],
    )

    assert workflow_payload_for_persistence(workflow)["workflow_id"] is None


def test_validate_composed_workflow_accepts_lifecycle_and_lineage() -> None:
    workflow = validate_composed_workflow(
        {
            "workflow_id": "wf_preview",
            "name": "Evening Summary",
            "description": "Gather then summarize",
            "steps": [
                {
                    "name": "gather",
                    "type": "run",
                    "prompt": "Gather the relevant inputs.",
                    "require_deliverable": False,
                },
                {
                    "name": "summarize",
                    "type": "run",
                    "prompt": "Write the final summary.",
                    "require_deliverable": True,
                },
            ],
            "lifecycle": "ephemeral",
            "lineage": {
                "base_workflow_id": "system:research",
                "source_skill_ids": ["skill_evening_summary"],
                "composition_source": "agent_composed",
            },
        }
    )

    assert str(workflow.lifecycle) == "ephemeral"
    assert workflow.lineage is not None
    assert workflow.lineage.base_workflow_id == "system:research"


def test_workflow_preview_payload_uses_step_names() -> None:
    workflow = validate_composed_workflow(
        {
            "workflow_id": "wf_preview",
            "name": "Daily Brief",
            "steps": [
                {"name": "collect", "type": "run", "prompt": "Collect inputs."},
                {"name": "brief", "type": "run", "prompt": "Write the brief."},
            ],
        }
    )

    preview = workflow_preview_payload(workflow)

    assert preview["name"] == "Daily Brief"
    assert preview["steps"] == ["collect", "brief"]
    assert preview["phases"] == []


def test_composed_workflow_round_trips_presentation_into_preview() -> None:
    workflow = validate_composed_workflow(
        {
            "workflow_id": "wf_phased",
            "name": "Phased",
            "steps": [
                {"name": "collect", "type": "run"},
                {"name": "deliver", "type": "run"},
            ],
            "presentation": {
                "phases": [
                    {"id": "prepare", "title": "Prepare", "step_names": ["collect"]},
                    {"id": "deliver", "title": "Deliver", "step_names": ["deliver"]},
                ]
            },
        }
    )

    assert workflow_preview_payload(workflow)["phases"] == [
        {"id": "prepare", "title": "Prepare", "step_names": ["collect"]},
        {"id": "deliver", "title": "Deliver", "step_names": ["deliver"]},
    ]


def test_agent_workflow_tools_expose_phase_capable_presentation_schema() -> None:
    for tool in (CREATE_WORKFLOW_TOOL, UPDATE_WORKFLOW_TOOL):
        presentation = tool.parameters["properties"]["presentation"]
        assert presentation["required"] == ["phases"]
        phase = presentation["properties"]["phases"]["items"]
        assert phase["required"] == ["id", "title", "step_names"]


def test_decompose_skill_material_requests_provider_json_mode_once() -> None:
    llm = _FallbackLLM(
        {
            "rationale": "Split the skill into gather and summary.",
            "steps": [
                {
                    "name": "gather",
                    "type": "run",
                    "prompt": "Gather the required inputs.",
                    "require_deliverable": False,
                },
                {
                    "name": "summarize",
                    "type": "run",
                    "prompt": "Write the final summary.",
                    "require_deliverable": True,
                },
            ],
        }
    )

    result = asyncio.run(
        decompose_skill_material(
            llm=llm,
            skill_id="skill_daily_brief",
            name="Daily Brief",
            description="Prepare a daily brief.",
            instructions="Gather updates and summarize them.",
            tools=[],
            prompt_templates={},
            timeout_seconds=6.0,
        )
    )

    assert result.steps[0]["name"] == "gather"
    assert len(llm.calls) == 1
    assert "response_format" in llm.calls[0]
    assert "max_tokens" not in llm.calls[0]


def test_decompose_skill_material_enforces_outer_timeout() -> None:
    llm = _SlowLLM()

    with pytest.raises(TimeoutError):
        asyncio.run(
            decompose_skill_material(
                llm=llm,
                skill_id="skill_daily_brief",
                name="Daily Brief",
                description="Prepare a daily brief.",
                instructions="Gather updates and summarize them.",
                tools=[],
                prompt_templates={},
                timeout_seconds=0.01,
            )
        )

    assert len(llm.calls) == 1
    assert "response_format" in llm.calls[0]


def test_decompose_skill_material_uses_single_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_timeouts: list[float] = []
    original_wait_for = asyncio.wait_for

    async def _record_wait_for(awaitable: object, timeout: float) -> object:
        recorded_timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(
        "cognis.core.workflow_composition.asyncio.wait_for",
        _record_wait_for,
    )

    llm = _FallbackLLM(
        {
            "rationale": "Split the skill into gather and summary.",
            "steps": [
                {
                    "name": "gather",
                    "type": "run",
                    "prompt": "Gather the required inputs.",
                    "require_deliverable": False,
                }
            ],
        }
    )

    asyncio.run(
        decompose_skill_material(
            llm=llm,
            skill_id="skill_daily_brief",
            name="Daily Brief",
            description="Prepare a daily brief.",
            instructions="Gather updates and summarize them.",
            tools=[],
            prompt_templates={},
            timeout_seconds=60.0,
        )
    )

    assert recorded_timeouts == [60.0]


def test_decompose_skill_material_assigns_step_profiles_for_skill_tools() -> None:
    llm = _FallbackLLM(
        {
            "rationale": "Run the skill tool and summarize the result.",
            "steps": [
                {
                    "name": "execute",
                    "type": "run",
                    "prompt": "Run the release helper.",
                    "require_deliverable": False,
                }
            ],
        }
    )

    result = asyncio.run(
        decompose_skill_material(
            llm=llm,
            skill_id="skill_release",
            name="Release",
            description="Run the release automation.",
            instructions="Use the release helper and summarize the result.",
            tools=[
                {
                    "name": "run_release",
                    "description": "Execute the release workflow.",
                }
            ],
            prompt_templates={},
            timeout_seconds=6.0,
        )
    )

    assert result.steps[0]["step_profile_id"] == "system:general-task"
    assert result.steps[0]["step_profile_mode"] == "hard"
    assert result.steps[0]["step_profile"]["tool_overrides"]["include"] == [
        "skill:skill_release:run_release"
    ]


def test_decompose_skill_material_assigns_step_profiles_for_linked_tools() -> None:
    llm = _FallbackLLM(
        {
            "rationale": "Use the linked tools and summarize the result.",
            "steps": [
                {
                    "name": "execute",
                    "type": "run",
                    "prompt": "Run the shell helpers.",
                    "require_deliverable": False,
                }
            ],
        }
    )

    result = asyncio.run(
        decompose_skill_material(
            llm=llm,
            skill_id="skill_release",
            name="Release",
            description="Run the release automation.",
            instructions="Use the release helper and summarize the result.",
            tools=[],
            linked_tool_ids=["builtin:bash", "builtin:read"],
            prompt_templates={},
            timeout_seconds=6.0,
        )
    )

    assert result.steps[0]["step_profile_id"] == "system:general-task"
    assert result.steps[0]["step_profile_mode"] == "hard"
    assert result.steps[0]["step_profile"]["tool_overrides"]["include"] == [
        "builtin:bash",
        "builtin:read",
    ]


def test_decompose_skill_material_includes_refresh_guidance_for_existing_steps() -> None:
    llm = _FallbackLLM(
        {
            "rationale": "Keep the setup step and update the final synthesis.",
            "steps": [
                {
                    "name": "resolve_window",
                    "type": "run",
                    "prompt": "Resolve the date window.",
                    "require_deliverable": False,
                },
                {
                    "name": "synthesize",
                    "type": "run",
                    "prompt": "Write the final brief.",
                    "input": {"type": "last", "source": "all"},
                    "require_deliverable": True,
                },
            ],
        }
    )

    asyncio.run(
        decompose_skill_material(
            llm=llm,
            skill_id="skill_brief",
            name="Daily Brief",
            description="Prepare a daily brief.",
            instructions="Write a concise brief.",
            tools=[],
            prompt_templates={},
            existing_steps=[
                {
                    "name": "resolve_window",
                    "type": "run",
                    "prompt": "Resolve the date window.",
                    "require_deliverable": False,
                }
            ],
            previous_instructions="Write a concise brief with one section.",
            timeout_seconds=6.0,
        )
    )

    prompt = str(llm.calls[-1]["messages"][-1]["content"])
    assert "Refresh this skill's existing decomposition selectively when possible" in prompt
    assert "source:'all'" in prompt
    assert "immediately preceding run step" in prompt


def test_compose_workflow_plan_requests_provider_json_mode_once() -> None:
    llm = _FallbackLLM(
        {
            "action": "reuse_existing",
            "workflow_id": "system:software-development",
            "rationale": "Existing workflow already fits.",
            "title": "Implement change",
            "expected_output": "Completed implementation",
        }
    )

    result = asyncio.run(
        compose_workflow_plan(
            llm=llm,
            intent="Implement the requested feature",
            context="User requested a coding workflow.",
            available_workflows=[],
            template_hints=[],
            base_workflow=None,
            skill_materials=[],
            persist=False,
            schedule_requested=False,
            timeout_seconds=6.0,
        )
    )

    assert result.action == "reuse_existing"
    assert result.workflow_id == "system:software-development"
    assert len(llm.calls) == 1
    assert "max_tokens" not in llm.calls[0]
    assert "response_format" in llm.calls[0]


def test_validate_composed_workflow_normalizes_missing_step_profiles_from_skill_materials() -> None:
    workflow = validate_composed_workflow(
        {
            "workflow_id": "wf_preview",
            "name": "Skill Workflow",
            "steps": [
                {
                    "name": "execute",
                    "type": "run",
                    "prompt": "Run the skill tool.",
                },
                {
                    "name": "summarize",
                    "type": "run",
                    "agent_override": "system:implement",
                    "prompt": "Summarize the result.",
                },
            ],
        },
        skill_materials=[
            SkillMaterial(
                skill_id="skill_release",
                name="Release",
                instructions="Run release automation.",
                tools=[
                    {
                        "name": "run_release",
                        "description": "Execute the release workflow.",
                    }
                ],
            )
        ],
    )

    assert workflow.steps[0].step_profile_id == "system:general-task"
    assert str(workflow.steps[0].step_profile_mode) == "hard"
    assert workflow.steps[0].step_profile is not None
    assert workflow.steps[0].step_profile.tool_overrides.include == [
        "skill:skill_release:run_release"
    ]
    assert workflow.steps[1].step_profile_id == "system:coding"
