from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cognis.core import tool_result_settlement
from cognis.core.context import events_to_messages
from cognis.core.tool_result_settlement import (
    CanonicalToolHistoryError,
    append_tool_result_once,
    canonical_tool_continuation_events,
    restore_missing_tool_call,
)
from cognis.models.session import EventAppendResult, EventReadResult, SessionEvent


class _Guardrails:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.record_count = 0
        self.idempotency_keys: list[str] = []
        self.read_requests: list[dict[str, Any]] = []
        self._append_results: dict[str, EventAppendResult] = {}

    async def read_events(self, **kwargs: Any) -> EventReadResult:
        self.read_requests.append(dict(kwargs))
        seqs = kwargs.get("seqs")
        events = (
            [event for event in self.events if event.get("seq") in seqs]
            if isinstance(seqs, list)
            else list(self.events)
        )
        return EventReadResult(events=events, last_seq=len(self.events), has_more=False)

    async def record_events(
        self,
        *,
        events: list[SessionEvent],
        idempotency_key: str,
        **_: Any,
    ) -> EventAppendResult:
        self.record_count += 1
        self.idempotency_keys.append(idempotency_key)
        existing = self._append_results.get(idempotency_key)
        if existing is not None:
            return existing
        for event in events:
            self.events.append(
                {"seq": len(self.events) + 1, "type": event.type, "data": event.data}
            )
        result = EventAppendResult(
            ok=True,
            count=len(events),
            first_seq=len(self.events),
            last_seq=len(self.events),
        )
        self._append_results[idempotency_key] = result
        return result


@pytest.mark.asyncio
async def test_canonical_reader_preserves_domain_error_for_stalled_pagination() -> None:
    class _StalledGuardrails:
        async def read_events(self, **_: Any) -> EventReadResult:
            return EventReadResult(
                events=[{"seq": "invalid", "type": "tool_call", "data": {}}],
                last_seq=1201,
                has_more=True,
            )

    with pytest.raises(CanonicalToolHistoryError, match="canonical event pagination"):
        await tool_result_settlement.read_canonical_tool_pairs(
            _StalledGuardrails(),
            session_id="session-1",
            turn_id="turn-1",
            tool_names={"call-1": "bash"},
            allow_missing_calls=True,
        )


def _tool_call() -> dict[str, Any]:
    return {
        "seq": 1,
        "type": "tool_call",
        "data": {
            "call_id": "call-1",
            "name": "bash",
            "turn_id": "turn-1",
            "arguments": '{"command":"pwd"}',
        },
    }


def _tool_call_event() -> SessionEvent:
    return SessionEvent(
        type="tool_call",
        data={
            "call_id": "call-1",
            "name": "bash",
            "turn_id": "turn-1",
            "arguments": '{"command":"pwd"}',
        },
    )


def _result(output: str) -> SessionEvent:
    return SessionEvent(
        type="tool_result",
        data={
            "call_id": "call-1",
            "name": "bash",
            "turn_id": "turn-1",
            "result": output,
            "is_error": True,
        },
    )


def test_canonical_continuation_projects_without_interrupting_sibling_call() -> None:
    continuation = canonical_tool_continuation_events(
        session_id="session-1",
        call_event=_tool_call_event(),
        result_event=_result("recovered"),
    )
    sibling_call = {
        "seq": 1,
        "type": "tool_call",
        "data": {
            "call_id": "call-sibling",
            "name": "read",
            "turn_id": "turn-1",
            "arguments": {"file_path": "/tmp/state"},
        },
    }
    projected = events_to_messages(
        [
            sibling_call,
            *[
                {"seq": index, "type": event.type, "data": event.data}
                for index, event in enumerate(continuation, start=2)
            ],
            {
                "seq": 4,
                "type": "tool_result",
                "data": {
                    "call_id": "call-sibling",
                    "name": "read",
                    "turn_id": "turn-1",
                    "result": "state",
                    "is_error": False,
                },
            },
        ]
    )

    tool_results = {
        message["tool_call_id"]: message["content"]
        for message in projected
        if message.get("role") == "tool"
    }
    assert tool_results["call-sibling"] == "state"
    assert tool_results[continuation[0].data["call_id"]] == "recovered"


def _real_result(output: str) -> SessionEvent:
    return SessionEvent(
        type="tool_result",
        data={
            "call_id": "call-1",
            "name": "bash",
            "turn_id": "turn-1",
            "result": output,
            "is_error": False,
        },
    )


@pytest.mark.asyncio
async def test_append_tool_result_once_real_result_wins_synthetic_race() -> None:
    guardrails = _Guardrails([_tool_call()])

    first = await append_tool_result_once(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_real_result("real output"),
        targeted_evidence=True,
    )
    second = await append_tool_result_once(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_result("synthetic interrupted result"),
        targeted_evidence=True,
    )

    assert first.appended is True
    assert second.appended is False
    assert guardrails.record_count == 2
    assert guardrails.idempotency_keys == [
        "session-1:turn:turn-1:tool-result:call-1",
        "session-1:turn:turn-1:tool-result:call-1",
    ]
    assert second.event["data"]["result"] == "real output"
    assert second.event["data"]["is_error"] is False
    assert all(request.get("seqs") == [2] for request in guardrails.read_requests)


@pytest.mark.asyncio
async def test_append_tool_result_once_consumes_existing_real_result() -> None:
    real_result = {
        "seq": 2,
        "type": "tool_result",
        "data": {
            "call_id": "call-1",
            "name": "bash",
            "turn_id": "turn-1",
            "result": "real output",
            "is_error": False,
        },
    }
    guardrails = _Guardrails([_tool_call(), real_result])

    settlement = await append_tool_result_once(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_result("synthetic"),
    )

    assert settlement.appended is False
    assert settlement.event == real_result
    assert guardrails.record_count == 0


@pytest.mark.asyncio
async def test_append_tool_result_once_ignores_same_call_id_from_other_turn() -> None:
    other_turn = {
        "seq": 1,
        "type": "tool_call",
        "data": {"call_id": "call-1", "name": "bash", "turn_id": "turn-old"},
    }
    guardrails = _Guardrails([other_turn, _tool_call()])

    settlement = await append_tool_result_once(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_result("interrupted"),
    )

    assert settlement.appended is True
    assert guardrails.record_count == 1


@pytest.mark.asyncio
async def test_live_tool_result_uses_targeted_evidence_with_long_history() -> None:
    history = [
        {
            "seq": seq,
            "type": "assistant_message",
            "data": {"turn_id": f"turn-{seq}", "content": "history"},
        }
        for seq in range(1, 2_001)
    ]
    history.append(
        {
            "seq": 2_001,
            "type": "tool_call",
            "data": dict(_tool_call_event().data),
        }
    )
    guardrails = _Guardrails(history)

    settlement = await append_tool_result_once(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_result("live output"),
        targeted_evidence=True,
        tool_call_event=_tool_call_event(),
        tool_call_seq=2_001,
    )

    assert settlement.appended is True
    assert guardrails.read_requests == [
        {"session_id": "session-1", "after_seq": 2_000, "limit": 500},
        {"session_id": "session-1", "seqs": [2_002], "limit": 1},
        {"session_id": "session-1", "after_seq": 2_000, "limit": 500},
    ]


@pytest.mark.asyncio
async def test_canonical_reader_uses_last_returned_event_as_page_cursor() -> None:
    class _PaginatedGuardrails(_Guardrails):
        async def read_events(self, **kwargs: Any) -> EventReadResult:
            self.read_requests.append(dict(kwargs))
            after_seq = int(kwargs.get("after_seq") or 0)
            limit = int(kwargs.get("limit") or 0)
            page = [event for event in self.events if int(event.get("seq") or 0) > after_seq]
            returned = page[:limit] if limit else page
            return EventReadResult(
                events=returned,
                last_seq=int(self.events[-1]["seq"]),
                has_more=len(page) > len(returned),
            )

    history = [
        {
            "seq": seq,
            "type": "assistant_message",
            "data": {"turn_id": f"turn-{seq}", "content": "history"},
        }
        for seq in range(1, 1_001)
    ]
    repaired_call = {**_tool_call(), "seq": 1_001}
    guardrails = _PaginatedGuardrails([*history, repaired_call])

    call, result = await tool_result_settlement.read_canonical_tool_pair(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
    )

    assert call == repaired_call
    assert result is None
    assert [request["after_seq"] for request in guardrails.read_requests] == [0, 500, 1_000]


@pytest.mark.asyncio
async def test_live_tool_result_waits_for_temporarily_missing_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingCallDuringResultAppendGuardrails(_Guardrails):
        hide_full_reads = 2

        async def read_events(self, **kwargs: Any) -> EventReadResult:
            result = await super().read_events(**kwargs)
            if kwargs.get("seqs") is None and self.hide_full_reads:
                self.hide_full_reads -= 1
                visible = [event for event in result.events if event.get("type") == "tool_result"]
                return EventReadResult(
                    events=visible,
                    last_seq=result.last_seq,
                    has_more=False,
                )
            return result

    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0, 0.0))
    guardrails = _MissingCallDuringResultAppendGuardrails([_tool_call()])

    settlement = await append_tool_result_once(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_result("live output"),
        targeted_evidence=True,
        tool_call_event=_tool_call_event(),
        tool_call_idempotency_key="original-batch-key",
    )

    assert settlement.appended is True
    assert settlement.event["data"]["result"] == "live output"
    assert guardrails.idempotency_keys == [
        "session-1:turn:turn-1:tool-result:call-1",
    ]
    assert any(request.get("seqs") is None for request in guardrails.read_requests)
    assert [event["type"] for event in guardrails.events] == ["tool_call", "tool_result"]


@pytest.mark.asyncio
async def test_live_tool_result_visibility_wait_honors_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HiddenCanonicalHistoryGuardrails(_Guardrails):
        async def read_events(self, **kwargs: Any) -> EventReadResult:
            result = await super().read_events(**kwargs)
            if kwargs.get("seqs") is None:
                return EventReadResult(events=[], last_seq=result.last_seq, has_more=False)
            return result

    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0, 60.0))
    guardrails = _HiddenCanonicalHistoryGuardrails([_tool_call()])
    cancel_event = asyncio.Event()

    task = asyncio.create_task(
        append_tool_result_once(
            guardrails,
            session_id="session-1",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="bash",
            event=_result("live output"),
            targeted_evidence=True,
            tool_call_event=_tool_call_event(),
            tool_call_idempotency_key="original-batch-key",
            cancel_event=cancel_event,
        )
    )
    await asyncio.sleep(0)
    cancel_event.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert guardrails.idempotency_keys == []


@pytest.mark.asyncio
async def test_restore_missing_tool_call_does_not_replay_after_cancellation() -> None:
    guardrails = _Guardrails([])
    cancel_event = asyncio.Event()
    cancel_event.set()

    with pytest.raises(asyncio.CancelledError):
        await restore_missing_tool_call(
            guardrails,
            session_id="session-1",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="bash",
            event=_tool_call_event(),
            original_idempotency_key="original-batch-key",
            cancel_event=cancel_event,
        )

    assert guardrails.idempotency_keys == []


@pytest.mark.asyncio
async def test_restore_missing_tool_call_reuses_the_checkpointed_event() -> None:
    guardrails = _Guardrails([])

    await restore_missing_tool_call(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_tool_call_event(),
        original_idempotency_key="original-batch-key",
    )

    assert guardrails.record_count == 1
    assert guardrails.idempotency_keys == ["session-1:turn:turn-1:tool-call-repair:call-1"]
    assert [event["type"] for event in guardrails.events] == ["tool_call"]
    assert guardrails.read_requests[-1] == {
        "session_id": "session-1",
        "seqs": [1],
        "limit": 1,
    }


@pytest.mark.asyncio
async def test_restore_missing_tool_call_rejects_conflicting_canonical_snapshot() -> None:
    conflicting = {
        **_tool_call(),
        "data": {**_tool_call()["data"], "arguments": '{"command":"whoami"}'},
    }
    guardrails = _Guardrails([conflicting])

    with pytest.raises(CanonicalToolHistoryError, match="durable recovery snapshot"):
        await restore_missing_tool_call(
            guardrails,
            session_id="session-1",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="bash",
            event=_tool_call_event(),
            original_idempotency_key="original-batch-key",
        )

    assert guardrails.idempotency_keys == []


@pytest.mark.asyncio
async def test_restore_missing_tool_call_repairs_after_original_ack_lost_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LostOriginalAppendGuardrails(_Guardrails):
        async def record_events(
            self,
            *,
            events: list[SessionEvent],
            idempotency_key: str,
            **kwargs: Any,
        ) -> EventAppendResult:
            if idempotency_key == "original-batch-key":
                self.record_count += 1
                self.idempotency_keys.append(idempotency_key)
                return EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1)
            return await super().record_events(
                events=events,
                idempotency_key=idempotency_key,
                **kwargs,
            )

    guardrails = _LostOriginalAppendGuardrails([])
    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0,))

    await restore_missing_tool_call(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_tool_call_event(),
        original_idempotency_key="original-batch-key",
    )

    assert guardrails.idempotency_keys == ["session-1:turn:turn-1:tool-call-repair:call-1"]
    assert [event["type"] for event in guardrails.events] == ["tool_call"]


@pytest.mark.asyncio
async def test_restore_missing_tool_call_reconciles_stale_repair_ack_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StaleRepairReplayGuardrails(_Guardrails):
        async def record_events(
            self,
            *,
            events: list[SessionEvent],
            idempotency_key: str,
            **kwargs: Any,
        ) -> EventAppendResult:
            if idempotency_key == "session-1:turn:turn-1:tool-call-repair:call-1":
                self.record_count += 1
                self.idempotency_keys.append(idempotency_key)
                # Simulate Intaris replaying an acknowledged append whose
                # buffered event was lost when Intaris restarted.
                return EventAppendResult(ok=True, count=1, first_seq=7, last_seq=7)
            return await super().record_events(
                events=events,
                idempotency_key=idempotency_key,
                **kwargs,
            )

    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0,))
    guardrails = _StaleRepairReplayGuardrails([])

    await restore_missing_tool_call(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_tool_call_event(),
        original_idempotency_key=None,
    )

    assert guardrails.idempotency_keys == [
        "session-1:turn:turn-1:tool-call-repair:call-1",
        "session-1:turn:turn-1:tool-call-repair-reconcile:call-1:7",
    ]
    assert [event["type"] for event in guardrails.events] == ["tool_call"]


@pytest.mark.asyncio
async def test_restore_missing_tool_call_avoids_legacy_original_key_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OriginalKeyConflictGuardrails(_Guardrails):
        async def record_events(
            self,
            *,
            events: list[SessionEvent],
            idempotency_key: str,
            **kwargs: Any,
        ) -> EventAppendResult:
            if idempotency_key == "original-batch-key":
                self.record_count += 1
                self.idempotency_keys.append(idempotency_key)
                raise RuntimeError("idempotency key payload conflict")
            return await super().record_events(
                events=events,
                idempotency_key=idempotency_key,
                **kwargs,
            )

    guardrails = _OriginalKeyConflictGuardrails([])
    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0,))

    await restore_missing_tool_call(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_tool_call_event(),
        original_idempotency_key="original-batch-key",
    )

    assert guardrails.idempotency_keys == ["session-1:turn:turn-1:tool-call-repair:call-1"]
    assert [event["type"] for event in guardrails.events] == ["tool_call"]


@pytest.mark.asyncio
async def test_restore_missing_tool_call_waits_for_event_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _EventuallyConsistentGuardrails(_Guardrails):
        hidden_reads = 2

        async def read_events(self, **kwargs: Any) -> EventReadResult:
            result = await super().read_events(**kwargs)
            if self.events and self.hidden_reads:
                self.hidden_reads -= 1
                return EventReadResult(events=[], last_seq=len(self.events), has_more=False)
            return result

    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0, 0.0, 0.0))
    guardrails = _EventuallyConsistentGuardrails([_tool_call()])

    await restore_missing_tool_call(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_tool_call_event(),
        original_idempotency_key=None,
    )

    assert guardrails.idempotency_keys == []
    assert [event["type"] for event in guardrails.events] == ["tool_call"]


@pytest.mark.asyncio
async def test_restore_missing_legacy_tool_call_uses_deterministic_repair_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0,))
    guardrails = _Guardrails([])

    await restore_missing_tool_call(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_tool_call_event(),
        original_idempotency_key=None,
    )

    assert guardrails.idempotency_keys == ["session-1:turn:turn-1:tool-call-repair:call-1"]
    assert [event["type"] for event in guardrails.events] == ["tool_call"]


@pytest.mark.asyncio
async def test_restore_missing_tool_call_waits_for_original_batch_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DelayedOriginalBatchGuardrails(_Guardrails):
        hidden_reads = 2

        async def read_events(self, **kwargs: Any) -> EventReadResult:
            result = await super().read_events(**kwargs)
            if self.events and self.hidden_reads:
                self.hidden_reads -= 1
                return EventReadResult(events=[], last_seq=len(self.events), has_more=False)
            return result

    monkeypatch.setattr(tool_result_settlement, "_TOOL_CALL_REPAIR_READ_DELAYS", (0.0, 0.0, 0.0))
    guardrails = _DelayedOriginalBatchGuardrails([])

    await restore_missing_tool_call(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
        event=_tool_call_event(),
        original_idempotency_key="original-batch-key",
    )

    assert guardrails.idempotency_keys == ["session-1:turn:turn-1:tool-call-repair:call-1"]
    assert [event["type"] for event in guardrails.events] == ["tool_call"]


@pytest.mark.asyncio
async def test_canonical_reader_accepts_identical_duplicate_tool_calls() -> None:
    duplicate = {**_tool_call(), "seq": 2}
    guardrails = _Guardrails([_tool_call(), duplicate])

    call, result = await tool_result_settlement.read_canonical_tool_pair(
        guardrails,
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="bash",
    )

    assert call["seq"] == 1
    assert result is None


@pytest.mark.asyncio
async def test_canonical_reader_rejects_conflicting_duplicate_tool_calls() -> None:
    duplicate = {
        **_tool_call(),
        "seq": 2,
        "data": {**_tool_call()["data"], "arguments": '{"command":"whoami"}'},
    }
    guardrails = _Guardrails([_tool_call(), duplicate])

    with pytest.raises(CanonicalToolHistoryError, match="conflicting duplicate"):
        await tool_result_settlement.read_canonical_tool_pair(
            guardrails,
            session_id="session-1",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="bash",
        )
