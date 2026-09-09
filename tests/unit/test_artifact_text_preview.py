from __future__ import annotations

import pytest
from fastapi import HTTPException

from cognis.api.routes.artifacts import TEXT_PREVIEW_MAX_BYTES, _text_preview_payload


def test_text_preview_accepts_json_with_generic_mime_type() -> None:
    payload = _text_preview_payload(
        filename="backup.json",
        content_type="application/octet-stream",
        content=b'{"enabled": true}',
    )

    assert payload["content"] == '{"enabled": true}'
    assert payload["truncated"] is False


def test_text_preview_truncates_large_content() -> None:
    payload = _text_preview_payload(
        filename="events.log",
        content_type="text/plain",
        content=b"a" * (TEXT_PREVIEW_MAX_BYTES + 1),
    )

    assert len(payload["content"]) == TEXT_PREVIEW_MAX_BYTES
    assert payload["truncated"] is True


def test_text_preview_does_not_split_utf8_character_at_limit() -> None:
    content = b"a" * (TEXT_PREVIEW_MAX_BYTES - 1) + "\N{EURO SIGN}".encode() + b"tail"

    payload = _text_preview_payload(
        filename="events.log",
        content_type="text/plain",
        content=content,
    )

    assert payload["content"] == "a" * (TEXT_PREVIEW_MAX_BYTES - 1)
    assert payload["truncated"] is True


@pytest.mark.parametrize(
    ("filename", "content_type", "content"),
    [
        ("archive.zip", "application/zip", b"binary"),
        ("fake.txt", "text/plain", b"text\x00binary"),
        ("invalid.txt", "text/plain", b"\xff"),
    ],
)
def test_text_preview_rejects_unsupported_or_binary_content(
    filename: str, content_type: str, content: bytes
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _text_preview_payload(
            filename=filename,
            content_type=content_type,
            content=content,
        )

    assert exc_info.value.status_code == 415
