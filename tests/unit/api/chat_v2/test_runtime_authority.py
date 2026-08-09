"""Cluster-authoritative Chat v2 runtime overlay tests."""

from __future__ import annotations

import pytest

from cognis.api.chat_v2.sync import runtime_input_from_scheduler


class _RemoteOwnerScheduler:
    def active_turn_checkpoint(self, _conversation_id: str):
        return None

    def running_turn_state(self, _conversation_id: str):
        return None

    async def durable_running_turn_state(self, _conversation_id: str):
        return {
            "turn_id": "turn-remote",
            "session_id": "session-remote",
            "status": "running",
            "chat_mode": None,
            "chat_mode_source": None,
            "started_at": "2026-07-28T08:00:00+00:00",
            "updated_at": "2026-07-28T08:00:01+00:00",
        }


@pytest.mark.anyio
async def test_runtime_overlay_keeps_remote_durable_turn_active() -> None:
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        active_session_id="session-local",
        turn_scheduler=_RemoteOwnerScheduler(),
    )

    assert runtime.active_turn == {
        "turn_id": "turn-remote",
        "session_id": "session-remote",
        "status": "running",
        "chat_mode": None,
        "chat_mode_source": None,
        "started_at": "2026-07-28T08:00:00+00:00",
        "updated_at": "2026-07-28T08:00:01+00:00",
    }
