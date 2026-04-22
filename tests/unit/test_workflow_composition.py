from __future__ import annotations

import asyncio
import json

from cognis.core.workflow_composition import (
    compose_workflow_plan,
    decompose_skill_material,
    validate_composed_workflow,
    workflow_preview_payload,
)


class _FallbackLLM:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    async def generate(self, messages: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
        self.calls.append({"messages": messages, **kwargs})
        if "response_format" in kwargs:
            raise TimeoutError()
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self.payload),
                    }
                }
            ]
        }


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


def test_decompose_skill_material_retries_without_response_format_after_timeout() -> None:
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
    assert len(llm.calls) == 2
    assert "response_format" in llm.calls[0]
    assert "response_format" not in llm.calls[1]
    assert "max_tokens" not in llm.calls[0]
    assert "max_tokens" not in llm.calls[1]


def test_compose_workflow_plan_retries_without_response_format_after_timeout() -> None:
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
    assert len(llm.calls) == 2
    assert "max_tokens" not in llm.calls[0]
    assert "max_tokens" not in llm.calls[1]
