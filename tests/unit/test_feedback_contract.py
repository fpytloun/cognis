from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cognis.models.feedback import (
    EntryEditRequest,
    FeedbackPreflightRequest,
    GrantRequest,
    HtmlLocator,
    ImageLocator,
    PublicEntryEditRequest,
    ResolveSourceRequest,
    ScopedTextRequest,
    ShareRequest,
    ShareUpdateRequest,
    TextRange,
    feedback_json_schema,
)
from cognis.models.feedback_routes import (
    CAPABILITY_HEADER,
    GUEST_HEADER,
    PRIVATE_ROUTES,
    PUBLIC_ROUTES,
    route_contract,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "feedback"


def test_exported_schema_matches_canonical_models() -> None:
    assert json.loads((FIXTURES / "schema.json").read_text()) == feedback_json_schema()
    inputs = json.loads((FIXTURES / "inputs.json").read_text())
    ResolveSourceRequest.model_validate(inputs["resolve_document"])
    FeedbackPreflightRequest.model_validate(inputs["preflight"])
    ScopedTextRequest.model_validate(inputs["rich_field_text"])
    ScopedTextRequest.model_validate(inputs["pdf_page_text"])
    ShareRequest.model_validate(inputs["share"])
    GrantRequest.model_validate(inputs["grant"])


def test_exact_route_headers_and_response_dtos_are_exported() -> None:
    from cognis.models import feedback

    assert CAPABILITY_HEADER == "X-Cognis-Feedback-Capability"
    assert GUEST_HEADER == "X-Cognis-Feedback-Guest"
    assert json.loads((FIXTURES / "routes.json").read_text()) == json.loads(
        json.dumps(route_contract())
    )
    for routes in (PRIVATE_ROUTES, PUBLIC_ROUTES):
        identities = {(row[0], row[1]) for row in routes}
        assert len(identities) == len(routes)
        for _, _, _, response, _ in routes:
            if response and "/" not in response and response != "source MIME":
                assert hasattr(feedback, response), response


def test_public_share_requires_exact_source_and_digest_and_cannot_retarget() -> None:
    share = json.loads((FIXTURES / "inputs.json").read_text())["share"]
    for field in ("source", "expected_content_digest"):
        with pytest.raises(ValidationError):
            ShareRequest.model_validate(
                {key: value for key, value in share.items() if key != field}
            )
    with pytest.raises(ValidationError):
        ShareUpdateRequest.model_validate({"expected_version": 1, "source": share["source"]})


def test_scoped_text_is_a_strict_body_contract() -> None:
    data = json.loads((FIXTURES / "inputs.json").read_text())["rich_field_text"]
    data["text"] = "Client-authored canonical text is not accepted"
    with pytest.raises(ValidationError):
        ScopedTextRequest.model_validate(data)


def test_edit_context_is_optional_and_only_authenticated() -> None:
    body = {"expected_version": 1, "body": "Edited comment"}
    assert EntryEditRequest.model_validate(body).context is None
    context = {"kind": "chat", "conversation_id": "conversation-under-review"}
    assert EntryEditRequest.model_validate({**body, "context": context}).context.kind == "chat"
    with pytest.raises(ValidationError):
        PublicEntryEditRequest.model_validate({**body, "context": context})
    PublicEntryEditRequest.model_validate(body)


def test_feedback_work_source_is_nullable_without_evidence() -> None:
    from cognis.api.chat_v2.feedback_evidence import bind_feedback_diff
    from cognis.api.chat_v2.schemas import FileDiffRef, SourceRef

    diff = FileDiffRef(path="src/controller.py", diff="+change\n")
    assert diff.feedback_source is None
    source = SourceRef(
        store="intaris",
        session_id="session-under-review",
        seq=42,
        event_type="tool_result",
    )
    bound = bind_feedback_diff(
        diff,
        activity_scope_id="activity-under-review",
        source=source,
        source_item_id="tool-call-under-review",
        file_ordinal=0,
    )
    assert bound.feedback_source is not None
    assert bound.feedback_source.source_event.seq == 42
    assert (
        bind_feedback_diff(
            diff.model_copy(update={"truncated": True}),
            activity_scope_id="activity-under-review",
            source=source,
            source_item_id="tool-call-under-review",
            file_ordinal=0,
        ).feedback_source
        is None
    )


def test_in_chat_preflight_cannot_route_to_another_destination() -> None:
    value = json.loads((FIXTURES / "inputs.json").read_text())["preflight"]
    value["context"] = {"kind": "chat", "conversation_id": "conversation-under-review"}
    with pytest.raises(ValidationError, match="originating composer"):
        FeedbackPreflightRequest.model_validate(value)


def test_html_geometry_requires_observed_capture() -> None:
    with pytest.raises(ValidationError, match="confirmed observed capture"):
        HtmlLocator.model_validate(
            {
                "kind": "html",
                "viewport": {
                    "width_css_px": 1024,
                    "height_css_px": 768,
                    "scroll_x_css_px": 0,
                    "scroll_y_css_px": 0,
                    "device_pixel_ratio": 1,
                },
                "render_state_digest": "a" * 64,
                "shapes": [{"kind": "pin", "point": {"x": 1, "y": 1}}],
            }
        )


def test_nonfinite_coordinates_and_reversed_ranges_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ImageLocator.model_validate(
            {
                "kind": "image",
                "intrinsic_width": 10,
                "intrinsic_height": 20,
                "orientation": 1,
                "shapes": [{"kind": "pin", "point": {"x": float("nan"), "y": 0}}],
            }
        )
    with pytest.raises(ValidationError, match="text range"):
        TextRange(
            representation="feedback-text-v1",
            text_digest="b" * 64,
            start=9,
            end=8,
            exact="selection",
        )


def test_comment_digest_is_self_contained_and_needs_no_tool_call() -> None:
    """Agents receive each comment's text, author and exact target without calling a tool."""
    from types import SimpleNamespace

    from cognis.core.feedback.submissions import Submissions
    from cognis.models import feedback as dto

    digest = "a" * 64
    image = {"kind": "document", "content_ref": "art_image"}
    report = {"kind": "document", "content_ref": "dlv_report"}

    def entry(index: int, source: dict, locator: dict, body: str) -> dict:
        return {
            "entry": {
                "entry_id": f"fbe_{index}",
                "entry_version_id": f"fbv_{index}",
                "version": 1,
                "thread_id": f"fbt_{index}",
                "body": body,
                "author": {"kind": "verified_user", "label": "Filip"},
                "audience": "owner",
                "created_at": "2026-11-01T08:00:00Z",
            },
            "anchor": {
                "anchor_id": f"fba_{index}",
                "source": source,
                "content_digest": digest,
                "locator": locator,
            },
            "role": "instruction",
        }

    text_range = {
        "representation": "feedback-text-v1",
        "text_digest": digest,
        "start": 10,
        "end": 15,
        "exact": "0.276",
        "prefix": "CPU before ",
        "suffix": " after 0.301",
    }
    manifest = dto.SubmissionManifest.model_validate(
        {
            "context": {"kind": "chat", "conversation_id": "conv_1"},
            "destination": {"kind": "origin_chat", "conversation_id": "conv_1"},
            "update_mode": "revise_source",
            "sources": [
                {
                    "subject_id": "fbs_1",
                    "source": image,
                    "content_digest": digest,
                    "format": "image",
                    "title": "tahak.png",
                    "revision": 2,
                    "availability": "available",
                },
                {
                    "subject_id": "fbs_2",
                    "source": report,
                    "content_digest": digest,
                    "format": "rich",
                    "title": "Report",
                    "availability": "available",
                },
            ],
            "entries": [
                entry(
                    1,
                    image,
                    {
                        "kind": "image",
                        "intrinsic_width": 1000,
                        "intrinsic_height": 500,
                        "orientation": 1,
                        "shapes": [
                            {"kind": "rectangle", "x": 200, "y": 50, "width": 400, "height": 100}
                        ],
                    },
                    "Change the title to MŮJ ASTROLOGICKÝ TAHÁK",
                ),
                entry(
                    2,
                    report,
                    {
                        "kind": "rich",
                        "block_id": "table-1",
                        "identity": "semantic",
                        "field_path": [],
                        "table_range": {
                            "representation": "rich-table-v1",
                            "scope": "cells",
                            "rows": [0, 1],
                            "column_start": 1,
                            "column_end": 2,
                        },
                    },
                    "Recheck these values",
                ),
                entry(
                    3,
                    report,
                    {"kind": "text", "range": text_range},
                    "Is this number right?",
                ),
            ],
        }
    )
    text = Submissions.comments_digest(manifest)
    assert 'artifact "tahak.png" (image), ref art_image, revision 2' in text
    assert 'deliverable "Report" (rich), ref dlv_report' in text
    assert "image 1000x500 px" in text and "20%-60% across, 10%-30% down" in text
    assert "table cells in block table-1: rows 1-2, column 2 (1-based)" in text
    assert 'text "0.276"; in context: …CPU before [0.276] after 0.301…' in text
    assert "Filip" in text and "> Change the title to MŮJ ASTROLOGICKÝ TAHÁK" in text

    notice = Submissions.notice(
        SimpleNamespace(submission_id="fbsu_1", manifest_hash=digest), manifest
    )
    assert "3 comments" in notice
    assert "feedback_submission_read" not in notice and "feedback_submission_record" not in notice
