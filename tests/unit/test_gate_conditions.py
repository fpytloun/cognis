from __future__ import annotations

import pytest

from cognis.core.gate_conditions import (
    evaluate_gate_conditions,
    evaluate_gate_conditions_detailed,
    validate_gate_conditions,
)
from cognis.models.workflow import (
    GateCondition,
    GateConfig,
    StepCompletionContract,
    StepCompletionMetadataField,
    StepDefinition,
    Workflow,
)


def test_evaluate_gate_conditions_supports_metadata_and_thresholds() -> None:
    assert evaluate_gate_conditions(
        ["metadata.plan.confidence < thresholds.min_confidence or metadata.plan.risk == 'high'"],
        step_outputs={"plan": {"metadata": {"confidence": 0.5, "risk": "low"}}},
        thresholds={"min_confidence": 0.6},
    )


def test_evaluate_gate_conditions_detailed_records_values_and_result() -> None:
    details = evaluate_gate_conditions_detailed(
        ["metadata.plan.confidence >= thresholds.min_confidence"],
        step_outputs={"plan": {"metadata": {"confidence": 0.7}}},
        thresholds={"min_confidence": 0.6},
    )

    assert details["passed"] is True
    condition = details["conditions"][0]
    assert condition["operator"] == ">="
    assert condition["referenced_values"] == {"metadata.plan.confidence": 0.7}
    assert condition["expected_values"] == {"thresholds.min_confidence": 0.6}
    assert condition["actual_result"] is True


def test_evaluate_gate_conditions_detailed_records_errors() -> None:
    details = evaluate_gate_conditions_detailed(
        ["metadata.plan.confidence + 1"],
        step_outputs={"plan": {"metadata": {"confidence": 0.7}}},
        thresholds={},
    )

    assert details["passed"] is False
    assert details["errors"]
    assert details["conditions"][0]["error"]


def test_validate_gate_conditions_rejects_undeclared_metadata() -> None:
    workflow = Workflow(
        workflow_id="wf",
        name="wf",
        steps=[
            StepDefinition(
                name="plan",
                type="run",
                metadata_contract=StepCompletionContract(
                    fields=[StepCompletionMetadataField(name="confidence", type="number")]
                ),
            ),
            StepDefinition(
                name="gate",
                type="gate",
                gate=GateConfig(
                    message="pause",
                    conditions=[GateCondition(expression="metadata.plan.risk == 'high'")],
                ),
            ),
        ],
    )

    with pytest.raises(ValueError, match="undeclared metadata"):
        validate_gate_conditions(workflow)
