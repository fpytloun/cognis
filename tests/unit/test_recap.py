"""Recaps are quiet lifecycle events, never agent turns or tool execution."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import RecapTimelineItem
from cognis.core.commands import CommandDispatcher, is_system_slash_command_message
from cognis.core.context import events_to_messages
from cognis.core.diff_stats import count_diff_lines
from cognis.core.recap import (
    RecapService,
    _evidence,
    _recent_window,
    _source_since_marker,
    _work_focus,
)
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    EventAppendResult,
    SessionEvent,
    SessionModel,
)
from cognis.runtime_context import (
    current_agent_id,
    current_agent_owner_email,
    current_user_email,
)


def _service(response: dict, events: list[dict]) -> tuple[RecapService, SimpleNamespace]:
    guardrails = SimpleNamespace(
        read_events=AsyncMock(return_value=SimpleNamespace(events=events)),
        record_events=AsyncMock(
            return_value=EventAppendResult(ok=True, count=1, first_seq=6, last_seq=6)
        ),
    )
    providers = SimpleNamespace(
        guardrails=guardrails,
        llm=SimpleNamespace(generate=AsyncMock(return_value=response)),
    )
    service = RecapService(
        session_factory=MagicMock(),
        providers=providers,
        scheduler=SimpleNamespace(
            has_active_turn=lambda _id: False,
            queued_count=lambda _id: 0,
            cluster_signals=SimpleNamespace(publish_chat_change=AsyncMock()),
        ),
        session_cache=SimpleNamespace(append_recorded_events=AsyncMock()),
        event_bus=SimpleNamespace(subscribe=MagicMock()),
    )
    service._auto_still_idle = AsyncMock(return_value=True)
    return service, providers


def _conversation(context_type: str = "web") -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-recap",
        user_email="owner@example.org",
        agent_id="agent-recap",
        context=ConversationContext(type=context_type),
    )


def _session() -> SessionModel:
    return SessionModel(
        session_id="session-recap",
        conversation_id="conv-recap",
        user_email="owner@example.org",
        agent_id="agent-recap",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("idle_states", [[False], [True, False]])
async def test_auto_defers_before_generation_or_publication_when_work_is_running(
    idle_states,
) -> None:
    service, providers = _service(
        {"choices": [{"message": {"content": '{"publish": true, "summary": "Completed."}'}}]},
        [
            {"seq": 1, "type": "user_message", "data": {"content": "Fix it"}},
            {
                "seq": 2,
                "type": "tool_result",
                "data": {"file_diffs": [{"path": "src/fix.py", "diff": "+fix\n"}]},
            },
            {"seq": 3, "type": "assistant_message", "data": {"content": "Fixed it."}},
        ],
    )
    service._auto_still_idle = AsyncMock(side_effect=idle_states)
    result = await service.generate(
        _conversation(), _session(), auto=True, agent_owner_email="owner@example.org"
    )
    assert result in {
        "Conversation is busy.",
        "Conversation resumed before the recap was published.",
    }
    providers.guardrails.record_events.assert_not_awaited()
    assert providers.llm.generate.await_count == len(idle_states) - 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind",
    ["managed", "managed_live", "managed_queued", "delegated", "task", "shell", "unknown", "idle"],
)
async def test_ongoing_work_sources_block_automatic_recap(monkeypatch, kind) -> None:
    service, providers = _service({}, [])
    service._scheduler.durable_running_turn_state = AsyncMock(return_value=None)
    service._scheduler.get_queued_messages = AsyncMock(return_value=[])
    if kind == "managed_live":
        service._scheduler.durable_running_turn_state.return_value = {"turn_id": "turn-child"}
    if kind == "managed_queued":
        service._scheduler.get_queued_messages.return_value = [{"queue_id": "queued-child"}]
    monkeypatch.setattr(
        "cognis.core.recap.list_active_delegation_sessions",
        AsyncMock(return_value=[object()] if kind == "delegated" else []),
    )
    monkeypatch.setattr(
        "cognis.core.recap.list_active_tasks_by_source_conversation",
        AsyncMock(return_value=[object()] if kind == "task" else []),
    )
    monkeypatch.setattr(
        "cognis.core.recap.list_open_managed_conversation_links",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    turn_state="running" if kind == "managed" else "idle",
                    target_conversation_id="conv-child",
                )
            ]
        ),
    )
    connection = SimpleNamespace(
        background_shell_status=AsyncMock(
            return_value={
                "shells": [
                    {
                        "user_email": "someone@example.org",
                        "conversation_id": "conv-recap",
                        "status": "running",
                    },
                    {
                        "user_email": "owner@example.org",
                        "conversation_id": "conv-other",
                        "status": "running",
                    },
                    {
                        "user_email": "owner@example.org",
                        "conversation_id": "conv-recap",
                        "status": "running" if kind == "shell" else "completed",
                    },
                ]
            }
        )
    )
    if kind == "unknown":
        connection.background_shell_status.side_effect = TimeoutError()
    providers.executor = SimpleNamespace(
        list_active=AsyncMock(return_value=[SimpleNamespace(executor_id="executor-test")]),
        get_executor=AsyncMock(return_value=connection),
    )
    assert await service._has_ongoing_work(_conversation()) is (kind != "idle")


@pytest.mark.anyio
async def test_recap_is_auxiliary_generation_side_call_with_no_turn_or_tools() -> None:
    service, providers = _service(
        {"choices": [{"message": {"content": "We researched caching. The next step is testing."}}]},
        [
            {"seq": 1, "type": "user_message", "data": {"content": "Explore caching"}},
            {"seq": 2, "type": "assistant_message", "data": {"content": "Found two approaches."}},
        ],
    )
    result = await service.generate(
        _conversation(), _session(), agent_owner_email="owner@example.org"
    )
    assert result == "We researched caching. The next step is testing."
    kwargs = providers.llm.generate.await_args.kwargs
    assert kwargs["task_type"] == "auxiliary_generation"
    assert "tools" not in kwargs
    assert len(providers.llm.generate.await_args.args[0]) == 1
    append = providers.guardrails.record_events.await_args
    assert append.args[1][0].type == "lifecycle"
    assert append.args[1][0].data["event"] == "conversation_recap"
    assert append.kwargs["idempotency_key"].startswith("session-recap:recap:")
    assert len(append.kwargs["idempotency_key"]) <= 256
    service._scheduler.cluster_signals.publish_chat_change.assert_awaited_once_with(
        "conv-recap", session_id="session-recap", revision=6
    )
    first_key = append.kwargs["idempotency_key"]
    await service.generate(_conversation(), _session(), agent_owner_email="owner@example.org")
    assert providers.guardrails.record_events.await_args.kwargs["idempotency_key"] == first_key


@pytest.mark.anyio
async def test_recap_reads_and_writes_intaris_as_conversation_owner() -> None:
    service, providers = _service(
        {"choices": [{"message": {"content": "We completed the investigation."}}]},
        [
            {"seq": 1, "type": "user_message", "data": {"content": "Investigate"}},
            {"seq": 2, "type": "assistant_message", "data": {"content": "Completed."}},
        ],
    )
    seen: list[tuple[str | None, str | None, str | None]] = []

    async def read_events(*_args: object, **_kwargs: object) -> SimpleNamespace:
        seen.append(
            (
                current_user_email.get(),
                current_agent_id.get(),
                current_agent_owner_email.get(),
            )
        )
        return SimpleNamespace(
            events=[
                {"seq": 1, "type": "user_message", "data": {"content": "Investigate"}},
                {"seq": 2, "type": "assistant_message", "data": {"content": "Completed."}},
            ]
        )

    async def record_events(*_args: object, **_kwargs: object) -> EventAppendResult:
        seen.append(
            (
                current_user_email.get(),
                current_agent_id.get(),
                current_agent_owner_email.get(),
            )
        )
        return EventAppendResult(ok=True, count=1, first_seq=6, last_seq=6)

    providers.guardrails.read_events.side_effect = read_events
    providers.guardrails.record_events.side_effect = record_events
    assert (
        await service.generate(
            _conversation(), _session(), agent_owner_email="shared-agent-owner@example.org"
        )
        == "We completed the investigation."
    )
    assert seen == [("owner@example.org", "agent-recap", "shared-agent-owner@example.org")] * 2
    assert current_user_email.get() is None
    assert current_agent_id.get() is None
    assert current_agent_owner_email.get() is None


@pytest.mark.anyio
async def test_recap_rejects_tool_proposal_without_recording() -> None:
    service, providers = _service(
        {"choices": [{"message": {"content": "", "tool_calls": [{"id": "call-1"}]}}]},
        [{"seq": 1, "type": "user_message", "data": {"content": "Do some research"}}],
    )
    with pytest.raises(RuntimeError, match="execution denied"):
        await service.generate(_conversation(), _session(), agent_owner_email="owner@example.org")
    providers.guardrails.record_events.assert_not_awaited()


@pytest.mark.anyio
async def test_recap_excludes_task_context_and_channel_auto() -> None:
    service, providers = _service({}, [])
    assert "only available in chat" in await service.generate(
        _conversation("task"), _session(), agent_owner_email="owner@example.org"
    )
    assert "web chat" in await service.generate(
        _conversation("signal"), _session(), agent_owner_email="owner@example.org", auto=True
    )
    providers.llm.generate.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("context_type", ["web", "signal"])
async def test_recap_slash_dispatches_to_quiet_service(context_type: str) -> None:
    recap_service = SimpleNamespace(generate=AsyncMock(return_value="We completed the research."))
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=None,
        notification_service=None,
        turn_scheduler=SimpleNamespace(has_active_turn=lambda _id: False),
        recap_service=recap_service,
    )
    assert is_system_slash_command_message("/recap")
    conversation = _conversation(context_type)
    session = _session()
    result = await dispatcher.dispatch(
        "/recap",
        conversation=conversation,
        session=session,
        agent=SimpleNamespace(owner_email="shared-agent-owner@example.org"),
        user_email="owner@example.org",
    )
    assert result is not None
    assert result.type == "recap"
    assert result.text == "We completed the research."
    recap_service.generate.assert_awaited_once_with(
        conversation, session, agent_owner_email="shared-agent-owner@example.org"
    )


def test_recap_projects_but_does_not_require_assistant_message() -> None:
    events = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="session-recap",
                seq=7,
                type="lifecycle",
                data={
                    "event": "conversation_recap",
                    "text": "We chose a safer route.",
                    "source_session_id": "session-recap",
                    "source_seq": 6,
                    "stats": {"deliverables": [{"id": "dlv-1", "title": "Plan"}]},
                },
            )
        ]
    )
    item = project_timeline(events.events).timeline.items[0]
    assert isinstance(item, RecapTimelineItem)
    assert item.deliverables[0]["title"] == "Plan"
    assert item.text == "We chose a safer route."
    assert item.stats_version == 1
    assert item.file_diffs_omitted is False
    assert item.scope == "legacy"


def test_recap_projects_persisted_artifact_sizes_as_numbers() -> None:
    events = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="session-recap",
                seq=7,
                type="lifecycle",
                data={
                    "event": "conversation_recap",
                    "text": "Generated images",
                    "stats": {
                        "artifacts": [
                            {
                                "id": "art-1",
                                "title": "image.png",
                                "mime_type": "image/png",
                                "size_bytes": 73269,
                            }
                        ]
                    },
                },
            )
        ]
    )

    item = project_timeline(events.events).timeline.items[0]
    assert isinstance(item, RecapTimelineItem)
    assert item.model_dump()["artifacts"] == [
        {"id": "art-1", "title": "image.png", "mime_type": "image/png", "size_bytes": 73269}
    ]


def test_recap_projects_versioned_file_stats_and_omissions() -> None:
    events = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="session-recap",
                seq=8,
                type="lifecycle",
                data={
                    "event": "conversation_recap",
                    "text": "Edited files",
                    "source_session_id": "session-recap",
                    "source_seq": 7,
                    "stats_version": 2,
                    "stats": {
                        "files": [{"path": "src/example.py", "additions": 2, "deletions": 1}],
                        "file_diffs_omitted": True,
                    },
                },
            )
        ]
    )
    item = project_timeline(events.events).timeline.items[0]
    assert isinstance(item, RecapTimelineItem)
    assert item.stats_version == 2
    assert item.file_diffs_omitted is True


@pytest.mark.parametrize("stats", [None, "not stats", []])
def test_recap_projects_missing_or_invalid_stats_with_empty_defaults(stats: object) -> None:
    events = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="session-recap",
                seq=8,
                type="lifecycle",
                data={"event": "conversation_recap", "text": "Older recap", "stats": stats},
            )
        ]
    )
    item = project_timeline(events.events).timeline.items[0]
    assert isinstance(item, RecapTimelineItem)
    assert item.deliverables == []
    assert item.artifacts == []
    assert item.files == []
    assert item.file_diffs_omitted is False


@pytest.mark.anyio
async def test_manual_recap_uses_recent_window_and_compaction_memory_not_previous_recap() -> None:
    events = [
        {"seq": 1, "type": "user_message", "data": {"content": "Old task"}},
        {
            "seq": 2,
            "type": "tool_result",
            "data": {"file_diffs": [{"path": "src/old.py", "diff": "@@ -1 +1 @@\n-old\n+new\n"}]},
        },
        {
            "seq": 3,
            "type": "compaction_summary",
            "data": {"summary": "The continuing goal is to improve chat recaps."},
        },
        {
            "seq": 4,
            "type": "lifecycle",
            "data": {
                "event": "conversation_recap",
                "text": "A previous recap should not become the memory source.",
                "source_session_id": "session-recap",
                "source_seq": 2,
            },
        },
    ]
    events.extend(
        {
            "seq": seq,
            "type": "user_message",
            "data": {"content": f"Recent request {seq}"},
        }
        for seq in range(5, 35)
    )
    service, providers = _service(
        {"choices": [{"message": {"content": "Recent work is ready."}}]},
        events,
    )
    assert (
        await service.generate(_conversation(), _session(), agent_owner_email="owner@example.org")
        == "Recent work is ready."
    )
    prompt = providers.llm.generate.await_args.args[0][0]["content"]
    assert "The continuing goal is to improve chat recaps." in prompt
    assert "A previous recap should not become the memory source." not in prompt
    assert "Old task" not in prompt
    assert "Recent request 5" in prompt
    recorded = providers.guardrails.record_events.await_args.args[1][0].data
    assert recorded["scope"] == "recent_window"
    assert recorded["window_message_limit"] == 30
    assert recorded["stats"]["files"] == []


def test_shared_diff_counts_content_lines_that_resemble_headers() -> None:
    assert count_diff_lines(
        "--- a/example.py\n+++ b/example.py\n"
        "@@ -1,2 +1,2 @@\n"
        "---deleted content\n"
        "+++added content\n"
    ) == (1, 1)


def test_recap_lifecycle_is_not_used_to_assemble_agent_context() -> None:
    events = [
        {"type": "user_message", "data": {"content": "Choose a route"}},
        {
            "type": "lifecycle",
            "data": {
                "event": "conversation_recap",
                "text": "Private recap card",
                "source_session_id": "session-recap",
                "source_seq": 1,
            },
        },
    ]
    assert events_to_messages(events) == events_to_messages(
        [
            {"type": "user_message", "data": {"content": "Choose a route"}},
        ]
    )


def test_evidence_keeps_tool_free_discussion_and_deduplicates_file_stats() -> None:
    messages, stats = _evidence(
        [
            ("session-recap", {"type": "user_message", "seq": 1, "data": {"content": "Choose A"}}),
            ("session-recap", {"type": "assistant_message", "seq": 2, "data": {"content": "A"}}),
            (
                "session-recap",
                {
                    "type": "file_diff",
                    "seq": 3,
                    "data": {"file_diffs": [{"path": "src/a.py", "additions": 3, "deletions": 1}]},
                },
            ),
            (
                "session-recap",
                {
                    "type": "file_diff",
                    "seq": 4,
                    "data": {"file_diffs": [{"path": "src/a.py", "additions": 2, "deletions": 0}]},
                },
            ),
        ]
    )
    assert len(messages) == 2
    assert stats["user_turns"] == 1
    assert stats["files"] == [{"path": "src/a.py", "additions": 5, "deletions": 1}]


def test_tool_result_evidence_includes_output_diffs_and_artifacts() -> None:
    messages, stats = _evidence(
        [
            (
                "session-recap",
                {
                    "type": "tool_result",
                    "seq": 7,
                    "data": {
                        "result": "Three sources confirmed the result.",
                        "file_diffs": [
                            {
                                "path": "src/example.py",
                                "diff": (
                                    "--- a/src/example.py\n"
                                    "+++ b/src/example.py\n"
                                    "@@ -1,2 +1,3 @@\n"
                                    "-old\n"
                                    "+new\n"
                                    "+more\n"
                                    " unchanged\n"
                                ),
                            }
                        ],
                        "attachments": [
                            {
                                "artifact_id": "art-1",
                                "filename": "chart.png",
                                "mime_type": "image/png",
                                "size_bytes": 120,
                            }
                        ],
                    },
                },
            )
        ]
    )
    assert messages == []
    assert stats["tool_results"] == 1
    assert stats["files"] == [{"path": "src/example.py", "additions": 2, "deletions": 1}]
    assert stats["artifacts"][0]["title"] == "chart.png"


def test_recap_does_not_report_zero_for_missing_or_truncated_diff() -> None:
    _messages, stats = _evidence(
        [
            (
                "session-recap",
                {
                    "type": "tool_result",
                    "seq": 1,
                    "data": {
                        "file_diffs": [
                            {"path": "src/no-preview.py", "diff": ""},
                            {
                                "path": "src/partial.py",
                                "diff": "@@ -1 +1 @@\n-old\n+new\n",
                                "truncated": True,
                            },
                            {"path": "", "diff": "", "truncated": True, "omitted_count": 5},
                        ]
                    },
                },
            ),
        ]
    )
    assert stats["files"] == [
        {"path": "src/no-preview.py", "additions": None, "deletions": None},
        {"path": "src/partial.py", "additions": None, "deletions": None},
    ]
    assert stats["file_diffs_omitted"] is True


def test_recap_unknown_count_propagates_across_repeated_file_edits() -> None:
    _messages, stats = _evidence(
        [
            (
                "session-recap",
                {
                    "type": "tool_result",
                    "seq": 1,
                    "data": {
                        "file_diffs": [
                            {"path": "src/example.py", "diff": "@@ -1 +1 @@\n-old\n+new\n"}
                        ]
                    },
                },
            ),
            (
                "session-recap",
                {
                    "type": "tool_result",
                    "seq": 2,
                    "data": {
                        "file_diffs": [{"path": "src/example.py", "diff": "", "truncated": True}]
                    },
                },
            ),
        ]
    )
    assert stats["files"] == [{"path": "src/example.py", "additions": None, "deletions": None}]


def test_concurrent_turn_before_recap_marker_remains_new_work() -> None:
    flattened = [
        ("session-recap", {"type": "user_message", "seq": 1, "data": {"content": "first"}}),
        ("session-recap", {"type": "assistant_message", "seq": 2, "data": {"content": "done"}}),
        ("session-recap", {"type": "user_message", "seq": 3, "data": {"content": "new work"}}),
        (
            "session-recap",
            {
                "type": "lifecycle",
                "seq": 4,
                "data": {
                    "event": "conversation_recap",
                    "source_session_id": "session-recap",
                    "source_seq": 2,
                    "text": "First work",
                },
            },
        ),
    ]
    source, marker = _source_since_marker(flattened)
    assert marker is flattened[-1][1]
    assert [event["seq"] for _, event in source] == [3]


def test_skipped_recap_preserves_pending_work_until_a_published_recap() -> None:
    flattened = [
        ("session-recap", {"type": "user_message", "seq": 1, "data": {"content": "Research"}}),
        ("session-recap", {"type": "assistant_message", "seq": 2, "data": {"content": "Ongoing"}}),
        (
            "session-recap",
            {
                "type": "lifecycle",
                "seq": 3,
                "data": {
                    "event": "conversation_recap_skipped",
                    "source_session_id": "session-recap",
                    "source_seq": 2,
                },
            },
        ),
        ("session-recap", {"type": "assistant_message", "seq": 4, "data": {"content": "Done"}}),
    ]
    source, marker = _source_since_marker(flattened)
    assert marker is None
    assert [event["seq"] for _, event in source] == [1, 2, 4]


def test_recent_window_limits_stats_to_the_same_30_messages_as_prose() -> None:
    flattened = [
        ("session-recap", {"type": "user_message", "seq": 1, "data": {"content": "Old work"}}),
        (
            "session-recap",
            {
                "type": "tool_result",
                "seq": 2,
                "data": {
                    "file_diffs": [{"path": "src/old.py", "diff": "@@ -1 +1 @@\n-old\n+new\n"}]
                },
            },
        ),
        (
            "session-recap",
            {"type": "assistant_message", "seq": 3, "data": {"content": "Old result"}},
        ),
        (
            "session-recap",
            {
                "type": "lifecycle",
                "seq": 4,
                "data": {
                    "event": "conversation_recap",
                    "text": "We finished the old work.",
                    "source_session_id": "session-recap",
                    "source_seq": 3,
                },
            },
        ),
    ]
    flattened.extend(
        (
            "session-recap",
            {
                "type": "user_message",
                "seq": seq,
                "data": {"content": f"Recent request {seq}"},
            },
        )
        for seq in range(5, 35)
    )
    window = _recent_window(flattened)
    messages, stats = _evidence(window)
    assert len(messages) == 30
    assert messages[0]["content"] == "Recent request 5"
    assert stats["files"] == []
    assert _source_since_marker(flattened)[0][0][1]["seq"] == 5


def test_long_turn_keeps_request_and_terminal_reply_without_progress_or_tool_text() -> None:
    flattened: list[tuple[str, dict[str, Any]]] = [
        (
            "session-recap",
            {
                "type": "user_message",
                "seq": 1,
                "data": {
                    "turn_id": "turn-long",
                    "content": "Internal augmented prompt",
                    "user_visible_content": "Investigate and fix the cache bug",
                },
            },
        ),
    ]
    for seq in range(2, 42):
        flattened.append(
            (
                "session-recap",
                {
                    "type": "tool_result",
                    "seq": seq,
                    "data": {"name": "read", "result": f"Tool output {seq}"},
                },
            )
        )
        if seq % 10 == 0:
            flattened.append(
                (
                    "session-recap",
                    {
                        "type": "assistant_message",
                        "seq": 50 + seq,
                        "data": {"turn_id": "turn-long", "content": f"Progress update {seq}"},
                    },
                )
            )
    flattened.append(
        (
            "session-recap",
            {
                "type": "assistant_message",
                "seq": 100,
                "data": {"turn_id": "turn-long", "content": "Fixed and verified the cache bug."},
            },
        )
    )
    window = _recent_window(flattened)
    messages, stats = _evidence(window)
    assert messages == [
        {"role": "User", "content": "Investigate and fix the cache bug"},
        {"role": "Assistant", "content": "Fixed and verified the cache bug."},
    ]
    assert stats["tool_results"] == 40


def test_focuses_last_completed_change_not_later_merge_status() -> None:
    events = [
        (
            "session-recap",
            {
                "seq": 1,
                "type": "user_message",
                "data": {"turn_id": "fix", "content": "Fix the PDF renderer"},
            },
        ),
        (
            "session-recap",
            {
                "seq": 2,
                "type": "tool_result",
                "data": {
                    "turn_id": "fix",
                    "result": "private tool output",
                    "file_diffs": [{"path": "src/renderer.py", "diff": "+fix\n"}],
                },
            },
        ),
        (
            "session-recap",
            {
                "seq": 3,
                "type": "assistant_message",
                "data": {"turn_id": "fix", "content": "Fixed PDF table and chart rendering."},
            },
        ),
        (
            "session-recap",
            {
                "seq": 4,
                "type": "user_message",
                "data": {"turn_id": "merge", "content": "Merge and push"},
            },
        ),
        (
            "session-recap",
            {
                "seq": 5,
                "type": "assistant_message",
                "data": {"turn_id": "merge", "content": "Merged and pushed."},
            },
        ),
    ]
    assert _work_focus(events) == ("Fix the PDF renderer", "Fixed PDF table and chart rendering.")


@pytest.mark.anyio
async def test_auto_recovers_completed_work_when_classifier_declines_merge_status() -> None:
    events = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"turn_id": "fix", "content": "Fix the PDF renderer"},
        },
        {
            "seq": 2,
            "type": "tool_result",
            "data": {
                "turn_id": "fix",
                "result": "Private output",
                "file_diffs": [{"path": "src/renderer.py", "diff": "+fix\n"}],
            },
        },
        {
            "seq": 3,
            "type": "assistant_message",
            "data": {"turn_id": "fix", "content": "Fixed PDF table and chart rendering."},
        },
        {
            "seq": 4,
            "type": "user_message",
            "data": {"turn_id": "merge", "content": "Merge and push"},
        },
        {
            "seq": 5,
            "type": "assistant_message",
            "data": {"turn_id": "merge", "content": "Merged and pushed."},
        },
    ]
    service, providers = _service(
        {"choices": [{"message": {"content": '{"publish": false, "summary": ""}'}}]},
        events,
    )
    providers.llm.generate.side_effect = [
        {"choices": [{"message": {"content": '{"publish": false, "summary": ""}'}}]},
        {
            "choices": [
                {
                    "message": {
                        "content": "Fixed PDF table and chart rendering; "
                        "the change is merged and pushed."
                    }
                }
            ]
        },
    ]
    assert (
        await service.generate(
            _conversation(), _session(), auto=True, agent_owner_email="owner@example.org"
        )
        == "Fixed PDF table and chart rendering; the change is merged and pushed."
    )
    assert providers.llm.generate.await_count == 2
    prompts = [call.args[0][0]["content"] for call in providers.llm.generate.await_args_list]
    assert all("Fixed PDF table and chart rendering." in prompt for prompt in prompts)
    assert all("Private output" not in prompt for prompt in prompts)
    assert providers.guardrails.record_events.await_args.args[1][0].data["event"] == (
        "conversation_recap"
    )


@pytest.mark.anyio
async def test_legacy_skip_is_reconsidered_once_under_current_policy() -> None:
    events = [
        {"seq": 1, "type": "user_message", "data": {"turn_id": "fix", "content": "Fix rendering"}},
        {
            "seq": 2,
            "type": "tool_result",
            "data": {"turn_id": "fix", "file_diffs": [{"path": "src/render.py", "diff": "+fix\n"}]},
        },
        {
            "seq": 3,
            "type": "assistant_message",
            "data": {"turn_id": "fix", "content": "Fixed renderer and tested it."},
        },
        {
            "seq": 4,
            "type": "lifecycle",
            "data": {
                "event": "conversation_recap_skipped",
                "source_seq": 3,
                "source_session_id": "session-recap",
                "auto": True,
            },
        },
    ]
    service, providers = _service(
        {
            "choices": [
                {
                    "message": {
                        "content": ('{"publish": true, "summary": "Renderer fixed and tested."}')
                    }
                }
            ]
        },
        events,
    )
    assert (
        await service.generate(
            _conversation(), _session(), auto=True, agent_owner_email="owner@example.org"
        )
        == "Renderer fixed and tested."
    )
    marker = providers.guardrails.record_events.await_args.args[1][0].data
    assert marker["event"] == "conversation_recap"
    assert marker["policy_version"] == 4


def test_mid_turn_user_update_survives_when_progress_is_not_recap_text() -> None:
    window = _recent_window(
        [
            (
                "session-recap",
                {
                    "seq": 1,
                    "type": "user_message",
                    "data": {"turn_id": "turn-1", "content": "Fix the bug"},
                },
            ),
            (
                "session-recap",
                {
                    "seq": 2,
                    "type": "assistant_message",
                    "data": {"turn_id": "turn-1", "content": "Checking logs"},
                },
            ),
            (
                "session-recap",
                {
                    "seq": 3,
                    "type": "tool_result",
                    "data": {"name": "read", "result": "Private diagnostic output"},
                },
            ),
            (
                "session-recap",
                {
                    "seq": 4,
                    "type": "user_message",
                    "data": {"turn_id": "turn-1", "content": "Also check the cache"},
                },
            ),
            (
                "session-recap",
                {
                    "seq": 5,
                    "type": "assistant_message",
                    "data": {"turn_id": "turn-1", "content": "Fixed the cache bug"},
                },
            ),
        ]
    )
    messages, stats = _evidence(window)
    assert messages == [
        {"role": "User", "content": "Fix the bug"},
        {"role": "User", "content": "Also check the cache"},
        {"role": "Assistant", "content": "Fixed the cache bug"},
    ]
    assert stats["user_turns"] == 1
    assert stats["tool_results"] == 1


@pytest.mark.anyio
async def test_auto_recap_does_not_count_tool_reads_as_completed_work() -> None:
    service, providers = _service(
        {"choices": [{"message": {"content": '{"publish": true, "summary": "Answered."}'}}]},
        [
            {"seq": 1, "type": "user_message", "data": {"content": "When is the timer?"}},
            {"seq": 2, "type": "tool_result", "data": {"result": "IDLE_SECONDS=300"}},
            {"seq": 3, "type": "tool_result", "data": {"result": "After turn completion"}},
            {"seq": 4, "type": "assistant_message", "data": {"content": "After five minutes."}},
        ],
    )
    result = await service.generate(
        _conversation(), _session(), agent_owner_email="owner@example.org", auto=True
    )
    assert result == "Not enough new activity to recap."
    providers.llm.generate.assert_not_awaited()
    providers.guardrails.record_events.assert_not_awaited()


@pytest.mark.anyio
async def test_auto_recap_considers_substantial_read_only_one_turn_from_messages() -> None:
    final = "We investigated the failure across multiple sources and confirmed the root cause. " * 9
    service, providers = _service(
        {
            "choices": [
                {"message": {"content": '{"publish": true, "summary": "Research completed."}'}}
            ]
        },
        [
            {
                "seq": 1,
                "type": "user_message",
                "data": {"turn_id": "research", "content": "Investigate the failure"},
            },
            {"seq": 2, "type": "tool_result", "data": {"result": "Raw private tool output"}},
            {
                "seq": 3,
                "type": "assistant_message",
                "data": {"turn_id": "research", "content": final},
            },
        ],
    )
    assert (
        await service.generate(
            _conversation(), _session(), agent_owner_email="owner@example.org", auto=True
        )
        == "Research completed."
    )
    prompt = providers.llm.generate.await_args.args[0][0]["content"]
    assert "Investigate the failure" in prompt
    assert "We investigated the failure" in prompt
    assert "Raw private tool output" not in prompt


@pytest.mark.anyio
async def test_long_single_turn_fetches_request_before_350_tool_events() -> None:
    final = "We found and fixed the root cause, and tests confirm the result. " * 10
    events = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"turn_id": "turn-long", "content": "Fix the login bug"},
        },
        *(
            {"seq": seq, "type": "tool_result", "data": {"result": f"Private output {seq}"}}
            for seq in range(2, 401)
        ),
        {
            "seq": 401,
            "type": "assistant_message",
            "data": {"turn_id": "turn-long", "content": final},
        },
    ]
    service, providers = _service(
        {
            "choices": [
                {"message": {"content": '{"publish": true, "summary": "The login bug is fixed."}'}}
            ]
        },
        events,
    )

    async def read_events(
        _session_id: str,
        *,
        last_n: int | None = None,
        before_seq: int | None = None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        if last_n is not None:
            return SimpleNamespace(events=events[-last_n:], has_more=True)
        assert before_seq == 52
        return SimpleNamespace(events=events[:51], has_more=False)

    providers.guardrails.read_events.side_effect = read_events
    assert (
        await service.generate(
            _conversation(), _session(), agent_owner_email="owner@example.org", auto=True
        )
        == "The login bug is fixed."
    )
    assert providers.guardrails.read_events.await_count == 2
    prompt = providers.llm.generate.await_args.args[0][0]["content"]
    assert "Fix the login bug" in prompt
    assert final[:100] in prompt
    assert "Private output" not in prompt


@pytest.mark.anyio
async def test_too_long_single_turn_does_not_recap_answer_without_request() -> None:
    events = [
        {"seq": 1, "type": "user_message", "data": {"turn_id": "long", "content": "Fix it"}},
        *(
            {"seq": seq, "type": "tool_result", "data": {"result": f"Tool output {seq}"}}
            for seq in range(2, 1601)
        ),
        {
            "seq": 1601,
            "type": "assistant_message",
            "data": {"turn_id": "long", "content": "Fixed and validated the issue. " * 25},
        },
    ]
    service, providers = _service(
        {"choices": [{"message": {"content": '{"publish": true, "summary": "Done."}'}}]},
        events,
    )

    async def read_events(
        _session_id: str,
        *,
        last_n: int | None = None,
        before_seq: int | None = None,
        limit: int | None = None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        if last_n is not None:
            return SimpleNamespace(events=events[-last_n:], has_more=True)
        assert before_seq is not None and limit is not None
        older = [event for event in events if event["seq"] < before_seq]
        return SimpleNamespace(events=older[-limit:], has_more=len(older) > limit)

    providers.guardrails.read_events.side_effect = read_events
    assert (
        await service.generate(
            _conversation(), _session(), agent_owner_email="owner@example.org", auto=True
        )
        == "Recent user request is outside the recap history window."
    )
    providers.llm.generate.assert_not_awaited()
    providers.guardrails.record_events.assert_not_awaited()


@pytest.mark.anyio
async def test_auto_recap_declines_plain_discussion_and_retries_only_with_new_work() -> None:
    events = [
        {"seq": 1, "type": "user_message", "data": {"content": "How does the timer work?"}},
        {"seq": 2, "type": "assistant_message", "data": {"content": "After five minutes."}},
        {"seq": 3, "type": "user_message", "data": {"content": "How about previous chats?"}},
        {"seq": 4, "type": "assistant_message", "data": {"content": "No backfill."}},
        {"seq": 5, "type": "user_message", "data": {"content": "How does Claude do it?"}},
        {"seq": 6, "type": "assistant_message", "data": {"content": "It uses recent messages."}},
    ]
    service, providers = _service(
        {"choices": [{"message": {"content": '{"publish": false, "summary": ""}'}}]},
        events,
    )

    async def record_events(
        _session_id: str, appended: list[SessionEvent], **_kwargs: Any
    ) -> EventAppendResult:
        events.append({"seq": len(events) + 1, "type": "lifecycle", "data": appended[0].data})
        return EventAppendResult(ok=True, count=1, first_seq=len(events), last_seq=len(events))

    providers.guardrails.record_events.side_effect = record_events
    args = (_conversation(), _session())
    assert await service.generate(*args, agent_owner_email="owner@example.org", auto=True) == (
        "Nothing worth recapping yet."
    )
    prompt = providers.llm.generate.await_args.args[0][0]["content"]
    assert "NEW activity" in prompt
    assert "short answer" in prompt
    assert events[-1]["data"]["event"] == "conversation_recap_skipped"
    assert await service.generate(*args, agent_owner_email="owner@example.org", auto=True) == (
        "No new activity since the last recap check."
    )
    assert providers.llm.generate.await_count == 1
    events.extend(
        [
            {"seq": 8, "type": "user_message", "data": {"content": "Implement the agreed fix"}},
            {"seq": 9, "type": "assistant_message", "data": {"content": "Implemented and tested."}},
        ]
    )
    providers.llm.generate.return_value = {
        "choices": [
            {"message": {"content": '{"publish": true, "summary": "Implemented and tested."}'}}
        ]
    }
    assert await service.generate(*args, agent_owner_email="owner@example.org", auto=True) == (
        "Implemented and tested."
    )
    prompt = providers.llm.generate.await_args.args[0][0]["content"]
    assert "How does the timer work?" in prompt
    assert "Implement the agreed fix" in prompt
    assert events[-1]["data"]["event"] == "conversation_recap"


@pytest.mark.anyio
async def test_unfinished_five_turn_plan_then_single_turn_build_recaps_completed_work() -> None:
    events: list[dict[str, Any]] = []
    for index in range(5):
        events.extend(
            [
                {
                    "seq": len(events) + 1,
                    "type": "user_message",
                    "data": {
                        "turn_id": f"plan-{index}",
                        "content": f"Consider feature option {index}",
                    },
                },
                {
                    "seq": len(events) + 2,
                    "type": "assistant_message",
                    "data": {
                        "turn_id": f"plan-{index}",
                        "content": "We have not finished planning.",
                    },
                },
            ]
        )
    service, providers = _service(
        {"choices": [{"message": {"content": '{"publish": false, "summary": ""}'}}]},
        events,
    )

    async def record_events(
        _session_id: str, appended: list[SessionEvent], **_kwargs: Any
    ) -> EventAppendResult:
        seq = len(events) + 1
        events.append({"seq": seq, "type": "lifecycle", "data": appended[0].data})
        return EventAppendResult(ok=True, count=1, first_seq=seq, last_seq=seq)

    providers.guardrails.record_events.side_effect = record_events
    args = (_conversation(), _session())
    assert await service.generate(*args, agent_owner_email="owner@example.org", auto=True) == (
        "Nothing worth recapping yet."
    )
    assert events[-1]["data"]["event"] == "conversation_recap_skipped"
    events.extend(
        [
            {
                "seq": len(events) + 1,
                "type": "user_message",
                "data": {"turn_id": "build", "content": "Build the planned feature"},
            },
            {
                "seq": len(events) + 2,
                "type": "tool_result",
                "data": {
                    "file_diffs": [
                        {"path": "src/feature.py", "diff": "@@ -0,0 +1 @@\n+implemented\n"}
                    ]
                },
            },
            {
                "seq": len(events) + 3,
                "type": "assistant_message",
                "data": {"turn_id": "build", "content": "Built the feature and verified it."},
            },
        ]
    )
    providers.llm.generate.return_value = {
        "choices": [
            {"message": {"content": '{"publish": true, "summary": "The feature is built."}'}}
        ]
    }
    assert await service.generate(*args, agent_owner_email="owner@example.org", auto=True) == (
        "The feature is built."
    )
    prompt = providers.llm.generate.await_args.args[0][0]["content"]
    assert "Consider feature option 0" in prompt
    assert "Build the planned feature" in prompt
    assert "Built the feature and verified it." in prompt
    assert events[-1]["data"]["stats"]["files"][0]["path"] == "src/feature.py"


@pytest.mark.anyio
async def test_returning_after_published_plan_recaps_one_turn_build_not_old_plan() -> None:
    events = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"turn_id": "plan", "content": "Choose a feature design"},
        },
        {
            "seq": 2,
            "type": "assistant_message",
            "data": {"turn_id": "plan", "content": "We agreed on the interface."},
        },
        {
            "seq": 3,
            "type": "lifecycle",
            "timestamp": "2001-01-01T00:00:00Z",
            "data": {
                "event": "conversation_recap",
                "text": "The design is agreed.",
                "source_session_id": "session-recap",
                "source_seq": 2,
            },
        },
        {
            "seq": 4,
            "type": "user_message",
            "data": {"turn_id": "build", "content": "Build the agreed feature"},
        },
        {
            "seq": 5,
            "type": "tool_result",
            "data": {
                "result": "Raw implementation output",
                "file_diffs": [{"path": "src/feature.py", "diff": "@@ -0,0 +1 @@\n+implemented\n"}],
            },
        },
        {
            "seq": 6,
            "type": "assistant_message",
            "data": {"turn_id": "build", "content": "Built the feature and tests passed."},
        },
    ]
    service, providers = _service(
        {
            "choices": [
                {"message": {"content": '{"publish": true, "summary": "The feature is built."}'}}
            ]
        },
        events,
    )
    # Opening an old conversation does not enqueue an automatic job.
    assert not service._jobs
    assert (
        await service.generate(
            _conversation(), _session(), agent_owner_email="owner@example.org", auto=True
        )
        == "The feature is built."
    )
    prompt = providers.llm.generate.await_args.args[0][0]["content"]
    assert "Build the agreed feature" in prompt
    assert "Built the feature and tests passed." in prompt
    assert "Raw implementation output" not in prompt
    assert "New activity to assess for automatic recap:\nUser: Build" in prompt
    assert providers.guardrails.record_events.await_args.args[1][0].data["source_seq"] == 6


@pytest.mark.anyio
async def test_manual_recap_can_publish_same_source_after_auto_skip() -> None:
    events = [
        {"seq": 1, "type": "user_message", "data": {"content": "Discuss a decision"}},
        {"seq": 2, "type": "assistant_message", "data": {"content": "Option A"}},
        {"seq": 3, "type": "user_message", "data": {"content": "Consider option B"}},
        {"seq": 4, "type": "assistant_message", "data": {"content": "Option B"}},
        {"seq": 5, "type": "user_message", "data": {"content": "Which is safer?"}},
        {"seq": 6, "type": "assistant_message", "data": {"content": "Option B is safer."}},
    ]
    service, providers = _service(
        {"choices": [{"message": {"content": '{"publish": false, "summary": ""}'}}]},
        events,
    )
    recorded_keys: set[str] = set()

    async def record_events(
        _session_id: str, appended: list[SessionEvent], *, idempotency_key: str, **_kwargs: Any
    ) -> EventAppendResult:
        if idempotency_key not in recorded_keys:
            recorded_keys.add(idempotency_key)
            events.append({"seq": len(events) + 1, "type": "lifecycle", "data": appended[0].data})
        return EventAppendResult(ok=True, count=1, first_seq=len(events), last_seq=len(events))

    providers.guardrails.record_events.side_effect = record_events
    args = (_conversation(), _session())
    assert await service.generate(*args, agent_owner_email="owner@example.org", auto=True) == (
        "Nothing worth recapping yet."
    )
    providers.llm.generate.return_value = {
        "choices": [{"message": {"content": "We chose B for its safer failure mode."}}]
    }
    assert await service.generate(*args, agent_owner_email="owner@example.org") == (
        "We chose B for its safer failure mode."
    )
    assert len(recorded_keys) == 2
    assert {event["data"]["event"] for event in events if event["type"] == "lifecycle"} == {
        "conversation_recap_skipped",
        "conversation_recap",
    }
