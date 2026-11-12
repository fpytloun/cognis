"""Opaque query-bound cursors; every use still needs fresh authorization."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import asdict
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cognis.core.usage.access import AuthorizedUsageScope
from cognis.core.usage.query_service_errors import UsageQueryError


class UsageCursors:
    """Process/composition-owned encryption key, not a credential or access grant."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("Usage cursor encryption requires at least 32 bytes")
        self.secret = secret
        self.cipher = AESGCM(hashlib.sha256(b"usage-private-tokens-v2" + secret).digest())

    def fingerprint(self, query: Any, scope: AuthorizedUsageScope, kind: str) -> str:
        """Bind complete selection, actor and immutable trusted invocation scope."""
        payload = {
            "query": query.model_dump(mode="json", exclude={"cursor", "limit"}),
            "scope": asdict(scope),
            "kind": kind,
        }
        value = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
        return hashlib.sha256(value).hexdigest()

    def encode(self, fingerprint: str, values: list[Any]) -> str:
        """Cursor data is content-free and authenticated, never reused as authority."""
        payload = json.dumps(
            [fingerprint, values, int(time.time()) + 3600], separators=(",", ":")
        ).encode()
        nonce = secrets.token_bytes(12)
        return "v2." + base64.urlsafe_b64encode(
            nonce + self.cipher.encrypt(nonce, payload, b"usage-v2")
        ).decode().rstrip("=")

    def decode(self, token: str | None, fingerprint: str) -> list[Any] | None:
        """Fail closed for stale/wrong query identity before issuing page SQL."""
        if token is None:
            return None
        try:
            if not token.startswith("v2."):
                raise ValueError
            encoded = token[3:]
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != encoded:
                raise ValueError
            payload = self.cipher.decrypt(decoded[:12], decoded[12:], b"usage-v2")
            actual, values, expiry = json.loads(payload)
            if actual != fingerprint or not isinstance(values, list) or expiry <= time.time():
                raise ValueError
            return values
        except (ValueError, TypeError, UnicodeError, InvalidTag) as exc:
            raise UsageQueryError(
                "invalid_usage_cursor", "Cursor does not match the authorized query"
            ) from exc
