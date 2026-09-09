"""Synthetic historical discovery and detail trust-boundary regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core import historical_tool_output as ht
from cognis.core.agent_registry import AgentRegistry
from cognis.core.tool_output_store import ToolOutputStore
from cognis.runtime_context import current_agent_id, current_agent_owner_email
from cognis.tools.builtin import conversations as ct
from cognis.tools.builtin.tool_output import handle_tool_output_tool
from tests.unit.test_conversation_tools import (
    _async_value,
    _context,
    _conversation,
    _session,
    _SessionContext,
)


class History:
    def __init__(self, streams):
        self.streams = streams
        self.calls = []

    async def read_events(self, session_id, **kwargs):
        self.calls.append(
            (session_id, kwargs, current_agent_id.get(), current_agent_owner_email.get())
        )
        events = list(self.streams.get(session_id, []))
        if "seqs" in kwargs:
            events = [event for event in events if event["seq"] in kwargs["seqs"]]
        if kwargs.get("types"):
            events = [event for event in events if event["type"] in kwargs["types"]]
        events = [event for event in events if event["seq"] > kwargs.get("after_seq", 0)]
        if "before_seq" in kwargs:
            events = [event for event in events if event["seq"] < kwargs["before_seq"]]
        limit = kwargs.get("last_n", kwargs.get("limit", 200))
        more = len(events) > limit
        events = events[-limit:] if "last_n" in kwargs or "before_seq" in kwargs else events[:limit]
        return SimpleNamespace(events=events, has_more=more)


def event(seq, kind="tool_call", **data):
    return {
        "seq": seq,
        "type": kind,
        "data": {"call_id": "synthetic-call-414", "name": "read", **data},
    }


@pytest.fixture
def setup_history(monkeypatch):
    conversation = _conversation()
    rows = [_session("sess-1", "int-1"), _session("sess-2", "int-2")]
    for module in (ct, ht):
        monkeypatch.setattr(module, "get_conversation", lambda *args: _async_value(conversation))
    monkeypatch.setattr(ct, "list_conversation_sessions", lambda *args: _async_value(rows))
    monkeypatch.setattr(
        ht,
        "get_session_row",
        lambda db, sid: _async_value(next((row for row in rows if row.session_id == sid), None)),
    )
    monkeypatch.setattr(
        AgentRegistry,
        "get",
        AsyncMock(return_value=SimpleNamespace(owner_email="source-owner@example.com")),
    )
    return conversation, rows


def listing(history):
    return ct.build_conversation_tool_handlers(lambda: _SessionContext(), history)[
        "read_conversation_messages"
    ]


async def detail(history, store, reference, part="result", **kwargs):
    return await handle_tool_output_tool(
        "read_tool_output",
        {"call_id": reference["call_id"], "reference": reference, "part": part, **kwargs},
        store,
        session_factory=lambda: _SessionContext(),
        intaris=history,
        user_email="alice@example.com",
    )


def ref(seq=429, kind="tool_result"):
    return dict(
        conversation_id="conv-1",
        session_id="sess-1",
        seq=seq,
        kind=kind,
        call_id="synthetic-call-414",
    )


@pytest.mark.asyncio
async def test_huge_listing_is_metadata_only(setup_history, monkeypatch):
    payload = "PAYLOAD_MUST_NOT_LEAK" * 100_000
    history = History(
        {
            "int-1": [
                event(414, arguments={"body": payload}, native_envelope={"body": payload}),
                event(429, "tool_result", result=payload, has_full_output=True),
            ]
        }
    )
    store = SimpleNamespace(read=AsyncMock(side_effect=AssertionError("listing read store")))
    monkeypatch.setattr(ToolOutputStore, "read", store.read)
    result = await listing(history)(
        {"kinds": ["tool_call", "tool_result"], "include_content_truncation": False}, _context()
    )
    assert len(json.dumps(result)) < 2500
    assert "PAYLOAD_MUST_NOT_LEAK" not in json.dumps(result)
    for item in result["events"]:
        assert set(item) == {"session_id", "seq", "kind", "call_id", "reference", "tool_name"}
        assert item["tool_name"] == "read"
    assert [item["seq"] for item in result["events"]] == [414, 429]
    store.read.assert_not_awaited()
    assert all(call[2:] == ("agent-1", "source-owner@example.com") for call in history.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["forward", "backward"])
@pytest.mark.parametrize("kinds", [["tool_call"], ["tool_result"], ["tool_call", "tool_result"]])
async def test_sparse_cross_session_cursors(setup_history, direction, kinds):
    history = History(
        {
            "int-1": [event(2), event(414), event(429, "tool_result")],
            "int-2": [event(3, "tool_result"), event(500), event(900, "tool_result")],
        }
    )
    read = listing(history)
    args = {
        "kinds": kinds,
        "limit": 1,
        "anchor": {"kind": "from_start" if direction == "forward" else "latest"},
    }
    seen = []
    for _ in range(10):
        result = await read(args, _context())
        items = [(item["session_id"], item["seq"]) for item in result["events"]]
        seen = seen + items if direction == "forward" else items + seen
        cursor = result["page"]["next_cursor" if direction == "forward" else "prev_cursor"]
        if cursor is None:
            break
        args = {"kinds": kinds, "limit": 1, "cursor": cursor}
    else:
        pytest.fail("cursor stalled")
    expected = [
        (f"sess-{index}", item["seq"])
        for index in (1, 2)
        for item in history.streams[f"int-{index}"]
        if item["type"] in kinds
    ]
    assert seen == expected


@pytest.mark.asyncio
async def test_around_seq_window_and_cursor_scope(setup_history):
    history = History({"int-1": [event(2), event(414), event(429, "tool_result"), event(700)]})
    read = listing(history)
    kinds = ["tool_call", "tool_result"]
    result = await read(
        {
            "kinds": kinds,
            "anchor": {
                "kind": "around",
                "session_id": "sess-1",
                "seq": 420,
                "before": 6,
                "after": 9,
            },
        },
        _context(),
    )
    assert [item["seq"] for item in result["events"]] == [414, 429]
    cursor = result["page"]["prev_cursor"]
    for patch in ({"kinds": ["tool_result"]},):
        with pytest.raises(ValueError):
            await read({"cursor": cursor, **patch}, _context())
    decoded = ct._decode_cursor(cursor)
    for key, value in (("conversation_id", "foreign"), ("dir", "sideways"), ("seq", True)):
        bad = ct._encode_cursor({**decoded, key: value})
        with pytest.raises(ValueError):
            await read({"cursor": bad, "kinds": kinds}, _context())
    legacy = ct._encode_cursor(
        dict(tool="read_conversation_messages", sid="sess-1", seq=429, dir="b")
    )
    await read({"cursor": legacy}, _context())
    with pytest.raises(ValueError):
        await read({"cursor": legacy, "kinds": kinds}, _context())


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{"path": "a", "offset": 2}, '{\n"path": "a",\n"offset": 2\n}'])
async def test_arguments_pagination_exact_lookup(setup_history, value):
    history = History({"int-1": [event(414, arguments=value)]})
    store = SimpleNamespace(read=AsyncMock())
    result = await detail(history, store, ref(414, "tool_call"), "arguments", offset=2, limit=1)
    assert not result.is_error
    assert '"path"' in result.output
    assert result.metadata["source"] == "persisted_arguments"
    assert result.metadata["original_arguments_exact"] is False
    assert result.metadata["has_more"] is True
    assert history.calls[0][1] == {"seqs": [414], "limit": 1, "allow_missing_stream": True}
    store.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_recovery_and_preview_fallback(setup_history):
    history = History(
        {
            "int-1": [
                event(
                    429,
                    "tool_result",
                    recovery_call_id="synthetic-recovery-429",
                    result="first\nsecond\nthird",
                    has_full_output=True,
                    agent_visible_truncated=True,
                )
            ]
        }
    )
    store = SimpleNamespace(
        read=AsyncMock(
            return_value=SimpleNamespace(
                content="2: complete",
                offset=2,
                limit=1,
                total_lines=3,
                has_more=True,
            )
        )
    )
    result = await detail(history, store, ref(), offset=2, limit=1)
    assert result.metadata["source"] == "stored_output"
    assert result.output == "2: complete\n[Continue with offset=3.]"
    store.read.assert_awaited_once_with("synthetic-recovery-429", offset=2, limit=1)
    store.read.return_value = None
    result = await detail(history, store, ref(), offset=2, limit=1)
    assert result.metadata["source"] == "event_preview"
    assert result.metadata["store_status"] == "missing_or_expired"
    assert result.metadata["persisted_truncated"] is True
    assert result.output.endswith("2: second\n[Continue with offset=3.]")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"session_id": "foreign"},
        {"conversation_id": "foreign"},
        {"seq": 414},
        {"seq": True},
        {"kind": "tool_call"},
        {"call_id": "forged"},
        {"recovery_call_id": "forged"},
    ],
)
async def test_forged_references_never_read_store(setup_history, patch):
    history = History({"int-1": [event(429, "tool_result", has_full_output=True)]})
    store = SimpleNamespace(read=AsyncMock())
    result = await detail(history, store, {**ref(), **patch})
    assert result.is_error
    store.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["arguments", "result"])
async def test_pressure_pagination_never_skips_lines(setup_history, part):
    lines = [f"row-{i:03d} " + "x" * 300 for i in range(200)]
    history = History(
        {
            "int-1": [
                event(
                    429,
                    "tool_call" if part == "arguments" else "tool_result",
                    arguments="\n".join(lines),
                    has_full_output=True,
                )
            ]
        }
    )

    async def read_store(call_id, *, offset, limit):
        selected = lines[offset - 1 : offset - 1 + limit]
        return SimpleNamespace(
            content="\n".join(f"{offset + i}: {line}" for i, line in enumerate(selected)),
            offset=offset,
            limit=limit,
            total_lines=len(lines),
        )

    store = SimpleNamespace(read=read_store)
    offset = 1
    consumed = []
    for _ in range(20):
        result = await handle_tool_output_tool(
            "read_tool_output",
            {
                "call_id": ref()["call_id"],
                "reference": ref(429, "tool_call" if part == "arguments" else "tool_result"),
                "part": part,
                "offset": offset,
                "limit": 200,
            },
            store,
            pressure_mode="critical",
            session_factory=lambda: _SessionContext(),
            intaris=history,
            user_email="alice@example.com",
        )
        assert len(result.output) <= 12000
        count = result.metadata["returned_lines"]
        for index in range(offset - 1, offset - 1 + count):
            assert lines[index] in result.output
            consumed.append(index)
        if not result.metadata["has_more"]:
            break
        assert result.metadata["next_offset"] == offset + count
        offset = result.metadata["next_offset"]
    assert consumed == list(range(200))


@pytest.mark.asyncio
async def test_absent_store_and_oversize_line_are_truthful(setup_history):
    history = History({"int-1": [event(429, "tool_result", result="z" * 100_000)]})
    result = await detail(history, None, ref())
    assert result.metadata["store_status"] == "unavailable"
    assert result.metadata["content_truncated"]
    assert "cannot recover" in result.output
    assert len(result.output) <= 50_000


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["conversation", "session", "membership"])
async def test_owner_membership_denied_before_event_read(setup_history, target):
    conversation, rows = setup_history
    if target == "conversation":
        conversation.user_email = "foreign@example.com"
    elif target == "session":
        rows[0].user_email = "foreign@example.com"
    else:
        rows[0].conversation_id = "foreign"
    history = History({})
    store = SimpleNamespace(read=AsyncMock())
    assert (await detail(history, store, ref())).is_error
    assert history.calls == []
    store.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_bounded_arguments_and_missing_result(setup_history):
    history = History(
        {
            "int-1": [
                event(414, arguments=json.dumps({"_truncated": True, "_preview": "retained"})),
                event(429, "tool_result"),
            ]
        }
    )
    store = SimpleNamespace(read=AsyncMock())
    result = await detail(history, store, ref(414, "tool_call"), "arguments")
    assert result.metadata["persisted_truncated"]
    assert "retained" in result.output
    result = await detail(history, store, ref())
    assert result.is_error
    assert result.metadata["source"] == "unavailable"
    store.read.assert_not_awaited()
