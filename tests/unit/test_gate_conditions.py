from __future__ import annotations

import pytest

from cognis.core.gate_conditions import evaluate_gate_conditions, validate_gate_conditions
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
