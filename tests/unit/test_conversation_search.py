from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core import conversation_search as cs
from cognis.models.search import SearchMatch, SearchSessionMatch, kind_rank


def test_search_kind_priority_matches_ui_navigation_semantics() -> None:
    assert kind_rank("reasoning") < kind_rank("intention") < kind_rank("summary")


@pytest.mark.asyncio
async def test_join_session_matches_drops_non_owned_and_sorts_by_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(
            session_id="sess-summary",
            intaris_session_id="int-summary",
            conversation_id="conv-1",
            started_at=None,
        ),
        SimpleNamespace(
            session_id="sess-reasoning",
            intaris_session_id="int-reasoning",
            conversation_id="conv-1",
            started_at=None,
        ),
        SimpleNamespace(
            session_id="sess-other",
            intaris_session_id="int-other",
            conversation_id="conv-2",
            started_at=None,
        ),
    ]
    conversations = {
        "conv-1": SimpleNamespace(
            conversation_id="conv-1",
            user_email="alice@example.com",
            title="Owned",
            agent_id="agent-1",
            project_id=None,
            status="active",
        ),
        "conv-2": SimpleNamespace(
            conversation_id="conv-2",
            user_email="bob@example.com",
            title="Other",
            agent_id="agent-1",
            project_id=None,
            status="active",
        ),
    }

    async def fake_list_sessions(_db: object, ids: list[str]) -> list[object]:
        return [row for row in rows if row.intaris_session_id in ids]

    async def fake_get_conversation(_db: object, conversation_id: str) -> object | None:
        return conversations.get(conversation_id)

    monkeypatch.setattr(cs, "list_sessions_by_intaris_session_ids", fake_list_sessions)
    monkeypatch.setattr(cs, "get_conversation", fake_get_conversation)

    matches = [
        SearchSessionMatch(
            session_id="int-summary",
            match_count=1,
            top_match=SearchMatch(
                session_id="int-summary",
                kind="summary",
                snippet="summary",
                score=0.99,
            ),
        ),
        SearchSessionMatch(
            session_id="int-other",
            match_count=1,
            top_match=SearchMatch(
                session_id="int-other",
                kind="reasoning",
                snippet="not owned",
                score=0.01,
            ),
        ),
        SearchSessionMatch(
            session_id="int-reasoning",
            match_count=1,
            top_match=SearchMatch(
                session_id="int-reasoning",
                kind="reasoning",
                snippet="reasoning",
                score=0.2,
            ),
        ),
    ]

    joined = await cs.join_session_matches(
        object(), user_email="alice@example.com", matches=matches
    )

    assert [item.intaris_session_id for item in joined] == ["int-reasoning", "int-summary"]
    assert all(item.conversation_id == "conv-1" for item in joined)
