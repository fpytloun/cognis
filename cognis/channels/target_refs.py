"""Authenticated opaque references for channel accounts and observed targets.

References expire after the configured TTL. Rotating the application artifact
signing secret invalidates all outstanding references. The codec derives an
independent AES-256-GCM key, so channel references do not reuse the signing key
directly or reveal provider routing identifiers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_AAD = b"cognis/channel-target-ref/v1"
_CURSOR_AAD = b"cognis/channel-message-cursor/v1"
_KEY_INFO = b"cognis/channel-target-ref/aes-256-gcm/v1"


class ChannelTargetRefError(ValueError):
    """Raised when an opaque channel reference is invalid or unusable."""


@dataclass(frozen=True, slots=True)
class ChannelTargetRef:
    kind: Literal["account", "target", "transcript"]
    user_email: str
    account_id: str
    channel_type: str
    chat_id: str | None = None
    chat_kind: Literal["direct", "group"] | None = None
    thread_id: str | None = None
    sender_id: str | None = None
    scope_conversation_id: str | None = None


class ChannelTargetRefCodec:
    """Encrypt and authenticate controller-owned channel references."""

    def __init__(self, application_secret: str | bytes, *, ttl_seconds: int = 86400) -> None:
        secret = (
            application_secret
            if isinstance(application_secret, bytes)
            else application_secret.encode("utf-8")
        )
        if not secret:
            raise ValueError("Channel target references require an application secret")
        if ttl_seconds <= 0:
            raise ValueError("Channel target reference TTL must be positive")
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=_KEY_INFO,
        ).derive(secret)
        self._ttl = timedelta(seconds=ttl_seconds)

    def encode(self, ref: ChannelTargetRef, *, now: datetime | None = None) -> str:
        issued_at = _utc(now)
        payload = {
            "v": 1,
            "kind": ref.kind,
            "user_email": ref.user_email,
            "account_id": ref.account_id,
            "channel_type": ref.channel_type,
            "chat_id": ref.chat_id,
            "chat_kind": ref.chat_kind,
            "thread_id": ref.thread_id,
            "sender_id": ref.sender_id,
            "scope_conversation_id": ref.scope_conversation_id,
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + self._ttl).timestamp()),
        }
        plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, plaintext, _AAD)
        return _b64encode(nonce + encrypted)

    def derive_domain_key(self, domain: bytes) -> bytes:
        """Derive a keyed subkey for a separate opaque-reference domain."""

        return hmac.new(self._key, domain, hashlib.sha256).digest()

    def decode(
        self,
        token: str,
        *,
        user_email: str,
        expected_kind: Literal["account", "target", "transcript"],
        scope_conversation_id: str | None = None,
        now: datetime | None = None,
    ) -> ChannelTargetRef:
        try:
            raw = _b64decode(token)
            if len(raw) < 29:
                raise ChannelTargetRefError("Malformed channel target reference")
            plaintext = AESGCM(self._key).decrypt(raw[:12], raw[12:], _AAD)
            payload: Any = json.loads(plaintext)
        except (
            InvalidTag,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as exc:
            if isinstance(exc, ChannelTargetRefError):
                raise
            raise ChannelTargetRefError("Invalid channel target reference") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ChannelTargetRefError("Unsupported channel target reference")
        if payload.get("kind") != expected_kind:
            raise ChannelTargetRefError("Channel target reference has the wrong kind")
        if payload.get("user_email") != user_email:
            raise ChannelTargetRefError("Channel target reference is not available")
        expiry = payload.get("exp")
        if not isinstance(expiry, int) or int(_utc(now).timestamp()) >= expiry:
            raise ChannelTargetRefError("Channel target reference has expired")
        account_id = payload.get("account_id")
        channel_type = payload.get("channel_type")
        chat_id = payload.get("chat_id")
        chat_kind = payload.get("chat_kind")
        thread_id = payload.get("thread_id")
        sender_id = payload.get("sender_id")
        encoded_scope_conversation_id = payload.get("scope_conversation_id")
        if not isinstance(account_id, str) or not account_id:
            raise ChannelTargetRefError("Invalid channel target reference payload")
        if not isinstance(channel_type, str) or not channel_type:
            raise ChannelTargetRefError("Invalid channel target reference payload")
        if expected_kind in {"target", "transcript"} and (
            not isinstance(chat_id, str) or not chat_id or chat_kind not in {"direct", "group"}
        ):
            raise ChannelTargetRefError("Invalid channel target reference payload")
        if expected_kind == "transcript" and (
            not isinstance(scope_conversation_id, str)
            or not scope_conversation_id
            or encoded_scope_conversation_id != scope_conversation_id
        ):
            raise ChannelTargetRefError("Channel transcript reference is not available")
        return ChannelTargetRef(
            kind=expected_kind,
            user_email=user_email,
            account_id=account_id,
            channel_type=channel_type,
            chat_id=chat_id if isinstance(chat_id, str) else None,
            chat_kind=chat_kind if chat_kind in {"direct", "group"} else None,
            thread_id=thread_id if isinstance(thread_id, str) and thread_id else None,
            sender_id=sender_id if isinstance(sender_id, str) and sender_id else None,
            scope_conversation_id=(
                encoded_scope_conversation_id
                if isinstance(encoded_scope_conversation_id, str) and encoded_scope_conversation_id
                else None
            ),
        )

    def encode_cursor(self, payload: dict[str, Any]) -> str:
        body = {"v": 1, **payload}
        nonce = os.urandom(12)
        plaintext = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        return _b64encode(nonce + AESGCM(self._key).encrypt(nonce, plaintext, _CURSOR_AAD))

    def decode_cursor(self, token: str) -> dict[str, Any]:
        try:
            raw = _b64decode(token)
            payload = json.loads(AESGCM(self._key).decrypt(raw[:12], raw[12:], _CURSOR_AAD))
        except Exception as exc:
            raise ChannelTargetRefError("Invalid channel message cursor") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ChannelTargetRefError("Unsupported channel message cursor")
        return payload


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    if not data:
        raise ChannelTargetRefError("Malformed channel target reference")
    padding = "=" * (-len(data) % 4)
    decoded = base64.urlsafe_b64decode(f"{data}{padding}".encode("ascii"))
    if _b64encode(decoded) != data:
        raise ChannelTargetRefError("Invalid channel target reference encoding")
    return decoded
