"""Validate synthetic dashboard content used by real-endpoint feedback acceptance."""

from __future__ import annotations

from pathlib import Path

import yaml

from cognis.core.workflow_registry import GENERAL_TASK_WORKFLOW
from cognis.models.deliverable import normalize_required_rich_payload


def test_dashboard_acceptance_payloads_use_real_authoring_contract() -> None:
    """Keep screenshot fixtures renderable and their revision identity dynamic."""
    path = Path(__file__).parents[1] / "e2e/scenarios/work-feedback-dashboard.yaml"
    scenario = yaml.safe_load(path.read_text())
    writes = [
        step
        for turn in scenario["turns"]
        for step in turn["steps"]
        if step["type"] == "tool_call" and step["name"] == "write_deliverable"
    ]
    assert len(writes) == 2
    for step in writes:
        arguments = step["arguments"]
        assert arguments["action"] == "rich"
        normalized, _warnings = normalize_required_rich_payload(arguments["payload"])
        assert len(normalized["blocks"]) == 5
        assert "Synthetic content" in normalized["blocks"][0]["content"]
    first_call_id = writes[0]["call_id"]
    assert writes[1]["arguments"]["supersedes"] == (
        "{{tool_result:" + first_call_id + ":deliverable_id}}"
    )


def test_task_fixture_supplies_the_general_task_completion_contract() -> None:
    """Scripted completion must satisfy the same required metadata as real agents."""
    path = Path(__file__).parents[1] / "e2e/scenarios/work-feedback-task-result.yaml"
    scenario = yaml.safe_load(path.read_text())
    completion = next(
        step
        for turn in scenario["turns"]
        for step in turn["steps"]
        if step["type"] == "tool_call" and step["name"] == "step_complete"
    )
    metadata = completion["arguments"]["metadata"]
    contract = GENERAL_TASK_WORKFLOW.steps[0].metadata_contract
    assert contract is not None
    for field in contract.fields:
        if field.required:
            assert field.name in metadata
        if field.enum is not None:
            assert metadata[field.name] in field.enum
    assert isinstance(metadata["verification"], dict)
