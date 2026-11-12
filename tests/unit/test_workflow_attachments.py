"""Compatibility and fail-closed projection checks for workflow-only attachments."""

from __future__ import annotations

import copy

import pytest

from cognis.core.workflow_attachments import project_workflow_attachments
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_rendering import (
    MAX_CONTEXT_STRING_BYTES,
    WorkflowRenderer,
    WorkflowRenderError,
)
from cognis.models.task import TaskModel
from cognis.models.tool import ToolResult
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState


def test_reference_projection_preserves_order_and_identity_without_mutating_evidence():
    refs = [
        {
            "artifact_id": "art_checkpoint",
            "filename": "checkpoint.json",
            "mime_type": "application/json",
            "kind": "file",
            "size_bytes": 40000,
            "content_b64": "YQ==" * 40000,
            "url": "https://files.test/checkpoint?sig=temporary",
            "lineage_id": "art_lineage",
            "revision": 2,
            "project_id": None,
            "source_path": "/executor/checkpoint.json",
        },
        {"url": "https://files.test/report.pdf", "filename": "report.pdf"},
        {"artifact_id": "dlv_report", "mime_type": "application/pdf"},
        {"artifact_id": "tool_artifact:call_accepted:binary"},
        {"lazy_artifact_ref": "tool_artifact:call_accepted:result:1"},
        {"url": "/api/v1/images/img_chart", "kind": "image"},
    ]
    original = copy.deepcopy(refs)
    projected = project_workflow_attachments(refs)
    assert refs == original
    assert len(projected) == len(refs)
    assert projected[1:] == refs[1:]
    assert projected[0] == {
        key: value
        for key, value in refs[0].items()
        if key not in {"content_b64", "url", "source_path"}
    }
    assert (
        WorkflowRenderer().render_native(
            "{{ result.attachments[0].revision }}", {"result": {"attachments": projected}}
        )
        == 2
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        [None],
        [{}],
        [{"artifact_id": ""}],
        [{"artifact_id": 42}],
        [{"artifact_id": "tool_artifact:missing-anchor"}],
        [{"lazy_artifact_ref": "tool_artifact:<call_id>:binary"}],
        [{"content_b64": "YQ=="}],
        [{"url": "https://files.test/image.png", "content_b64": "YQ=="}],
        [{"artifact_id": "tool_artifact:call_accepted:binary", "content_b64": "YQ=="}],
        [{"transfer_receipt": {}}],
        [{"artifact_id": "art_checkpoint", "transfer_receipt": {}}],
        [
            {
                "artifact_id": "art_checkpoint",
                "source_artifact_download": {"url": "https://files.test"},
            }
        ],
        [{"artifact_id": "art_checkpoint", "size_bytes": True}],
        [{"artifact_id": "art_checkpoint", "filename": {"content_b64": "YQ=="}}],
        [{"url": "data:application/json;base64,YQ=="}],
    ],
)
def test_invalid_or_unmaterialized_attachments_fail_closed(value):
    with pytest.raises(WorkflowRenderError):
        project_workflow_attachments(value)


def test_metadata_remains_bounded_and_inline_template_is_unavailable():
    result = ToolResult(
        output="Published",
        attachments=[{"artifact_id": "art_checkpoint", "content_b64": "YQ=="}],
    )
    context = WorkflowEngine._deterministic_tool_result_context(result, raw_output=None)
    assert (
        WorkflowRenderer().render_native(
            "{{ result.attachments[0].artifact_id }}", {"result": context}
        )
        == "art_checkpoint"
    )
    with pytest.raises(WorkflowRenderError):
        WorkflowRenderer().render_native(
            "{{ result.attachments[0].content_b64 }}", {"result": context}
        )
    result.attachments[0]["filename"] = "x" * (MAX_CONTEXT_STRING_BYTES + 1)
    context = WorkflowEngine._deterministic_tool_result_context(result, raw_output=None)
    with pytest.raises(WorkflowRenderError, match="string exceeds size limit"):
        WorkflowRenderer().render_native(
            {"artifact_id": "{{ result.attachments[0].artifact_id }}"}, {"result": context}
        )


def test_historical_context_and_recovery_use_same_projection():
    engine = object.__new__(WorkflowEngine)
    output = {
        "summary": "Published",
        "attachments": [{"artifact_id": "art_checkpoint", "content_b64": "x" * 40000}],
    }
    state = WorkflowState(step_outputs={"publish": output})
    step = StepDefinition(name="complete", type="complete", complete={"summary": "Done"})
    workflow = Workflow(workflow_id="wf-historical-refs", name="Historical refs", steps=[step])
    context = engine._deterministic_render_context(
        TaskModel(
            task_id="task-historical-refs",
            title="Historical refs",
            created_by="user@example.com",
            agent_id="agent-1",
        ),
        state,
        workflow,
        step,
    )
    assert (
        WorkflowRenderer().render_native("{{ steps.publish.attachments[0].artifact_id }}", context)
        == "art_checkpoint"
    )
    recovered = engine._deterministic_result_from_persisted(
        "persisted-step-run", "approved", output, {}, workflow
    )
    assert recovered.output.attachments == context["steps"]["publish"]["attachments"]
    assert "content_b64" in output["attachments"][0]
