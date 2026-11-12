from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from cognis.core.events import Event, EventType
from cognis.core.title_policy import can_generate_title, sync_intaris_title
from cognis.core.turn_scheduler import TurnScheduler
from cognis.models.session import ConversationContext, ConversationModel


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


@pytest.mark.parametrize(
    "source", ["manual", "channel_seed", "agent_direct", "intaris", "cognis", "user", "app_contact"]
)
def test_supplied_and_generated_title_sources_are_protected(source: str) -> None:
    assert not can_generate_title(_conversation(title="Pinned", title_source=source))
    assert not can_generate_title(_conversation(title_source=source))


def test_only_empty_unset_non_direct_titles_are_eligible() -> None:
    assert can_generate_title(_conversation())
    assert can_generate_title(_conversation(title=" "))
    assert not can_generate_title(_conversation(title="Existing"))
    assert not can_generate_title(_conversation(platform_data={"kind": "agent_direct"}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,source", [(None, "unset"), ("Pinned", "manual"), ("Old", "intaris")]
)
async def test_intaris_suggestion_never_changes_visible_title(monkeypatch, title, source):
    update = AsyncMock(return_value=True)
    monkeypatch.setattr("cognis.core.title_policy.update_conversation_context_data", update)
    conversation = _conversation(title=title, title_source=source)
    assert await sync_intaris_title(object(), conversation, "Suggestion", updated_at="2026-01-01")
    assert (conversation.title, conversation.title_source) == (title, source)
    assert conversation.context.platform_data["intaris_latest_title"] == "Suggestion"
    assert conversation.context.platform_data["intaris_latest_title_at"] == "2026-01-01"
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_scheduler_reloads_persisted_title_for_change_detection(monkeypatch) -> None:
    class Row:
        title = "Persisted title"

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(
        "cognis.core.turn_scheduler.queries.get_conversation", AsyncMock(return_value=Row())
    )
    scheduler = cast(Any, TurnScheduler.__new__(TurnScheduler))
    scheduler._session_factory = Session
    assert await scheduler._load_visible_conversation_title("conv-1", None) == "Persisted title"


@pytest.mark.asyncio
async def test_turn_scheduler_suppresses_already_published_title_update() -> None:
    scheduler = cast(Any, TurnScheduler.__new__(TurnScheduler))
    scheduler._published_title_updates = {}
    await scheduler._handle_conversation_updated(
        Event(
            type=EventType.CONVERSATION_UPDATED,
            data={"conversation_id": "conv-1", "title": "Cognis title"},
        )
    )
    assert scheduler._published_title_updates["conv-1"] == "Cognis title"
    latest_title = "Cognis title"
    already_published_title = scheduler._published_title_updates.get("conv-1")
    assert not (latest_title and latest_title != already_published_title)
