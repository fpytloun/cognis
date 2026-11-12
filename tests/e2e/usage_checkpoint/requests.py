"""Construct checkpoint requests through the owning API contract."""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import ValidationError

from cognis.core.usage.query_models import UsageAttemptQuery, UsageQuery


def grouped_query(scope: dict[str, str], start: str, end: str) -> dict[str, Any]:
    """Validate the real scope preflight before serializing its HTTP payload."""
    return UsageQuery.model_validate(
        {
            "scope": scope,
            "start": start,
            "end": end,
            "bucket": "day",
            "group_by": "provider",
        }
    ).model_dump(mode="json", exclude_none=True)


def validate_actor_request(payload: dict[str, Any]) -> None:
    """Validate the actual retained request without altering or decoding its ref."""
    query = UsageAttemptQuery.model_validate(payload)
    assert query.scope.mode == "personal" and query.scope.project_id is None
    assert query.selection_ref, "Actor comparison requires the original protected selection"


if __name__ == "__main__":
    try:
        validate_actor_request(json.load(sys.stdin))
    except (ValidationError, AssertionError, ValueError):
        # Never echo private selector inputs through subprocess diagnostics.
        raise SystemExit("Actor request violates the canonical personal-scope contract") from None
    print(json.dumps({"scope": "personal", "canonical_request_valid": True}))
