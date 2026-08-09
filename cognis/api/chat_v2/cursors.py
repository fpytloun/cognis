"""Signed opaque cursor helpers for Chat v2."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from cognis.api.chat_v2.schemas import StrictModel

CursorErrorCode = Literal[
    "cursor_invalid",
    "cursor_expired",
    "projection_version_changed",
    "unsupported_cursor",
]


class ChatCursorError(ValueError):
    """Raised when a Chat v2 cursor cannot be accepted."""

    def __init__(self, code: CursorErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class CursorSessionWatermark(StrictModel):
    """Per-session source watermark embedded in a cursor."""

    store: str
    session_id: str
    last_seq: int = Field(ge=0)


class CursorLineageEntry(StrictModel):
    """Ordered session-lineage entry embedded in a cursor."""

    store: str
    session_id: str
    role: str
    ordinal: int = Field(ge=0)


class InternalChatCursorPayload(StrictModel):
    """Tamper-evident internal cursor payload.

    This model is serialized and signed. Clients must treat the resulting token
    as opaque.
    """

    version: Literal[1] = 1
    scope_key: str
    conversation_id: str | None = None
    projection_version: str
    session_watermarks: list[CursorSessionWatermark] = Field(default_factory=list)
    before_positions: list[CursorSessionWatermark] = Field(default_factory=list)
    ordinal_frontiers: list[int] = Field(default_factory=list)
    lineage: list[CursorLineageEntry] = Field(default_factory=list)
    graph_fingerprint: str | None = None
    view_revision: int = Field(ge=0)
    issued_at: str
    expires_at: str | None = None


def encode_cursor(payload: InternalChatCursorPayload, secret: str | bytes) -> str:
    """Encode and sign a Chat v2 cursor payload."""

    secret_bytes = _secret_bytes(secret)
    payload_json = json.dumps(
        payload.model_dump(mode="json", exclude_none=True),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_part = _b64encode(payload_json)
    signature_part = _b64encode(_sign(payload_json, secret_bytes))
    return f"{payload_part}.{signature_part}"


def decode_cursor(token: str, secret: str | bytes) -> InternalChatCursorPayload:
    """Decode and verify a Chat v2 cursor token."""

    secret_bytes = _secret_bytes(secret)
    try:
        payload_part, signature_part = token.split(".", 1)
        payload_json = _b64decode(payload_part)
        signature = _b64decode(signature_part)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ChatCursorError("cursor_invalid", "Malformed chat cursor") from exc

    expected_signature = _sign(payload_json, secret_bytes)
    if not hmac.compare_digest(signature, expected_signature):
        raise ChatCursorError("cursor_invalid", "Invalid chat cursor signature")

    try:
        raw_payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ChatCursorError("cursor_invalid", "Invalid chat cursor payload") from exc

    if isinstance(raw_payload, dict) and raw_payload.get("version") != 1:
        raise ChatCursorError("unsupported_cursor", "Unsupported chat cursor version")

    try:
        return InternalChatCursorPayload.model_validate(raw_payload)
    except ValueError as exc:
        raise ChatCursorError("cursor_invalid", "Invalid chat cursor schema") from exc


def validate_cursor(
    token: str,
    secret: str | bytes,
    *,
    scope_key: str,
    projection_version: str,
    now: datetime | None = None,
) -> InternalChatCursorPayload:
    """Decode a cursor and validate conversation, projection, and expiry."""

    payload = decode_cursor(token, secret)
    if payload.scope_key != scope_key:
        raise ChatCursorError("cursor_invalid", "Chat cursor belongs to a different timeline scope")
    if payload.projection_version != projection_version:
        raise ChatCursorError(
            "projection_version_changed",
            "Chat cursor was issued for a different projection version",
        )
    if payload.expires_at is not None:
        expires_at = _parse_datetime(payload.expires_at)
        current_time = now or datetime.now(UTC)
        if current_time >= expires_at:
            raise ChatCursorError("cursor_expired", "Chat cursor has expired")
    return payload


def _sign(payload_json: bytes, secret: bytes) -> bytes:
    return hmac.new(secret, payload_json, hashlib.sha256).digest()


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, bytes):
        return secret
    return secret.encode("utf-8")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}".encode("ascii"))


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
