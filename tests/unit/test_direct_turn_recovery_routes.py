from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from cognis.api.common import AuthenticatedUser
from cognis.api.models import ResolveAmbiguousDirectTurnRequest
from cognis.api.routes import system


def _request(*, role: str = "admin", wake: AsyncMock | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="admin@example.com", role=role)),
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=object(),
                turn_scheduler=SimpleNamespace(wake_direct_turn_runtime=wake or AsyncMock()),
            )
        ),
    )


@pytest.mark.asyncio
async def test_stale_list_is_admin_only_bounded_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(
        request_id="dtr-1",
        conversation_id="conv-1",
        owner_controller_id="controller-a",
        owner_incarnation_id="boot-a",
        fencing_token=7,
        status="running",
        outcome={
            "phase": "tool_in_flight",
            "call_id": "call-1",
            "phase_started_at": now.isoformat(),
            "tool_args": {"credential": "must-not-leak"},
            "tool_result": "secret result",
        },
        payload={"content": "secret payload"},
        created_at=now,
        updated_at=now,
        started_at=now,
        admission_order=4,
    )
    store = SimpleNamespace(list_stale_active_page=AsyncMock(return_value=([row], False)))
    monkeypatch.setattr(system, "DirectTurnStore", lambda _: store)

    with pytest.raises(HTTPException) as exc:
        await system.list_stale_direct_turns(_request(role="user"))
    assert exc.value.status_code == 403

    result = await system.list_stale_direct_turns(_request(), limit=100, cursor=None)
    encoded = str(result)
    assert result["has_more"] is False
    assert result["items"][0]["call_id"] == "call-1"
    assert "secret payload" not in encoded
    assert "secret result" not in encoded
    assert "must-not-leak" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_fails", [False, True])
async def test_recovery_wakes_only_after_changed_commit_and_tolerates_failure(
    monkeypatch: pytest.MonkeyPatch,
    wake_fails: bool,
) -> None:
    wake = AsyncMock(side_effect=RuntimeError("wake failed") if wake_fails else None)
    result = SimpleNamespace(
        request_id="dtr-1",
        conversation_id="conv-1",
        status="ambiguous",
        phase="ambiguous",
        fencing_token=8,
        changed=True,
    )
    store = SimpleNamespace(resolve_stale_tool_ambiguous=AsyncMock(return_value=result))
    monkeypatch.setattr(system, "DirectTurnStore", lambda _: store)
    now = datetime.now(UTC)
    payload = ResolveAmbiguousDirectTurnRequest(
        reason="dead controller",
        client_transaction_id="txn-1",
        conversation_id="conv-1",
        status="running",
        phase="tool_in_flight",
        owner_controller_id="controller-a",
        owner_incarnation_id="boot-a",
        fencing_token=7,
        updated_at=now,
    )

    response = await system.resolve_ambiguous_direct_turn(
        _request(wake=wake),
        "dtr-1",
        payload,
    )

    assert response.changed is True
    store.resolve_stale_tool_ambiguous.assert_awaited_once()
    wake.assert_awaited_once()
