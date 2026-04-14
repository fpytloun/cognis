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
