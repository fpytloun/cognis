"""Keep real-app preflight payloads aligned with the canonical query model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cognis.core.usage.query_models import UsageAttemptQuery, UsageQuery
from tests.e2e.usage_checkpoint.requests import grouped_query, validate_actor_request
from tests.e2e.usage_checkpoint.seed import PROJECT, SESSION


@pytest.mark.parametrize(
    "scope",
    [
        {"mode": "personal"},
        {"mode": "admin"},
        {"mode": "project", "project_id": PROJECT},
    ],
)
def test_grouped_preflight_uses_canonical_query_contract(scope: dict[str, str]) -> None:
    payload = grouped_query(scope, "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")
    query = UsageQuery.model_validate(payload)
    assert query.scope.mode == scope["mode"]
    assert query.scope.project_id == scope.get("project_id")
    assert payload["group_by"] == "provider"
    assert payload["bucket"] == "day"
    assert "resolution" not in payload


def test_actor_request_validation_preserves_the_original_personal_payload() -> None:
    # This checks DTO shape only; the real browser supplies its actual opaque ref.
    payload = {
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-03T00:00:00Z",
        "selection_ref": SESSION,
    }
    original = payload.copy()
    assert UsageAttemptQuery.model_validate(payload).scope.mode == "personal"
    validate_actor_request(payload)
    assert payload == original


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"scope": {"mode": "admin"}}, AssertionError),
        ({"scope": {"mode": "project", "project_id": PROJECT}}, AssertionError),
        ({"selection_ref": None}, AssertionError),
        ({"unexpected": True}, ValidationError),
    ],
)
def test_actor_request_preflight_rejects_contract_or_scope_drift(
    override: dict, expected: type[Exception]
) -> None:
    payload = {
        "scope": {"mode": "personal"},
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-03T00:00:00Z",
        "selection_ref": SESSION,
        **override,
    }
    with pytest.raises(expected):
        validate_actor_request(payload)
