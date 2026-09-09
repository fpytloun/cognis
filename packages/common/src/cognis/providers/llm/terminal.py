"""Typed terminal metadata shared across provider and executor boundaries."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class ResponseIncompleteDetails(TypedDict, total=False):
    """Provider details for an incomplete Responses request."""

    reason: str


class LLMStreamTerminal(TypedDict):
    """Terminal fields that survive provider normalization and executor RPC."""

    usage: dict[str, Any]
    finish_reason: str
    response_status: str
    done: NotRequired[bool]
    response_incomplete_details: NotRequired[ResponseIncompleteDetails]
    error: NotRequired[str | None]
    backend_metadata: NotRequired[dict[str, Any] | None]


def response_incomplete_details(value: Any) -> ResponseIncompleteDetails | None:
    """Return a safe incomplete-details mapping when the provider supplied one."""

    if not isinstance(value, dict):
        return None
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    return {"reason": reason}
