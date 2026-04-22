"""Shared API helpers for auth, errors, and cursors."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from cognis.api.middleware import AuthenticatedUser
from cognis.api.models import ErrorBody, ErrorResponse


def api_exception(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    """Create a structured API exception."""
    return HTTPException(
        status_code=status_code,
        detail=ErrorBody(code=code, message=message, details=details).model_dump(),
    )


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Create a structured JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error=ErrorBody(code=code, message=message, details=details)
        ).model_dump(),
    )


def require_current_user(request: Request) -> AuthenticatedUser:
    """Return the authenticated user from request state."""
    user = getattr(request.state, "user", None)
    if not isinstance(user, AuthenticatedUser):
        raise api_exception(401, "unauthorized", "Authentication required")
    return user


def require_session_user(request: Request) -> AuthenticatedUser:
    """Require a human interactive session authenticated by cookie or bearer."""

    user = require_current_user(request)
    if user.auth_type not in {"jwt", "session"}:
        raise api_exception(403, "forbidden", "This endpoint requires session authentication")
    return user


def require_jwt_user(request: Request) -> AuthenticatedUser:
    """Backward-compatible alias for interactive session auth."""

    return require_session_user(request)


def require_admin(request: Request) -> AuthenticatedUser:
    """Require the authenticated user to be an admin."""
    user = require_current_user(request)
    if user.role != "admin":
        raise api_exception(403, "forbidden", "Admin access required")
    return user


def require_owner_or_admin(request: Request, owner_email: str) -> AuthenticatedUser:
    """Require resource ownership or admin access."""
    user = require_current_user(request)
    if user.role != "admin" and user.email != owner_email:
        raise api_exception(403, "forbidden", "Resource access denied")
    return user


def forbid_mutation_for_viewer(request: Request) -> None:
    """Reject mutation attempts from viewer accounts."""
    user = require_current_user(request)
    if user.role == "viewer":
        raise api_exception(403, "forbidden", "Viewer accounts are read-only")


def encode_cursor(payload: dict[str, Any]) -> str:
    """Encode an opaque cursor payload."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    """Decode an opaque cursor payload."""
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return cast(dict[str, Any], json.loads(base64.urlsafe_b64decode(padded).decode()))
    except Exception as exc:
        raise api_exception(400, "validation_error", "Invalid cursor value") from exc


def datetime_to_cursor(created_at: datetime | None, item_id: str) -> str | None:
    """Build a cursor from timestamp + stable identifier."""
    if created_at is None:
        return None
    return encode_cursor({"created_at": created_at.isoformat(), "id": item_id})


def paginate_items[T](
    items: list[T],
    *,
    limit: int,
    cursor: str | None,
    get_item_id: Callable[[T], str],
) -> tuple[list[T], str | None, bool]:
    """Apply a simple opaque cursor over an already ordered list."""
    start_index = 0
    payload = decode_cursor(cursor)
    if payload is not None:
        cursor_id = str(payload.get("id", ""))
        for index, item in enumerate(items):
            if get_item_id(item) == cursor_id:
                start_index = index + 1
                break

    page_slice = items[start_index : start_index + limit + 1]
    has_more = len(page_slice) > limit
    page_items = page_slice[:limit]
    next_cursor = None
    if has_more and page_items:
        next_cursor = encode_cursor({"id": get_item_id(page_items[-1])})
    return page_items, next_cursor, has_more


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    ``"Research Assistant"`` → ``"research-assistant"``
    ``"My OpenAI (custom)"`` → ``"my-openai-custom"``
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")[:64] or "unnamed"
