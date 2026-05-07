from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core import conversation_search as cs
from cognis.models.search import MIN_DISPLAY_SCORE, SearchMatch, SearchSessionMatch, kind_rank


def test_search_kind_priority_matches_ui_navigation_semantics() -> None:
    assert kind_rank("reasoning") < kind_rank("intention") < kind_rank("summary")


def test_search_display_threshold_prefers_precision_over_recall() -> None:
    assert MIN_DISPLAY_SCORE >= 0.3


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
            context_type="web",
        ),
        "conv-2": SimpleNamespace(
            conversation_id="conv-2",
            user_email="bob@example.com",
            title="Other",
            agent_id="agent-1",
            project_id=None,
            status="active",
            context_type="web",
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
                score=0.8,
            ),
        ),
    ]

    joined = await cs.join_session_matches(
        object(), user_email="alice@example.com", matches=matches
    )

    assert [item.intaris_session_id for item in joined] == ["int-reasoning", "int-summary"]
    assert all(item.conversation_id == "conv-1" for item in joined)


@pytest.mark.asyncio
async def test_join_session_matches_filters_score_deleted_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(
            session_id="sess-web",
            intaris_session_id="int-web",
            conversation_id="conv-web",
            started_at=None,
        ),
        SimpleNamespace(
            session_id="sess-signal",
            intaris_session_id="int-signal",
            conversation_id="conv-signal",
            started_at=None,
        ),
        SimpleNamespace(
            session_id="sess-deleted",
            intaris_session_id="int-deleted",
            conversation_id="conv-deleted",
            started_at=None,
        ),
        SimpleNamespace(
            session_id="sess-low",
            intaris_session_id="int-low",
            conversation_id="conv-web",
            started_at=None,
        ),
    ]
    conversations = {
        "conv-web": SimpleNamespace(
            conversation_id="conv-web",
            user_email="alice@example.com",
            title="Web",
            agent_id="agent-1",
            project_id=None,
            status="active",
            context_type="web",
        ),
        "conv-signal": SimpleNamespace(
            conversation_id="conv-signal",
            user_email="alice@example.com",
            title="Signal",
            agent_id="agent-1",
            project_id=None,
            status="active",
            context_type="signal",
        ),
        "conv-deleted": SimpleNamespace(
            conversation_id="conv-deleted",
            user_email="alice@example.com",
            title="Deleted",
            agent_id="agent-1",
            project_id=None,
            status="deleted",
            context_type="web",
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
            session_id="int-web",
            match_count=1,
            top_match=SearchMatch(
                session_id="int-web",
                kind="reasoning",
                snippet="web",
                score=0.8,
            ),
        ),
        SearchSessionMatch(
            session_id="int-signal",
            match_count=1,
            top_match=SearchMatch(
                session_id="int-signal",
                kind="reasoning",
                snippet="signal",
                score=0.8,
            ),
        ),
        SearchSessionMatch(
            session_id="int-deleted",
            match_count=1,
            top_match=SearchMatch(
                session_id="int-deleted",
                kind="reasoning",
                snippet="deleted",
                score=0.8,
            ),
        ),
        SearchSessionMatch(
            session_id="int-low",
            match_count=1,
            top_match=SearchMatch(
                session_id="int-low",
                kind="reasoning",
                snippet="low score",
                score=0.01,
            ),
        ),
    ]

    joined = await cs.join_session_matches(
        object(),
        user_email="alice@example.com",
        matches=matches,
        context_type="web",
    )

    assert [item.intaris_session_id for item in joined] == ["int-web"]


def test_attach_extra_matches_skips_top_match_duplicates_and_low_scores() -> None:
    row = cs.ConversationSearchMatch(
        conversation_id="conv-1",
        agent_id="agent-1",
        status="active",
        session_id="sess-1",
        intaris_session_id="int-1",
        match_count=4,
        top_match=SearchMatch(
            session_id="int-1",
            kind="reasoning",
            ref_id="top",
            snippet="top",
            score=0.9,
        ),
    )
    cs.attach_extra_matches(
        [row],
        [
            SearchMatch(
                session_id="int-1",
                kind="reasoning",
                ref_id="top",
                snippet="duplicate top",
                score=0.8,
            ),
            SearchMatch(
                session_id="int-1",
                kind="summary",
                ref_id="low",
                snippet="low",
                score=0.01,
            ),
            SearchMatch(
                session_id="int-1",
                kind="intention",
                ref_id="extra-1",
                snippet="extra intention",
                score=0.7,
            ),
            SearchMatch(
                session_id="int-1",
                kind="reasoning",
                ref_id="extra-2",
                snippet="extra reasoning",
                score=0.6,
            ),
        ],
        per_session_limit=2,
    )

    assert [match.ref_id for match in row.extra_matches] == ["extra-2", "extra-1"]


def test_attach_extra_matches_deduplicates_matches_without_ref_ids() -> None:
    row = cs.ConversationSearchMatch(
        conversation_id="conv-1",
        agent_id="agent-1",
        status="active",
        session_id="sess-1",
        intaris_session_id="int-1",
        match_count=2,
        top_match=SearchMatch(
            session_id="int-1",
            kind="summary",
            snippet="same snippet",
            ts="2026-05-01T00:00:00Z",
            score=0.9,
        ),
    )

    cs.attach_extra_matches(
        [row],
        [
            SearchMatch(
                session_id="int-1",
                kind="summary",
                snippet="same snippet",
                ts="2026-05-01T00:00:00Z",
                score=0.8,
            ),
            SearchMatch(
                session_id="int-1",
                kind="summary",
                snippet="different snippet",
                ts="2026-05-01T00:00:01Z",
                score=0.7,
            ),
        ],
    )

    assert [match.snippet for match in row.extra_matches] == ["different snippet"]
