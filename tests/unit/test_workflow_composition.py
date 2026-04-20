from __future__ import annotations

from cognis.core.workflow_composition import validate_composed_workflow, workflow_preview_payload


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
