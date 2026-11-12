from __future__ import annotations

from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from cognis.models import feedback as dto
from tests.unit.test_feedback_runtime import OWNER, Harness, harness  # noqa: F401


@pytest.mark.parametrize("body", ["", " \t\n", "é" * 8193, "x" * 16385])
@pytest.mark.parametrize(
    "model",
    [
        dto.ThreadCreateRequest,
        dto.EntryCreateRequest,
        dto.EntryEditRequest,
        dto.EntryVersion,
        dto.PublicThreadCreateRequest,
        dto.PublicEntryCreateRequest,
    ],
)
def test_entry_bodies_remain_strict(model: type[dto.FeedbackModel], body: str) -> None:
    # Validate the owning field independently of each request's other required fields.
    from pydantic import TypeAdapter

    with pytest.raises(ValidationError):
        TypeAdapter(model.model_fields["body"].rebuild_annotation()).validate_python(body)


@pytest.mark.parametrize("body", ["", " \t\n", "é" * 8192, "x" * 16384])
def test_draft_body_preserves_bounded_text(body: str) -> None:
    draft = dto.DraftRequest(expected_version=0, context={"kind": "standalone"}, body=body)
    assert draft.body == body


@pytest.mark.asyncio
async def test_http_image_annotation_save_then_clear_draft(harness: Harness) -> None:  # noqa: F811
    image = BytesIO()
    Image.new("RGB", (40, 30)).save(image, format="PNG")
    source = await harness.source(image.getvalue(), "image/png", "annotation.png")
    anchor = {
        "source_token": source.source_token,
        "content_digest": source.content_digest,
        "locator": {
            "kind": "image",
            "intrinsic_width": 40,
            "intrinsic_height": 30,
            "orientation": 1,
            "shapes": [{"kind": "rectangle", "x": 1, "y": 2, "width": 10, "height": 12}],
        },
    }
    draft_key = uuid4().hex
    payload = {
        "expected_version": 0,
        "context": {"kind": "standalone"},
        "source": source.source.model_dump(mode="json"),
        "anchor": anchor,
        "body": "Review this area",
        "selection": [],
        "overall_message": None,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app()),
        base_url="http://feedback.test",
        headers={"x-test-user": OWNER},
    ) as client:
        url = f"/api/v1/feedback/drafts/{draft_key}"
        saved = await client.put(url, json=payload)
        assert saved.status_code == 200, saved.text
        thread = await client.post(
            "/api/v1/feedback/threads",
            json={
                "creation_key": uuid4().hex,
                "context": payload["context"],
                "anchor": anchor,
                "body": payload["body"],
            },
        )
        assert thread.status_code == 201, thread.text
        entry = thread.json()["entries"][0]
        for body, selection in [
            ("", []),
            (" \t\n", []),
            ("", [{"entry_version_id": entry["entry_version_id"], "expected_entry_version": 1}]),
        ]:
            payload.update(
                expected_version=saved.json()["version"],
                anchor=None,
                body=body,
                selection=selection,
            )
            saved = await client.put(url, json=payload)
            assert saved.status_code == 200, saved.text
            fetched = await client.get(url)
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["draft"] == dto.DraftRequest.model_validate(payload).model_dump(
                mode="json"
            )
        refreshed = await client.get(f"/api/v1/feedback/threads/{thread.json()['thread_id']}")
        assert refreshed.status_code == 200
        assert refreshed.json()["entries"][0]["body"] == "Review this area"
        assert refreshed.json()["anchor"]["locator"] == anchor["locator"]
        for body in ["é" * 8193, "x" * 16385]:
            payload.update(expected_version=saved.json()["version"], body=body)
            rejected = await client.put(url, json=payload)
            assert rejected.status_code == 422
        unchanged = await client.get(url)
        assert unchanged.json() == saved.json()
