"""Primary admission identity stays authoritative across runtime projection."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2.schemas import RuntimeActiveTurn, RuntimeAuthority
from cognis.api.chat_v2.snapshot_coordinator import _hydrate_runtime_input
from cognis.api.chat_v2.sync import runtime_input_from_scheduler
from cognis.core.turn_scheduler import TurnScheduler


def _row(*, system_initiated: bool = False) -> SimpleNamespace:
    timestamp = datetime.fromisoformat("2026-10-24T19:00:00+00:00")
    return SimpleNamespace(
        request_id="request-primary",
        turn_id="turn-primary",
        session_id="session-primary",
        status="running",
        payload={
            "content": "",
            "metadata": {
                "client_message_id": "client-primary",
                "system_initiated": system_initiated,
            },
        },
        outcome={},
        created_at=timestamp,
        updated_at=timestamp,
        started_at=timestamp,
        cancel_requested_at=None,
        next_attempt_at=None,
    )


def test_durable_primary_identity_comes_from_the_admitted_request() -> None:
    runtime = TurnScheduler._durable_runtime_state_from_row(_row(), None)
    assert runtime["primary_client_message_id"] == "client-primary"
    assert RuntimeActiveTurn.model_validate(runtime).primary_client_message_id == "client-primary"
    runtime = TurnScheduler._durable_runtime_state_from_row(_row(system_initiated=True), None)
    assert runtime["primary_client_message_id"] is None


@pytest.mark.anyio
async def test_snapshot_and_relay_keep_the_durable_primary_identity() -> None:
    row = _row()
    authority = RuntimeAuthority(
        direct_request_id=row.request_id,
        turn_id=row.turn_id,
        fencing_token=14,
        lifecycle="active",
    )
    scheduler = SimpleNamespace(
        active_turn_checkpoint=lambda _: None,
        durable_runtime_context=AsyncMock(
            return_value={
                "running": TurnScheduler._durable_runtime_state_from_row(row, None),
                "pending_user_message": TurnScheduler._durable_pending_user_message(row),
                "authority": authority,
            }
        ),
    )
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-primary",
        active_session_id=row.session_id,
        turn_scheduler=scheduler,
    )
    assert runtime.active_turn["primary_client_message_id"] == "client-primary"
    envelope = SimpleNamespace(
        active_turn=RuntimeActiveTurn(
            turn_id=row.turn_id, session_id=row.session_id, status="running"
        ),
        authority=authority,
        volatile_items=[],
        volatile_items_complete=True,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            chat_v2_runtime_relay=SimpleNamespace(
                hydrate_latest_authority=AsyncMock(return_value=envelope),
            )
        )
    )
    hydrated = await _hydrate_runtime_input(app, "conversation-primary", runtime)
    assert hydrated.active_turn["primary_client_message_id"] == "client-primary"
