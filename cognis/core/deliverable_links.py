"""Helpers for bounded deliverable view links."""

from __future__ import annotations

import base64
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

DEFAULT_DELIVERABLE_SHARE_TTL_SECONDS = 7 * 24 * 60 * 60


class DeliverableShareUnavailable(RuntimeError):
    """Raised when a signed public deliverable view link cannot be created."""


@dataclass(frozen=True)
class DeliverableViewLink:
    """A standalone deliverable view URL."""

    url: str
    expires_at: datetime | None = None
    public: bool = False
    stable_url: str | None = None


def private_deliverable_view_url(
    deliverable_id: str,
    *,
    base_url: str = "",
) -> str | None:
    """Return the authenticated standalone view URL when a base URL is known."""

    if not base_url.strip():
        return None
    return (
        f"{base_url.strip().rstrip('/')}/api/v1/deliverables/{quote(deliverable_id, safe='')}/view"
    )


def signed_deliverable_view_link(
    artifact_store: Any,
    deliverable_id: str,
    *,
    base_url: str = "",
    ttl_seconds: int = DEFAULT_DELIVERABLE_SHARE_TTL_SECONDS,
) -> DeliverableViewLink:
    """Create a stateless signed public standalone view link."""

    resolved_base_url = _resolve_base_url(artifact_store, base_url)
    if not resolved_base_url:
        raise DeliverableShareUnavailable("public base URL is not configured")
    secret = _signing_secret(artifact_store)
    expires_at = int(time.time()) + max(60, int(ttl_seconds))
    token = _sign_share_token(secret, deliverable_id, expires_at)
    short_url = f"{resolved_base_url}/api/v1/deliverables/s/{quote(token, safe='')}"
    return DeliverableViewLink(
        url=short_url,
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
        public=True,
        stable_url=short_url,
    )


def verify_deliverable_share_token(artifact_store: Any, token: str) -> tuple[str, int]:
    """Verify a signed public deliverable share token."""

    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        deliverable_id, expires_raw, signature = raw.rsplit(".", 2)
        expires_at = int(expires_raw)
    except Exception as exc:
        raise DeliverableShareUnavailable("share link is invalid") from exc
    if expires_at < int(time.time()):
        raise DeliverableShareUnavailable("share link has expired")
    expected = _sign_share_token(_signing_secret(artifact_store), deliverable_id, expires_at)
    if not hmac.compare_digest(expected, token):
        raise DeliverableShareUnavailable("share link is invalid")
    return deliverable_id, expires_at


def _resolve_base_url(artifact_store: Any, base_url: str) -> str:
    if base_url.strip():
        return base_url.rstrip("/")
    config = getattr(artifact_store, "_config", None)
    return str(getattr(config, "base_url", "") or "").rstrip("/")


def _signing_secret(artifact_store: Any) -> str:
    config = getattr(artifact_store, "_config", None)
    secret = str(getattr(config, "signing_secret", "") or "")
    if not secret:
        raise DeliverableShareUnavailable("deliverable sharing is not configured")
    return secret


def _sign_share_token(secret: str, deliverable_id: str, expires_at: int) -> str:
    payload = f"{deliverable_id}.{expires_at}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), "sha256").hexdigest()
    raw = f"{payload}.{signature}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
