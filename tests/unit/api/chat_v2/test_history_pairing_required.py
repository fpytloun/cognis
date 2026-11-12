from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.schemas import (
    ConversationStateView,
    ConversationSummary,
    QueueState,
    TimelineScope,
)
from cognis.api.chat_v2.snapshot_coordinator import (
    ConversationSnapshotContext,
    build_chat_snapshot_coordinated,
)
from cognis.api.chat_v2.sync import (
    ChatV2SyncError,
    ConversationSessionRef,
    RuntimeOverlayInput,
    build_chat_snapshot,
    clear_chat_v2_read_caches,
)
from tests.unit.api.chat_v2.test_sync import FakeEventStore


@pytest.mark.asyncio
async def test_required_pairing_failure_cannot_reuse_best_effort_projection():
    class UnavailablePairingStore(FakeEventStore):
        pairing_failures = 0

        async def read_session_events(self, *, session_id, **kwargs):
            if kwargs.get("before_seq") == 3:
                self.pairing_failures += 1
                raise OSError("Pairing source unavailable")
            return await super().read_session_events(session_id=session_id, **kwargs)

    event = RawSessionEvent(
        store_id="intaris",
        session_id="history-pairing",
        seq=3,
        event_id="history-result",
        type="tool_result",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        data={"call_id": "history-write", "tool_name": "write_deliverable", "result": {}},
    )
    clear_chat_v2_read_caches()
    store = UnavailablePairingStore({"history-pairing": [event]})
    kwargs = dict(
        conversation=ConversationSummary(conversation_id="history-pairing", agent_id="agent-owner"),
        scope=TimelineScope(
            kind="conversation",
            key="conversation:history-pairing",
            conversation_id="history-pairing",
        ),
        state=ConversationStateView(state_version=1, snapshot_generated_at="2026-01-01T00:00:00Z"),
        session_refs=[
            ConversationSessionRef(
                session_id="history-pairing",
                event_store_session_id="history-pairing",
                ordinal=0,
            )
        ],
        event_store=store,
        cursor_secret="history-pairing-test",
        initial_read_window=1,
    )
    await build_chat_snapshot(**kwargs)
    with pytest.raises(ChatV2SyncError) as error:
        await build_chat_snapshot(**kwargs, strict_pairing=True)
    assert error.value.code == "event_store_paging_failed"

    async def process(events):
        return events

    context = ConversationSnapshotContext(
        conversation=kwargs["conversation"],
        scope=kwargs["scope"],
        state=kwargs["state"],
        session_refs=kwargs["session_refs"],
        event_store=store,
        cursor_secret=kwargs["cursor_secret"],
        queue=QueueState(queued_count=0),
        runtime_input=RuntimeOverlayInput(runtime_epoch="history-runtime", runtime_revision=0),
        session_cache=None,
        event_post_processor=process,
        owner_email="owner@example.com",
        conversation_id="history-pairing",
    )
    with pytest.raises(ChatV2SyncError) as error:
        await build_chat_snapshot_coordinated(SimpleNamespace(state=SimpleNamespace()), context)
    assert error.value.code == "event_store_paging_failed"
    assert store.pairing_failures >= 3
