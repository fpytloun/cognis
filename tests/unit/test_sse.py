from __future__ import annotations

import pytest

from cognis.api.sse import SSETurnObserver
from cognis.core.turn_scheduler import TurnResult


@pytest.mark.asyncio
async def test_sse_turn_complete_includes_sanitized_attachments() -> None:
    observer = SSETurnObserver("conv-1")

    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-1",
            attachments=[
                {
                    "artifact_id": "img_1",
                    "kind": "image",
                    "mime_type": "image/png",
                    "filename": "image.png",
                    "size_bytes": 3,
                    "content_b64": "YWJj",
                }
            ],
        )
    )

    event = await observer._queue.get()  # noqa: SLF001
    assert event["event"] == "complete"
    assert event["data"]["attachments"] == [
        {
            "artifact_id": "img_1",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "image.png",
            "size_bytes": 3,
        }
    ]


@pytest.mark.asyncio
async def test_sse_tool_result_includes_file_diffs() -> None:
    observer = SSETurnObserver("conv-1")
    file_diffs = [{"path": "example.py", "diff": "--- example.py\n+++ example.py\n"}]

    await observer.on_tool_result(
        "conv-1",
        "sess-1",
        "call-1",
        "edit",
        "done",
        False,
        20,
        None,
        None,
        file_diffs,
    )

    event = await observer._queue.get()  # noqa: SLF001
    assert event["event"] == "tool_result"
    assert event["data"]["file_diffs"] == file_diffs


@pytest.mark.asyncio
async def test_sse_tool_result_includes_sanitized_attachments() -> None:
    observer = SSETurnObserver("conv-1")

    await observer.on_tool_result(
        "conv-1",
        "sess-1",
        "call-1",
        "generate_document",
        "done",
        False,
        20,
        None,
        [
            {
                "artifact_id": "doc_1",
                "kind": "pdf",
                "mime_type": "application/pdf",
                "filename": "summary.pdf",
                "size_bytes": 5,
                "content_b64": "YWJj",
            }
        ],
    )

    event = await observer._queue.get()  # noqa: SLF001
    assert event["event"] == "tool_result"
    assert event["data"]["attachments"] == [
        {
            "artifact_id": "doc_1",
            "kind": "pdf",
            "mime_type": "application/pdf",
            "filename": "summary.pdf",
            "size_bytes": 5,
        }
    ]
