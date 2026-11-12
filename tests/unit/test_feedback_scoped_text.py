from __future__ import annotations

import pytest

from cognis.core.feedback.primitives import FeedbackError, digest
from cognis.core.feedback.sources import Sources, describe_text, utf16_length, utf16_slice
from cognis.models import feedback as dto


def test_rich_field_extraction_is_backend_owned_and_field_local() -> None:
    payload = {
        "blocks": [
            {"id": "intro", "body": "Repeated **text** 😀\r\nsecond line"},
            {"body": "Other field"},
        ]
    }
    scope = dto.RichFieldTextScope(
        kind="rich_field", identity="semantic", block_id="intro", field_path=["body"]
    )
    text = Sources.rich_field_text(payload, scope)
    assert text == "Repeated text 😀\nsecond line"
    descriptor = describe_text(text)
    assert descriptor.length == utf16_length(text)
    Sources.validate_range(
        text,
        dto.TextRange(
            representation="feedback-text-v1",
            text_digest=descriptor.text_digest,
            start=0,
            end=utf16_length("Repeated text 😀"),
            exact="Repeated text 😀",
        ),
    )
    with pytest.raises(FeedbackError):
        Sources.validate_range(
            text,
            dto.TextRange(
                representation="feedback-text-v1",
                text_digest=digest(b"Other field"),
                start=0,
                end=5,
                exact="Other",
            ),
        )
    assert (
        Sources.rich_field_text(
            payload,
            dto.RichFieldTextScope(
                kind="rich_field",
                identity="revision_local",
                block_id="/blocks/1",
                field_path=["body"],
            ),
        )
        == "Other field"
    )


def test_ambiguous_semantic_block_and_invalid_utf16_boundary_fail_closed() -> None:
    scope = dto.RichFieldTextScope(
        kind="rich_field", identity="semantic", block_id="duplicate", field_path=["body"]
    )
    with pytest.raises(FeedbackError, match="ambiguous"):
        Sources.rich_field_text(
            {
                "blocks": [
                    {"id": "duplicate", "body": "First"},
                    {"id": "duplicate", "body": "Second"},
                ]
            },
            scope,
        )
    with pytest.raises(FeedbackError):
        utf16_slice("😀", 0, 1)
