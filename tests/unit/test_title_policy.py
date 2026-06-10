from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from cognis.core.events import Event, EventType
from cognis.core.title_policy import can_adopt_intaris_title, sync_intaris_title
from cognis.core.turn_scheduler import TurnScheduler
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    SessionModel,
    SessionStatus,
)


def _conversation(
    *,
    title: str | None = None,
    title_source: str = "unset",
    platform_data: dict[str, object] | None = None,
) -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        title=title,
        title_source=title_source,
        context=ConversationContext(type="web", platform_data=platform_data or {}),
    )


def test_manual_and_channel_titles_are_protected() -> None:
    assert can_adopt_intaris_title(_conversation(title="Pinned", title_source="manual")) is False
    assert (
        can_adopt_intaris_title(_conversation(title="Alice", title_source="channel_seed")) is False
    )


def test_agent_direct_titles_do_not_adopt_intaris_title() -> None:
    assert (
        can_adopt_intaris_title(
            _conversation(title_source="agent_direct", platform_data={"kind": "agent_direct"})
        )
        is False
    )
    assert (
        can_adopt_intaris_title(
            _conversation(
                title="Existing title",
                title_source="agent_direct",
                platform_data={"kind": "agent_direct"},
            )
        )
        is False
    )


@pytest.mark.asyncio
async def test_sync_intaris_title_updates_only_unprotected_conversations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str | None]] = []

    async def _update_conversation(
        session: object,
        conversation_id: str,
        *,
        title: str | None = None,
        title_source: str | None = None,
    ) -> bool:
        del session
        calls.append(
            {
                "conversation_id": conversation_id,
                "title": title,
                "title_source": title_source,
            }
        )
        return True

    monkeypatch.setattr("cognis.core.title_policy.update_conversation", _update_conversation)
    context_updates: list[dict[str, object]] = []

    async def _update_context_data(
        session: object,
        conversation_id: str,
        *,
        context_data: dict[str, object],
    ) -> bool:
        del session
        context_updates.append({"conversation_id": conversation_id, "context_data": context_data})
        return True

    monkeypatch.setattr(
        "cognis.core.title_policy.update_conversation_context_data", _update_context_data
    )

    class _Session:
        async def flush(self) -> None:
            return None

    session = cast(Any, _Session())

    conversation = _conversation()
    updated = await sync_intaris_title(
        session, conversation, "Fresh title", updated_at="2026-01-01T00:00:00Z"
    )

    assert updated is True
    assert conversation.title == "Fresh title"
    assert conversation.title_source == "intaris"
    assert conversation.context.platform_data["intaris_latest_title"] == "Fresh title"
    assert conversation.context.platform_data["intaris_latest_title_at"] == "2026-01-01T00:00:00Z"
    assert context_updates[-1] == {
        "conversation_id": "conv-1",
        "context_data": {
            "intaris_latest_title": "Fresh title",
            "intaris_latest_title_at": "2026-01-01T00:00:00Z",
        },
    }
    assert calls == [
        {
            "conversation_id": "conv-1",
            "title": "Fresh title",
            "title_source": "intaris",
        }
    ]

    calls.clear()
    context_updates.clear()
    protected = _conversation(title="Pinned", title_source="manual")
    updated = await sync_intaris_title(session, protected, "Ignored title")

    assert updated is True
    assert protected.title == "Pinned"
    assert protected.context.platform_data["intaris_latest_title"] == "Ignored title"
    assert context_updates[-1] == {
        "conversation_id": "conv-1",
        "context_data": {"intaris_latest_title": "Ignored title"},
    }
    assert calls == []

    calls.clear()
    context_updates.clear()
    direct = _conversation(
        title="Agent 1",
        title_source="agent_direct",
        platform_data={"kind": "agent_direct"},
    )
    updated = await sync_intaris_title(session, direct, "Topic title")

    assert updated is True
    assert direct.title == "Agent 1"
    assert direct.context.platform_data["intaris_latest_title"] == "Topic title"
    assert context_updates[-1] == {
        "conversation_id": "conv-1",
        "context_data": {"kind": "agent_direct", "intaris_latest_title": "Topic title"},
    }
    assert calls == []


@pytest.mark.asyncio
async def test_turn_scheduler_reloads_persisted_title_for_change_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_title = "Placeholder"
    persisted_title = "Fresh Intaris title"

    class _Row:
        title = persisted_title

    class _Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class _Factory:
        def __call__(self) -> _Session:
            return _Session()

    async def _get_conversation(session: object, conversation_id: str) -> _Row:
        del session
        assert conversation_id == "conv-1"
        return _Row()

    monkeypatch.setattr("cognis.core.turn_scheduler.queries.get_conversation", _get_conversation)

    scheduler = cast(Any, TurnScheduler.__new__(TurnScheduler))
    scheduler._session_factory = _Factory()

    title = await scheduler._load_visible_conversation_title("conv-1", stale_title)

    assert title == persisted_title
    assert title != stale_title


@pytest.mark.asyncio
async def test_turn_scheduler_suppresses_already_published_title_update() -> None:
    scheduler = cast(Any, TurnScheduler.__new__(TurnScheduler))
    scheduler._published_title_updates = {}

    await scheduler._handle_conversation_updated(
        Event(
            type=EventType.CONVERSATION_UPDATED,
            data={"conversation_id": "conv-1", "title": "Fresh Intaris title"},
        )
    )

    latest_title = "Fresh Intaris title"
    pre_turn_title = None
    already_published_title = scheduler._published_title_updates.get("conv-1")
    title_changed = bool(
        latest_title and latest_title != pre_turn_title and latest_title != already_published_title
    )

    assert title_changed is False


@pytest.mark.asyncio
async def test_turn_scheduler_adopts_late_intaris_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = _conversation()
    session_model = SessionModel(
        session_id="session-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        status=SessionStatus.ACTIVE,
        intaris_session_id="intaris-1",
    )
    commits = 0

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            nonlocal commits
            commits += 1

    class _Factory:
        def __call__(self) -> _Session:
            return _Session()

    async def _update_conversation(
        session: object,
        conversation_id: str,
        *,
        title: str | None = None,
        title_source: str | None = None,
    ) -> bool:
        del session
        assert conversation_id == "conv-1"
        assert title == "Late Intaris title"
        assert title_source == "intaris"
        return True

    async def _update_context_data(
        session: object,
        conversation_id: str,
        *,
        context_data: dict[str, object],
    ) -> bool:
        del session
        assert conversation_id == "conv-1"
        assert context_data["intaris_latest_title"] == "Late Intaris title"
        assert context_data["intaris_latest_title_at"] == "2026-05-20T10:00:00Z"
        return True

    monkeypatch.setattr("cognis.core.title_policy.update_conversation", _update_conversation)
    monkeypatch.setattr(
        "cognis.core.title_policy.update_conversation_context_data", _update_context_data
    )

    scheduler = cast(Any, TurnScheduler.__new__(TurnScheduler))
    scheduler._session_factory = _Factory()
    scheduler._providers = SimpleNamespace(
        guardrails=SimpleNamespace(
            get_session=AsyncMock(
                return_value=SimpleNamespace(
                    title="Late Intaris title",
                    updated_at="2026-05-20T10:00:00Z",
                )
            )
        )
    )

    changed = await scheduler._adopt_late_intaris_title(conversation, session_model)

    assert changed is True
    assert commits == 1
    assert conversation.title == "Late Intaris title"
    assert conversation.title_source == "intaris"
    scheduler._providers.guardrails.get_session.assert_awaited_once_with("intaris-1")
