from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.models.search import SearchBackends, SearchSessionsResponse
from cognis.models.tool import ExecutorHandle
from cognis.tools.builtin import conversations as ct
from cognis.tools.registry import ToolExecutionContext


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeIntaris:
    def __init__(self, events_by_session: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.events_by_session = events_by_session or {}

    async def read_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        types: list[str] | None = None,
        last_n: int | None = None,
        allow_missing_stream: bool = False,
    ) -> SimpleNamespace:
        del allow_missing_stream
        events = [
            event
            for event in self.events_by_session.get(session_id, [])
            if event["seq"] > after_seq
        ]
        if types is not None:
            events = [event for event in events if event["type"] in types]
        if last_n is not None:
            has_more = len(events) > last_n
            events = events[-last_n:] if last_n > 0 else []
        elif limit:
            has_more = len(events) > limit
            events = events[:limit]
        else:
            has_more = False
        return SimpleNamespace(
            events=events, last_seq=events[-1]["seq"] if events else 0, has_more=has_more
        )

    async def search_health(self, *, user_email: str | None = None) -> SimpleNamespace:
        del user_email
        return SimpleNamespace(enabled=True, notes=[])

    async def search_sessions(self, *_args: object, **_kwargs: object) -> SearchSessionsResponse:
        return SearchSessionsResponse(
            sessions=[],
            total_estimated=0,
            backend=SearchBackends(lexical="sqlite", vector="disabled", mode_used="lexical"),
        )


class _FakeSessionCache:
    def __init__(self, entry: Any | None = None) -> None:
        self.entry = entry
        self.refreshed: list[str] = []

    def get_entry(self, session_id: str) -> Any | None:
        del session_id
        return self.entry

    async def refresh(self, session: Any) -> Any | None:
        self.refreshed.append(session.session_id)
        return self.entry


class _FakeCompactionStrategy:
    def __init__(self, entry: Any | None = None) -> None:
        self.session_cache = _FakeSessionCache(entry)
        self.preview_calls: list[dict[str, Any]] = []

    async def preview_summary_from_events(self, session: Any, **kwargs: Any) -> SimpleNamespace:
        self.preview_calls.append({"session": session, **kwargs})
        return SimpleNamespace(
            compacted=True,
            method="llm",
            summary="## Goal\n- generated",
            turns_compacted=3,
            tokens_before=120,
            tokens_after=24,
            tail_start_seq=42,
        )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="exec-1", executor_type="in_process"),
        runtime_metadata={"user_email": "alice@example.com", "conversation_id": "conv-1"},
    )


def _event(seq: int, content: str, event_type: str = "user_message") -> dict[str, Any]:
    role = "assistant" if event_type == "assistant_message" else "user"
    return {
        "seq": seq,
        "type": event_type,
        "data": {"role": role, "content": content},
        "ts": f"2026-05-07T12:00:{seq:02d}Z",
    }


def _conversation(status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id="conv-1",
        user_email="alice@example.com",
        title="Conversation",
        agent_id="agent-1",
        project_id=None,
        status=status,
        context_type="web",
        context_ref="web:conv-1",
        context_data={},
        memory_labels={},
        active_session_id="sess-1",
        active_executor_id=None,
        last_read_at=None,
        last_message_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
        created_at=datetime(2026, 5, 7, 11, 0, tzinfo=UTC),
    )


def _session(session_id: str, intaris_session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        intaris_session_id=intaris_session_id,
        conversation_id="conv-1",
        user_email="alice@example.com",
        agent_id="agent-1",
        parent_session_id=None,
        previous_session_id=None,
        delegation_mode=None,
        delegation_task=None,
        status="active",
        completion_reason=None,
        mnemory_session_id=None,
        started_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
        idle_since=None,
        completed_at=None,
        result_summary=None,
        result_content=None,
        updated_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_read_conversation_messages_returns_session_anchors_and_uses_prev_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_row = _session("sess-1", "int-1")
    monkeypatch.setattr(ct, "get_conversation", lambda *_args: _async_value(_conversation()))
    monkeypatch.setattr(
        ct, "list_conversation_sessions", lambda *_args: _async_value([session_row])
    )

    handlers = ct.build_conversation_tool_handlers(
        lambda: _SessionContext(),
        _FakeIntaris(
            {
                "int-1": [
                    _event(1, "one"),
                    _event(2, "two"),
                    {
                        **_event(3, "three"),
                        "data": {
                            "role": "user",
                            "content": "three",
                            "attachments": [
                                {
                                    "artifact_id": "img-conversation",
                                    "kind": "image",
                                    "mime_type": "image/png",
                                    "filename": "conversation.png",
                                    "size_bytes": 42,
                                    "url": "https://provider.invalid/private",
                                    "path": "/private/tmp/private.png",
                                }
                            ],
                        },
                    },
                ]
            }
        ),
    )

    result = await handlers["read_conversation_messages"]({"limit": 2}, _context())

    assert [event["seq"] for event in result["events"]] == [2, 3]
    assert result["events"][0]["session_id"] == "sess-1"
    assert result["events"][0]["anchor"] == "sess-1:2"
    assert result["events"][1]["attachments"] == [
        {
            "artifact_id": "img-conversation",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "conversation.png",
            "size_bytes": 42,
        }
    ]
    assert result["page"]["prev_cursor"]

    previous = await handlers["read_conversation_messages"](
        {"cursor": result["page"]["prev_cursor"], "limit": 2},
        _context(),
    )

    assert [event["seq"] for event in previous["events"]] == [1]
    assert previous["page"]["next_cursor"]


@pytest.mark.asyncio
async def test_read_conversation_messages_validates_anchor_kind_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_row = _session("sess-1", "int-1")
    monkeypatch.setattr(ct, "get_conversation", lambda *_args: _async_value(_conversation()))
    monkeypatch.setattr(
        ct, "list_conversation_sessions", lambda *_args: _async_value([session_row])
    )
    handlers = ct.build_conversation_tool_handlers(lambda: _SessionContext(), _FakeIntaris())

    with pytest.raises(ValueError, match="anchor.kind"):
        await handlers["read_conversation_messages"](
            {"anchor": {"kind": "sideways"}},
            _context(),
        )

    with pytest.raises(ValueError, match="anchor.session_id"):
        await handlers["read_conversation_messages"](
            {"anchor": {"kind": "after", "session_id": "missing", "seq": 1}},
            _context(),
        )


@pytest.mark.asyncio
async def test_read_conversation_messages_supports_around_anchor_and_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_row = _session("sess-1", "int-1")
    monkeypatch.setattr(ct, "get_conversation", lambda *_args: _async_value(_conversation()))
    monkeypatch.setattr(
        ct, "list_conversation_sessions", lambda *_args: _async_value([session_row])
    )
    long_content = "x" * 4_100
    handlers = ct.build_conversation_tool_handlers(
        lambda: _SessionContext(),
        _FakeIntaris({"int-1": [_event(1, "one"), _event(2, long_content), _event(3, "three")]}),
    )

    result = await handlers["read_conversation_messages"](
        {"anchor": {"kind": "around", "session_id": "sess-1", "seq": 2, "before": 0, "after": 0}},
        _context(),
    )

    assert [event["seq"] for event in result["events"]] == [2]
    assert len(result["events"][0]["content"]) == 4_000
    assert result["events"][0]["content_truncated"] is True


@pytest.mark.asyncio
async def test_read_conversation_messages_rejects_unsupported_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_row = _session("sess-1", "int-1")
    monkeypatch.setattr(ct, "get_conversation", lambda *_args: _async_value(_conversation()))
    monkeypatch.setattr(
        ct, "list_conversation_sessions", lambda *_args: _async_value([session_row])
    )
    handlers = ct.build_conversation_tool_handlers(lambda: _SessionContext(), _FakeIntaris())

    with pytest.raises(ValueError, match="Unsupported message kind"):
        await handlers["read_conversation_messages"]({"kinds": ["tool_call"]}, _context())


@pytest.mark.asyncio
async def test_list_conversations_filters_time_and_paginates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = _conversation()
    older.conversation_id = "older"
    older.last_message_at = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    older.updated_at = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    older.created_at = datetime(2026, 5, 6, 11, 0, tzinfo=UTC)
    first = _conversation()
    first.conversation_id = "first"
    first.last_message_at = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    second = _conversation()
    second.conversation_id = "second"
    second.last_message_at = datetime(2026, 5, 7, 13, 0, tzinfo=UTC)
    monkeypatch.setattr(
        ct, "list_conversations", lambda *_args, **_kwargs: _async_value([second, first, older])
    )

    handlers = ct.build_conversation_tool_handlers(lambda: _SessionContext(), _FakeIntaris())
    result = await handlers["list_conversations"](
        {"since": "2026-05-07T00:00:00Z", "limit": 1},
        _context(),
    )

    assert [row["conversation_id"] for row in result["conversations"]] == ["second"]
    assert result["next_cursor"]

    next_page = await handlers["list_conversations"](
        {"since": "2026-05-07T00:00:00Z", "limit": 1, "cursor": result["next_cursor"]},
        _context(),
    )
    assert [row["conversation_id"] for row in next_page["conversations"]] == ["first"]
    assert next_page["next_cursor"] is None


@pytest.mark.asyncio
async def test_summarize_conversation_generates_read_only_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_row = _session("sess-1", "int-1")
    strategy = _FakeCompactionStrategy()
    monkeypatch.setattr(ct, "get_conversation", lambda *_args: _async_value(_conversation()))
    monkeypatch.setattr(ct, "get_session_row", lambda *_args: _async_value(session_row))
    monkeypatch.setattr(
        ct,
        "list_conversation_sessions",
        lambda *_args: _async_value([session_row]),
    )
    handlers = ct.build_conversation_tool_handlers(
        lambda: _SessionContext(),
        _FakeIntaris(),
        strategy,
    )

    result = await handlers["summarize_conversation"]({}, _context())

    assert result["summary"] == "## Goal\n- generated"
    assert result["format"] == "compaction_summary_v1"
    assert result["generated"] is True
    assert result["method"] == "llm"
    assert result["session_id"] == "sess-1"
    assert result["intaris_session_id"] == "int-1"
    assert len(strategy.preview_calls) == 1
    assert strategy.preview_calls[0]["session"].session_id == "sess-1"
    assert strategy.preview_calls[0]["trigger"] == "tool_preview"


@pytest.mark.asyncio
async def test_summarize_conversation_returns_cached_summary_without_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_row = _session("sess-1", "int-1")
    strategy = _FakeCompactionStrategy()
    monkeypatch.setattr(ct, "get_conversation", lambda *_args: _async_value(_conversation()))
    monkeypatch.setattr(ct, "get_session_row", lambda *_args: _async_value(session_row))
    monkeypatch.setattr(
        ct,
        "list_conversation_sessions",
        lambda *_args: _async_value([session_row]),
    )
    handlers = ct.build_conversation_tool_handlers(
        lambda: _SessionContext(),
        _FakeIntaris(
            {
                "int-1": [
                    {
                        "seq": 4,
                        "type": "compaction_summary",
                        "data": {"summary": "## Goal\n- cached"},
                        "ts": "2026-05-07T12:00:04Z",
                    }
                ]
            }
        ),
        strategy,
    )

    result = await handlers["summarize_conversation"]({}, _context())

    assert result["summary"] == "## Goal\n- cached"
    assert result["generated"] is False
    assert result["method"] == "cached_compaction_summary"
    assert strategy.preview_calls == []
    assert strategy.session_cache.refreshed == []


def _async_value(value: Any) -> Any:
    async def _inner(*_args: object, **_kwargs: object) -> Any:
        return value

    return _inner()
