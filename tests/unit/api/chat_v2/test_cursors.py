from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from cognis.api.chat_v2.cursors import (
    ChatCursorError,
    CursorLineageEntry,
    CursorSessionWatermark,
    InternalChatCursorPayload,
    decode_cursor,
    encode_cursor,
    validate_cursor,
)


def _payload(**overrides: object) -> InternalChatCursorPayload:
    values: dict[str, object] = {
        "conversation_id": "conv-1",
        "projection_version": "chat-v2.1",
        "session_watermarks": [
            CursorSessionWatermark(store="intaris", session_id="sess-1", last_seq=42)
        ],
        "lineage": [
            CursorLineageEntry(store="intaris", session_id="sess-1", role="root", ordinal=0)
        ],
        "view_revision": 42,
        "issued_at": "2026-06-29T10:00:00Z",
    }
    values.update(overrides)
    return InternalChatCursorPayload.model_validate(values)


def _signed_raw_cursor(payload: dict[str, object], secret: str) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload_json, hashlib.sha256).digest()
    payload_part = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
    signature_part = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload_part}.{signature_part}"


def test_cursor_round_trips_signed_payload() -> None:
    token = encode_cursor(_payload(), "secret")

    decoded = decode_cursor(token, "secret")

    assert decoded.conversation_id == "conv-1"
    assert decoded.session_watermarks[0].last_seq == 42
    assert decoded.lineage[0].ordinal == 0


def test_cursor_rejects_tampering() -> None:
    token = encode_cursor(_payload(), "secret")
    payload_part, signature_part = token.split(".", 1)
    tampered = f"{payload_part[:-1]}x.{signature_part}"

    with pytest.raises(ChatCursorError) as exc_info:
        decode_cursor(tampered, "secret")

    assert exc_info.value.code == "cursor_invalid"


def test_cursor_rejects_wrong_secret() -> None:
    token = encode_cursor(_payload(), "secret")

    with pytest.raises(ChatCursorError) as exc_info:
        decode_cursor(token, "other-secret")

    assert exc_info.value.code == "cursor_invalid"


def test_cursor_rejects_malformed_base64() -> None:
    with pytest.raises(ChatCursorError) as exc_info:
        decode_cursor("not-valid-@@@.also-invalid-@@@", "secret")

    assert exc_info.value.code == "cursor_invalid"


def test_cursor_rejects_unsupported_version_with_specific_code() -> None:
    token = _signed_raw_cursor(
        {
            "version": 2,
            "conversation_id": "conv-1",
            "projection_version": "chat-v2.1",
            "session_watermarks": [],
            "lineage": [],
            "view_revision": 1,
            "issued_at": "2026-06-29T10:00:00Z",
        },
        "secret",
    )

    with pytest.raises(ChatCursorError) as exc_info:
        decode_cursor(token, "secret")

    assert exc_info.value.code == "unsupported_cursor"


def test_validate_cursor_rejects_wrong_conversation() -> None:
    token = encode_cursor(_payload(), "secret")

    with pytest.raises(ChatCursorError) as exc_info:
        validate_cursor(
            token,
            "secret",
            conversation_id="conv-2",
            projection_version="chat-v2.1",
        )

    assert exc_info.value.code == "cursor_invalid"


def test_validate_cursor_rejects_projection_version_change() -> None:
    token = encode_cursor(_payload(), "secret")

    with pytest.raises(ChatCursorError) as exc_info:
        validate_cursor(
            token,
            "secret",
            conversation_id="conv-1",
            projection_version="chat-v2.2",
        )

    assert exc_info.value.code == "projection_version_changed"


def test_validate_cursor_rejects_expired_cursor() -> None:
    now = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)
    token = encode_cursor(
        _payload(expires_at=(now - timedelta(seconds=1)).isoformat()),
        "secret",
    )

    with pytest.raises(ChatCursorError) as exc_info:
        validate_cursor(
            token,
            "secret",
            conversation_id="conv-1",
            projection_version="chat-v2.1",
            now=now,
        )

    assert exc_info.value.code == "cursor_expired"
