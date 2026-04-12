from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core.title_policy import can_adopt_intaris_title, sync_intaris_title
from cognis.models.session import ConversationContext, ConversationModel


def _conversation(*, title: str | None = None, title_source: str = "unset") -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        title=title,
        title_source=title_source,
        context=ConversationContext(type="web"),
    )


def test_manual_and_channel_titles_are_protected() -> None:
    assert can_adopt_intaris_title(_conversation(title="Pinned", title_source="manual")) is False
    assert (
        can_adopt_intaris_title(_conversation(title="Alice", title_source="channel_seed")) is False
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

    conversation = _conversation()
    updated = await sync_intaris_title(SimpleNamespace(), conversation, "Fresh title")

    assert updated is True
    assert conversation.title == "Fresh title"
    assert conversation.title_source == "intaris"
    assert calls == [
        {
            "conversation_id": "conv-1",
            "title": "Fresh title",
            "title_source": "intaris",
        }
    ]

    calls.clear()
    protected = _conversation(title="Pinned", title_source="manual")
    updated = await sync_intaris_title(SimpleNamespace(), protected, "Ignored title")

    assert updated is False
    assert protected.title == "Pinned"
    assert calls == []
