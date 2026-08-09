from cognis.api.chat_v2.schemas import (
    ActivityRecentWork,
    FileDiffRef,
    WorkArtifact,
    WorkCommandEvent,
    WorkDeliverable,
    WorkMutationEvent,
)
from cognis.api.chat_v2.work_repository import (
    ACTIVITY_OVERVIEW_COMMAND_PREVIEW_MAX_BYTES,
    _lightweight_recent_work,
)


def test_lightweight_recent_work_strips_heavy_bodies_and_preserves_metadata() -> None:
    work = ActivityRecentWork(
        commands=[
            WorkCommandEvent(
                id=f"command-{index}",
                call_id=f"call-{index}",
                sort_key=str(index),
                command="printf output",
                status="complete",
                arguments={"command": "printf output", "secret": "heavy"},
                evaluation={"body": "heavy"},
                error="é" * 3000,
                preview="é" * 5000,
            )
            for index in range(10)
        ],
        mutations=[
            WorkMutationEvent(
                id="mutation",
                call_id="mutation-call",
                sort_key="1",
                tool_name="write",
                category="filesystem",
                operation_kind="write",
                status="complete",
                arguments={"content": "heavy"},
                result_preview="heavy",
                streamed_output="heavy",
                evaluation={"body": "heavy"},
                paths=["src/app.py"],
                file_diffs=[
                    FileDiffRef(
                        path="src/app.py",
                        path_id="root:src/app.py",
                        diff="+heavy",
                        additions=1,
                        deletions=0,
                    )
                ],
                additions=1,
            )
        ],
        files=[
            WorkMutationEvent(
                id="file",
                call_id="file-call",
                sort_key="2",
                tool_name="write",
                category="filesystem",
                operation_kind="write",
                status="complete",
                arguments={"content": "heavy"},
                file_diffs=[FileDiffRef(path="src/file.py", diff="+heavy")],
            )
        ],
        artifacts=[
            WorkArtifact(
                artifact_id="artifact-id",
                filename="report.pdf",
                size_bytes=123,
            )
        ],
        deliverables=[
            WorkDeliverable(
                deliverable_id="deliverable-id",
                format="markdown",
                title="Report",
                content="heavy",
                render_metadata={"body": "heavy"},
                export_metadata={"body": "heavy"},
            )
        ],
    )

    shaped = _lightweight_recent_work(work)

    assert len(shaped.commands) == 10
    assert shaped.commands[0].command == "printf output"
    assert shaped.commands[0].arguments == {}
    assert shaped.commands[0].evaluation is None
    assert shaped.commands[0].preview_truncated is True
    assert (
        len(
            (shaped.commands[0].error or "").encode("utf-8")
            + (shaped.commands[0].preview or "").encode("utf-8")
        )
        <= ACTIVITY_OVERVIEW_COMMAND_PREVIEW_MAX_BYTES
    )
    assert shaped.mutations[0].paths == ["src/app.py"]
    assert shaped.mutations[0].additions == 1
    assert shaped.mutations[0].arguments == {}
    assert shaped.mutations[0].result_preview is None
    assert shaped.mutations[0].streamed_output is None
    assert shaped.mutations[0].evaluation is None
    assert shaped.mutations[0].error is None
    assert shaped.mutations[0].file_diffs[0].path_id == "root:src/app.py"
    assert shaped.mutations[0].file_diffs[0].diff == ""
    assert shaped.mutations[0].file_diffs[0].content_truncated is True
    assert shaped.files[0].file_diffs[0].diff == ""
    assert shaped.artifacts[0].filename == "report.pdf"
    assert shaped.deliverables[0].deliverable_id == "deliverable-id"
    assert shaped.deliverables[0].content is None
    assert shaped.deliverables[0].content_preview_truncated is True
    assert shaped.deliverables[0].render_metadata is None
    assert shaped.deliverables[0].export_metadata is None
