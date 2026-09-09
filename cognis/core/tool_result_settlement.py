"""Canonical, restart-safe tool-result settlement."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

from cognis.models.session import (
    EventAppendResult,
    EventPaginationError,
    SessionEvent,
    next_event_page_after_seq,
)

_READ_PAGE_SIZE = 500
_TOOL_CALL_REPAIR_READ_DELAYS = (0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 3.0)


class CanonicalToolHistoryError(RuntimeError):
    """Canonical tool history is missing or internally inconsistent."""


class CanonicalToolRepairUnavailable(CanonicalToolHistoryError):
    """An acknowledged repair is not readable from the physical session."""

    def __init__(
        self,
        *,
        call_event: SessionEvent,
        append_result: EventAppendResult,
        result_event: SessionEvent | None = None,
    ) -> None:
        super().__init__("canonical tool_call is missing after deterministic repair")
        self.call_event = call_event
        self.append_result = append_result
        self.result_event = result_event


def canonical_tool_continuation_events(
    *,
    session_id: str,
    call_event: SessionEvent,
    result_event: SessionEvent,
) -> list[SessionEvent]:
    """Build an adjacent synthetic pair for an unreadable original call."""

    turn_id = str(call_event.data["turn_id"])
    original_call_id = str(call_event.data["call_id"])
    digest = hashlib.sha256(f"{session_id}\0{turn_id}\0{original_call_id}".encode()).hexdigest()[
        :24
    ]
    continuation_call_id = f"call_recovery_{digest}"
    marker = {
        "canonical_recovery_boundary": True,
        "recovered_call_id": original_call_id,
    }
    generic_call_data = {
        key: call_event.data[key]
        for key in (
            "name",
            "arguments",
            "turn_id",
            "assistant_phase_index",
            "turn_cycle_index",
            "classification",
            "agent_visible",
            "view_kind",
        )
        if key in call_event.data
    }
    return [
        SessionEvent(
            type="tool_call",
            data={**generic_call_data, **marker, "call_id": continuation_call_id},
        ),
        SessionEvent(
            type="tool_result",
            data={**result_event.data, **marker, "call_id": continuation_call_id},
        ),
    ]


async def _wait_or_cancel(delay: float, cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError
    if not delay:
        return
    if cancel_event is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
    except TimeoutError:
        return
    raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class ToolResultSettlement:
    append_result: EventAppendResult
    event: dict[str, Any]
    appended: bool


def tool_result_idempotency_key(session_id: str, turn_id: str, call_id: str) -> str:
    """Return the controller-incarnation-independent result identity."""

    return f"{session_id}:turn:{turn_id}:tool-result:{call_id}"


def tool_call_idempotency_key(session_id: str, turn_id: str, call_id: str) -> str:
    """Return the controller-incarnation-independent call identity."""

    return f"{session_id}:turn:{turn_id}:tool-call:{call_id}"


def tool_call_repair_idempotency_key(session_id: str, turn_id: str, call_id: str) -> str:
    """Return the deterministic identity for one canonical repair append."""

    return f"{session_id}:turn:{turn_id}:tool-call-repair:{call_id}"


def tool_call_repair_reconciliation_idempotency_key(
    session_id: str,
    turn_id: str,
    call_id: str,
    acknowledged_seq: int,
) -> str:
    """Return a fresh identity when an acknowledged repair is not observable.

    Intaris can acknowledge an append while its event is still buffered. If
    that buffer is lost during an Intaris restart, replaying the original
    idempotency key returns the old sequence forever without re-appending.
    The acknowledged sequence makes this retry deterministic while keeping it
    distinct from the possibly stale original ledger entry.
    """

    return f"{session_id}:turn:{turn_id}:tool-call-repair-reconcile:{call_id}:{acknowledged_seq}"


def _same_canonical_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.get("type") == right.get("type") and left.get("data") == right.get("data")


async def read_canonical_tool_pair(
    guardrails: Any,
    *,
    session_id: str,
    turn_id: str,
    call_id: str,
    tool_name: str,
    after_seq: int = 0,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pairs = await read_canonical_tool_pairs(
        guardrails,
        session_id=session_id,
        turn_id=turn_id,
        tool_names={call_id: tool_name},
        after_seq=after_seq,
    )
    call, result = pairs[call_id]
    if call is None:
        raise CanonicalToolHistoryError("canonical tool_call is missing")
    return call, result


async def read_canonical_tool_pairs(
    guardrails: Any,
    *,
    session_id: str,
    turn_id: str,
    tool_names: dict[str, str],
    allow_missing_calls: bool = False,
    after_seq: int = 0,
) -> dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Read and validate a bounded descriptor set with one history scan."""

    call_events: dict[str, dict[str, Any]] = {}
    result_events: dict[str, dict[str, Any]] = {}
    while True:
        page = await guardrails.read_events(
            session_id=session_id,
            after_seq=after_seq,
            limit=_READ_PAGE_SIZE,
        )
        events = [event for event in page.events if isinstance(event, dict)]
        for event in events:
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            call_id = data.get("call_id")
            if not isinstance(call_id, str) or call_id not in tool_names:
                continue
            if data.get("turn_id") != turn_id:
                continue
            event_type = event.get("type")
            if event_type == "tool_call":
                if data.get("name") != tool_names[call_id]:
                    raise CanonicalToolHistoryError("tool_call name does not match descriptor")
                if call_id in call_events:
                    if not _same_canonical_event(call_events[call_id], event):
                        raise CanonicalToolHistoryError("conflicting duplicate canonical tool_call")
                    continue
                call_events[call_id] = event
            elif event_type == "tool_result":
                if data.get("name") != tool_names[call_id]:
                    raise CanonicalToolHistoryError("tool_result name does not match descriptor")
                if call_id in result_events:
                    if not _same_canonical_event(result_events[call_id], event):
                        raise CanonicalToolHistoryError(
                            "conflicting duplicate canonical tool_result"
                        )
                    continue
                result_events[call_id] = event
        try:
            next_after_seq = next_event_page_after_seq(page, after_seq)
        except EventPaginationError as exc:
            raise CanonicalToolHistoryError("canonical event pagination did not advance") from exc
        if next_after_seq is None:
            break
        after_seq = next_after_seq
    missing = tool_names.keys() - call_events.keys()
    if missing and not allow_missing_calls:
        raise CanonicalToolHistoryError("canonical tool_call is missing")
    return {
        call_id: (call_events.get(call_id), result_events.get(call_id)) for call_id in tool_names
    }


async def restore_missing_tool_call(
    guardrails: Any,
    *,
    session_id: str,
    turn_id: str,
    call_id: str,
    tool_name: str,
    event: SessionEvent,
    original_idempotency_key: str | None,
    acknowledged_seq: int | None = None,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Restore a checkpointed call that disappeared from canonical history."""

    if event.type != "tool_call":
        raise CanonicalToolHistoryError("durable tool_call snapshot has an invalid type")
    data = event.data
    if (
        data.get("turn_id") != turn_id
        or data.get("call_id") != call_id
        or data.get("name") != tool_name
    ):
        raise CanonicalToolHistoryError("durable tool_call snapshot does not match descriptor")

    async def _call_is_present() -> bool:
        await _wait_or_cancel(0.0, cancel_event)
        pairs = await read_canonical_tool_pairs(
            guardrails,
            session_id=session_id,
            turn_id=turn_id,
            tool_names={call_id: tool_name},
            allow_missing_calls=True,
            after_seq=max(0, (acknowledged_seq or 1) - 1),
        )
        await _wait_or_cancel(0.0, cancel_event)
        canonical_call = pairs[call_id][0]
        if canonical_call is None:
            return False
        expected_call = {"type": event.type, "data": event.data}
        if not _same_canonical_event(canonical_call, expected_call):
            raise CanonicalToolHistoryError(
                "canonical tool_call conflicts with durable recovery snapshot"
            )
        return True

    async def _wait_for_call() -> bool:
        for delay in _TOOL_CALL_REPAIR_READ_DELAYS:
            await _wait_or_cancel(delay, cancel_event)
            if await _call_is_present():
                return True
        return False

    if await _call_is_present():
        return
    if await _wait_for_call():
        return
    await _wait_or_cancel(0.0, cancel_event)
    append_result = await guardrails.record_events(
        session_id=session_id,
        events=[event],
        source="cognis",
        idempotency_key=tool_call_repair_idempotency_key(session_id, turn_id, call_id),
    )
    await _wait_or_cancel(0.0, cancel_event)
    if not append_result.ok:
        raise RuntimeError("canonical tool_call repair append failed")
    if append_result.first_seq > 0:
        evidence = await guardrails.read_events(
            session_id=session_id,
            seqs=[append_result.first_seq],
            limit=1,
        )
        targeted_call = _validated_targeted_tool_call(
            evidence.events,
            seq=append_result.first_seq,
            turn_id=turn_id,
            call_id=call_id,
            tool_name=tool_name,
        )
        if targeted_call is not None:
            expected_call = {"type": event.type, "data": event.data}
            if not _same_canonical_event(targeted_call, expected_call):
                raise CanonicalToolHistoryError(
                    "canonical tool_call conflicts with durable recovery snapshot"
                )
            return
    if await _wait_for_call():
        return
    reconciliation_result = await guardrails.record_events(
        session_id=session_id,
        events=[event],
        source="cognis",
        idempotency_key=tool_call_repair_reconciliation_idempotency_key(
            session_id,
            turn_id,
            call_id,
            append_result.first_seq,
        ),
    )
    await _wait_or_cancel(0.0, cancel_event)
    if not reconciliation_result.ok:
        raise RuntimeError("canonical tool_call reconciliation append failed")
    if await _wait_for_call():
        return
    raise CanonicalToolRepairUnavailable(
        call_event=event,
        append_result=reconciliation_result,
    )


async def append_tool_result_once(
    guardrails: Any,
    *,
    session_id: str,
    turn_id: str,
    call_id: str,
    tool_name: str,
    event: SessionEvent,
    known_absent: bool = False,
    verify_after_append: bool = True,
    targeted_evidence: bool = False,
    tool_call_event: SessionEvent | None = None,
    tool_call_idempotency_key: str | None = None,
    tool_call_seq: int | None = None,
    cancel_event: asyncio.Event | None = None,
) -> ToolResultSettlement:
    """Append one result or return the canonical result that won the race."""

    existing = None
    if tool_call_event is not None:
        try:
            await restore_missing_tool_call(
                guardrails,
                session_id=session_id,
                turn_id=turn_id,
                call_id=call_id,
                tool_name=tool_name,
                event=tool_call_event,
                original_idempotency_key=tool_call_idempotency_key,
                acknowledged_seq=tool_call_seq,
                cancel_event=cancel_event,
            )
        except CanonicalToolRepairUnavailable as exc:
            raise CanonicalToolRepairUnavailable(
                call_event=exc.call_event,
                append_result=exc.append_result,
                result_event=event,
            ) from exc
    if not known_absent and not targeted_evidence:
        _, existing = await read_canonical_tool_pair(
            guardrails,
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
            tool_name=tool_name,
        )
    if existing is not None:
        seq = int(existing.get("seq") or 0)
        return ToolResultSettlement(
            append_result=EventAppendResult(ok=True, count=0, first_seq=seq, last_seq=seq),
            event=existing,
            appended=False,
        )
    append_result = await guardrails.record_events(
        session_id=session_id,
        events=[event],
        source="cognis",
        idempotency_key=tool_result_idempotency_key(session_id, turn_id, call_id),
    )
    if not append_result.ok:
        raise RuntimeError("canonical tool_result append failed")
    if not verify_after_append:
        return ToolResultSettlement(
            append_result=append_result,
            event={"type": event.type, "data": event.data},
            appended=True,
        )
    if targeted_evidence and append_result.first_seq > 0:
        evidence = await guardrails.read_events(
            session_id=session_id,
            seqs=[append_result.first_seq],
            limit=1,
        )
        winner = _validated_targeted_tool_result(
            evidence.events,
            turn_id=turn_id,
            call_id=call_id,
            tool_name=tool_name,
        )
        if winner is not None and tool_call_event is None:
            return ToolResultSettlement(
                append_result=append_result,
                event=winner,
                appended=winner.get("data") == event.data,
            )
    _, winner = await read_canonical_tool_pair(
        guardrails,
        session_id=session_id,
        turn_id=turn_id,
        call_id=call_id,
        tool_name=tool_name,
        after_seq=max(0, (tool_call_seq or 1) - 1),
    )
    if winner is None:
        raise CanonicalToolHistoryError("canonical tool_result is missing after append")
    return ToolResultSettlement(
        append_result=append_result,
        event=winner,
        appended=winner.get("data") == event.data,
    )


def _validated_targeted_tool_result(
    events: list[dict[str, Any]],
    *,
    turn_id: str,
    call_id: str,
    tool_name: str,
) -> dict[str, Any] | None:
    """Return exact sequence evidence, or defer to full reconciliation."""

    if len(events) != 1:
        return None
    event = events[0]
    data = event.get("data")
    if (
        event.get("type") != "tool_result"
        or not isinstance(data, dict)
        or data.get("turn_id") != turn_id
        or data.get("call_id") != call_id
        or data.get("name") != tool_name
    ):
        return None
    return event


def _validated_targeted_tool_call(
    events: list[dict[str, Any]],
    *,
    seq: int,
    turn_id: str,
    call_id: str,
    tool_name: str,
) -> dict[str, Any] | None:
    """Return exact sequence evidence for one repaired tool call."""

    if len(events) != 1:
        return None
    event = events[0]
    data = event.get("data")
    if (
        event.get("seq") != seq
        or event.get("type") != "tool_call"
        or not isinstance(data, dict)
        or data.get("turn_id") != turn_id
        or data.get("call_id") != call_id
        or data.get("name") != tool_name
    ):
        return None
    return event
