from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2 import routes
from cognis.api.chat_v2.event_store import IntarisSessionEventStore
from cognis.api.chat_v2.sync import ConversationSessionRef
from cognis.api.common import AuthenticatedUser


class _AsyncContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _request(events: list[dict[str, object]]) -> SimpleNamespace:
    guardrails = SimpleNamespace(
        read_events=AsyncMock(
            return_value=SimpleNamespace(events=events, last_seq=len(events), has_more=False)
        )
    )
    return SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="alice@example.com", role="user")),
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=lambda: _AsyncContext(),
                providers=SimpleNamespace(guardrails=guardrails),
            )
        ),
    )


def _ref(events: list[dict[str, object]]) -> ConversationSessionRef:
    guardrails = SimpleNamespace(
        read_events=AsyncMock(
            return_value=SimpleNamespace(events=events, last_seq=len(events), has_more=False)
        )
    )
    return ConversationSessionRef(
        session_id="sess-1",
        event_store_session_id="isess-1",
        ordinal=0,
        reader=IntarisSessionEventStore(guardrails),
    )


@pytest.mark.asyncio
async def test_retry_source_uses_durable_failed_turn_without_duplicate_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {
                "turn_id": "turn-1",
                "content": "retry me",
                "client_message_id": "client-1",
                "attachments": [],
            },
        },
        {
            "seq": 2,
            "type": "lifecycle",
            "data": {
                "event": "turn_error",
                "turn_id": "turn-1",
                "error_code": "executor_unavailable",
            },
        },
    ]
    monkeypatch.setattr(
        routes,
        "_session_refs",
        AsyncMock(return_value=[_ref(events)]),
    )
    request = _request(events)

    source, failed_turn_found = await routes._retry_source_from_failed_turn(
        request,
        SimpleNamespace(conversation_id="conv-1", active_session_id="sess-1"),
        "turn-1",
    )

    assert failed_turn_found is True
    assert source == {
        "content": "retry me",
        "attachments": [],
        "client_message_id": "client-1",
    }


@pytest.mark.asyncio
async def test_retry_source_distinguishes_legacy_failure_without_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {
            "seq": 1,
            "type": "lifecycle",
            "data": {"event": "turn_error", "turn_id": "turn-legacy"},
        }
    ]
    monkeypatch.setattr(
        routes,
        "_session_refs",
        AsyncMock(return_value=[_ref(events)]),
    )
    request = _request(events)

    source, failed_turn_found = await routes._retry_source_from_failed_turn(
        request,
        SimpleNamespace(conversation_id="conv-1", active_session_id="sess-1"),
        "turn-legacy",
    )

    assert source is None
    assert failed_turn_found is True
    assert routes._retry_unavailable_error(failed_turn_found) == (
        "retry_source_not_persisted",
        "This failed legacy turn has no persisted source message and cannot be retried.",
    )


def test_retry_unavailable_error_preserves_inactive_nonfailed_distinction() -> None:
    assert routes._retry_unavailable_error(False) == (
        "retry_turn_not_available",
        "Only failed, inactive turns can be retried.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_outcome", ["failed", "succeeded", "orphaned"])
async def test_retry_source_only_consumes_explicitly_successful_retry_attempt(
    monkeypatch: pytest.MonkeyPatch,
    retry_outcome: str,
) -> None:
    events: list[dict[str, object]] = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"turn_id": "turn-1", "content": "retry me"},
        },
        {
            "seq": 2,
            "type": "lifecycle",
            "data": {"event": "turn_error", "turn_id": "turn-1"},
        },
        {
            "seq": 3,
            "type": "system_message",
            "data": {
                "event": "system_notice",
                "turn_id": "turn-retry",
                "retry_source_turn_id": "turn-1",
            },
        },
    ]
    if retry_outcome == "succeeded":
        events.append(
            {
                "seq": 4,
                "type": "lifecycle",
                "data": {
                    "event": "retry_source_consumed",
                    "turn_id": "turn-1",
                    "retry_source_turn_id": "turn-1",
                    "retry_turn_id": "turn-retry",
                },
            }
        )
    elif retry_outcome == "failed":
        events.append(
            {
                "seq": 4,
                "type": "lifecycle",
                "data": {"event": "turn_error", "turn_id": "turn-retry"},
            }
        )
    monkeypatch.setattr(
        routes,
        "_session_refs",
        AsyncMock(return_value=[_ref(events)]),
    )

    source, failed_turn_found = await routes._retry_source_from_failed_turn(
        _request(events),
        SimpleNamespace(conversation_id="conv-1", active_session_id="sess-1"),
        "turn-1",
    )

    if retry_outcome == "succeeded":
        assert source is None
        assert failed_turn_found is False
    else:
        assert source is not None
        assert source["content"] == "retry me"
        assert failed_turn_found is True
