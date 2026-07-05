"""Tests for the agent loop engine."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import cognis.core.agent_loop as agent_loop_module
from cognis.core.agent_loop import (
    _DELEGATION_RESULT_MAX_CHARS,
    _MAX_TOOL_CALL_ARGUMENT_CHARS,
    _RECOVERY_NON_RETRYABLE_CATEGORIES,
    CHAT_POLICY,
    CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    CONTROLLER_TOOL_SURFACE_WORKFLOW,
    DELEGATION_POLICY,
    DIRECT_CHAT_DELEGATION_POLICY,
    SECONDARY_AGENT_DELEGATION_POLICY,
    SECONDARY_POLICY,
    WORKFLOW_POLICY,
    AgentLoop,
    LLMStreamIdleStats,
    PauseResolution,
    PauseWaiter,
    PendingPause,
    PendingToolCallState,
    SessionLock,
    StepContext,
    StreamAccumulator,
    _append_tool_result_event,
    _bounded_tool_arguments,
    _build_delegation_message_result,
    _controller_builtin_enabled,
    _cycle_cache_breakpoints,
    _emit_token,
    _emit_tool_result_callback,
    _emit_with_optional_trailing_arg,
    _filter_model_inventory_tools,
    _has_compactable_pre_turn_history,
    _iterate_llm_stream_with_idle_timeout,
    _PreparedRegularToolCall,
    _reattach_anthropic_thinking_blocks,
    _reattach_responses_output_items,
    _responses_output_items_for_persistence,
    _result_sections_from_content,
    _should_auto_continue_after_mid_stream_failure,
    _should_continue_after_exhausted_mid_stream_failure,
    _should_run_post_turn_auto_compaction,
    _should_run_pre_turn_auto_compaction,
    _strip_internal_message_fields,
    _validate_step_completion_notification,
    _visible_allowed_tool_names,
)
from cognis.core.context_projection import ProjectionPolicy, ProjectionResult, ProjectionTurnState
from cognis.core.events import EventType
from cognis.core.followups import LLM_CYCLE_CEILING_CONTINUATION_REASON, ContinuationFollowUp
from cognis.core.project_context import ProjectContextEntry
from cognis.core.prompts import PromptContext
from cognis.core.runtime import ResolvedStepRuntime, build_local_executor_environment
from cognis.core.session_cache import SessionCache
from cognis.core.tool_router import ToolRouter
from cognis.core.turn_scheduler import TurnResult
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.deliverable import Deliverable
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    EventAppendResult,
    EventReadResult,
    ReasoningReportResult,
    SessionEvent,
)
from cognis.models.tool import (
    Permission,
    ToolCall,
    ToolCapability,
    ToolDefinition,
    ToolResult,
    ToolSource,
    stable_tool_id,
)
from cognis.models.workflow import (
    CompletionDeliveryPolicy,
    StepCompletionContract,
    StepCompletionMetadataField,
    StepDefinition,
    StepInputConfig,
    StepOutput,
    WorkflowState,
)
from cognis.providers.llm.errors import (
    LLMStreamProviderError,
    MidStreamErrorCategory,
    ToolArgumentParseFailure,
)
from cognis.providers.llm.litellm import OpenAIToolSearchFallbackRequired
from cognis.providers.llm.retry import LLMContextOverflowError
from cognis.runtime_context import scoped_runtime_context
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_managed_conversation_link,
    create_session,
    create_user,
    get_managed_conversation_link,
    get_managed_conversation_link_for_target,
    update_managed_conversation_link,
)
from cognis.tools.builtin.orchestration import OrchestrationMode
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL
from cognis.tools.registry import RegisteredTool, ToolExecutionContext, ToolRegistry

# ---------------------------------------------------------------------------
# StreamAccumulator tests
# ---------------------------------------------------------------------------


def test_stream_accumulator_collects_text() -> None:
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"content": "Hello"}}]})
    acc.feed({"choices": [{"delta": {"content": " world"}}]})

    assert acc.get_content() == "Hello world"
    assert not acc.has_tool_calls()


def test_stream_accumulator_normalizes_cumulative_reasoning_snapshots() -> None:
    acc = StreamAccumulator()

    acc.feed({"choices": [{"delta": {"reasoning": "Fixing footer"}}]})
    acc.feed({"choices": [{"delta": {"reasoning": "Fixing footer issues"}}]})
    blocks = acc.finalize_thinking()

    assert len(blocks) == 1
    assert blocks[0].get_content() == "Fixing footer issues"


@pytest.mark.asyncio
async def test_llm_stream_idle_timeout_resets_on_meaningful_activity() -> None:
    async def _stream():
        for part in ("a", "b", "c", "d"):
            await asyncio.sleep(0.35)
            yield {"choices": [{"delta": {"content": part}}]}

    chunks = [
        chunk
        async for chunk in _iterate_llm_stream_with_idle_timeout(_stream(), idle_timeout_seconds=1)
    ]

    assert len(chunks) == 4


@pytest.mark.asyncio
async def test_llm_stream_idle_timeout_tracks_provider_liveness_without_activity() -> None:
    async def _stream():
        await asyncio.sleep(0)
        yield {"provider_event": "responses", "provider_event_type": "response.created"}
        await asyncio.sleep(0.02)
        yield {"provider_event": "responses", "provider_event_type": "response.in_progress"}

    stats = LLMStreamIdleStats()
    chunks = [
        chunk
        async for chunk in _iterate_llm_stream_with_idle_timeout(
            _stream(),
            idle_timeout_seconds=1,
            stats=stats,
        )
    ]

    assert len(chunks) == 2
    assert stats.raw_chunks == 2
    assert stats.meaningful_chunks == 0
    assert stats.timeout_phase == "activity"


@pytest.mark.asyncio
async def test_llm_stream_idle_timeout_stops_on_cancel_event() -> None:
    cancel_event = asyncio.Event()
    closed = False

    async def _stream():
        nonlocal closed
        try:
            yield {"choices": [{"delta": {"content": "started"}}]}
            cancel_event.set()
            yield {"choices": [{"delta": {"content": "should-not-yield"}}]}
        finally:
            closed = True

    iterator = _iterate_llm_stream_with_idle_timeout(
        _stream(),
        idle_timeout_seconds=30,
        cancel_event=cancel_event,
    )

    chunks: list[dict[str, Any]] = []
    with pytest.raises(asyncio.CancelledError):
        async for chunk in iterator:
            chunks.append(chunk)

    assert chunks == [{"choices": [{"delta": {"content": "started"}}]}]
    assert closed is True


@pytest.mark.asyncio
async def test_llm_stream_provider_error_preserves_quota_payload() -> None:
    class _UsageLimitReached(Exception):
        status_code = 429
        body = {"error": {"code": "usage_limit_reached", "message": "Usage limit reached"}}

    async def _stream():
        raise _UsageLimitReached("HTTP 429 usage_limit_reached")
        yield {}  # pragma: no cover

    with pytest.raises(LLMStreamProviderError) as exc_info:
        async for _chunk in _iterate_llm_stream_with_idle_timeout(
            _stream(),
            idle_timeout_seconds=30,
        ):
            pass

    assert exc_info.value.to_payload()["category"] == MidStreamErrorCategory.QUOTA_EXHAUSTED.value


def test_idle_timeout_failure_can_start_auto_continuation() -> None:
    assert _should_auto_continue_after_mid_stream_failure(
        "LLM stream produced no meaningful activity for 90s"
    )
    assert _should_auto_continue_after_mid_stream_failure(
        "LLM stream produced provider reasoning events but no meaningful output for 270s"
    )
    assert _should_auto_continue_after_mid_stream_failure("Provider disconnected while streaming")


def test_exhausted_idle_timeout_can_auto_continue() -> None:
    assert _should_continue_after_exhausted_mid_stream_failure(
        "LLM stream produced no meaningful activity for 90s",
        {"category": "idle_timeout_activity"},
    )
    assert _should_continue_after_exhausted_mid_stream_failure(
        "Provider disconnected while streaming",
        {"category": "connection"},
    )
    assert not _should_continue_after_exhausted_mid_stream_failure(
        "HTTP 429 usage_limit_reached",
        {"category": "quota_exhausted"},
    )
    assert not _should_continue_after_exhausted_mid_stream_failure(
        "TypeError: '<' not supported between instances of 'list' and 'int'",
        {"category": "other"},
    )


def test_context_overflow_mid_stream_failures_are_not_retried() -> None:
    assert MidStreamErrorCategory.CONTEXT_OVERFLOW.value in _RECOVERY_NON_RETRYABLE_CATEGORIES


@pytest.mark.asyncio
async def test_llm_stream_idle_timeout_uses_reasoning_phase() -> None:
    async def _stream():
        await asyncio.sleep(0)
        yield {
            "provider_event": "responses",
            "provider_event_type": "response.reasoning_summary_part.added",
            "choices": [{"delta": {"reasoning_part_boundary": {"part_index": 0}}}],
        }

    stats = LLMStreamIdleStats()
    chunks = [
        chunk
        async for chunk in _iterate_llm_stream_with_idle_timeout(
            _stream(),
            idle_timeout_seconds=1,
            stats=stats,
        )
    ]

    assert len(chunks) == 1
    assert stats.raw_chunks == 1
    assert stats.reasoning_chunks == 1
    assert stats.meaningful_chunks == 0
    assert stats.timeout_phase == "reasoning"


def test_stream_accumulator_collects_tool_calls() -> None:
    acc = StreamAccumulator()
    # First chunk: tool call id + name
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_123",
                                "function": {"name": "step_complete", "arguments": '{"summ'},
                            }
                        ],
                    },
                }
            ],
        }
    )
    # Second chunk: more arguments
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'ary": "done"}'},
                            }
                        ],
                    },
                }
            ],
        }
    )

    assert acc.has_tool_calls()
    tool_calls = acc.get_tool_calls()
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "step_complete"
    assert tool_calls[0].arguments == {"summary": "done"}
    assert tool_calls[0].call_id == "call_123"


def test_stream_accumulator_preserves_text_when_tool_calls_follow() -> None:
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"content": "I'll inspect that now."}}]})
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_read",
                                "function": {"name": "read", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
        }
    )

    assert acc.get_content() == "I'll inspect that now."
    assert acc.get_internal_content() == ""
    assert acc.has_tool_calls()


def test_stream_accumulator_preserves_commentary_as_visible_content() -> None:
    acc = StreamAccumulator()
    delta = acc.feed(
        {
            "choices": [{"delta": {"content": "I'll inspect that now."}}],
            "response_message_phase": "commentary",
        }
    )

    assert delta == "I'll inspect that now."
    assert acc.get_content() == "I'll inspect that now."


def test_stream_accumulator_collects_tool_progress_events() -> None:
    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_progress": {
                            "id": "call_patch",
                            "name": "apply_patch",
                            "phase": "preparing_input",
                            "input_chars": 1200,
                            "input_lines": 40,
                            "complete": False,
                        }
                    }
                }
            ]
        }
    )

    events = acc.pop_tool_progress_events()

    assert len(events) == 1
    assert events[0].call_id == "call_patch"
    assert events[0].tool_name == "apply_patch"
    assert events[0].phase == "preparing_input"
    assert events[0].input_chars == 1200
    assert events[0].input_lines == 40
    assert events[0].complete is False
    assert acc.pop_tool_progress_events() == []


def test_stream_accumulator_rejects_unparseable_tool_arguments() -> None:
    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_bad",
                                "function": {"name": "write_file", "arguments": '{"path": "x"'},
                            }
                        ]
                    }
                }
            ]
        }
    )

    tool_calls = acc.get_tool_calls()

    assert len(tool_calls) == 1
    assert isinstance(tool_calls[0], ToolArgumentParseFailure)
    assert tool_calls[0].name == "write_file"
    assert tool_calls[0].call_id == "call_bad"


def test_stream_accumulator_rejects_empty_apply_patch_arguments() -> None:
    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_patch",
                                "function": {"name": "apply_patch", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        }
    )

    tool_calls = acc.get_tool_calls()

    assert len(tool_calls) == 1
    assert isinstance(tool_calls[0], ToolArgumentParseFailure)
    assert tool_calls[0].name == "apply_patch"
    assert tool_calls[0].call_id == "call_patch"
    assert tool_calls[0].raw == ""
    assert tool_calls[0].recovery_attempts == ("non_empty_arguments_required",)


def test_stream_accumulator_handles_multiple_tool_calls() -> None:
    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "tool_a", "arguments": "{}"},
                            },
                            {
                                "index": 1,
                                "id": "call_2",
                                "function": {"name": "tool_b", "arguments": "{}"},
                            },
                        ],
                    },
                }
            ],
        }
    )

    tool_calls = acc.get_tool_calls()
    assert len(tool_calls) == 2
    assert tool_calls[0].name == "tool_a"
    assert tool_calls[1].name == "tool_b"


def test_stream_accumulator_retry_restore_deduplicates_replayed_tool_chunks() -> None:
    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_retry",
                                "function": {"name": "step_complete", "arguments": '{"summ'},
                            }
                        ],
                    },
                }
            ],
        }
    )

    restored = StreamAccumulator()
    restored.restore_tool_call_state(acc.clone_tool_call_state())
    restored.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_retry",
                                "function": {"name": "step_complete", "arguments": '{"summ'},
                            }
                        ],
                    },
                }
            ],
        }
    )
    restored.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'ary": "done"}'},
                            }
                        ],
                    },
                }
            ],
        }
    )

    tool_calls = restored.get_tool_calls()
    assert len(tool_calls) == 1
    assert tool_calls[0].arguments == {"summary": "done"}


def test_stream_accumulator_retry_fresh_sample_drops_stale_partial_tool_calls() -> None:
    """A retry with new call ids must not surface the failed attempt's partials."""
    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 2,
                                "id": "call_old",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path": "/tmp/fo',
                                },
                            }
                        ],
                    },
                }
            ],
        }
    )

    restored = StreamAccumulator()
    restored.restore_tool_call_state(acc.clone_tool_call_state())
    # Fresh retry sample: different call id, different index.
    restored.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_new",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path": "/tmp/foo.txt"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }
    )

    tool_calls = restored.get_tool_calls()
    assert len(tool_calls) == 1
    assert tool_calls[0].call_id == "call_new"
    assert tool_calls[0].arguments == {"path": "/tmp/foo.txt"}


def test_stream_accumulator_retry_index_shift_still_matches_by_call_id() -> None:
    """A resumed stream re-emitting the same call at a different index dedups."""
    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 2,
                                "id": "call_same",
                                "function": {"name": "bash", "arguments": '{"comm'},
                            }
                        ],
                    },
                }
            ],
        }
    )

    restored = StreamAccumulator()
    restored.restore_tool_call_state(acc.clone_tool_call_state())
    restored.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_same",
                                "function": {"name": "bash", "arguments": '{"comm'},
                            }
                        ],
                    },
                }
            ],
        }
    )
    restored.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'and": "ls"}'}}],
                    },
                }
            ],
        }
    )

    tool_calls = restored.get_tool_calls()
    assert len(tool_calls) == 1
    assert tool_calls[0].arguments == {"command": "ls"}


def test_stream_accumulator_handles_empty_chunks() -> None:
    acc = StreamAccumulator()
    result = acc.feed({"choices": []})
    assert result is None
    assert acc.get_content() == ""


def test_stream_accumulator_reset() -> None:
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"content": "text"}}]})
    acc.reset()
    assert acc.get_content() == ""
    assert not acc.has_tool_calls()


def test_bounded_tool_arguments_remain_json_safe() -> None:
    arguments = {"patchText": "x" * 20_000}

    bounded = _bounded_tool_arguments(arguments)

    assert bounded["_truncated"] is True
    assert bounded["_original_size"] > 20_000
    assert isinstance(json.dumps(bounded), str)
    assert "..." in str(bounded["_preview"])


def test_stream_accumulator_collects_usage() -> None:
    acc = StreamAccumulator()
    acc.feed(
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 7},
                "completion_tokens_details": {"reasoning_tokens": 2},
            }
        }
    )
    assert acc.usage is not None
    assert acc.usage["total_tokens"] == 30
    assert acc.usage["cached_tokens"] == 7
    assert acc.usage["reasoning_tokens"] == 2


@pytest.mark.asyncio
async def test_run_step_uses_configured_default_step_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_wait_for(awaitable: object, timeout: float) -> StepOutput:
        captured["timeout"] = timeout
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        return StepOutput(summary="done")

    monkeypatch.setattr("cognis.core.agent_loop.asyncio.wait_for", _fake_wait_for)

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_step_timeout_seconds=3600,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert captured["timeout"] == 3600


@pytest.mark.asyncio
async def test_run_step_prefers_agent_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_wait_for(awaitable: object, timeout: float) -> StepOutput:
        captured["timeout"] = timeout
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        return StepOutput(summary="done")

    monkeypatch.setattr("cognis.core.agent_loop.asyncio.wait_for", _fake_wait_for)

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_step_timeout_seconds=3600,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            execution={"step_timeout_seconds": 42},
        ),
        policy=CHAT_POLICY,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert captured["timeout"] == 42


@pytest.mark.asyncio
async def test_run_step_continues_once_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _fake_wait_for(awaitable: object, timeout: float) -> StepOutput:
        nonlocal calls
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        calls += 1
        if calls == 1:
            raise TimeoutError
        return StepOutput(summary="continued")

    monkeypatch.setattr("cognis.core.agent_loop.asyncio.wait_for", _fake_wait_for)

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_step_timeout_seconds=3600,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.summary == "continued"
    assert calls == 2
    assert ctx.timeout_continuation_count == 1
    assert "verify that your current work is still aligned" in (
        ctx.timeout_continuation_message or ""
    )


@pytest.mark.asyncio
async def test_run_step_timeout_error_carries_continuation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _fake_wait_for(awaitable: object, timeout: float) -> StepOutput:
        nonlocal calls
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        calls += 1
        raise TimeoutError

    monkeypatch.setattr("cognis.core.agent_loop.asyncio.wait_for", _fake_wait_for)

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_step_timeout_seconds=3600,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        todos=[
            {"content": "finish implementation", "status": "in_progress"},
            {"content": "done", "status": "completed"},
        ],
    )

    output = await agent_loop.run_step(ctx)

    assert calls == 2
    assert output is not None
    assert output.error == "Step timed out after 3600s"
    assert output.metadata == {
        "continuation_reason": "step_timeout",
        "timeout_seconds": 3600,
        "pending_todos": [{"content": "finish implementation", "status": "in_progress"}],
        "timeout_continuation_count": 1,
        "max_timeout_continuations": 1,
    }


def test_read_only_web_tools_parallelize_under_evaluate_permission() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    agent_loop.tool_router = SimpleNamespace(
        _is_non_bypassable=lambda _name, non_bypassable: non_bypassable
    )
    tool = ToolDefinition(
        name="web_fetch",
        description="Fetch a URL",
        parameters={},
        source=ToolSource(type="executor"),
        category="web",
        read_only=True,
    )
    registry = {"web_fetch": SimpleNamespace(definition=tool)}
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(
                tool_permissions={stable_tool_id(tool): Permission.EVALUATE}
            ),
        ),
        policy=CHAT_POLICY,
    )

    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-1", name="web_fetch", arguments={}),
            registry,
        )
        is True
    )


def test_classified_dynamic_read_only_tool_parallelizes() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    agent_loop.tool_router = SimpleNamespace(
        _is_non_bypassable=lambda _name, non_bypassable: non_bypassable
    )
    raw_tool = ToolDefinition(
        name="mcp_github__search_issues",
        description="Search GitHub issues",
        parameters={},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
        read_only=False,
    )
    classified_tool = raw_tool.model_copy(update={"read_only": True})
    registry = {raw_tool.name: SimpleNamespace(definition=raw_tool)}
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        classified_tool_definitions={stable_tool_id(raw_tool): classified_tool},
    )

    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-1", name=raw_tool.name, arguments={}),
            registry,
        )
        is True
    )


@pytest.mark.asyncio
async def test_classified_registry_overlay_updates_guardrails_tool_context() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    raw_tool = ToolDefinition(
        name="mcp_mfg-portal__alertmanager_alerts",
        description="List Alertmanager alerts",
        parameters={
            "type": "object",
            "properties": {"filter": {"type": "array", "items": {"type": "string"}}},
        },
        source=ToolSource(
            type="intaris_mcp",
            server_id="mcp_fddc5ac26b29",
            server_name="mfg-portal",
            raw_tool_name="alertmanager.alerts",
        ),
        category="mcp",
        read_only=False,
        capabilities=[ToolCapability.WRITE],
    )

    async def handler(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        return ToolResult(output="not called")

    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=raw_tool, handler=handler))
    classified_tool = raw_tool.model_copy(
        update={
            "read_only": True,
            "capabilities": [ToolCapability.READ],
            "classification_status": "ready",
            "classification_source": "llm",
            "classification_confidence": 0.98,
        }
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        classified_tool_definitions={stable_tool_id(raw_tool): classified_tool},
    )

    overlaid = agent_loop._get_classified_tool_registry(ctx, registry)
    assert isinstance(overlaid, ToolRegistry)
    raw_registered = registry.get(raw_tool.name)
    registered = overlaid.get(raw_tool.name)

    assert raw_registered is not None
    assert registered is not None
    assert registered.handler is handler
    assert raw_registered.definition.read_only is False
    assert registered.definition.read_only is True
    assert registered.definition.capabilities == [ToolCapability.READ]

    context = await ToolRouter(guardrails=None)._evaluation_context(
        ToolCall(
            call_id="call-1",
            name=raw_tool.name,
            arguments={"filter": ["alertname=RcloneAggregatorSlowDurationWarning"]},
        ),
        registered.definition,
    )

    assert context["tool"]["read_only"] is True
    assert context["tool"]["capabilities"] == ["read"]
    assert context["tool"]["classification"] == {
        "status": "ready",
        "source": "llm",
        "confidence": 0.98,
    }


def test_classified_registry_overlay_controls_same_executor_retry_safety() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    raw_tool = ToolDefinition(
        name="read",
        description="Read a file",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
        category="filesystem",
        read_only=True,
        capabilities=[ToolCapability.READ],
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=raw_tool))
    classified_tool = raw_tool.model_copy(
        update={
            "read_only": False,
            "capabilities": [ToolCapability.WRITE],
            "classification_status": "ready",
        }
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        classified_tool_definitions={stable_tool_id(raw_tool): classified_tool},
    )

    overlaid = agent_loop._get_classified_tool_registry(ctx, registry)
    assert isinstance(overlaid, ToolRegistry)
    raw_registered = registry.get(raw_tool.name)
    classified_registered = overlaid.get(raw_tool.name)

    assert raw_registered is not None
    assert classified_registered is not None
    assert agent_loop._tool_safe_for_same_executor_retry(raw_registered) is True
    assert agent_loop._tool_safe_for_same_executor_retry(classified_registered) is False


def test_safe_executor_mutations_parallelize_under_evaluate_permission() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    agent_loop.tool_router = SimpleNamespace(
        _is_non_bypassable=lambda _name, non_bypassable: non_bypassable
    )
    tool = ToolDefinition(
        name="write",
        description="Write a file",
        parameters={},
        source=ToolSource(type="executor"),
        category="filesystem",
        read_only=False,
        non_bypassable=True,
    )
    registry = {"write": SimpleNamespace(definition=tool)}
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(
                tool_permissions={stable_tool_id(tool): Permission.EVALUATE}
            ),
        ),
        policy=CHAT_POLICY,
    )

    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-1", name="write", arguments={"file_path": "a.txt"}),
            registry,
        )
        is True
    )


def test_unsafe_executor_mutations_stay_serial() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    agent_loop.tool_router = SimpleNamespace(
        _is_non_bypassable=lambda _name, non_bypassable: non_bypassable
    )
    bash_tool = ToolDefinition(
        name="bash",
        description="Run shell",
        parameters={},
        source=ToolSource(type="executor"),
        category="shell",
        read_only=False,
        non_bypassable=True,
    )
    browser_tool = ToolDefinition(
        name="browser_click",
        description="Click",
        parameters={},
        source=ToolSource(type="executor"),
        category="browser",
        read_only=False,
        non_bypassable=True,
    )
    registry = {
        "bash": SimpleNamespace(definition=bash_tool),
        "browser_click": SimpleNamespace(definition=browser_tool),
    }
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-bash", name="bash", arguments={}),
            registry,
        )
        is False
    )
    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-click", name="browser_click", arguments={}),
            registry,
        )
        is False
    )


def test_artifact_executor_tools_parallelize() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    agent_loop.tool_router = SimpleNamespace(
        _is_non_bypassable=lambda _name, non_bypassable: non_bypassable
    )
    artifact_save = ToolDefinition(
        name="artifact_save",
        description="Save artifact",
        parameters={},
        source=ToolSource(type="executor"),
        category="filesystem",
        read_only=False,
        non_bypassable=True,
    )
    artifact_publish = ToolDefinition(
        name="artifact_publish",
        description="Publish artifact",
        parameters={},
        source=ToolSource(type="executor"),
        category="document",
        read_only=True,
    )
    registry = {
        "artifact_save": SimpleNamespace(definition=artifact_save),
        "artifact_publish": SimpleNamespace(definition=artifact_publish),
    }
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-save", name="artifact_save", arguments={}),
            registry,
        )
        is True
    )
    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-publish", name="artifact_publish", arguments={}),
            registry,
        )
        is True
    )


def _parallel_group_ctx() -> StepContext:
    return StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        working_directory="/tmp",
    )


def test_default_parallel_path_uses_home_without_working_directory() -> None:
    ctx = _parallel_group_ctx()
    ctx.working_directory = None
    ctx.workspace_root = "/tmp"

    assert AgentLoop._default_parallel_path(ctx) != "/tmp"


def test_parallel_execution_groups_split_conflicting_mutation_paths() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    first = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/a.txt"},
        ),
        tool_id="builtin:write",
    )
    second = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-edit",
            name="edit",
            arguments={"file_path": "/tmp/a.txt"},
        ),
        tool_id="builtin:edit",
    )
    third = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-other-write",
            name="write",
            arguments={"file_path": "/tmp/b.txt"},
        ),
        tool_id="builtin:write",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [first, second, third])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-write"],
        ["call-edit", "call-other-write"],
    ]


def test_parallel_execution_groups_keep_apply_patch_exclusive() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    patch = _PreparedRegularToolCall(
        tool_call=ToolCall(call_id="call-patch", name="apply_patch", arguments={}),
        tool_id="builtin:apply_patch",
    )
    write = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/a.txt"},
        ),
        tool_id="builtin:write",
    )
    read = _PreparedRegularToolCall(
        tool_call=ToolCall(call_id="call-read", name="read", arguments={"file_path": "/tmp/a.txt"}),
        tool_id="builtin:read",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [patch, read, write])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-patch"],
        ["call-read"],
        ["call-write"],
    ]


def test_parallel_execution_groups_split_write_before_same_path_publish() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    write = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/report.pdf"},
        ),
        tool_id="builtin:write",
    )
    publish = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-publish",
            name="artifact_publish",
            arguments={"path": "/tmp/report.pdf"},
        ),
        tool_id="builtin:artifact_publish",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [write, publish])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-write"],
        ["call-publish"],
    ]


def test_parallel_execution_groups_split_write_before_same_path_read_alias() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    write = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/report.md"},
        ),
        tool_id="builtin:write",
    )
    read = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-read",
            name="read",
            arguments={"file_path": "./report.md"},
        ),
        tool_id="builtin:read",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [write, read])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-write"],
        ["call-read"],
    ]


def test_parallel_execution_groups_split_write_before_dynamic_path_read() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    write = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/report.md"},
        ),
        tool_id="builtin:write",
    )
    dynamic_read = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-dynamic-read",
            name="mcp_files__read_file",
            arguments={"path": "./report.md"},
        ),
        tool_id="mcp:files:read_file",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [write, dynamic_read])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-write"],
        ["call-dynamic-read"],
    ]


def test_parallel_execution_groups_split_write_before_directory_read() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    write = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/report.md"},
        ),
        tool_id="builtin:write",
    )
    list_directory = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-list",
            name="list_directory",
            arguments={"path": "/tmp"},
        ),
        tool_id="builtin:list_directory",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [write, list_directory])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-write"],
        ["call-list"],
    ]


def test_parallel_execution_groups_split_write_before_default_path_grep() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    write = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/report.md"},
        ),
        tool_id="builtin:write",
    )
    grep = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-grep",
            name="grep",
            arguments={"pattern": "report"},
        ),
        tool_id="builtin:grep",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [write, grep])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-write"],
        ["call-grep"],
    ]


def test_parallel_execution_groups_split_write_before_document_asset_read() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    write = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-write",
            name="write",
            arguments={"file_path": "/tmp/chart.png"},
        ),
        tool_id="builtin:write",
    )
    document_generate = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-doc",
            name="document_generate",
            arguments={
                "content": "![chart](asset:chart)",
                "assets": [{"name": "chart", "path": "/tmp/chart.png"}],
            },
        ),
        tool_id="builtin:document_generate",
    )

    groups = agent_loop._parallel_execution_groups(
        _parallel_group_ctx(), [write, document_generate]
    )

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-write"],
        ["call-doc"],
    ]


def test_parallel_execution_groups_keep_apply_patch_exclusive_after_untracked_tool() -> None:
    agent_loop = AgentLoop.__new__(AgentLoop)
    web_fetch = _PreparedRegularToolCall(
        tool_call=ToolCall(
            call_id="call-web", name="web_fetch", arguments={"url": "https://example.com"}
        ),
        tool_id="builtin:web_fetch",
    )
    patch = _PreparedRegularToolCall(
        tool_call=ToolCall(call_id="call-patch", name="apply_patch", arguments={}),
        tool_id="builtin:apply_patch",
    )

    groups = agent_loop._parallel_execution_groups(_parallel_group_ctx(), [web_fetch, patch])

    assert [[item.tool_call.call_id for item in group] for group in groups] == [
        ["call-web"],
        ["call-patch"],
    ]


def test_parallelizability_marks_serial_regular_tool_as_batch_boundary() -> None:
    read_tool = ToolDefinition(
        name="web_fetch",
        description="Fetch a URL",
        parameters={},
        source=ToolSource(type="executor"),
        category="web",
        read_only=True,
    )
    write_tool = ToolDefinition(
        name="memory_write_note",
        description="Write a note",
        parameters={},
        source=ToolSource(type="executor"),
        category="memory",
        read_only=False,
    )

    class _ToolRouter:
        def _is_non_bypassable(self, _name: str, non_bypassable: bool) -> bool:
            return non_bypassable

    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=read_tool, handler=None))
    registry.register(RegisteredTool(definition=write_tool, handler=None))
    agent_loop = AgentLoop.__new__(AgentLoop)
    agent_loop.tool_router = _ToolRouter()
    ctx = _parallel_group_ctx()

    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-read", name="web_fetch", arguments={}),
            registry,
        )
        is True
    )
    assert (
        agent_loop._is_parallelizable_regular_tool_call(
            ctx,
            ToolCall(call_id="call-write", name="memory_write_note", arguments={}),
            registry,
        )
        is False
    )


# ---------------------------------------------------------------------------
# SessionLock tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_lock_acquires_and_releases() -> None:
    lock = SessionLock()
    await lock.acquire("sess_1")
    lock.release("sess_1")

    # Should be able to acquire again
    await lock.acquire("sess_1")
    lock.release("sess_1")


@pytest.mark.asyncio
async def test_session_lock_evict() -> None:
    lock = SessionLock()
    await lock.acquire("sess_1")
    lock.release("sess_1")
    lock.evict("sess_1")
    # No error expected
    lock.evict("nonexistent")


@pytest.mark.asyncio
async def test_session_lock_reports_stale_unlocked_sessions() -> None:
    lock = SessionLock()
    await lock.acquire("sess_1")
    lock.release("sess_1")

    assert lock.stale_unlocked_session_ids(max_idle_seconds=0) == ["sess_1"]


# ---------------------------------------------------------------------------
# PauseWaiter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_waiter_resolve_before_timeout() -> None:
    waiter = PauseWaiter()

    import asyncio

    async def _resolve_soon() -> None:
        await asyncio.sleep(0.01)
        waiter.resolve("p1", PauseResolution(decision="approve", data={"ok": True}))

    asyncio.create_task(_resolve_soon())
    result = await waiter.wait("p1", timeout=1.0)

    assert result.decision == "approve"
    assert result.data["ok"] is True


@pytest.mark.asyncio
async def test_pause_waiter_timeout() -> None:
    waiter = PauseWaiter()

    with pytest.raises(TimeoutError):
        await waiter.wait("p2", timeout=0.01)


@pytest.mark.asyncio
async def test_pause_waiter_resolve_unknown() -> None:
    waiter = PauseWaiter()
    result = waiter.resolve("unknown", PauseResolution(decision="approve"))
    assert result is False


def test_pause_waiter_pending_count() -> None:
    waiter = PauseWaiter()
    assert waiter.pending_count() == 0


@pytest.mark.asyncio
async def test_run_step_uses_step_local_pending_events_on_concurrent_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    captured: dict[str, list[dict[str, object]]] = {}
    ready = asyncio.Event()
    started: list[str] = []

    async def _fake_execute_step(
        ctx: StepContext,
        *,
        on_token: object = None,
        on_thinking: object = None,
        on_tool_call: object = None,
        on_tool_result: object = None,
    ) -> None:
        del on_token, on_thinking, on_tool_call, on_tool_result
        ctx.pending_events = [
            SessionEvent(
                type="lifecycle",
                data={"event": "system_notice", "message": f"pending-{ctx.session.session_id}"},
            )
        ]
        started.append(ctx.session.session_id)
        if len(started) == 2:
            ready.set()
        await ready.wait()
        raise RuntimeError(f"boom-{ctx.session.session_id}")

    async def _fake_record_events_strict(
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: object = None,
    ) -> bool:
        del reason, on_token
        captured[ctx.session.session_id] = [event.data for event in events]
        events.clear()
        return True

    monkeypatch.setattr(agent_loop, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(agent_loop, "_record_events_strict", _fake_record_events_strict)

    ctx_a = StepContext(
        step_definition=StepDefinition(name="a", type="run"),
        session=SimpleNamespace(session_id="sess-a", intaris_session_id="sess-a"),
        conversation=SimpleNamespace(conversation_id="conv-a"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="A"),
        policy=CHAT_POLICY,
    )
    ctx_b = StepContext(
        step_definition=StepDefinition(name="b", type="run"),
        session=SimpleNamespace(session_id="sess-b", intaris_session_id="sess-b"),
        conversation=SimpleNamespace(conversation_id="conv-b"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="B"),
        policy=CHAT_POLICY,
    )

    output_a, output_b = await asyncio.gather(
        agent_loop.run_step(ctx_a), agent_loop.run_step(ctx_b)
    )

    assert output_a is not None and output_b is not None
    assert output_a.error == "RuntimeError: boom-sess-a"
    assert output_b.error == "RuntimeError: boom-sess-b"
    assert [item["message"] for item in captured["sess-a"]] == [
        "pending-sess-a",
        "Step failed: RuntimeError: boom-sess-a",
    ]
    assert [item["message"] for item in captured["sess-b"]] == [
        "pending-sess-b",
        "Step failed: RuntimeError: boom-sess-b",
    ]


@pytest.mark.asyncio
async def test_record_outgoing_audit_messages_copies_replay_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    captured: list[SessionEvent] = []

    async def _fake_record_events_strict(
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: object = None,
    ) -> bool:
        del ctx, reason, on_token
        captured.extend(events)
        events.clear()
        return True

    monkeypatch.setattr(agent_loop, "_record_events_strict", _fake_record_events_strict)

    ctx = StepContext(
        step_definition=StepDefinition(name="step", type="run"),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        turn_id="turn-1",
    )

    await agent_loop._record_outgoing_audit_messages(
        ctx,
        [
            {
                "role": "developer",
                "source": "memory_search",
                "content": '<memory_context trust="untrusted">remembered</memory_context>',
                "content_type": "text",
                "position": 3,
                "hash": "hash",
                "metadata": {
                    "context_injection": True,
                    "replayable": True,
                    "replay_scope": "same_session",
                    "visibility": "agent_context",
                    "model_role": "system",
                    "trust": "untrusted",
                },
            }
        ],
    )

    assert len(captured) == 1
    assert captured[0].type == "developer_message"
    assert captured[0].data["source"] == "memory_search"
    assert captured[0].data["turn_id"] == "turn-1"
    assert captured[0].data["context_injection"] is True
    assert captured[0].data["replayable"] is True
    assert captured[0].data["visibility"] == "agent_context"
    assert captured[0].data["model_role"] == "system"


@pytest.mark.asyncio
async def test_emergency_flush_repairs_interrupted_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    captured: list[SessionEvent] = []

    async def _fake_record_events_strict(
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: object = None,
    ) -> bool:
        del ctx, reason, on_token
        captured.extend(events)
        events.clear()
        return True

    monkeypatch.setattr(agent_loop, "_record_events_strict", _fake_record_events_strict)

    ctx = StepContext(
        step_definition=StepDefinition(name="step", type="run"),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )
    pending_call = ToolCall(call_id="call-1", name="bash", arguments={"command": "sleep 10"})
    ctx.pending_tool_calls[pending_call.call_id] = PendingToolCallState(
        tool_call=pending_call,
        tool_id="bash",
    )

    await agent_loop._emergency_flush_events(ctx, [])

    assert len(captured) == 1
    assert captured[0].type == "tool_result"
    assert captured[0].data["call_id"] == "call-1"
    assert captured[0].data["is_error"] is True
    assert "interrupted before a result was recorded" in str(captured[0].data["result"])


def test_filter_model_inventory_tools_excludes_controller_and_denied_tools() -> None:
    agent = AgentDefinition(
        agent_id="agent-a",
        owner_email="user@example.com",
        name="Agent A",
        tools={},
        permissions=AgentPermissions(
            tool_permissions={"*": Permission.EVALUATE, "builtin:bash": Permission.DENY}
        ),
    )
    tools = [
        ToolDefinition(
            name="step_complete",
            description="controller",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="builtin"),
            category="workflow",
        ),
        ToolDefinition(
            name="delegate",
            description="orchestration",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="builtin"),
            category="orchestration",
        ),
        ToolDefinition(
            name="bash",
            description="shell",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="executor"),
            category="shell",
        ),
        ToolDefinition(
            name="read",
            description="filesystem",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="executor"),
            category="filesystem",
        ),
    ]

    filtered = _filter_model_inventory_tools(agent, tools)

    assert [tool.name for tool in filtered] == ["read"]


def test_filter_model_inventory_tools_keeps_write_tools_visible_for_plan_mode() -> None:
    agent = AgentDefinition(
        agent_id="agent-a",
        owner_email="user@example.com",
        name="Agent A",
        tools={},
        permissions=AgentPermissions(tool_permissions={"*": Permission.EVALUATE}),
    )
    tools = [
        ToolDefinition(
            name="write",
            description="write file",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="executor"),
            category="filesystem",
            read_only=False,
        ),
        ToolDefinition(
            name="read",
            description="read file",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="executor"),
            category="filesystem",
            read_only=True,
        ),
    ]

    filtered = _filter_model_inventory_tools(agent, tools)

    assert [tool.name for tool in filtered] == ["write", "read"]


def test_controller_builtin_enabled_honors_disabled_tools() -> None:
    agent = AgentDefinition(
        agent_id="agent-a",
        owner_email="user@example.com",
        name="Agent A",
        tools={"disabled_tools": [SEARCH_TOOLS_TOOL.name]},
        permissions=AgentPermissions(tool_permissions={"*": Permission.EVALUATE}),
    )

    assert _controller_builtin_enabled(agent, SEARCH_TOOLS_TOOL) is False


@pytest.mark.asyncio
async def test_run_child_session_resolves_fresh_runtime() -> None:
    runtime_calls: list[tuple[str, str]] = []
    cleanup_called = False
    captured_tool_registry: list[object] = []
    captured_contexts: list[object] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del executor_agent
        runtime_calls.append((agent.agent_id, user_email))

        async def _cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _SessionContextManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    class _SessionManager:
        def session_factory(self) -> _SessionContextManager:
            return _SessionContextManager()

    class _Guardrails:
        async def record_events(self, **_: object) -> None:
            return None

    class _EventBus:
        async def publish(self, _: object) -> None:
            return None

    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_Guardrails()),
        session_manager=_SessionManager(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_run_step(ctx: object, **_: object) -> StepOutput:
        assert hasattr(ctx, "tool_registry")
        captured_contexts.append(ctx)
        captured_tool_registry.append(ctx.tool_registry)
        return StepOutput(
            summary="done",
            content="done",
            outputs={},
            claims=[],
            session_id="child",
            intaris_session_id="child",
            completed_at=datetime.now(UTC),
        )

    async def _fake_set_session_status(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)
    monkeypatch.setattr("cognis.store.queries.set_session_status", _fake_set_session_status)
    try:
        output = await agent_loop._run_child_session(
            child_session=SimpleNamespace(
                session_id="child",
                user_email="user@example.com",
                agent_id="agent-a",
                intaris_session_id="child",
            ),
            conversation=SimpleNamespace(conversation_id="conv-1"),
            agent=AgentDefinition(
                agent_id="agent-a",
                owner_email="user@example.com",
                name="Agent A",
            ),
            task_description="Do the thing",
            parent_intaris_session_id="parent-intaris",
            deliverable_step_run_id="sr-parent",
        )
    finally:
        monkeypatch.undo()

    assert output is not None
    assert runtime_calls == [("agent-a", "user@example.com")]
    assert captured_tool_registry == ["child-registry"]
    assert captured_contexts[0].deliverable_step_run_id == "sr-parent"
    assert captured_contexts[0].step_definition.require_deliverable is False
    assert cleanup_called is True


@pytest.mark.asyncio
async def test_run_child_session_continues_after_tool_call_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_called = False
    captured_follow_ups: list[object] = []
    captured_retry_states: list[bool] = []
    captured_user_messages: list[str] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _SessionManager(_NoopSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self.completed: list[dict[str, object]] = []

        async def mark_completed(self, session_id: str, **kwargs: object) -> None:
            self.completed.append({"session_id": session_id, **kwargs})

    event_bus = _NoopEventBus()
    session_manager = _SessionManager()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_NoopGuardrails()),
        session_manager=session_manager,
        session_cache=_NoopSessionCache(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=event_bus,
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    outputs = [
        StepOutput(
            summary="Stopped after reaching the tool-call ceiling.",
            content="partial",
            outputs={},
            claims=[],
            metadata={
                "interrupted": True,
                "continuation_reason": "tool_call_ceiling_reached",
                "tool_call_count": 200,
                "max_tool_calls": 200,
                "pending_todos": [{"content": "finish delegated work", "status": "pending"}],
            },
            session_id="child",
            intaris_session_id="child",
        ),
        StepOutput(
            summary="done",
            content="final delegated result",
            outputs={},
            claims=[],
            session_id="child",
            intaris_session_id="child",
        ),
    ]

    async def _fake_run_step(ctx: StepContext, **_: object) -> StepOutput:
        captured_follow_ups.append(ctx.follow_up)
        captured_retry_states.append(ctx.is_retry)
        captured_user_messages.append(ctx.user_message)
        ctx.turn_id = f"turn-{len(captured_follow_ups)}"
        return outputs.pop(0)

    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)

    output = await agent_loop._run_child_session(
        child_session=SimpleNamespace(
            session_id="child",
            user_email="user@example.com",
            agent_id="agent-a",
            intaris_session_id="child",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-a",
            owner_email="user@example.com",
            name="Agent A",
        ),
        task_description="Do the thing",
        parent_intaris_session_id="parent-intaris",
    )

    assert output is not None
    assert output.summary == "done"
    assert output.content == "final delegated result"
    assert captured_follow_ups[0] is None
    follow_up = captured_follow_ups[1]
    assert isinstance(follow_up, ContinuationFollowUp)
    assert follow_up.reason == "tool_call_ceiling_reached"
    assert follow_up.attempt == 1
    assert follow_up.pending_todos == [{"content": "finish delegated work", "status": "pending"}]
    assert captured_retry_states == [False, True]
    assert captured_user_messages[0] == "Do the thing"
    assert (
        "Continue the delegated work from the recorded session history"
        in (captured_user_messages[1])
    )
    assert session_manager.completed
    assert cleanup_called is True


def test_prepare_child_context_for_llm_cycle_continuation_uses_reason_specific_message() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="delegate", type="run", prompt=""),
        session=SimpleNamespace(session_id="child", intaris_session_id="child"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="original delegated task",
        user_attachments=[],
        system_initiated=False,
    )
    follow_up = ContinuationFollowUp(
        follow_up_id="fup-1",
        mode="integrate",
        origin_kind="continuation",
        relevance_hint="same_thread",
        required_action="integrate_result",
        topic_ref="turn-1",
        status="completed",
        reason=LLM_CYCLE_CEILING_CONTINUATION_REASON,
        attempt=1,
        max_attempts=3,
        cycle_count=150,
        max_llm_cycles=150,
    )

    AgentLoop._prepare_child_context_for_continuation(ctx, follow_up)

    assert "LLM cycle ceiling" in ctx.user_message
    assert "tool-call ceiling" not in ctx.user_message
    assert ctx.follow_up is follow_up
    assert ctx.is_retry is True
    assert ctx.turn_id is None
    assert ctx.timeout_continuation_message == ctx.user_message


@pytest.mark.asyncio
async def test_run_child_session_fails_after_repeated_tool_call_ceilings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_follow_ups: list[object] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _SessionManager(_NoopSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self.failed: list[dict[str, object]] = []

        async def mark_failed(self, session_id: str, **kwargs: object) -> None:
            self.failed.append({"session_id": session_id, **kwargs})

    event_bus = _NoopEventBus()
    session_manager = _SessionManager()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_NoopGuardrails()),
        session_manager=session_manager,
        session_cache=_NoopSessionCache(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=event_bus,
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_run_step(ctx: StepContext, **_: object) -> StepOutput:
        captured_follow_ups.append(ctx.follow_up)
        ctx.turn_id = f"turn-{len(captured_follow_ups)}"
        return StepOutput(
            summary="Stopped after reaching the tool-call ceiling.",
            content="partial",
            outputs={},
            claims=[],
            metadata={
                "interrupted": True,
                "continuation_reason": "tool_call_ceiling_reached",
                "tool_call_count": 200,
                "max_tool_calls": 200,
            },
            session_id="child",
            intaris_session_id="child",
        )

    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)

    output = await agent_loop._run_child_session(
        child_session=SimpleNamespace(
            session_id="child",
            user_email="user@example.com",
            agent_id="agent-a",
            intaris_session_id="child",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-a",
            owner_email="user@example.com",
            name="Agent A",
        ),
        task_description="Do the thing",
        parent_intaris_session_id="parent-intaris",
    )

    assert output is None
    assert [getattr(follow_up, "attempt", None) for follow_up in captured_follow_ups] == [
        None,
        1,
        2,
        3,
    ]
    assert session_manager.failed
    assert any(event.type is EventType.DELEGATION_FAILED for event in event_bus.events)


@pytest.mark.asyncio
async def test_run_child_session_direct_chat_secondary_uses_secondary_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_policies: list[object] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_resolve_child_agent(
        child_agent_id: str,
        parent_agent: AgentDefinition,
        *,
        user_email: str,
    ) -> AgentDefinition:
        del child_agent_id, parent_agent, user_email
        return AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            system=True,
        )

    async def _fake_run_step(ctx: StepContext, **_: object) -> StepOutput:
        captured_policies.append(ctx.policy)
        return StepOutput(
            summary="done",
            content="done",
            outputs={},
            claims=[],
            session_id="child",
            intaris_session_id="child",
        )

    monkeypatch.setattr(agent_loop, "_resolve_child_agent", _fake_resolve_child_agent)
    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)

    await agent_loop._run_child_session(
        child_session=SimpleNamespace(
            session_id="child",
            user_email="user@example.com",
            agent_id="system:explore",
            intaris_session_id="child",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-a",
            owner_email="user@example.com",
            name="Agent A",
        ),
        task_description="Explore this",
        parent_intaris_session_id="parent-intaris",
        controller_tool_surface=CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    )

    assert captured_policies == [SECONDARY_AGENT_DELEGATION_POLICY]


@pytest.mark.asyncio
async def test_run_child_session_ignores_implicit_runtime_workdir() -> None:
    captured_contexts: list[object] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _SessionContextManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    class _SessionManager:
        def session_factory(self) -> _SessionContextManager:
            return _SessionContextManager()

    class _Guardrails:
        async def record_events(self, **_: object) -> None:
            return None

    class _EventBus:
        async def publish(self, _: object) -> None:
            return None

    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_Guardrails()),
        session_manager=_SessionManager(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_run_step(ctx: object, **_: object) -> StepOutput:
        captured_contexts.append(ctx)
        return StepOutput(summary="done", content="done", outputs={}, claims=[])

    async def _fake_set_session_status(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)
    monkeypatch.setattr("cognis.store.queries.set_session_status", _fake_set_session_status)
    try:
        with scoped_runtime_context(
            workspace_root="/home/user/src/codex",
            effective_working_directory="/home/user/src/codex/codex-rs/protocol/src",
        ):
            output = await agent_loop._run_child_session(
                child_session=SimpleNamespace(
                    session_id="child",
                    user_email="user@example.com",
                    agent_id="agent-a",
                    intaris_session_id="child",
                ),
                conversation=SimpleNamespace(conversation_id="conv-1"),
                agent=AgentDefinition(
                    agent_id="agent-a",
                    owner_email="user@example.com",
                    name="Agent A",
                ),
                task_description="Do the thing",
                parent_intaris_session_id="parent-intaris",
            )
    finally:
        monkeypatch.undo()

    assert output is not None
    assert captured_contexts[0].workspace_root is None
    assert captured_contexts[0].working_directory is None


@pytest.mark.asyncio
async def test_run_child_session_uses_explicit_runtime_workdir() -> None:
    captured_contexts: list[object] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _SessionContextManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    class _SessionManager:
        def session_factory(self) -> _SessionContextManager:
            return _SessionContextManager()

    class _Guardrails:
        async def record_events(self, **_: object) -> None:
            return None

    class _EventBus:
        async def publish(self, _: object) -> None:
            return None

    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_Guardrails()),
        session_manager=_SessionManager(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_run_step(ctx: object, **_: object) -> StepOutput:
        captured_contexts.append(ctx)
        return StepOutput(summary="done", content="done", outputs={}, claims=[])

    async def _fake_set_session_status(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)
    monkeypatch.setattr("cognis.store.queries.set_session_status", _fake_set_session_status)
    try:
        output = await agent_loop._run_child_session(
            child_session=SimpleNamespace(
                session_id="child",
                user_email="user@example.com",
                agent_id="agent-a",
                intaris_session_id="child",
            ),
            conversation=SimpleNamespace(conversation_id="conv-1"),
            agent=AgentDefinition(
                agent_id="agent-a",
                owner_email="user@example.com",
                name="Agent A",
            ),
            task_description="Do the thing",
            parent_intaris_session_id="parent-intaris",
            workspace_root="/home/user/src/cognis",
            working_directory="/home/user/src/cognis/cognis/core",
            workspace_root_explicit=True,
            working_directory_explicit=True,
        )
    finally:
        monkeypatch.undo()

    assert output is not None
    assert captured_contexts[0].workspace_root == "/home/user/src/cognis"
    assert captured_contexts[0].working_directory == "/home/user/src/cognis/cognis/core"
    assert captured_contexts[0].workspace_root_explicit is True
    assert captured_contexts[0].working_directory_explicit is True


@pytest.mark.asyncio
async def test_run_child_session_async_preserves_explicit_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, object] = {}
    published_events: list[object] = []

    class _EventBus:
        async def publish(self, event: object) -> None:
            published_events.append(event)

    agent_loop = AgentLoop(
        providers=SimpleNamespace(),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    async def _fake_run_child_session(**kwargs: object) -> StepOutput:
        captured_kwargs.update(kwargs)
        on_tool_call = kwargs.get("on_tool_call")
        if callable(on_tool_call):
            await on_tool_call("grep", "call-child-1", {"pattern": "x"})
        on_tool_result = kwargs.get("on_tool_result")
        if callable(on_tool_result):
            await on_tool_result("call-child-1", "grep", "{}", False, None, None)
        return StepOutput(summary="done", content="done", outputs={}, claims=[])

    async def _fake_untrack_child(parent_session_id: str, child_session_id: str) -> None:
        captured_kwargs["untracked"] = (parent_session_id, child_session_id)

    monkeypatch.setattr(agent_loop, "_run_child_session", _fake_run_child_session)
    monkeypatch.setattr(agent_loop, "_untrack_child", _fake_untrack_child)

    await agent_loop._run_child_session_async(
        child_session=SimpleNamespace(
            session_id="child",
            parent_session_id="parent",
            user_email="user@example.com",
            agent_id="agent-a",
            intaris_session_id="child",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-a",
            owner_email="user@example.com",
            name="Agent A",
        ),
        task_description="Do the thing",
        parent_intaris_session_id="parent-intaris",
        workspace_root="/home/user/src/cognis",
        working_directory="/home/user/src/cognis/cognis/core",
        workspace_root_explicit=True,
        working_directory_explicit=True,
        parent_turn_id="turn-1",
        parent_assistant_phase_index=4,
        parent_turn_cycle_index=4,
    )

    assert captured_kwargs["workspace_root"] == "/home/user/src/cognis"
    assert captured_kwargs["working_directory"] == "/home/user/src/cognis/cognis/core"
    assert captured_kwargs["workspace_root_explicit"] is True
    assert captured_kwargs["working_directory_explicit"] is True
    assert captured_kwargs["parent_turn_cycle_index"] == 4
    assert captured_kwargs["untracked"] == ("parent", "child")
    assert published_events
    progress_events = [
        event
        for event in published_events
        if getattr(event, "type", None) == EventType.DELEGATION_PROGRESS
    ]
    assert progress_events
    assert progress_events[0].data["turn_id"] == "turn-1"
    assert progress_events[0].data["assistant_phase_index"] == 4
    assert progress_events[0].data["turn_cycle_index"] == 4
    assert progress_events[0].data["tool_call_count"] == 1
    assert progress_events[0].data["last_tool"] == "grep"


@pytest.mark.asyncio
async def test_run_child_session_returns_selected_assistant_output_without_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    substantive = (
        "Final delegated findings: cognis/core/agent_loop.py:6602 returned the stale "
        "StepOutput content instead of the selected child assistant message. Use the "
        "selected result content for no-deliverable sync delegations."
    )
    stale_tail = (
        "Already provided the final findings above. No additional user-facing information "
        "remains; the remaining todo state is stale."
    )

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _Guardrails:
        async def record_events(self, **_: object) -> None:
            return None

        async def read_events(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                events=[
                    {"seq": 1, "type": "assistant_message", "data": {"content": substantive}},
                    {"seq": 2, "type": "assistant_message", "data": {"content": stale_tail}},
                ],
                last_seq=2,
                has_more=False,
                missing_stream_fallback_used=False,
            )

    class _EventBus:
        async def publish(self, _: object) -> None:
            return None

    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_Guardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_run_step(ctx: object, **_: object) -> StepOutput:
        del ctx
        return StepOutput(
            summary=stale_tail,
            content=stale_tail,
            outputs={},
            claims=[],
            session_id="child",
            intaris_session_id="child",
            completed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)

    output = await agent_loop._run_child_session(
        child_session=SimpleNamespace(
            session_id="child",
            user_email="user@example.com",
            agent_id="system:explore",
            intaris_session_id="child",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            is_system=True,
        ),
        task_description="Investigate",
        parent_intaris_session_id="parent-intaris",
    )

    assert output is not None
    assert output.deliverable_id is None
    assert substantive in output.content
    assert stale_tail in output.content
    assert output.content.index(substantive) < output.content.index(stale_tail)
    assert substantive in output.summary


@pytest.mark.asyncio
async def test_run_child_session_treats_step_output_error_as_failure(monkeypatch) -> None:
    completed: list[str] = []
    failed: list[tuple[str, str | None]] = []
    published: list[object] = []
    recorded_events: list[object] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _SessionContextManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    class _SessionManager:
        def session_factory(self) -> _SessionContextManager:
            return _SessionContextManager()

        async def mark_completed(self, session_id: str, **_: object) -> None:
            completed.append(session_id)

        async def mark_failed(self, session_id: str, result_summary: str | None = None) -> None:
            failed.append((session_id, result_summary))

    class _Guardrails:
        async def record_events(self, **kwargs: object) -> None:
            recorded_events.extend(kwargs.get("events", []))

    class _EventBus:
        async def publish(self, event: object) -> None:
            published.append(event)

    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_Guardrails()),
        session_manager=_SessionManager(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_run_step(ctx: object, **_: object) -> StepOutput:
        return StepOutput(
            summary="Step failed: IntegrityError",
            error="IntegrityError: duplicate key",
            session_id=ctx.session.session_id,
            intaris_session_id=ctx.session.intaris_session_id,
            completed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)

    output = await agent_loop._run_child_session(
        child_session=SimpleNamespace(
            session_id="child",
            user_email="user@example.com",
            agent_id="agent-a",
            intaris_session_id="child",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-a",
            owner_email="user@example.com",
            name="Agent A",
        ),
        task_description="Do the thing",
        parent_intaris_session_id="parent-intaris",
    )

    assert output is None
    assert completed == []
    assert failed == [
        (
            "child",
            "Delegation failed: RuntimeError: Delegation step failed: IntegrityError: duplicate key",
        )
    ]
    assert len(published) == 2
    assert getattr(published[0], "type", None) == EventType.SYSTEM_NOTICE
    assert getattr(published[0], "data", {}).get("child_session_id") == "child"
    assert any(
        getattr(event, "type", None) == "delegation"
        and getattr(event, "data", {}).get("status") == "failed"
        for event in recorded_events
    )


@pytest.mark.asyncio
async def test_run_child_session_recovers_saved_tool_work_when_no_step_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed: list[tuple[str, str | None, str | None]] = []
    failed: list[str] = []
    published: list[object] = []
    recorded_events: list[object] = []

    async def _runtime_factory(
        *, agent: AgentDefinition, user_email: str, executor_agent: AgentDefinition
    ) -> ResolvedStepRuntime:
        del agent, user_email, executor_agent

        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="child-registry",
            executor_connection="child-executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    class _SessionContextManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    class _SessionManager:
        def session_factory(self) -> _SessionContextManager:
            return _SessionContextManager()

        async def mark_completed(
            self,
            session_id: str,
            *,
            result_summary: str | None = None,
            result_content: str | None = None,
        ) -> None:
            completed.append((session_id, result_summary, result_content))

        async def mark_failed(self, session_id: str, **_: object) -> None:
            failed.append(session_id)

    class _Guardrails:
        async def record_events(self, **kwargs: object) -> None:
            recorded_events.extend(kwargs.get("events", []))

        async def read_events(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                events=[
                    {
                        "seq": 1,
                        "type": "tool_call",
                        "data": {
                            "name": "grep",
                            "call_id": "call_saved",
                            "arguments": {"pattern": "retry"},
                        },
                    },
                    {
                        "seq": 2,
                        "type": "tool_result",
                        "data": {
                            "name": "grep",
                            "call_id": "call_saved",
                            "is_error": False,
                            "result": "cognis/core/agent_loop.py:3144: no step output",
                            "has_full_output": True,
                            "recovery_call_id": "call_saved",
                        },
                    },
                ],
                last_seq=2,
                has_more=False,
                missing_stream_fallback_used=False,
            )

    class _EventBus:
        async def publish(self, event: object) -> None:
            published.append(event)

    agent_loop = AgentLoop(
        providers=SimpleNamespace(guardrails=_Guardrails()),
        session_manager=_SessionManager(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    async def _fake_run_step(ctx: object, **_: object) -> None:
        del ctx
        return None

    monkeypatch.setattr(agent_loop, "run_step", _fake_run_step)

    output = await agent_loop._run_child_session(
        child_session=SimpleNamespace(
            session_id="child",
            user_email="user@example.com",
            agent_id="system:explore",
            intaris_session_id="child-intaris",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            is_system=True,
        ),
        task_description="Investigate retry handling",
        parent_intaris_session_id="parent-intaris",
        parent_tool_call_id="call-parent",
        parent_turn_id="turn-1",
        parent_assistant_phase_index=2,
        parent_turn_cycle_index=2,
    )

    assert output is not None
    assert output.outputs["recovered_from_saved_tool_results"] is True
    assert "Partial delegated result recovered from saved tool outputs" in output.content
    assert "cognis/core/agent_loop.py:3144: no step output" in output.content
    assert "read_tool_output(call_id='call_saved')" in output.content
    assert completed and completed[0][0] == "child"
    assert completed[0][2] and "call_saved" in completed[0][2]
    assert failed == []
    assert any(
        getattr(event, "type", None) == "delegation"
        and getattr(event, "data", {}).get("status") == "completed"
        and getattr(event, "data", {}).get("result_source") == "saved_tool_results"
        and getattr(event, "data", {}).get("turn_cycle_index") == 2
        for event in recorded_events
    )
    assert any(
        getattr(event, "type", None) == EventType.DELEGATION_COMPLETED
        and getattr(event, "data", {}).get("turn_cycle_index") == 2
        for event in published
    )


class _ReminderStop(RuntimeError):
    pass


def _test_model_info() -> SimpleNamespace:
    return SimpleNamespace(
        max_tools=None,
        supports_parallel_tool_calls=False,
        supports_tool_choice=False,
        supports_cache_control=False,
        supports_defer_loading=False,
        supports_openai_allowed_tools=False,
        supports_openai_namespace_tools=False,
        supports_tool_search=False,
        provider="test",
    )


class _FakeReminderLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text)

    async def get_model_info(self, model: str | None, **_: object) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) >= 2:
            yield {
                "choices": [
                    {
                        "delta": {
                            "content": "Captured reminder.",
                        }
                    }
                ]
            }
            return
        if False:
            yield {}
        return


class _EmptyThenTextDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None, **_: object) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            if False:
                yield {}
            return
        yield {"choices": [{"delta": {"content": "Recovered direct reply."}}]}


class _AlwaysEmptyDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if False:
            yield {}
        return


class _TodoCleanupOnlyDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            if False:
                yield {}
            return
        if len(self.calls) == 2:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_todos_done",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": json.dumps(
                                            {
                                                "todos": [
                                                    {
                                                        "content": "keep working",
                                                        "status": "completed",
                                                    }
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return
        if False:
            yield {}
        return


class _SilentThenRecoveredDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            while True:
                await asyncio.sleep(60)
                yield {"choices": []}
        yield {"choices": [{"delta": {"content": "Recovered after silence."}}]}


class _ModelErrorThenRecoveredDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) <= 2:
            if len(self.calls) == 2:
                assert "report.pdf" in str(messages)
                assert (
                    "https://cognis.fpy.cz/api/v1/artifacts/content/documents/doc_1/report.pdf"
                    not in str(messages)
                )
            raise RuntimeError(
                'BadRequestError: {"error":{"message":"Timeout while downloading '
                'https://cognis.fpy.cz/api/v1/artifacts/content/documents/doc_1/report.pdf.",'
                '"param": "url"}}'
            )
        yield {"choices": [{"delta": {"content": "Recovered after model error."}}]}


class _ImageInputErrorThenRecoveredDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            raise LLMStreamProviderError(
                "The image data you provided does not represent a valid image.",
                payload={
                    "category": MidStreamErrorCategory.ATTACHMENT_INPUT.value,
                    "message": "The image data you provided does not represent a valid image.",
                    "artifact_ids": ["img_1"],
                    "param": "input",
                },
            )
        assert "photo.png" in str(messages)
        assert "https://cognis.fpy.cz/api/v1/artifacts/content/images/img_1/photo.png" not in str(
            messages
        )
        assert "https://cognis.fpy.cz/api/v1/artifacts/content/images/img_2/good.png" in str(
            messages
        )
        yield {"choices": [{"delta": {"content": "Recovered without native image."}}]}


class _AlwaysSilentDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        while True:
            await asyncio.sleep(60)
            yield {"choices": []}


class _SilentThenContinuationRecoveredDirectLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 3:
            assert any(
                message["role"] == "system"
                and "previous model stream failed" in str(message["content"])
                for message in messages
            )
            yield {"choices": [{"delta": {"content": "Recovered after continuation."}}]}
            return
        while True:
            await asyncio.sleep(60)
            yield {"choices": []}


class _RepeatedIdleThenContinuationRecoveredDirectLLM:
    def __init__(self, *, idle_failures: int) -> None:
        self.idle_failures = idle_failures
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) <= self.idle_failures:
            while True:
                await asyncio.sleep(60)
                yield {"choices": []}
        yield {"choices": [{"delta": {"content": "Recovered after repeated idle."}}]}


class _ProjectedRetryProbeLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    def count_messages_tokens(
        self, messages: list[dict[str, object]], model: str | None = None
    ) -> int:
        del model
        if any("new result" in str(message.get("content")) for message in messages):
            return 98_000
        return 0

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        info = _test_model_info()
        info.max_input_tokens = 100_000
        info.max_output_tokens = 0
        return info

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            while True:
                await asyncio.sleep(60)
                yield {"choices": []}
        yield {"choices": [{"delta": {"content": "Recovered with original projection."}}]}


class _FakeContextAssembler:
    def __init__(self, *, max_context_tokens: int = 0) -> None:
        self.calls: list[dict[str, object]] = []
        self.max_context_tokens = max_context_tokens

    async def assemble(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        role = kwargs.get("user_message_role", "user")
        user_message = kwargs.get("user_message", "")
        disabled_urls = set(kwargs.get("disabled_artifact_urls") or [])
        disabled_ids = set(kwargs.get("disabled_artifact_ids") or [])
        attachment_parts = []
        for attachment in kwargs.get("user_attachments") or []:
            if not isinstance(attachment, dict):
                continue
            attachment_parts.append({"type": "text", "text": str(attachment.get("filename"))})
            artifact_id = attachment.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id in disabled_ids:
                continue
            url = attachment.get("url")
            if isinstance(url, str) and url not in disabled_urls:
                attachment_parts.append(
                    {
                        "type": "file",
                        "file": {"file_url": url, "filename": attachment.get("filename")},
                    }
                )
        content = (
            [{"type": "text", "text": str(user_message)}, *attachment_parts]
            if attachment_parts
            else user_message
        )
        return SimpleNamespace(
            messages=[{"role": role, "content": content}],
            resolved_model="test-model",
            cache_breakpoint_index=None,
            prompt_tokens=0,
            static_tokens=0,
            dynamic_tokens=0,
            max_context_tokens=self.max_context_tokens,
            recommend_compaction=False,
        )


class _FakeNativeImageContextAssembler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.max_context_tokens = 0

    async def assemble(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        disabled_urls = set(kwargs.get("disabled_artifact_urls") or [])
        disabled_ids = set(kwargs.get("disabled_artifact_ids") or [])
        image_urls = {
            "img_1": "https://cognis.fpy.cz/api/v1/artifacts/content/images/img_1/photo.png",
            "img_2": "https://cognis.fpy.cz/api/v1/artifacts/content/images/img_2/good.png",
        }
        content: list[dict[str, object]] = [
            {"type": "text", "text": "photo.png artifact_id=img_1, good.png artifact_id=img_2"}
        ]
        for artifact_id, image_url in image_urls.items():
            if image_url not in disabled_urls and artifact_id not in disabled_ids:
                content.append({"type": "image_url", "image_url": {"url": image_url}})
        return SimpleNamespace(
            messages=[{"role": "user", "content": content}],
            resolved_model="test-model",
            cache_breakpoint_index=None,
            prompt_tokens=0,
            static_tokens=0,
            dynamic_tokens=0,
            max_context_tokens=self.max_context_tokens,
            recommend_compaction=False,
        )


class _SingleTextLLM:
    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        yield {"choices": [{"delta": {"content": "Done."}}]}


class _RecordingSingleTextLLM(_SingleTextLLM):
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        yield {"choices": [{"delta": {"content": "Done."}}]}


class _StepCompleteValidationLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_invalid",
                                    "function": {
                                        "name": "step_complete",
                                        "arguments": (
                                            '{"summary":"done","outcome":{"status":"failed"}}'
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_valid",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": (
                                        '{"summary":"done","claims":["Reported the failure"],'
                                        '"outcome":{"status":"failed","reason":"git identity missing"}}'
                                    ),
                                },
                            }
                        ]
                    }
                }
            ]
        }


class _SilentStepCompleteValidationLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_invalid_silent",
                                    "function": {
                                        "name": "step_complete",
                                        "arguments": (
                                            '{"summary":"done","notification":{"mode":"silent","reason":"Nothing actionable happened."}}'
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_valid_default",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": (
                                        '{"summary":"done","claims":["Reported the result"]}'
                                    ),
                                },
                            }
                        ]
                    }
                }
            ]
        }
        return
        return


class _StepCompleteRetryLLM:
    def __init__(self, first_arguments: dict[str, object], second_arguments: dict[str, object]):
        self.calls: list[list[dict[str, object]]] = []
        self._arguments = [first_arguments, second_arguments]

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        arguments = self._arguments[min(len(self.calls) - 1, len(self._arguments) - 1)]
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_step_complete_{len(self.calls)}",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                }
            ]
        }


class _StepCompleteOrderingLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_step_complete_early",
                                    "function": {
                                        "name": "step_complete",
                                        "arguments": '{"summary":"done"}',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call_trailing_todos",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": '{"todos":[]}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            }
            return

        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_step_complete_final",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": '{"summary":"done"}',
                                },
                            }
                        ]
                    }
                }
            ]
        }


class _TerminalTodosThenExtraToolLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_todos_done",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": json.dumps(
                                            {
                                                "todos": [
                                                    {
                                                        "content": "Inspect scope",
                                                        "status": "completed",
                                                    }
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return
        if len(self.calls) == 2:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_todo_list_after_done",
                                    "function": {"name": "step_todo_list", "arguments": "{}"},
                                },
                                {
                                    "index": 1,
                                    "id": "call_status_after_done",
                                    "function": {"name": "get_status", "arguments": "{}"},
                                },
                            ]
                        }
                    }
                ]
            }
            return
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_step_complete_after_guard",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": '{"summary":"done","claims":["Finalized"]}',
                                },
                            }
                        ]
                    }
                }
            ]
        }


class _TerminalTodosThenTodoCorrectionLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) <= 2:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_todo_write_{len(self.calls)}",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": json.dumps(
                                            {
                                                "todos": [
                                                    {
                                                        "content": "Inspect scope",
                                                        "status": "completed",
                                                    }
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_done_after_todo_correction",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": '{"summary":"done","claims":["Finalized"]}',
                                },
                            }
                        ]
                    }
                }
            ]
        }


class _FinalAssistantContentLLM:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [{"delta": {"content": "Meta commentary that should not be evaluated."}}]
            }
            return
        yield {"choices": [{"delta": {"content": "Final clean briefing text."}}]}
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_write",
                                "function": {
                                    "name": "write_deliverable",
                                    "arguments": '{"content":"Final clean briefing text."}',
                                },
                            },
                            {
                                "index": 1,
                                "id": "call_done",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": '{"summary":"done","claims":["Delivered final text"]}',
                                },
                            },
                        ]
                    }
                }
            ]
        }


class _ResponsesTextThenToolLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "responses_output_item": {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "encrypted",
                    "summary": [],
                },
                "provider_event": "responses",
                "provider_event_type": "response.output_item.done",
            }
            yield {
                "responses_output_item": {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "I'll inspect that now."}],
                },
                "provider_event": "responses",
                "provider_event_type": "response.output_item.done",
            }
            yield {
                "choices": [{"delta": {"content": "I'll inspect that now."}}],
                "provider_event": "responses",
                "provider_event_type": "response.output_text.delta",
                "response_message_phase": "commentary",
            }
            yield {
                "responses_output_item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_todo_done",
                    "name": "step_todo_write",
                    "arguments": json.dumps(
                        {
                            "todos": [
                                {
                                    "content": "Inspect implementation",
                                    "status": "completed",
                                }
                            ]
                        }
                    ),
                },
                "provider_event": "responses",
                "provider_event_type": "response.output_item.done",
            }
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_todo_done",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": json.dumps(
                                            {
                                                "todos": [
                                                    {
                                                        "content": "Inspect implementation",
                                                        "status": "completed",
                                                    }
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ],
                "provider_event": "responses",
                "provider_event_type": "response.function_call_arguments.done",
            }
            yield {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "provider_event": "responses",
                "provider_event_type": "response.completed",
            }
            return

        yield {
            "choices": [{"delta": {"content": "Final user-visible answer."}}],
            "provider_event": "responses",
            "provider_event_type": "response.output_text.delta",
        }
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "provider_event": "responses",
            "provider_event_type": "response.completed",
        }


class _ToolCallCeilingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        self.calls += 1
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_ceiling",
                                "function": {"name": "bash", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        }
        return
        return


class _MalformedSiblingToolCallLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.messages_by_call: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.messages_by_call.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_valid_1",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": (
                                            '{"todos":[{"content":"one","status":"pending"}]}'
                                        ),
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call_bad",
                                    "function": {"name": "step_todo_write", "arguments": "{"},
                                },
                                {
                                    "index": 2,
                                    "id": "call_valid_2",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": (
                                            '{"todos":[{"content":"two","status":"pending"}]}'
                                        ),
                                    },
                                },
                            ]
                        }
                    }
                ]
            }
            return
        yield {"choices": [{"delta": {"content": "done"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class _DelegationDeliverableThenLimitLLM:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_write_before_limit",
                                    "function": {
                                        "name": "write_deliverable",
                                        "arguments": (
                                            '{"content":"Deliverable output from delegated run.",'
                                            '"outputs":{"source":"deliverable"}}'
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        yield {
            "choices": [
                {
                    "delta": {
                        "content": (
                            "Already provided the final findings above. "
                            "No additional user-facing information remains; "
                            "the remaining todo state is stale."
                        )
                    }
                }
            ]
        }


class _DelegationMultiToolThenSummaryLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.tools_by_call: list[object] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **kwargs: object):
        del messages
        self.calls += 1
        self.tools_by_call.append(kwargs.get("tools"))
        if self.calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_probe_1",
                                    "function": {
                                        "name": "bash",
                                        "arguments": '{"command":"pwd"}',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call_probe_2",
                                    "function": {
                                        "name": "bash",
                                        "arguments": '{"command":"ls"}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            }
            return

        yield {"choices": [{"delta": {"content": "Final delegated summary."}}]}


class _DelegationOpenTodosThenMaxStepsLLM:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_open_todos",
                                    "function": {
                                        "name": "step_todo_write",
                                        "arguments": json.dumps(
                                            {
                                                "todos": [
                                                    {
                                                        "content": "Inspect the repository",
                                                        "status": "in_progress",
                                                    },
                                                    {
                                                        "content": "Summarize findings",
                                                        "status": "pending",
                                                    },
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        yield {"choices": [{"delta": {"content": "Maximum steps reached summary."}}]}


class _NoopRememberQueue:
    async def enqueue(self, _: object) -> None:
        return None


@pytest.mark.asyncio
async def test_emit_token_supports_legacy_one_argument_callbacks() -> None:
    tokens: list[str] = []

    async def _on_token(token: str) -> None:
        tokens.append(token)

    await _emit_token(_on_token, "hello", 2)

    assert tokens == ["hello"]


@pytest.mark.asyncio
async def test_emit_token_forwards_turn_cycle_to_two_argument_callbacks() -> None:
    events: list[tuple[str, int | None]] = []

    async def _on_token(token: str, turn_cycle_index: int | None = None) -> None:
        events.append((token, turn_cycle_index))

    await _emit_token(_on_token, "hello", 2)

    assert events == [("hello", 2)]


@pytest.mark.asyncio
async def test_emit_with_optional_trailing_arg_supports_legacy_callbacks() -> None:
    events: list[tuple[str, str]] = []

    async def _callback(tool_name: str, call_id: str) -> None:
        events.append((tool_name, call_id))

    await _emit_with_optional_trailing_arg(_callback, ("read", "call_1"), 2)

    assert events == [("read", "call_1")]


@pytest.mark.asyncio
async def test_emit_with_optional_trailing_arg_forwards_when_supported() -> None:
    events: list[tuple[str, str, int | None]] = []

    async def _callback(
        tool_name: str,
        call_id: str,
        turn_cycle_index: int | None = None,
    ) -> None:
        events.append((tool_name, call_id, turn_cycle_index))

    await _emit_with_optional_trailing_arg(_callback, ("read", "call_1"), 2)

    assert events == [("read", "call_1", 2)]


class _NoopEventBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)
        return None


class _NoopGuardrails:
    async def record_events(self, *_: object, **__: object) -> EventAppendResult:
        return EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1)

    async def read_events(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            events=[],
            last_seq=0,
            has_more=False,
            missing_stream_fallback_used=False,
        )

    async def report_reasoning(self, **_: object) -> ReasoningReportResult:
        return ReasoningReportResult(ok=True)

    async def health(self) -> SimpleNamespace:
        return SimpleNamespace(status="healthy")


class _RecordedEventsGuardrails(_NoopGuardrails):
    def __init__(self, events_by_session: dict[str, list[dict[str, object]]]) -> None:
        self.events_by_session = events_by_session
        self.read_calls: list[dict[str, object]] = []

    async def read_events(self, **kwargs: object) -> EventReadResult:
        self.read_calls.append(dict(kwargs))
        session_id = str(kwargs.get("session_id") or "")
        events = list(self.events_by_session.get(session_id, []))
        return EventReadResult(
            events=events,
            last_seq=len(events),
            has_more=False,
            missing_stream_fallback_used=False,
        )


class _NoopSessionManager:
    def __init__(self) -> None:
        self.rotations: list[dict[str, object]] = []

    def session_factory(self) -> object:
        class _Dummy:
            async def __aenter__(self) -> SimpleNamespace:
                return SimpleNamespace()

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        return _Dummy()

    async def rotate_session(self, **kwargs: object) -> SimpleNamespace:
        self.rotations.append(dict(kwargs))
        current_session = kwargs.get("current_session")
        return SimpleNamespace(
            session_id="sess-rotated",
            intaris_session_id="sess-rotated",
            conversation_id=getattr(current_session, "conversation_id", "conv-1"),
            user_email=getattr(current_session, "user_email", "user@example.com"),
            agent_id=getattr(current_session, "agent_id", "agent-1"),
            mnemory_session_id=None,
        )


class _IdleWaitScheduler:
    def __init__(self) -> None:
        self.waits: list[dict[str, object]] = []
        self.active_turns: dict[str, str | None] = {}

    def active_turn_id(self, conversation_id: str) -> str | None:
        return self.active_turns.get(conversation_id)

    async def wait_for_turn(
        self,
        conversation_id: str,
        *,
        timeout_seconds: int | None = None,
    ) -> None:
        self.waits.append({"conversation_id": conversation_id, "timeout_seconds": timeout_seconds})
        return None


def _background_work_agent_loop(
    session_factory,
    scheduler: _IdleWaitScheduler | None = None,
    *,
    guardrails: object | None = None,
) -> AgentLoop:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(
            llm=SimpleNamespace(),
            guardrails=guardrails if guardrails is not None else _NoopGuardrails(),
        ),
        session_manager=SimpleNamespace(session_factory=session_factory),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    if scheduler is not None:
        agent_loop.set_turn_scheduler(scheduler)
    return agent_loop


def _background_work_ctx(
    conversation_id: str,
    *,
    context_type: str = "web",
    context_ref: str | None = None,
    parent_session_id: str | None = None,
) -> StepContext:
    return StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="controller-session",
            intaris_session_id="controller-session",
            user_email="user@example.com",
            agent_id="controller-agent",
            parent_session_id=parent_session_id,
        ),
        conversation=SimpleNamespace(
            conversation_id=conversation_id,
            project_id=None,
            context=ConversationContext(type=context_type, ref=context_ref),
        ),
        agent=AgentDefinition(
            agent_id="controller-agent",
            owner_email="user@example.com",
            name="Controller",
        ),
        policy=CHAT_POLICY,
        orchestration_mode=OrchestrationMode.FULL,
    )


async def _create_background_work_base(session_factory):
    async with session_factory() as db_session:
        await create_user(db_session, "user@example.com", "User", "hash")
        await create_agent(
            db_session,
            agent_id="controller-agent",
            owner_email="user@example.com",
            name="Controller",
            status="active",
        )
        await create_agent(
            db_session,
            agent_id="target-agent",
            owner_email="user@example.com",
            name="Target",
            status="active",
        )
        controller = await create_conversation(
            db_session,
            user_email="user@example.com",
            agent_id="controller-agent",
            context_type="web",
        )
        await create_session(
            db_session,
            controller.conversation_id,
            "user@example.com",
            "controller-agent",
            session_id="controller-session",
        )
        await db_session.commit()
        return controller


async def _create_managed_link_for_background_work(
    db_session,
    controller_conversation_id: str,
    *,
    title: str,
    target_conversation_id: str,
):
    target = await create_conversation(
        db_session,
        user_email="user@example.com",
        agent_id="target-agent",
        context_type="agent_work",
        conversation_id=target_conversation_id,
    )
    return await create_managed_conversation_link(
        db_session,
        user_email="user@example.com",
        controller_agent_id="controller-agent",
        controller_conversation_id=controller_conversation_id,
        controller_session_id="controller-session",
        target_agent_id="target-agent",
        target_conversation_id=target.conversation_id,
        target_session_id=f"{target.conversation_id}-session",
        title=title,
    )


@pytest.mark.asyncio
async def test_background_work_reminder_prioritizes_warnings_and_caps_items(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'background-work.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)

    now = datetime.now(UTC)
    async with session_factory() as db_session:
        warning_link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Impossible running work",
            target_conversation_id="conv-warning",
        )
        warning_link.turn_state = "running"
        warning_link.active_turn_id = "turn-warning"
        warning_link.completed_at = now
        warning_link.updated_at = now - timedelta(minutes=10)

        failed_link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Failed work",
            target_conversation_id="conv-failed",
        )
        failed_link.turn_state = "failed"
        failed_link.last_error = "boom"
        failed_link.updated_at = now - timedelta(minutes=1)

        running_link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Running work",
            target_conversation_id="conv-running",
        )
        running_link.turn_state = "running"
        running_link.active_turn_id = "turn-running"
        running_link.updated_at = now

        idle_link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Idle open work",
            target_conversation_id="conv-idle",
        )
        idle_link.turn_state = "idle"
        idle_link.updated_at = now

        completed_link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Clean completed work",
            target_conversation_id="conv-completed",
        )
        completed_link.conversation_state = "completed"
        completed_link.turn_state = "completed"
        completed_link.completed_at = now
        completed_link.updated_at = now
        await db_session.commit()

    scheduler = _IdleWaitScheduler()
    scheduler.active_turns["conv-running"] = "turn-running"
    agent_loop = _background_work_agent_loop(session_factory, scheduler)
    reminder = await agent_loop._build_background_work_status_reminder(
        _background_work_ctx(controller.conversation_id)
    )

    assert reminder is not None
    assert reminder["_background_work_status_reminder"] is True
    content = reminder["content"]
    assert content.index("Impossible running work") < content.index("Failed work")
    assert content.index("Failed work") < content.index("Running work")
    assert "warnings: running+completed_at" in content
    assert (
        "recommended_action: keep in mind; continue other work; use "
        "agent_conversation_get/agent_conversation_wait only if this turn depends on the result"
        in content
    )
    assert "1 additional background work items omitted, ids: conv-idle" in content
    assert "Clean completed work" not in content
    await engine.dispose()


@pytest.mark.asyncio
async def test_background_work_reminder_includes_recent_completed_open_managed_conversations(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'background-reusable-managed.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)

    now = datetime.now(UTC)
    async with session_factory() as db_session:
        for index in range(6):
            link = await _create_managed_link_for_background_work(
                db_session,
                controller.conversation_id,
                title=f"Reusable completed work {index}",
                target_conversation_id=f"conv-reusable-{index}",
            )
            link.turn_state = "completed"
            link.completed_at = now - timedelta(minutes=index)
            link.updated_at = now - timedelta(minutes=index)

        closed_link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Closed completed work",
            target_conversation_id="conv-closed-completed",
        )
        closed_link.conversation_state = "closed"
        closed_link.turn_state = "completed"
        closed_link.completed_at = now
        closed_link.updated_at = now
        await db_session.commit()

    agent_loop = _background_work_agent_loop(session_factory)
    reminder = await agent_loop._build_background_work_status_reminder(
        _background_work_ctx(controller.conversation_id)
    )

    assert reminder is not None
    content = reminder["content"]
    assert "recent_completed_open_managed_conversations:" in content
    assert (
        "Prefer agent_conversation_get or agent_conversation_send when continuing related work "
        "instead of starting a new managed conversation." in content
    )
    for index in range(5):
        assert f"Reusable completed work {index}" in content
    assert "Reusable completed work 5" not in content
    assert (
        "1 additional completed-open managed conversations omitted, ids: conv-reusable-5" in content
    )
    assert (
        "recommended_action: use agent_conversation_get to inspect context; "
        "agent_conversation_send to continue related work; "
        "agent_conversation_close if obsolete"
    ) in content
    assert "Closed completed work" not in content
    await engine.dispose()


@pytest.mark.asyncio
async def test_background_work_reminder_includes_delegated_sessions_and_suppresses_child_context(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'background-delegates.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)

    now = datetime.now(UTC)
    async with session_factory() as db_session:
        running_child = await create_session(
            db_session,
            controller.conversation_id,
            "user@example.com",
            "target-agent",
            parent_session_id="controller-session",
            delegation_mode="delegate_async",
            delegation_task="Trace reminder path",
            status="active",
            session_id="sess-running-child",
        )
        running_child.updated_at = now
        stale_child = await create_session(
            db_session,
            controller.conversation_id,
            "user@example.com",
            "target-agent",
            parent_session_id="controller-session",
            delegation_mode="delegate_async",
            delegation_task="Stale reminder path",
            status="active",
            session_id="sess-stale-child",
        )
        stale_child.updated_at = now - timedelta(minutes=10)
        failed_child = await create_session(
            db_session,
            controller.conversation_id,
            "user@example.com",
            "target-agent",
            parent_session_id="controller-session",
            delegation_mode="delegate",
            delegation_task="Review result",
            status="failed",
            session_id="sess-failed-child",
        )
        failed_child.updated_at = now - timedelta(minutes=1)
        completed_child = await create_session(
            db_session,
            controller.conversation_id,
            "user@example.com",
            "target-agent",
            parent_session_id="controller-session",
            delegation_mode="delegate",
            delegation_task="Completed child",
            status="completed",
            session_id="sess-completed-child",
        )
        completed_child.completed_at = now
        completed_child.updated_at = now
        await db_session.commit()

    agent_loop = _background_work_agent_loop(session_factory)
    running_task = asyncio.create_task(asyncio.sleep(3600))
    try:
        async with agent_loop._children_lock:
            agent_loop._active_children.setdefault("controller-session", {})[
                "sess-running-child"
            ] = running_task
        reminder = await agent_loop._build_background_work_status_reminder(
            _background_work_ctx(controller.conversation_id)
        )
    finally:
        running_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running_task

    assert reminder is not None
    content = reminder["content"]
    assert "sess-running-child" in content
    assert (
        "recommended_action: keep in mind; continue other work; use get_subsession only "
        "if this turn depends on the result" in content
    )
    assert "sess-stale-child" in content
    assert "warnings: active-no-running-task, stale-active" in content
    assert "sess-failed-child" in content
    assert "failed-no-summary" in content
    assert "use get_subsession; re-delegate if still needed" in content
    assert "Completed child" not in content

    child_context_reminder = await agent_loop._build_background_work_status_reminder(
        _background_work_ctx(controller.conversation_id, context_type="agent_work")
    )
    assert child_context_reminder is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_background_work_reminder_ignores_non_database_session_factory() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    reminder = await agent_loop._build_background_work_status_reminder(
        _background_work_ctx("conv-no-db")
    )

    assert reminder is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_state", "active_turn_id"),
    [
        ("running", "turn-1"),
        ("queued", None),
        ("idle", "turn-1"),
    ],
)
async def test_agent_conversation_wait_reports_running_when_link_still_active(
    tmp_path: Path,
    turn_state: str,
    active_turn_id: str | None,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'managed-wait.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db_session:
        await create_user(db_session, "user@example.com", "User", "hash")
        await create_agent(
            db_session,
            agent_id="controller-agent",
            owner_email="user@example.com",
            name="Controller",
            status="active",
        )
        await create_agent(
            db_session,
            agent_id="target-agent",
            owner_email="user@example.com",
            name="Target",
            status="active",
        )
        controller = await create_conversation(
            db_session,
            user_email="user@example.com",
            agent_id="controller-agent",
            context_type="web",
        )
        target = await create_conversation(
            db_session,
            user_email="user@example.com",
            agent_id="target-agent",
            context_type="agent_work",
        )
        link = await create_managed_conversation_link(
            db_session,
            user_email="user@example.com",
            controller_agent_id="controller-agent",
            controller_conversation_id=controller.conversation_id,
            controller_session_id="controller-session",
            target_agent_id="target-agent",
            target_conversation_id=target.conversation_id,
            target_session_id="target-session",
            title="Target",
        )
        await update_managed_conversation_link(
            db_session,
            link.link_id,
            conversation_state="open",
            turn_state=turn_state,
            active_turn_id=active_turn_id,
        )
        await db_session.commit()

    scheduler = _IdleWaitScheduler()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=SimpleNamespace(session_factory=session_factory),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    agent_loop.set_turn_scheduler(scheduler)
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="controller-session",
            intaris_session_id="controller-session",
            user_email="user@example.com",
            agent_id="controller-agent",
            parent_session_id=None,
        ),
        conversation=SimpleNamespace(
            conversation_id=controller.conversation_id,
            project_id=None,
        ),
        agent=AgentDefinition(
            agent_id="controller-agent",
            owner_email="user@example.com",
            name="Controller",
        ),
        policy=CHAT_POLICY,
        orchestration_mode=OrchestrationMode.FULL,
    )

    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_wait",
            arguments={
                "conversation_id": target.conversation_id,
                "timeout_seconds": 1,
            },
        ),
        ctx=ctx,
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == ("queued" if turn_state == "queued" else "running")
    assert payload["waited"] is False
    assert payload["conversation"]["turn_state"] == turn_state
    assert payload["conversation"]["active_turn_id"] == active_turn_id
    assert scheduler.waits == [{"conversation_id": target.conversation_id, "timeout_seconds": 1}]
    async with session_factory() as db_session:
        refreshed = await get_managed_conversation_link(db_session, link.link_id)
        assert refreshed is not None
        assert refreshed.turn_state == turn_state
        assert refreshed.active_turn_id == active_turn_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_conversation_create_sets_completion_notification_before_submit(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'managed-create-race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)

    class _CreateSessionManager:
        def __init__(self, factory) -> None:
            self.session_factory = factory

        async def create_conversation_with_root_session(self, **kwargs: object):
            async with self.session_factory() as db_session:
                conversation = await create_conversation(
                    db_session,
                    user_email=str(kwargs["user_email"]),
                    agent_id=str(kwargs["agent_id"]),
                    context_type=kwargs["context"].type,
                    title=str(kwargs["title"]),
                    title_source=str(kwargs["title_source"]),
                    context_ref=kwargs["context"].ref,
                    context_data=dict(kwargs["context"].platform_data),
                    memory_labels=dict(kwargs["context"].memory_labels),
                    conversation_id="conv-created",
                    project_id=kwargs["project_id"],
                )
                session = await create_session(
                    db_session,
                    conversation.conversation_id,
                    str(kwargs["user_email"]),
                    str(kwargs["agent_id"]),
                    session_id="sess-created",
                )
                await db_session.commit()
                return (
                    SimpleNamespace(
                        conversation_id=conversation.conversation_id,
                        project_id=conversation.project_id,
                    ),
                    SimpleNamespace(
                        session_id=session.session_id,
                        intaris_session_id=session.session_id,
                        user_email=session.user_email,
                        agent_id=session.agent_id,
                    ),
                )

    class _CreateCompletesDuringSubmitScheduler:
        def __init__(self) -> None:
            self.notify_on_completion_at_submit: bool | None = None
            self.turn_state_at_submit: str | None = None

        def active_turn_id(self, conversation_id: str) -> str | None:
            assert conversation_id == "conv-created"
            return None

        async def submit_turn(
            self, conversation_id: str, *_args: object, **_kwargs: object
        ) -> None:
            assert conversation_id == "conv-created"
            async with session_factory() as db_session:
                link = await get_managed_conversation_link_for_target(
                    db_session,
                    conversation_id,
                )
                assert link is not None
                self.notify_on_completion_at_submit = link.notify_on_completion
                self.turn_state_at_submit = link.turn_state
                await update_managed_conversation_link(
                    db_session,
                    link.link_id,
                    conversation_state="completed",
                    turn_state="completed",
                    clear_active_turn_id=True,
                    notify_on_completion=False,
                    last_result_summary="done",
                    completed=True,
                )
                await db_session.commit()
            return None

    scheduler = _CreateCompletesDuringSubmitScheduler()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_CreateSessionManager(session_factory),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    agent_loop.set_turn_scheduler(scheduler)

    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_create",
            arguments={
                "agent_id": "target-agent",
                "title": "Target work",
                "initial_message": "Do the work.",
                "wait": False,
            },
        ),
        ctx=_background_work_ctx(
            controller.conversation_id,
            context_ref="web:user:user@example.com:default",
        ),
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == "accepted"
    assert "notified/resumed" in payload["reminder"]
    assert "end this turn now" in payload["reminder"]
    assert result.metadata is not None
    assert result.metadata["async_orchestration_spawned"] is True
    assert scheduler.notify_on_completion_at_submit is True
    assert scheduler.turn_state_at_submit == "running"
    async with session_factory() as db_session:
        link = await get_managed_conversation_link_for_target(db_session, "conv-created")
        assert link is not None
        assert link.turn_state == "completed"
        assert link.conversation_state == "completed"
        assert link.notify_on_completion is False
        assert link.last_result_summary == "done"

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_conversation_create_defaults_to_wait_from_direct_topic(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'managed-create-sync.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)

    class _CreateSessionManager:
        def __init__(self, factory) -> None:
            self.session_factory = factory

        async def create_conversation_with_root_session(self, **kwargs: object):
            async with self.session_factory() as db_session:
                conversation = await create_conversation(
                    db_session,
                    user_email=str(kwargs["user_email"]),
                    agent_id=str(kwargs["agent_id"]),
                    context_type=kwargs["context"].type,
                    title=str(kwargs["title"]),
                    title_source=str(kwargs["title_source"]),
                    context_ref=kwargs["context"].ref,
                    context_data=dict(kwargs["context"].platform_data),
                    memory_labels=dict(kwargs["context"].memory_labels),
                    conversation_id="conv-created-sync",
                    project_id=kwargs["project_id"],
                )
                session = await create_session(
                    db_session,
                    conversation.conversation_id,
                    str(kwargs["user_email"]),
                    str(kwargs["agent_id"]),
                    session_id="sess-created-sync",
                )
                await db_session.commit()
                return (
                    SimpleNamespace(
                        conversation_id=conversation.conversation_id,
                        project_id=conversation.project_id,
                    ),
                    SimpleNamespace(
                        session_id=session.session_id,
                        intaris_session_id=session.session_id,
                        user_email=session.user_email,
                        agent_id=session.agent_id,
                    ),
                )

    class _WaitScheduler:
        def __init__(self) -> None:
            self.waits: list[str] = []

        async def submit_turn(
            self, conversation_id: str, *_args: object, **_kwargs: object
        ) -> None:
            assert conversation_id == "conv-created-sync"
            return None

        def active_turn_id(self, conversation_id: str) -> str | None:
            assert conversation_id == "conv-created-sync"
            return None

        async def wait_for_turn(
            self,
            conversation_id: str,
            *,
            timeout_seconds: int | None = None,
        ) -> TurnResult:
            assert timeout_seconds is None
            self.waits.append(conversation_id)
            return TurnResult(
                conversation_id=conversation_id,
                session_id="sess-created-sync",
                message_id="msg-sync",
                turn_id="turn-sync",
                final_content="done",
            )

    scheduler = _WaitScheduler()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_CreateSessionManager(session_factory),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    agent_loop.set_turn_scheduler(scheduler)

    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_create",
            arguments={
                "agent_id": "target-agent",
                "title": "Target work",
                "initial_message": "Do the work.",
            },
        ),
        ctx=_background_work_ctx(controller.conversation_id),
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert result.metadata is None
    assert payload["created"] is True
    assert payload["waited"] is True
    assert payload["turn"]["turn_id"] == "turn-sync"
    assert scheduler.waits == ["conv-created-sync"]
    async with session_factory() as db_session:
        link = await get_managed_conversation_link_for_target(db_session, "conv-created-sync")
        assert link is not None
        assert link.notify_on_completion is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_conversation_fork_sets_completion_notification_before_submit(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'managed-fork-race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)
    async with session_factory() as db_session:
        source_conversation = await create_conversation(
            db_session,
            user_email="user@example.com",
            agent_id="target-agent",
            context_type="agent_work",
            conversation_id="conv-source",
        )
        source_session = await create_session(
            db_session,
            source_conversation.conversation_id,
            "user@example.com",
            "target-agent",
            session_id="sess-source",
        )
        await create_managed_conversation_link(
            db_session,
            user_email="user@example.com",
            controller_agent_id="controller-agent",
            controller_conversation_id=controller.conversation_id,
            controller_session_id="controller-session",
            target_agent_id="target-agent",
            target_conversation_id=source_conversation.conversation_id,
            target_session_id=source_session.session_id,
            title="Source work",
        )
        await db_session.commit()

    class _ForkSessionManager:
        def __init__(self, factory) -> None:
            self.session_factory = factory

        async def fork_into_new_conversation(self, **kwargs: object):
            async with self.session_factory() as db_session:
                conversation = await create_conversation(
                    db_session,
                    user_email=str(kwargs["user_email"]),
                    agent_id=kwargs["agent"].agent_id,
                    context_type=kwargs["context"].type,
                    title=str(kwargs["title"]),
                    title_source="managed_agent",
                    context_ref=kwargs["context"].ref,
                    context_data=dict(kwargs["context"].platform_data),
                    memory_labels=dict(kwargs["context"].memory_labels),
                    conversation_id="conv-forked",
                )
                session = await create_session(
                    db_session,
                    conversation.conversation_id,
                    str(kwargs["user_email"]),
                    kwargs["agent"].agent_id,
                    session_id="sess-forked",
                )
                await db_session.commit()
                return (
                    SimpleNamespace(
                        conversation_id=conversation.conversation_id,
                        project_id=conversation.project_id,
                        title=conversation.title,
                    ),
                    SimpleNamespace(
                        session_id=session.session_id,
                        intaris_session_id=session.session_id,
                        user_email=session.user_email,
                        agent_id=session.agent_id,
                    ),
                    True,
                )

    class _ForkCompletesDuringSubmitScheduler:
        def __init__(self) -> None:
            self.notify_on_completion_at_submit: bool | None = None
            self.turn_state_at_submit: str | None = None

        def active_turn_checkpoint(self, conversation_id: str) -> None:
            assert conversation_id == "conv-source"
            return None

        def active_turn_id(self, conversation_id: str) -> str | None:
            assert conversation_id == "conv-forked"
            return None

        async def submit_turn(
            self, conversation_id: str, *_args: object, **_kwargs: object
        ) -> None:
            assert conversation_id == "conv-forked"
            async with session_factory() as db_session:
                link = await get_managed_conversation_link_for_target(
                    db_session,
                    conversation_id,
                )
                assert link is not None
                self.notify_on_completion_at_submit = link.notify_on_completion
                self.turn_state_at_submit = link.turn_state
                await update_managed_conversation_link(
                    db_session,
                    link.link_id,
                    conversation_state="completed",
                    turn_state="completed",
                    clear_active_turn_id=True,
                    notify_on_completion=False,
                    last_result_summary="fork done",
                    completed=True,
                )
                await db_session.commit()
            return None

    scheduler = _ForkCompletesDuringSubmitScheduler()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_ForkSessionManager(session_factory),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    agent_loop.set_turn_scheduler(scheduler)

    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_fork",
            arguments={
                "conversation_id": "conv-source",
                "message": "Continue in fork.",
                "wait": False,
            },
        ),
        ctx=_background_work_ctx(
            controller.conversation_id,
            context_ref="web:user:user@example.com:default",
        ),
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == "forked"
    assert "notified/resumed" in payload["reminder"]
    assert "end this turn now" in payload["reminder"]
    assert result.metadata is not None
    assert result.metadata["async_orchestration_spawned"] is True
    assert scheduler.notify_on_completion_at_submit is True
    assert scheduler.turn_state_at_submit == "running"
    async with session_factory() as db_session:
        link = await get_managed_conversation_link_for_target(db_session, "conv-forked")
        assert link is not None
        assert link.turn_state == "completed"
        assert link.conversation_state == "completed"
        assert link.notify_on_completion is False
        assert link.last_result_summary == "fork done"

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_conversation_send_wait_for_queued_turn_uses_observer_result(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'managed-queued-wait.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)
    async with session_factory() as db_session:
        link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Target",
            target_conversation_id="conv-target",
        )
        await db_session.commit()

    class _QueuedWaitScheduler:
        def __init__(self) -> None:
            self.wait_called = False
            self.submitted_observer_count = 0

        def has_active_turn(self, conversation_id: str) -> bool:
            assert conversation_id == "conv-target"
            return True

        def active_turn_id(self, conversation_id: str) -> str | None:
            assert conversation_id == "conv-target"
            return "turn-queued"

        async def submit_turn(self, conversation_id: str, *_args: object, **kwargs: object) -> None:
            assert conversation_id == "conv-target"
            observers = tuple(kwargs["turn_observers"])
            self.submitted_observer_count = len(observers)
            result = TurnResult(
                conversation_id="conv-target",
                session_id="conv-target-session",
                message_id="msg-queued",
                turn_id="turn-queued",
                final_content="queued done",
            )
            await observers[0].on_turn_complete(result)
            return None

        async def wait_for_turn(self, *_args: object, **_kwargs: object) -> None:
            self.wait_called = True
            raise AssertionError("queued wait=true should use the queued observer result")

    scheduler = _QueuedWaitScheduler()
    agent_loop = _background_work_agent_loop(session_factory, scheduler)
    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_send",
            arguments={
                "conversation_id": "conv-target",
                "message": "continue",
                "wait": True,
            },
        ),
        ctx=_background_work_ctx(
            controller.conversation_id,
            context_ref="web:user:user@example.com:default",
        ),
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == "completed"
    assert payload["waited"] is True
    assert payload["turn"]["turn_id"] == "turn-queued"
    assert payload["turn"]["final_content"] == "queued done"
    assert scheduler.submitted_observer_count == 1
    assert scheduler.wait_called is False
    async with session_factory() as db_session:
        refreshed = await get_managed_conversation_link(db_session, link.link_id)
        assert refreshed is not None
        assert refreshed.turn_state == "running"
        assert refreshed.active_turn_id == "turn-queued"
        assert refreshed.notify_on_completion is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_conversation_send_observer_supports_mid_turn_absorb(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'managed-queued-absorb.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)
    async with session_factory() as db_session:
        await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Target",
            target_conversation_id="conv-target",
        )
        await db_session.commit()

    class _QueuedAbsorbScheduler:
        def __init__(self) -> None:
            self.submitted_observers: tuple[object, ...] = ()

        def has_active_turn(self, conversation_id: str) -> bool:
            assert conversation_id == "conv-target"
            return True

        def active_turn_id(self, conversation_id: str) -> str | None:
            assert conversation_id == "conv-target"
            return "turn-active"

        async def submit_turn(self, conversation_id: str, *_args: object, **kwargs: object) -> None:
            assert conversation_id == "conv-target"
            self.submitted_observers = tuple(kwargs["turn_observers"])
            return None

    scheduler = _QueuedAbsorbScheduler()
    agent_loop = _background_work_agent_loop(session_factory, scheduler)
    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_send",
            arguments={
                "conversation_id": "conv-target",
                "message": "continue",
                "wait": False,
            },
        ),
        ctx=_background_work_ctx(
            controller.conversation_id,
            context_ref="web:user:user@example.com:default",
        ),
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == "accepted"
    assert "notified/resumed" in payload["reminder"]
    assert "end this turn now" in payload["reminder"]
    assert result.metadata is not None
    assert result.metadata["async_orchestration_spawned"] is True
    assert len(scheduler.submitted_observers) == 1
    assert getattr(scheduler.submitted_observers[0], "supports_mid_turn_absorb", False) is True

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_state", ["interrupted", "failed"])
async def test_agent_conversation_retry_replays_recorded_target_user_message(
    tmp_path: Path,
    turn_state: str,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / f'managed-retry-{turn_state}.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    controller = await _create_background_work_base(session_factory)
    async with session_factory() as db_session:
        link = await _create_managed_link_for_background_work(
            db_session,
            controller.conversation_id,
            title="Target",
            target_conversation_id="conv-target",
        )
        await update_managed_conversation_link(
            db_session,
            link.link_id,
            conversation_state="open",
            turn_state=turn_state,
            clear_active_turn_id=True,
            last_error="The current turn was cancelled." if turn_state == "interrupted" else "boom",
        )
        await db_session.commit()

    guardrails = _RecordedEventsGuardrails(
        {
            "conv-target-session": [
                {
                    "seq": 1,
                    "type": "developer_message",
                    "data": {"content": "Agent work context"},
                },
                {
                    "seq": 2,
                    "type": "user_message",
                    "data": {"content": "initial managed instruction"},
                },
                {
                    "seq": 3,
                    "type": "assistant_message",
                    "data": {"content": "partial"},
                },
                {
                    "seq": 4,
                    "type": "user_message",
                    "data": {
                        "content": "latest continuation from send",
                        "chat_mode": "build",
                        "chat_mode_source": "one_shot",
                    },
                },
            ]
        }
    )

    class _RetryScheduler:
        def __init__(self) -> None:
            self.submissions: list[dict[str, object]] = []

        def has_active_turn(self, conversation_id: str) -> bool:
            assert conversation_id == "conv-target"
            return False

        def active_turn_id(self, conversation_id: str) -> str | None:
            assert conversation_id == "conv-target"
            return "turn-retry"

        async def submit_turn(
            self,
            conversation_id: str,
            message: str,
            **kwargs: object,
        ) -> None:
            self.submissions.append(
                {"conversation_id": conversation_id, "message": message, **kwargs}
            )
            return None

    scheduler = _RetryScheduler()
    agent_loop = _background_work_agent_loop(
        session_factory,
        scheduler,
        guardrails=guardrails,
    )
    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_retry",
            arguments={
                "conversation_id": "conv-target",
                "wait": False,
            },
        ),
        ctx=_background_work_ctx(
            controller.conversation_id,
            context_ref="web:user:user@example.com:default",
        ),
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == "accepted"
    assert "notified/resumed" in payload["reminder"]
    assert "end this turn now" in payload["reminder"]
    assert scheduler.submissions == [
        {
            "conversation_id": "conv-target",
            "message": "latest continuation from send",
            "user_email": "user@example.com",
            "one_shot_chat_mode": "build",
        }
    ]
    assert guardrails.read_calls == [
        {
            "session_id": "conv-target-session",
            "after_seq": 0,
            "limit": 500,
            "types": ["user_message"],
            "allow_missing_stream": True,
        }
    ]
    async with session_factory() as db_session:
        refreshed = await get_managed_conversation_link(db_session, link.link_id)
        assert refreshed is not None
        assert refreshed.turn_state == "running"
        assert refreshed.active_turn_id == "turn-retry"
        assert refreshed.last_error is None

    await engine.dispose()


class _NoopSessionCache:
    def __init__(self) -> None:
        self.tool_runtime_info: dict[str, object] | None = None
        self.activated_skill_tool_ids: set[str] = set()
        self.skill_tool_classifications: dict[str, list[str]] = {}
        self.entries: dict[str, SimpleNamespace] = {}

    def get_entry(self, session_id: str) -> SimpleNamespace:
        entry = self.entries.get(session_id)
        if entry is None:
            entry = SimpleNamespace(last_llm_usage=None)
            self.entries[session_id] = entry
        return entry

    def get_model_override(self, _: str) -> None:
        return None

    def get_reasoning_effort_override(self, _: str) -> None:
        return None

    def update_context_usage(self, *_: object, **__: object) -> None:
        return None

    def note_context_reserve_clamp(self, _: str) -> bool:
        return True

    def update_tool_runtime_info(self, _: str, info: dict[str, object] | None) -> None:
        self.tool_runtime_info = info

    def get_tool_runtime_info(self, _: str) -> dict[str, object] | None:
        return self.tool_runtime_info

    def get_activated_skill_tool_ids(self, _: str) -> set[str]:
        return set(self.activated_skill_tool_ids)

    def activate_skill_tools(self, _: str, skill_id: str, tool_ids: set[str]) -> None:
        del skill_id
        self.activated_skill_tool_ids.update(tool_ids)

    def get_skill_tool_classification(self, _: str, cache_key: str) -> list[str] | None:
        cached = self.skill_tool_classifications.get(cache_key)
        return list(cached) if cached is not None else None

    def set_skill_tool_classification(self, _: str, cache_key: str, tool_ids: list[str]) -> None:
        self.skill_tool_classifications[cache_key] = list(tool_ids)

    async def append_recorded_events(self, *_: object, **__: object) -> None:
        return None

    async def update_intention(self, *_: object, **__: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_handle_delegate_rejects_async_from_managed_agent_conversation() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="managed", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="worker",
        ),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="worker",
            context=ConversationContext(
                type="agent_work",
                platform_data={"kind": "agent_work"},
            ),
        ),
        agent=AgentDefinition(agent_id="worker", owner_email="user@example.com", name="Worker"),
        policy=CHAT_POLICY,
        orchestration_mode=OrchestrationMode.FULL,
    )

    result = await agent_loop._handle_delegate(
        ToolCall(
            call_id="call-1",
            name="delegate",
            arguments={"task": "Investigate.", "agent_id": "system:explore", "wait": False},
        ),
        ctx=ctx,
        events_to_record=[],
    )

    payload = json.loads(result.output)
    assert result.is_error is True
    assert payload["code"] == "delegate_async_not_allowed"


@pytest.mark.asyncio
async def test_handle_delegate_defaults_to_sync_from_managed_agent_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_handle_delegate_tool_call(*args: object, **kwargs: object):
        captured["wait"] = kwargs.get("wait")
        return ToolResult(output=json.dumps({"status": "started"})), SimpleNamespace(
            session_id="child-1",
            intaris_session_id="child-1",
            agent_id="system:explore",
        )

    async def _fake_run_child_session(**kwargs: object) -> StepOutput:
        captured.update(kwargs)
        return StepOutput(summary="done", content="done", outputs={})

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    monkeypatch.setattr(
        "cognis.core.agent_loop.handle_delegate_tool_call",
        _fake_handle_delegate_tool_call,
    )
    monkeypatch.setattr(agent_loop, "_run_child_session", _fake_run_child_session)

    async def _noop_record(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(agent_loop, "_record_events_strict", _noop_record)
    ctx = StepContext(
        step_definition=StepDefinition(name="managed", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="worker",
        ),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="worker",
            context=ConversationContext(
                type="agent_work",
                platform_data={"kind": "agent_work"},
            ),
        ),
        agent=AgentDefinition(agent_id="worker", owner_email="user@example.com", name="Worker"),
        policy=CHAT_POLICY,
        orchestration_mode=OrchestrationMode.FULL,
    )

    result = await agent_loop._handle_delegate(
        ToolCall(
            call_id="call-1",
            name="delegate",
            arguments={"task": "Investigate.", "agent_id": "system:explore"},
        ),
        ctx=ctx,
        events_to_record=[],
    )

    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert captured["wait"] is True


@pytest.mark.asyncio
async def test_handle_delegate_creation_failure_preserves_parent_cycle_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_handle_delegate_tool_call(*args: object, **kwargs: object):
        del args, kwargs
        return ToolResult(output=json.dumps({"status": "failed"}), is_error=True), None

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    monkeypatch.setattr(
        "cognis.core.agent_loop.handle_delegate_tool_call",
        _fake_handle_delegate_tool_call,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="chat", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="worker",
        ),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="worker",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="worker", owner_email="user@example.com", name="Worker"),
        policy=CHAT_POLICY,
        orchestration_mode=OrchestrationMode.FULL,
    )
    ctx.turn_id = "turn-1"
    ctx.current_turn_cycle_index = 5
    events_to_record: list[SessionEvent] = []

    result = await agent_loop._handle_delegate(
        ToolCall(
            call_id="call-1",
            name="delegate",
            arguments={"task": "Investigate.", "agent_id": "system:explore"},
            runtime_metadata={"assistant_phase_index": 7, "turn_cycle_index": 5},
        ),
        ctx=ctx,
        events_to_record=events_to_record,
    )

    assert result.is_error is True
    assert len(events_to_record) == 1
    event = events_to_record[0]
    assert event.type == "delegation"
    assert event.data["assistant_phase_index"] == 7
    assert event.data["turn_cycle_index"] == 5


def test_managed_agent_conversation_hides_async_delegate_and_conversation_tools() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="managed", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="worker",
            context=ConversationContext(
                type="agent_work",
                platform_data={"kind": "agent_work"},
            ),
        ),
        agent=AgentDefinition(agent_id="worker", owner_email="user@example.com", name="Worker"),
        policy=CHAT_POLICY,
    )

    schemas = loop._build_controller_tool_schemas(ctx)
    by_name = {schema["function"]["name"]: schema for schema in schemas}

    delegate_schema = by_name["delegate"]["function"]["parameters"]
    assert "wait" not in delegate_schema["properties"]
    assert "agent_conversation_create" not in by_name
    assert "agent_conversation_send" not in by_name
    assert "create_task" not in by_name
    assert "compose_and_run_workflow" not in by_name


def test_direct_topic_conversation_hides_async_delegate_but_keeps_managed_conversations() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    schemas = loop._build_controller_tool_schemas(ctx)
    by_name = {schema["function"]["name"]: schema for schema in schemas}

    delegate_schema = by_name["delegate"]["function"]["parameters"]
    assert "wait" not in delegate_schema["properties"]
    assert "agent_conversation_create" in by_name
    assert (
        "wait" not in by_name["agent_conversation_create"]["function"]["parameters"]["properties"]
    )
    assert "wait" not in by_name["agent_conversation_send"]["function"]["parameters"]["properties"]
    assert "wait" not in by_name["agent_conversation_retry"]["function"]["parameters"]["properties"]
    assert "wait" not in by_name["agent_conversation_fork"]["function"]["parameters"]["properties"]
    assert "create_task" in by_name


def test_direct_chat_controller_tools_use_chat_aliases() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt="", allow_questions=True),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        interaction_mode="step_requests",
        controller_tool_surface=CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    )

    exposure = loop._build_controller_tool_exposure(ctx)
    by_name = {schema["function"]["name"]: schema for schema in exposure.schemas}

    assert "request_user_input" in by_name
    assert "todo_write" in by_name
    assert "todo_list" in by_name
    assert "step_request_questions" not in by_name
    assert "step_todo_write" not in by_name
    assert "step_todo_list" not in by_name
    assert "step_complete" not in by_name
    assert "write_deliverable" not in by_name
    assert exposure.alias_map == {
        "request_user_input": "step_request_questions",
        "todo_write": "step_todo_write",
        "todo_list": "step_todo_list",
    }


def test_workflow_controller_tools_keep_step_names() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="plan", type="run", prompt="", allow_questions=True),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="task", ref="task-1"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        interaction_mode="step_requests",
        controller_tool_surface=CONTROLLER_TOOL_SURFACE_WORKFLOW,
    )

    exposure = loop._build_controller_tool_exposure(ctx)
    by_name = {schema["function"]["name"]: schema for schema in exposure.schemas}

    assert "step_request_questions" in by_name
    assert "step_todo_write" in by_name
    assert "step_todo_list" in by_name
    assert "step_complete" in by_name
    assert "request_user_input" not in by_name
    assert "todo_write" not in by_name
    assert "todo_list" not in by_name
    assert exposure.alias_map == {}


def test_direct_chat_delegation_policy_hides_workflow_finalization_tools() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(
            name="delegation", type="run", prompt="", allow_questions=False
        ),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=DIRECT_CHAT_DELEGATION_POLICY,
        interaction_mode="explicit_gates",
        controller_tool_surface=CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    )

    exposure = loop._build_controller_tool_exposure(ctx)
    by_name = {schema["function"]["name"]: schema for schema in exposure.schemas}

    assert "request_user_input" not in by_name
    assert "todo_write" in by_name
    assert "todo_list" in by_name
    assert "step_complete" not in by_name
    assert "write_deliverable" not in by_name
    assert exposure.alias_map == {
        "todo_write": "step_todo_write",
        "todo_list": "step_todo_list",
    }


def test_finalization_allowed_tools_use_visible_direct_chat_aliases() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(
            name="delegation", type="run", prompt="", allow_questions=False
        ),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=DIRECT_CHAT_DELEGATION_POLICY,
        interaction_mode="explicit_gates",
        controller_tool_surface=CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    )

    exposure = loop._build_controller_tool_exposure(ctx)

    assert _visible_allowed_tool_names(
        frozenset({"step_todo_write", "step_complete"}),
        exposure,
    ) == ["todo_write"]


def test_child_background_shell_status_requires_session_match() -> None:
    ctx = SimpleNamespace(
        session=SimpleNamespace(session_id="sess-child", parent_session_id="sess-parent"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=SimpleNamespace(agent_id="agent-1"),
    )

    assert (
        AgentLoop._background_shell_status_matches_context(
            ctx,
            {
                "conversation_id": "conv-1",
                "session_id": "sess-parent",
                "agent_id": "agent-1",
            },
        )
        is False
    )
    assert (
        AgentLoop._background_shell_status_matches_context(
            ctx,
            {
                "conversation_id": "conv-1",
                "session_id": "sess-child",
                "agent_id": "agent-1",
            },
        )
        is True
    )


def test_web_main_chat_exposes_async_delegate_and_managed_conversations() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="web-main", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(
                type="web",
                ref="web:user:user@example.com:default",
            ),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    schemas = loop._build_controller_tool_schemas(ctx)
    by_name = {schema["function"]["name"]: schema for schema in schemas}

    delegate_schema = by_name["delegate"]["function"]["parameters"]
    assert "wait" in delegate_schema["properties"]
    assert "agent_conversation_create" in by_name
    assert "wait" in by_name["agent_conversation_create"]["function"]["parameters"]["properties"]
    assert "create_task" in by_name


def test_task_surface_hides_async_orchestration_tools() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="task", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="task", ref="task-1"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    schemas = loop._build_controller_tool_schemas(ctx)
    by_name = {schema["function"]["name"]: schema for schema in schemas}

    delegate_schema = by_name["delegate"]["function"]["parameters"]
    assert "wait" not in delegate_schema["properties"]
    assert "agent_conversation_create" not in by_name
    assert "create_task" not in by_name
    assert "compose_and_run_workflow" not in by_name


@pytest.mark.asyncio
async def test_agent_conversation_create_rejects_explicit_async_from_direct_topic() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    agent_loop.set_turn_scheduler(SimpleNamespace())

    result = await agent_loop._handle_managed_conversation_tool(
        ToolCall(
            call_id="call-1",
            name="agent_conversation_create",
            arguments={
                "agent_id": "target-agent",
                "title": "Target work",
                "initial_message": "Do the work.",
                "wait": False,
            },
        ),
        ctx=_background_work_ctx("conv-1"),
    )

    payload = json.loads(result.output)
    assert result.is_error is True
    assert payload["code"] == "managed_conversation_async_not_allowed"


@pytest.mark.asyncio
async def test_handle_delegate_rejects_explicit_async_from_direct_topic_conversation() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=ConversationModel(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        orchestration_mode=OrchestrationMode.FULL,
    )

    result = await agent_loop._handle_delegate(
        ToolCall(
            call_id="call-1",
            name="delegate",
            arguments={"task": "Investigate.", "agent_id": "system:explore", "wait": False},
        ),
        ctx=ctx,
        events_to_record=[],
    )

    payload = json.loads(result.output)
    assert result.is_error is True
    assert payload["code"] == "delegate_async_not_allowed"


class _ContextOverflowThenTextLLM:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None, **_: object) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        self.calls += 1
        if self.calls == 1:
            raise LLMContextOverflowError(
                provider_id="provider-1",
                model_id="model-1",
                reason="context_length_exceeded",
            )
        yield {"choices": [{"delta": {"content": "Recovered after compaction."}}]}


class _StreamProviderContextOverflowThenTextLLM(_ContextOverflowThenTextLLM):
    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        self.calls += 1
        if self.calls == 1:
            raise LLMStreamProviderError(
                "context_length_exceeded",
                payload={
                    "category": MidStreamErrorCategory.CONTEXT_OVERFLOW.value,
                    "code": "context_length_exceeded",
                    "message": "context_length_exceeded",
                },
            )
        yield {"choices": [{"delta": {"content": "Recovered after compaction."}}]}


class _StreamFailureChunkContextOverflowThenTextLLM(_ContextOverflowThenTextLLM):
    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        del messages
        self.calls += 1
        if self.calls == 1:
            yield {
                "mid_stream_failure": True,
                "error": "context_length_exceeded",
                "response_error": {
                    "category": MidStreamErrorCategory.CONTEXT_OVERFLOW.value,
                    "code": "context_length_exceeded",
                    "message": "context_length_exceeded",
                },
            }
            return
        yield {"choices": [{"delta": {"content": "Recovered after compaction."}}]}


class _SuccessfulCompactionStrategy:
    compaction_threshold = 0.85
    preserve_turns = 10

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def compact(
        self,
        session: object,
        *,
        trigger: str = "manual",
        model_context: object | None = None,
        long_lived_chat: bool = False,
    ) -> SimpleNamespace:
        del long_lived_chat
        del model_context
        del session
        self.calls.append(trigger)
        return SimpleNamespace(
            compacted=True,
            method="llm",
            summary="compact summary",
            turns_compacted=3,
            tokens_before=100,
            tokens_after=10,
            preserved_tail_events=[],
        )


def test_retry_auto_compaction_skips_non_critical_recommendation() -> None:
    ctx = SimpleNamespace(
        policy=CHAT_POLICY,
        is_retry=True,
        runtime_info={},
    )
    context_result = SimpleNamespace(
        recommend_compaction=True,
        prompt_tokens=220_000,
        max_context_tokens=300_000,
        max_input_tokens=272_000,
        available_prompt_tokens=272_000,
        loop_pressure_threshold_prompt_tokens=258_400,
    )

    assert not _should_run_pre_turn_auto_compaction(ctx, context_result)


def test_retry_auto_compaction_runs_when_rotated_session_still_hard_pressure() -> None:
    ctx = SimpleNamespace(
        policy=CHAT_POLICY,
        is_retry=True,
        runtime_info={},
    )
    context_result = SimpleNamespace(
        recommend_compaction=True,
        prompt_tokens=283_682,
        max_context_tokens=300_000,
        max_input_tokens=272_000,
        available_prompt_tokens=272_000,
        loop_pressure_threshold_prompt_tokens=258_400,
    )

    assert _should_run_pre_turn_auto_compaction(ctx, context_result)


def test_post_turn_auto_compaction_skips_when_projection_made_prompt_safe() -> None:
    ctx = SimpleNamespace(
        policy=CHAT_POLICY,
        last_projection_exceeded_selected_budget=False,
    )
    context_result = SimpleNamespace(recommend_compaction=True)

    assert not _should_run_post_turn_auto_compaction(ctx, context_result)


def test_post_turn_auto_compaction_runs_when_projection_pressure_unresolved() -> None:
    ctx = SimpleNamespace(
        policy=CHAT_POLICY,
        last_projection_exceeded_selected_budget=True,
    )
    context_result = SimpleNamespace(recommend_compaction=True)

    assert _should_run_post_turn_auto_compaction(ctx, context_result)


def test_post_turn_auto_compaction_preserves_conservative_fallback_without_projection() -> None:
    ctx = SimpleNamespace(
        policy=CHAT_POLICY,
        last_projection_exceeded_selected_budget=None,
    )
    context_result = SimpleNamespace(recommend_compaction=True)

    assert _should_run_post_turn_auto_compaction(ctx, context_result)


def test_pre_turn_compaction_history_gate_skips_same_turn_pressure() -> None:
    ctx = SimpleNamespace(session=SimpleNamespace(session_id="sess-1"))
    events = [
        SimpleNamespace(type="user_message"),
        SimpleNamespace(type="assistant_message"),
        SimpleNamespace(type="user_message"),
        SimpleNamespace(type="assistant_message"),
    ]
    cache = SimpleNamespace(
        get_entry=lambda _session_id: SimpleNamespace(),
        get_events_since_compaction=lambda _session_id, _types=None: list(events),
    )

    assert not _has_compactable_pre_turn_history(ctx, cache, preserve_turns=2)


def test_pre_turn_compaction_history_gate_allows_old_history_compaction() -> None:
    ctx = SimpleNamespace(session=SimpleNamespace(session_id="sess-1"))
    events = [
        SimpleNamespace(type="user_message"),
        SimpleNamespace(type="assistant_message"),
        SimpleNamespace(type="user_message"),
        SimpleNamespace(type="assistant_message"),
        SimpleNamespace(type="user_message"),
    ]
    cache = SimpleNamespace(
        get_entry=lambda _session_id: SimpleNamespace(),
        get_events_since_compaction=lambda _session_id, _types=None: list(events),
    )

    assert _has_compactable_pre_turn_history(ctx, cache, preserve_turns=2)


def test_pre_turn_compaction_history_gate_preserves_unknown_cache_behavior() -> None:
    ctx = SimpleNamespace(session=SimpleNamespace(session_id="sess-1"))
    cache = SimpleNamespace(
        get_entry=lambda _session_id: None,
        get_events_since_compaction=lambda _session_id, _types=None: [],
    )

    assert _has_compactable_pre_turn_history(ctx, cache, preserve_turns=2)


@pytest.mark.asyncio
async def test_pre_turn_hard_pressure_without_compactable_history_reaches_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HardPressureAssembler:
        async def assemble(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                messages=[{"role": "user", "content": "Continue."}],
                resolved_model="test-model",
                cache_breakpoint_index=None,
                audit_messages=[],
                system_notices=[],
                prompt_tokens=260_190,
                static_tokens=0,
                dynamic_tokens=260_190,
                max_context_tokens=300_000,
                max_input_tokens=272_000,
                available_prompt_tokens=272_000,
                compaction_threshold=0.85,
                compaction_threshold_prompt_tokens=231_200,
                loop_pressure_threshold_prompt_tokens=258_400,
                recommend_compaction=True,
            )

    class _FewTurnSessionCache(_NoopSessionCache):
        def get_events_since_compaction(
            self, _session_id: str, _types: list[str] | None = None
        ) -> list[SimpleNamespace]:
            del _types
            return [
                SimpleNamespace(type="user_message"),
                SimpleNamespace(type="assistant_message"),
                SimpleNamespace(type="user_message"),
            ]

    fake_llm = _RecordingSingleTextLLM()
    event_bus = _NoopEventBus()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_FewTurnSessionCache(),
        context_assembler=_HardPressureAssembler(),
        compaction_strategy=SimpleNamespace(preserve_turns=2),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=event_bus,
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    auto_compact_saw_model_call: list[bool] = []

    async def _record_auto_compact(*_: object, **__: object) -> None:
        auto_compact_saw_model_call.append(bool(fake_llm.calls))
        if not fake_llm.calls:
            raise AssertionError("pre-turn auto-compaction should be skipped")
        return None

    monkeypatch.setattr(agent_loop, "_auto_compact", _record_auto_compact)

    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Continue.",
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.summary == "Done."
    assert len(fake_llm.calls) == 1
    assert auto_compact_saw_model_call == [True]
    assert any(
        getattr(event, "type", None) == EventType.SYSTEM_NOTICE
        and "Continuing with prompt projection" in str(event.data.get("message"))
        for event in event_bus.events
    )


def test_projection_exact_pressure_forces_critical_reproject_from_skip_path() -> None:
    loop = object.__new__(AgentLoop)
    loop.providers = SimpleNamespace(
        llm=SimpleNamespace(
            count_messages_tokens=lambda messages, _model: (
                98_000 if any(message.get("content") == "new result" for message in messages) else 0
            ),
            count_tokens=lambda text, _model=None: len(str(text)),
        )
    )
    ctx = SimpleNamespace(
        current_model="test-model",
        current_model_info=SimpleNamespace(max_input_tokens=100_000, max_output_tokens=0),
        agent=SimpleNamespace(llm_config=None),
        turn_id="turn-1",
        session=SimpleNamespace(session_id="sess-1"),
        projection_state=ProjectionTurnState(
            turn_id="turn-1",
            policy=ProjectionPolicy.from_budget(
                max_context_tokens=100_000,
                available_prompt_tokens=100_000,
                phase="within_turn",
                pressure_mode="normal",
            ),
            last_result=ProjectionResult(messages=[], mutable_start_index=0),
            last_message_count=0,
        ),
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-old",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-old",
            "content": "old result",
            "_tool_name": "bash",
            "_recovery_call_id": "call-old",
            "_output_size": 10,
        },
        {"role": "user", "content": "new turn"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-new",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-new",
            "content": "new result",
            "_tool_name": "read",
            "_recovery_call_id": "call-new",
            "_output_size": 10,
        },
    ]

    projected = loop._project_model_messages_for_budget(
        ctx,
        messages=messages,
        tool_schemas=[],
        resolved_model="test-model",
        max_context_tokens=100_000,
    )

    assert projected.mode == "critical"
    assert ctx.projection_state.pressure_mode == "critical"
    assert ctx.projection_state.forced_critical_count == 1
    assert "Tool output omitted from prompt." in str(projected.messages[1]["content"])
    assert projected.messages[4]["content"] == "new result"


def test_projection_skip_reprojects_when_tool_prefix_mutates() -> None:
    loop = object.__new__(AgentLoop)
    loop.providers = SimpleNamespace(
        llm=SimpleNamespace(
            count_messages_tokens=lambda _messages, _model: 0,
            count_tokens=lambda text, _model=None: len(str(text)),
        )
    )
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=100_000,
        available_prompt_tokens=100_000,
        phase="within_turn",
        pressure_mode="normal",
    )
    ctx = SimpleNamespace(
        current_model="test-model",
        current_model_info=SimpleNamespace(max_input_tokens=100_000, max_output_tokens=0),
        agent=SimpleNamespace(llm_config=None),
        turn_id="turn-1",
        session=SimpleNamespace(session_id="sess-1"),
        projection_state=ProjectionTurnState(
            turn_id="turn-1",
            policy=policy,
            last_result=ProjectionResult(
                messages=[{"role": "assistant", "content": "saved work"}],
                mutable_start_index=0,
            ),
            last_message_count=1,
        ),
    )
    ctx.projection_state.last_prefix_fingerprint = "stale"
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-mutated",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-mutated",
            "content": "ok",
            "_tool_name": "read",
            "_recovery_call_id": "call-mutated",
            "_output_size": 2,
        },
    ]

    projected = loop._project_model_messages_for_budget(
        ctx,
        messages=messages,
        tool_schemas=[],
        resolved_model="test-model",
        max_context_tokens=100_000,
    )

    assert projected.messages[0]["tool_calls"][0]["id"] == "call-mutated"
    assert projected.messages[1]["tool_call_id"] == "call-mutated"
    assert ctx.projection_state.reproject_count == 1
    assert ctx.projection_state.skip_count == 0


@pytest.mark.asyncio
async def test_idle_timeout_retry_reuses_original_projection() -> None:
    fake_llm = _ProjectedRetryProbeLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(max_context_tokens=100_000),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=100_000,
        available_prompt_tokens=100_000,
        phase="within_turn",
        pressure_mode="normal",
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-idle-projection-retry",
            intaris_session_id="sess-idle-projection-retry",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-idle-projection-retry"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-idle-projection-retry",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )
    ctx.projection_state = ProjectionTurnState(
        turn_id="turn-projection-retry",
        policy=policy,
        last_result=ProjectionResult(
            messages=[{"role": "system", "content": "projected prefix"}],
            mutable_start_index=0,
        ),
        last_message_count=1,
        last_prefix_fingerprint="stale",
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered with original projection."
    assert len(fake_llm.calls) == 2
    assert fake_llm.calls[0] == fake_llm.calls[1]
    assert ctx.projection_state.forced_critical_count == 0


class _ProjectContextProbeExecutor:
    async def tool_execute(
        self, tool_call: ToolCall, timeout_seconds: int | None = None
    ) -> ToolResult:
        del timeout_seconds
        if tool_call.name != "_project_context_probe":
            return ToolResult(output="unexpected tool", is_error=True)
        return ToolResult(
            output="loaded",
            metadata={
                "project_context": {
                    "status": "loaded",
                    "project_root": "/workspace/cognis",
                    "working_directory": "/workspace/cognis",
                    "source_path": "/workspace/cognis/AGENTS.md",
                    "content": (
                        "Instructions for project at /workspace/cognis loaded from "
                        "/workspace/cognis/AGENTS.md.\nProject root: /workspace/cognis\n"
                        "Effective working directory: /workspace/cognis\n\n"
                        "<project_instructions>\nUse pytest.\n</project_instructions>"
                    ),
                    "content_hash": "hash",
                }
            },
        )


class _CrossProjectProbeExecutor:
    def __init__(self) -> None:
        self.probes: list[dict[str, object]] = []

    async def tool_execute(
        self, tool_call: ToolCall, timeout_seconds: int | None = None
    ) -> ToolResult:
        del timeout_seconds
        if tool_call.name != "_project_context_probe":
            return ToolResult(output="unexpected tool", is_error=True)
        self.probes.append(dict(tool_call.arguments))
        return ToolResult(
            output="loaded",
            metadata={
                "project_context": {
                    "status": "loaded",
                    "project_root": "/workspace/obsidian",
                    "working_directory": "/workspace/obsidian",
                    "source_path": "/workspace/obsidian/AGENTS.md",
                    "content": (
                        "Instructions for project at /workspace/obsidian loaded from "
                        "/workspace/obsidian/AGENTS.md.\nProject root: /workspace/obsidian\n"
                        "Effective working directory: /workspace/obsidian\n\n"
                        "<project_instructions>\nUse markdown notes.\n</project_instructions>"
                    ),
                    "content_hash": "obsidian-hash",
                }
            },
        )


class _ReadOnlyProjectContextExecutor:
    def __init__(self) -> None:
        self.probes: list[dict[str, object]] = []
        self.executed_tools: list[str] = []

    async def tool_execute(
        self, tool_call: ToolCall, timeout_seconds: int | None = None
    ) -> ToolResult:
        del timeout_seconds
        if tool_call.name == "_project_context_probe":
            self.probes.append(dict(tool_call.arguments))
            path = str(tool_call.arguments.get("path") or "")
            project_name = "other" if path.startswith("/workspace/other") else "cognis"
            project_root = f"/workspace/{project_name}"
            return ToolResult(
                output="loaded",
                metadata={
                    "project_context": {
                        "status": "loaded",
                        "project_root": project_root,
                        "working_directory": project_root,
                        "source_path": f"{project_root}/AGENTS.md",
                        "content": (
                            f"Instructions for project at {project_root} loaded from "
                            f"{project_root}/AGENTS.md.\nProject root: {project_root}\n"
                            f"Effective working directory: {project_root}\n\n"
                            f"<project_instructions>\nUse {project_name} instructions.\n"
                            "</project_instructions>"
                        ),
                        "content_hash": f"{project_name}-hash",
                    }
                },
            )
        self.executed_tools.append(tool_call.name)
        if tool_call.name == "list_directory":
            return ToolResult(output="listed project files")
        return ToolResult(output="unexpected tool", is_error=True)


class _ProjectContextLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_list_project",
                                    "function": {
                                        "name": "list_directory",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        assert not any(
            message.get("role") == "tool"
            and "project_instructions_loaded" in str(message.get("content"))
            for message in messages
        )
        yield {"choices": [{"delta": {"content": "Done without implicit project context."}}]}
        return


class _OversizedToolArgumentsLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_huge_patch",
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": '{"patchText":"'
                                        + ("x" * (_MAX_TOOL_CALL_ARGUMENT_CHARS + 1)),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        failed_tool_results = [
            message
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_huge_patch"
        ]
        assert len(failed_tool_results) == 1
        payload = json.loads(str(failed_tool_results[0].get("content") or "{}"))
        assert payload["status"] == "rejected"
        assert payload["reason"] == "tool_call_arguments_too_large"
        assert payload["tool"] == "apply_patch"
        assert payload["limit_chars"] == _MAX_TOOL_CALL_ARGUMENT_CHARS
        assert payload["argument_length"] > _MAX_TOOL_CALL_ARGUMENT_CHARS
        assert "x" * 10_000 not in json.dumps(messages)

        yield {"choices": [{"delta": {"content": "Recovered after tool argument rejection."}}]}


class _ReadOnlyProjectContextLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_list_project",
                                    "function": {
                                        "name": "list_directory",
                                        "arguments": '{"path":"/workspace/cognis"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        assert any(
            message.get("role") == "system"
            and "/workspace/cognis/AGENTS.md" in str(message.get("content"))
            for message in messages
        )
        assert any(
            message.get("role") == "tool" and "listed project files" in str(message.get("content"))
            for message in messages
        )
        assert not any(
            message.get("role") == "tool"
            and "project_instructions_loaded" in str(message.get("content"))
            for message in messages
        )
        project_system_indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "system"
            and "/workspace/cognis/AGENTS.md" in str(message.get("content"))
        ]
        tool_indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "tool"
            and "listed project files" in str(message.get("content"))
        ]
        assert tool_indexes and project_system_indexes
        assert max(tool_indexes) < min(project_system_indexes)
        yield {"choices": [{"delta": {"content": "Read-only tool continued."}}]}


class _MixedProjectContextLLM:
    def __init__(
        self,
        *,
        write_path: str = "/workspace/cognis/out.txt",
        expected_source: str = "/workspace/cognis/AGENTS.md",
        second_read_path: str | None = None,
    ) -> None:
        self.calls: list[list[dict[str, object]]] = []
        self.write_path = write_path
        self.expected_source = expected_source
        self.second_read_path = second_read_path

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            tool_calls = [
                {
                    "index": 0,
                    "id": "call_list_project",
                    "function": {
                        "name": "list_directory",
                        "arguments": '{"path":"/workspace/cognis"}',
                    },
                }
            ]
            if self.second_read_path is not None:
                tool_calls.append(
                    {
                        "index": 1,
                        "id": "call_list_second_project",
                        "function": {
                            "name": "list_directory",
                            "arguments": json.dumps({"path": self.second_read_path}),
                        },
                    }
                )
            tool_calls.append(
                {
                    "index": len(tool_calls),
                    "id": "call_write_project",
                    "function": {
                        "name": "write",
                        "arguments": json.dumps({"file_path": self.write_path, "content": "x"}),
                    },
                }
            )
            yield {"choices": [{"delta": {"tool_calls": tool_calls}}]}
            return

        assert any(
            message.get("role") == "tool" and "listed project files" in str(message.get("content"))
            for message in messages
        )
        assert any(
            message.get("role") == "tool"
            and "project_instructions_loaded" in str(message.get("content"))
            and self.expected_source in str(message.get("content"))
            for message in messages
        )
        project_system_indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "system"
            and "/workspace/cognis/AGENTS.md" in str(message.get("content"))
        ]
        tool_indexes = [
            index for index, message in enumerate(messages) if message.get("role") == "tool"
        ]
        assert tool_indexes and project_system_indexes
        assert max(tool_indexes) < min(project_system_indexes)
        yield {"choices": [{"delta": {"content": "Mutating tool was retried."}}]}


class _CrossProjectLLM:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_list_obsidian",
                                    "function": {
                                        "name": "list_directory",
                                        "arguments": '{"path":"/workspace/obsidian"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            return

        assert any(
            message.get("role") == "system"
            and "/workspace/obsidian/AGENTS.md" in str(message.get("content"))
            for message in messages
        )
        yield {"choices": [{"delta": {"content": "Cross-project instructions loaded."}}]}


class _ExplicitProjectContextLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        yield {"choices": [{"delta": {"content": "Done with explicit project context."}}]}
        return


class _ExistingProjectSessionCache(_NoopSessionCache):
    def __init__(self) -> None:
        super().__init__()
        self.project_contexts = {
            "/workspace/current": ProjectContextEntry(
                project_root="/workspace/current",
                source_path="/workspace/current/AGENTS.md",
                content="Current project instructions.",
                content_hash="current-hash",
                working_directory="/workspace/current",
            )
        }

    def get_project_context(
        self, session_id: str, project_root: str | None
    ) -> ProjectContextEntry | None:
        del session_id
        return None if project_root is None else self.project_contexts.get(project_root)

    async def store_project_context(
        self, session_id: str, project_context: ProjectContextEntry
    ) -> ProjectContextEntry:
        del session_id
        self.project_contexts[project_context.project_root] = project_context
        return project_context


async def _run_with_assembler(ctx: StepContext, assembler: _FakeContextAssembler) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_FinalAssistantContentLLM(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=assembler,
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    output = await agent_loop.run_step(ctx)
    assert output is not None


async def _run_reminder_capture(ctx: object) -> list[list[dict[str, object]]]:
    fake_llm = _FakeReminderLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    output = await agent_loop.run_step(ctx)
    assert output is not None
    assert len(fake_llm.calls) >= 2
    return fake_llm.calls


@pytest.mark.asyncio
async def test_oversized_tool_arguments_return_tool_error_and_continue() -> None:
    fake_llm = _OversizedToolArgumentsLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-oversized-tool-arguments",
            intaris_session_id="sess-oversized-tool-arguments",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-oversized-tool-arguments"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Apply a huge patch",
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.content == "Recovered after tool argument rejection."
    assert len(fake_llm.calls) == 2


@pytest.mark.asyncio
async def test_project_context_is_not_loaded_from_ambient_workdir_only() -> None:
    fake_llm = _ProjectContextLLM()
    fake_context = _FakeContextAssembler()
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="list_directory",
                description="List a directory",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
            handler=None,
        )
    )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=fake_context,
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(
            _is_non_bypassable=lambda _name, non_bypassable: non_bypassable
        ),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-project",
            conversation_id="conv-project",
            intaris_session_id="sess-project",
            mnemory_session_id="mem-project",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-project",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect the cognis project",
        tool_registry=registry,
        executor_connection=_ProjectContextProbeExecutor(),
        executor_environment=build_local_executor_environment(
            executor_id="exec-project",
            executor_type="in_process",
            source="test",
        ),
        orchestration_mode=OrchestrationMode.FULL,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Done without implicit project context."
    assert len(fake_llm.calls) == 2
    assert fake_context.calls[0]["include_project_context"] is False


@pytest.mark.asyncio
async def test_explicit_cross_project_path_still_triggers_project_probe() -> None:
    fake_llm = _CrossProjectLLM()
    probe_executor = _CrossProjectProbeExecutor()
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="list_directory",
                description="List a directory",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
            handler=None,
        )
    )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_ExistingProjectSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-cross-project",
            conversation_id="conv-cross-project",
            intaris_session_id="sess-cross-project",
            mnemory_session_id="mem-cross-project",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-cross-project",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect the obsidian project",
        tool_registry=registry,
        executor_connection=probe_executor,
        executor_environment=build_local_executor_environment(
            executor_id="exec-cross-project",
            executor_type="in_process",
            source="test",
        ),
        workspace_root="/workspace/current",
        working_directory="/workspace/current",
        orchestration_mode=OrchestrationMode.FULL,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Cross-project instructions loaded."
    assert len(probe_executor.probes) == 1
    assert probe_executor.probes[0]["path"] == "/workspace/obsidian"


@pytest.mark.asyncio
async def test_read_only_tool_continues_after_project_context_load() -> None:
    fake_llm = _ReadOnlyProjectContextLLM()
    probe_executor = _ReadOnlyProjectContextExecutor()

    async def execute_tool(tool_call: ToolCall, *args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        return await probe_executor.tool_execute(tool_call)

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="list_directory",
                description="List a directory",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
            handler=None,
        )
    )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_ExistingProjectSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(
            execute=execute_tool,
            _is_non_bypassable=lambda _name, non_bypassable: non_bypassable,
        ),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-read-only-project",
            conversation_id="conv-read-only-project",
            intaris_session_id="sess-read-only-project",
            mnemory_session_id="mem-read-only-project",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-read-only-project",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect the cognis project",
        tool_registry=registry,
        executor_connection=probe_executor,
        executor_environment=build_local_executor_environment(
            executor_id="exec-read-only-project",
            executor_type="in_process",
            source="test",
        ),
        workspace_root="/workspace/current",
        working_directory="/workspace/current",
        orchestration_mode=OrchestrationMode.FULL,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Read-only tool continued."
    assert probe_executor.executed_tools == ["list_directory"]
    assert len(probe_executor.probes) == 1
    assert len(fake_llm.calls) == 2


@pytest.mark.asyncio
async def test_mutating_trailing_tool_retries_after_read_only_project_context_load() -> None:
    fake_llm = _MixedProjectContextLLM()
    probe_executor = _ReadOnlyProjectContextExecutor()

    async def execute_tool(tool_call: ToolCall, *args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        return await probe_executor.tool_execute(tool_call)

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="list_directory",
                description="List a directory",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
            handler=None,
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="write",
                description="Write a file",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
                source=ToolSource(type="executor"),
                read_only=False,
            ),
            handler=None,
        )
    )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_ExistingProjectSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(
            execute=execute_tool,
            _is_non_bypassable=lambda _name, non_bypassable: non_bypassable,
        ),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-mixed-project",
            conversation_id="conv-mixed-project",
            intaris_session_id="sess-mixed-project",
            mnemory_session_id="mem-mixed-project",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-mixed-project",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect then modify the cognis project",
        tool_registry=registry,
        executor_connection=probe_executor,
        executor_environment=build_local_executor_environment(
            executor_id="exec-mixed-project",
            executor_type="in_process",
            source="test",
        ),
        workspace_root="/workspace/current",
        working_directory="/workspace/current",
        orchestration_mode=OrchestrationMode.FULL,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Mutating tool was retried."
    assert probe_executor.executed_tools == ["list_directory"]
    assert [probe["path"] for probe in probe_executor.probes] == [
        "/workspace/cognis",
        "/workspace/cognis/out.txt",
    ]
    assert len(fake_llm.calls) == 2


@pytest.mark.asyncio
async def test_cross_project_mutating_trailing_tool_loads_own_project_context() -> None:
    fake_llm = _MixedProjectContextLLM(
        write_path="/workspace/other/out.txt",
        expected_source="/workspace/other/AGENTS.md",
    )
    probe_executor = _ReadOnlyProjectContextExecutor()

    async def execute_tool(tool_call: ToolCall, *args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        return await probe_executor.tool_execute(tool_call)

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="list_directory",
                description="List a directory",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
            handler=None,
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="write",
                description="Write a file",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
                source=ToolSource(type="executor"),
                read_only=False,
            ),
            handler=None,
        )
    )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_ExistingProjectSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(
            execute=execute_tool,
            _is_non_bypassable=lambda _name, non_bypassable: non_bypassable,
        ),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-cross-mixed-project",
            conversation_id="conv-cross-mixed-project",
            intaris_session_id="sess-cross-mixed-project",
            mnemory_session_id="mem-cross-mixed-project",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-cross-mixed-project",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect cognis then modify other project",
        tool_registry=registry,
        executor_connection=probe_executor,
        executor_environment=build_local_executor_environment(
            executor_id="exec-cross-mixed-project",
            executor_type="in_process",
            source="test",
        ),
        workspace_root="/workspace/current",
        working_directory="/workspace/current",
        orchestration_mode=OrchestrationMode.FULL,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Mutating tool was retried."
    assert probe_executor.executed_tools == ["list_directory"]
    assert [probe["path"] for probe in probe_executor.probes] == [
        "/workspace/cognis",
        "/workspace/other/out.txt",
    ]


@pytest.mark.asyncio
async def test_mutating_tool_retries_with_matching_earlier_project_context() -> None:
    fake_llm = _MixedProjectContextLLM(
        write_path="/workspace/cognis/out.txt",
        expected_source="/workspace/cognis/AGENTS.md",
        second_read_path="/workspace/other",
    )
    probe_executor = _ReadOnlyProjectContextExecutor()

    async def execute_tool(tool_call: ToolCall, *args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        return await probe_executor.tool_execute(tool_call)

    registry = ToolRegistry()
    for name, read_only in (("list_directory", True), ("write", False)):
        registry.register(
            RegisteredTool(
                definition=ToolDefinition(
                    name=name,
                    description=name,
                    parameters={"type": "object", "properties": {}},
                    source=ToolSource(type="executor"),
                    read_only=read_only,
                ),
                handler=None,
            )
        )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_ExistingProjectSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(
            execute=execute_tool,
            _is_non_bypassable=lambda _name, non_bypassable: non_bypassable,
        ),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-multi-project",
            conversation_id="conv-multi-project",
            intaris_session_id="sess-multi-project",
            mnemory_session_id="mem-multi-project",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-multi-project",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect two projects then modify cognis",
        tool_registry=registry,
        executor_connection=probe_executor,
        executor_environment=build_local_executor_environment(
            executor_id="exec-multi-project",
            executor_type="in_process",
            source="test",
        ),
        workspace_root="/workspace/current",
        working_directory="/workspace/current",
        orchestration_mode=OrchestrationMode.FULL,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Mutating tool was retried."
    assert probe_executor.executed_tools == ["list_directory", "list_directory"]
    assert [probe["path"] for probe in probe_executor.probes] == [
        "/workspace/cognis",
        "/workspace/other",
        "/workspace/cognis/out.txt",
    ]


def test_project_context_fallback_matches_relative_tool_path_to_workdir() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-relative", intaris_session_id="sess-relative"),
        conversation=SimpleNamespace(conversation_id="conv-relative"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        working_directory="/workspace/cognis",
    )
    project_context = ProjectContextEntry(
        project_root="/workspace/cognis",
        source_path="/workspace/cognis/AGENTS.md",
        content="Instructions",
        content_hash="hash",
        working_directory="/workspace/cognis",
    )

    matched = agent_loop._project_context_loaded_for_tool_target(
        ctx,
        ToolCall(name="write", call_id="call_write", arguments={"file_path": "out.txt"}),
        {project_context.project_root: project_context},
    )

    assert matched is project_context


@pytest.mark.asyncio
async def test_explicit_task_workdir_keeps_project_context_autoload() -> None:
    fake_llm = _ExplicitProjectContextLLM()
    fake_context = _FakeContextAssembler()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_ExistingProjectSessionCache(),
        context_assembler=fake_context,
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(
            _is_non_bypassable=lambda _name, non_bypassable: non_bypassable
        ),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="task", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-explicit-project",
            conversation_id="conv-explicit-project",
            intaris_session_id="sess-explicit-project",
            mnemory_session_id="mem-explicit-project",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-explicit-project",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect the explicitly configured project",
        executor_environment=build_local_executor_environment(
            executor_id="exec-explicit-project",
            executor_type="in_process",
            source="test",
        ),
        workspace_root="/workspace/current",
        working_directory="/workspace/current",
        workspace_root_explicit=True,
        working_directory_explicit=True,
        orchestration_mode=OrchestrationMode.FULL,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Done with explicit project context."
    assert fake_context.calls[0]["include_project_context"] is True


@pytest.mark.asyncio
async def test_direct_todo_reprompt_is_system_message() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[{"content": "keep working", "status": "pending"}],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[
            SimpleNamespace(
                model_dump=lambda **_: {
                    "artifact_id": "art_1",
                    "kind": "pdf",
                    "mime_type": "application/pdf",
                    "filename": "report.pdf",
                    "url": "https://cognis.fpy.cz/api/v1/artifacts/content/documents/doc_1/report.pdf",
                }
            )
        ],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-1",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )
    calls = await _run_reminder_capture(ctx)
    assert calls[1][-1]["role"] == "system"
    # New prescriptive wording: "non-terminal todos" + terminal-state
    # instruction + do-not-repeat guidance.
    content = str(calls[1][-1]["content"])
    assert "non-terminal todos" in content
    assert "'completed'" in content and "'cancelled'" in content
    assert "produce no assistant text" in content
    assert "Do not repeat, restate, or paraphrase" in content


@pytest.mark.asyncio
async def test_direct_todo_cleanup_only_can_complete_silently() -> None:
    fake_llm = _TodoCleanupOnlyDirectLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-todo-cleanup",
            intaris_session_id="sess-todo-cleanup",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-todo-cleanup"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[{"content": "keep working", "status": "pending"}],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id=None,
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Todo cleanup completed"
    assert output.content == ""
    assert len(fake_llm.calls) == 3
    assert "non-terminal todos" in str(fake_llm.calls[1][-1]["content"])
    assert "previous response was empty" not in str(fake_llm.calls[2])
    assert ctx.todos == [{"content": "keep working", "status": "completed"}]


@pytest.mark.asyncio
async def test_responses_text_with_tool_call_is_streamed_persisted_and_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_llm = _ResponsesTextThenToolLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    recorded_batches: list[list[SessionEvent]] = []

    async def _record_events_strict(
        ctx: StepContext,
        events: list[SessionEvent],
        **_: object,
    ) -> bool:
        del ctx
        recorded_batches.append(list(events))
        return True

    monkeypatch.setattr(agent_loop, "_record_events_strict", _record_events_strict)
    streamed: list[str] = []

    async def _on_token(token: str) -> None:
        streamed.append(token)

    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-responses-tool-text",
            intaris_session_id="sess-responses-tool-text",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-responses-tool-text"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[{"content": "Inspect implementation", "status": "pending"}],
        policy=CHAT_POLICY,
        user_message="Inspect this.",
        user_attachments=[],
        system_initiated=False,
    )

    output = await agent_loop.run_step(ctx, on_token=_on_token)

    assert output is not None
    assert output.content == "Final user-visible answer."
    assert streamed == ["I'll inspect that now.", "\n\n", "Final user-visible answer."]
    recorded_events = [event for batch in recorded_batches for event in batch]
    assert any(
        event.type == "assistant_message" and event.data.get("content") == "I'll inspect that now."
        for event in recorded_events
    )
    assert any(
        event.type == "assistant_message"
        and event.data.get("content") == "Final user-visible answer."
        for event in recorded_events
    )
    assert not any(
        event.type == "lifecycle" and event.data.get("event") == "assistant_internal_trace"
        for event in recorded_events
    )
    replayed_assistant = [
        message for message in fake_llm.calls[1] if message.get("role") == "assistant"
    ]
    assert replayed_assistant
    assert replayed_assistant[-1]["content"] == "I'll inspect that now."


def test_reattach_responses_output_items_restores_projected_assistant_metadata() -> None:
    source_messages = [
        {
            "role": "assistant",
            "content": "I'll inspect that now.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
            "_responses_output_items": [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "encrypted"},
                {"type": "message", "id": "msg_1", "phase": "commentary"},
                {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
            ],
        }
    ]
    projected_messages = [
        {
            "role": "assistant",
            "content": "I'll inspect that now.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        }
    ]

    restored = _reattach_responses_output_items(projected_messages, source_messages)

    assert [item["type"] for item in restored[0].get("_responses_output_items", [])] == [
        "reasoning",
        "message",
        "function_call",
    ]


def test_reattach_responses_output_items_matches_content_only_assistant_message() -> None:
    source_messages = [
        {
            "role": "assistant",
            "content": "Reminder acknowledged.",
            "_responses_output_items": [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "encrypted"}
            ],
        }
    ]
    projected_messages = [{"role": "assistant", "content": "Reminder acknowledged."}]

    restored = _reattach_responses_output_items(projected_messages, source_messages)

    assert restored[0]["_responses_output_items"] == source_messages[0]["_responses_output_items"]


def test_responses_output_items_for_persistence_filters_and_caps_payload() -> None:
    persisted = _responses_output_items_for_persistence(
        [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "encrypted"},
            {"type": "message", "id": "msg_1", "content": "not durable"},
            {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
            {
                "type": "apply_patch_call",
                "call_id": "call_patch",
                "operation": {"type": "update_file", "path": "/tmp/a.txt", "diff": "@@\n-x\n+y\n"},
            },
        ]
    )

    assert [item["type"] for item in persisted] == [
        "reasoning",
        "function_call",
        "apply_patch_call",
    ]
    assert (
        _responses_output_items_for_persistence(
            [{"type": "reasoning", "encrypted_content": "x" * (256 * 1024)}]
        )
        == []
    )


def test_responses_output_items_for_persistence_bounds_delegate_arguments() -> None:
    persisted = _responses_output_items_for_persistence(
        [
            {
                "type": "function_call",
                "call_id": "call_delegate",
                "name": "delegate",
                "arguments": json.dumps(
                    {
                        "task": "Inspect private details",
                        "context": "sensitive prompt content",
                        "expected_output": "full report",
                        "wait": True,
                    }
                ),
            }
        ]
    )

    arguments = json.loads(persisted[0]["arguments"])
    assert arguments == {
        "title": "Inspect private details",
        "task": "Inspect private details",
        "context": "sensitive prompt content",
        "expected_output": "full report",
        "wait": True,
        "_bounded": "delegate input limited to 4000 chars per string field",
    }


def test_responses_output_items_for_persistence_truncates_delegate_arguments() -> None:
    persisted = _responses_output_items_for_persistence(
        [
            {
                "type": "function_call",
                "call_id": "call_delegate",
                "name": "delegate",
                "arguments": json.dumps(
                    {
                        "task": "x" * 5000,
                        "context": "context",
                        "expected_output": "report",
                        "wait": True,
                    }
                ),
            }
        ]
    )

    arguments = json.loads(persisted[0]["arguments"])
    assert arguments["task"] == "x" * 4000
    assert arguments["task_truncated"] is True
    assert arguments["context"] == "context"
    assert arguments["expected_output"] == "report"
    assert arguments["wait"] is True
    assert arguments["_bounded"] == "delegate input limited to 4000 chars per string field"


def test_reattach_anthropic_thinking_blocks_restores_projected_assistant_metadata() -> None:
    source_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
            "_anthropic_thinking_blocks": [
                {"type": "thinking", "thinking": "Need to inspect.", "signature": "sig-1"},
                {"type": "redacted_thinking", "data": "opaque"},
            ],
        }
    ]
    projected_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        }
    ]

    restored = _reattach_anthropic_thinking_blocks(projected_messages, source_messages)

    assert restored[0]["_anthropic_thinking_blocks"] == [
        {"type": "thinking", "thinking": "Need to inspect.", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]


def test_strip_internal_message_fields_removes_anthropic_thinking_blocks() -> None:
    stripped = _strip_internal_message_fields(
        {
            "role": "assistant",
            "content": "visible",
            "_anthropic_thinking_blocks": [
                {"type": "thinking", "thinking": "private", "signature": "sig"}
            ],
        }
    )

    assert stripped == {"role": "assistant", "content": "visible"}


def test_cycle_cache_breakpoints_move_per_cycle_and_preserve_ttl_order() -> None:
    messages = [
        {"role": "system", "content": "prefix"},
        {"role": "user", "content": "previous", "_turn_boundary": True},
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "current", "_turn_boundary": True},
        {"role": "assistant", "content": None, "tool_calls": []},
    ]

    first_cycle = _cycle_cache_breakpoints(messages[:4], prefix_index=0, ttl="1h")
    second_cycle = _cycle_cache_breakpoints(messages, prefix_index=0, ttl="1h")

    assert first_cycle == [
        {"index": 0, "ttl": "1h"},
        {"index": 2, "ttl": "5m"},
        {"index": 3, "ttl": "5m"},
    ]
    assert second_cycle == [
        {"index": 0, "ttl": "1h"},
        {"index": 2, "ttl": "5m"},
        {"index": 4, "ttl": "5m"},
    ]
    assert [item["ttl"] for item in second_cycle] == ["1h", "5m", "5m"]


@pytest.mark.asyncio
async def test_direct_turn_absorbs_queued_batch_before_todo_reprompt() -> None:
    fake_llm = _FakeReminderLLM()
    consumed_reasons: list[str] = []

    async def _consume_boundary_batch(reason: str) -> list[dict[str, object]]:
        consumed_reasons.append(reason)
        if len(consumed_reasons) > 1:
            return []
        return [
            {
                "content": "Also include the deployment notes.",
                "attachments": [],
                "system_initiated": False,
                "follow_up": None,
            }
        ]

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[{"content": "keep working", "status": "pending"}],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-1",
        orchestration_mode=OrchestrationMode.NONE,
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
        consume_boundary_batch=_consume_boundary_batch,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert isinstance(output.error, str)
    assert "after_assistant_message" in consumed_reasons
    assert len(fake_llm.calls) >= 2
    assert fake_llm.calls[1][-1]["role"] == "user"
    assert fake_llm.calls[1][-1]["content"] == "Also include the deployment notes."


@pytest.mark.asyncio
async def test_workflow_step_absorbs_boundary_batch_before_step_complete_reprompt() -> None:
    fake_llm = _FakeReminderLLM()
    consumed_reasons: list[str] = []
    recorded_batches: list[list[SessionEvent]] = []

    async def _consume_boundary_batch(reason: str) -> list[dict[str, object]]:
        consumed_reasons.append(reason)
        if len(consumed_reasons) > 1:
            return []
        return [
            {
                "content": (
                    "Additional workflow task context from user@example.com:\n\n"
                    "Please include the rollout impact."
                ),
                "attachments": [],
                "system_initiated": False,
                "follow_up": None,
                "source": "task_context_comment",
                "comment_id": "tcmt-1",
                "author_email": "user@example.com",
            }
        ]

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    async def _record_events_strict(
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: object | None = None,
    ) -> bool:
        del ctx, reason, on_token
        recorded_batches.append(list(events))
        events.clear()
        return True

    agent_loop._record_events_strict = _record_events_strict  # type: ignore[method-assign]
    ctx = StepContext(
        step_definition=StepDefinition(name="build", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=WorkflowState(),
        step_run_id="sr-1",
        orchestration_mode=OrchestrationMode.NONE,
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
        consume_boundary_batch=_consume_boundary_batch,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert isinstance(output.error, str)
    assert "after_assistant_message" in consumed_reasons
    assert len(fake_llm.calls) >= 2
    assert fake_llm.calls[1][-1]["role"] == "user"
    assert fake_llm.calls[1][-1]["content"].endswith("Please include the rollout impact.")
    recorded_events = [event for batch in recorded_batches for event in batch]
    context_events = [
        event
        for event in recorded_events
        if event.type == "user_message" and event.data.get("source") == "task_context_comment"
    ]
    assert len(context_events) == 1
    assert context_events[0].data["comment_id"] == "tcmt-1"
    assert context_events[0].data["author_email"] == "user@example.com"


@pytest.mark.asyncio
async def test_boundary_absorbed_user_message_persists_client_identity() -> None:
    """Mid-turn absorbed queued messages must persist client_message_id/queue_id.

    The optimistic bubble and live WS event use user:{client_message_id}; a
    canonical projection without it derives user:user:{session}:{seq}, so the
    optimistic message never confirms — duplicating or vanishing on refresh.
    """
    fake_llm = _FakeReminderLLM()
    consumed_reasons: list[str] = []
    recorded_batches: list[list[SessionEvent]] = []

    async def _consume_boundary_batch(reason: str) -> list[dict[str, object]]:
        consumed_reasons.append(reason)
        if len(consumed_reasons) > 1:
            return []
        return [
            {
                "queue_id": "qmsg_123",
                "client_message_id": "cmsg_abc",
                "content": "Queued while streaming.",
                "attachments": [],
                "system_initiated": False,
                "follow_up": None,
            }
        ]

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    async def _record_events_strict(
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: object | None = None,
    ) -> bool:
        del ctx, reason, on_token
        recorded_batches.append(list(events))
        events.clear()
        return True

    agent_loop._record_events_strict = _record_events_strict  # type: ignore[method-assign]
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[{"content": "keep working", "status": "pending"}],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-1",
        orchestration_mode=OrchestrationMode.NONE,
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
        consume_boundary_batch=_consume_boundary_batch,
    )

    await agent_loop.run_step(ctx)

    recorded_events = [event for batch in recorded_batches for event in batch]
    absorbed = [
        event
        for event in recorded_events
        if event.type == "user_message" and event.data.get("content") == "Queued while streaming."
    ]
    assert len(absorbed) == 1
    assert absorbed[0].data["client_message_id"] == "cmsg_abc"
    assert absorbed[0].data["queue_id"] == "qmsg_123"
    assert absorbed[0].data["message_id"] == "cmsg_abc"


class _ResponsesApiToolCycleLLM:
    """Fake LLM that exposes Responses API and runs one tool cycle to a final answer.

    Used to verify that with Responses API selected, every cycle uses canonical
    projection (no continuation kwargs, no opaque provider state) and the model
    sees tool results in the projected transcript on the next cycle.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text) // 4 + 1

    def count_messages_tokens(
        self, messages: list[dict[str, object]], model: str | None = None
    ) -> int:
        del model
        return len(json.dumps(messages, default=str)) // 4 + 1

    async def get_model_info(self, model: str | None, **_: object) -> SimpleNamespace:
        del model
        info = _test_model_info()
        info.max_input_tokens = 100_000
        info.max_output_tokens = 8_000
        return info

    async def stream_generate(self, messages: list[dict[str, object]], **kwargs: object):
        self.calls.append(
            {"messages": [dict(message) for message in messages], "kwargs": dict(kwargs)}
        )
        if len(self.calls) == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_lookup",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
            return
        yield {"choices": [{"delta": {"content": "Grounded final reply."}}]}


class _LookupToolRouter:
    async def execute(self, *_: object, **__: object) -> ToolResult:
        return ToolResult(output="lookup result", is_error=False)


@pytest.mark.asyncio
async def test_responses_api_tool_cycle_uses_canonical_projection() -> None:
    """Responses API must send canonical projection on every cycle.

    Verifies the controller never sends opaque continuation inputs
    (``cognis_responses_input_items``, ``previous_response_id``,
    ``responses_continuation_items``) and that tool results appear in the
    canonical projected transcript on the next cycle.
    """
    fake_llm = _ResponsesApiToolCycleLLM()
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="lookup",
                description="Lookup evidence",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
            handler=None,
        )
    )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(max_context_tokens=100_000),
        compaction_strategy=SimpleNamespace(),
        tool_router=_LookupToolRouter(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-responses-canonical",
            intaris_session_id="sess-responses-canonical",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-responses-canonical",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Use lookup before answering.",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=False,
        is_retry=False,
        workflow_state=None,
        step_run_id=None,
        executor_environment=build_local_executor_environment(
            executor_id="exec-responses-canonical",
            executor_type="in_process",
            source="test",
        ),
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=registry,
        executor_connection=SimpleNamespace(),
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Grounded final reply."
    assert len(fake_llm.calls) == 2

    # Continuation kwargs must never reach the provider on any cycle.
    forbidden_kwargs = (
        "cognis_responses_input_items",
        "cognis_responses_continuation_mode",
        "cognis_responses_capability_decision",
        "cognis_responses_fallback_reason",
        "previous_response_id",
    )
    for call in fake_llm.calls:
        for key in forbidden_kwargs:
            assert key not in call["kwargs"], f"{key} must not be sent to provider"

    # The model sees the tool result in canonical projection on the second cycle.
    second_messages = fake_llm.calls[1]["messages"]
    assert "lookup result" in str(second_messages)


@pytest.mark.asyncio
async def test_step_complete_reprompt_is_system_message() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="step-a", type="run", prompt="", allow_questions=False),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=WORKFLOW_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=True,
        workflow_state=SimpleNamespace(last_evaluation_feedback="Please revise."),
        step_run_id="sr-1",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )
    calls = await _run_reminder_capture(ctx)
    assert calls[1][-1]["role"] == "system"
    assert "ensure you have called write_deliverable" in str(calls[1][-1]["content"])


@pytest.mark.asyncio
async def test_direct_empty_response_reprompts_instead_of_failing_step() -> None:
    fake_llm = _EmptyThenTextDirectLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-empty",
            intaris_session_id="sess-empty",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-empty"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-empty",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered direct reply."
    assert output.content == "Recovered direct reply."
    assert len(fake_llm.calls) == 2
    assert fake_llm.calls[1][-1]["role"] == "system"
    assert "previous response was empty" in str(fake_llm.calls[1][-1]["content"])


@pytest.mark.asyncio
async def test_direct_repeated_empty_responses_fail_gracefully() -> None:
    fake_llm = _AlwaysEmptyDirectLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-empty-fail",
            intaris_session_id="sess-empty-fail",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-empty-fail"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-empty-fail",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.summary == "Step failed: empty assistant response"
    assert output.content == ""
    assert output.error == "Model returned an empty response without tool calls."
    assert len(fake_llm.calls) == 3


@pytest.mark.asyncio
async def test_direct_idle_timeout_retries_before_auto_continuation() -> None:
    fake_llm = _SilentThenRecoveredDirectLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-idle",
            intaris_session_id="sess-idle",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-idle"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-idle",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered after silence."
    assert output.content == "Recovered after silence."
    assert len(fake_llm.calls) == 2
    assert not any(
        "previous model stream failed" in str(message["content"]) for message in fake_llm.calls[1]
    )


@pytest.mark.asyncio
async def test_direct_idle_timeout_continues_after_retry_budget() -> None:
    fake_llm = _RepeatedIdleThenContinuationRecoveredDirectLLM(idle_failures=6)
    event_bus = _NoopEventBus()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=event_bus,
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-idle-exhausted",
            intaris_session_id="sess-idle-exhausted",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-idle-exhausted"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-idle-exhausted",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered after repeated idle."
    assert output.content == "Recovered after repeated idle."
    assert len(fake_llm.calls) == 7
    assert any(
        message["role"] == "system" and "previous model stream failed" in str(message["content"])
        for message in fake_llm.calls[6]
    )
    notices = [
        getattr(event, "data", {})
        for event in event_bus.events
        if getattr(event, "type", None) == EventType.SYSTEM_NOTICE
    ]
    recovery_notices = [
        notice
        for notice in notices
        if notice.get("kind") == "model_recovery" and notice.get("scope") == "continuation"
    ]
    assert len(recovery_notices) == 3
    assert [notice.get("attempt") for notice in recovery_notices] == [1, 2, 3]
    assert all(notice.get("max_attempts") == 3 for notice in recovery_notices)
    assert not any(notice.get("kind") == "model_retry" for notice in notices)
    assert all(
        notice.get("kind") == "model_recovery" and notice.get("scope") == "continuation"
        for notice in recovery_notices
    )


@pytest.mark.asyncio
async def test_direct_model_error_auto_continues_after_retry_budget() -> None:
    fake_llm = _ModelErrorThenRecoveredDirectLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-model-error",
            intaris_session_id="sess-model-error",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-model-error"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-model-error",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered after model error."
    assert output.content == "Recovered after model error."
    assert len(fake_llm.calls) == 3
    assert any(
        message["role"] == "system" and "previous model stream failed" in str(message["content"])
        for message in fake_llm.calls[2]
    )
    assert any(
        'artifact_read artifact_id="doc_1"' in str(message["content"])
        for message in fake_llm.calls[2]
    )


@pytest.mark.asyncio
async def test_native_image_input_error_strips_image_url_and_retries() -> None:
    fake_llm = _ImageInputErrorThenRecoveredDirectLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeNativeImageContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-image-input-error",
            intaris_session_id="sess-image-input-error",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-image-input-error"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-image-input-error",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered without native image."
    assert len(fake_llm.calls) == 2
    assert any(
        'artifact_read artifact_id="img_1"' in str(message["content"])
        for message in fake_llm.calls[1]
    )


@pytest.mark.asyncio
async def test_mid_stream_retry_keeps_notice_until_saved_state_continuation() -> None:
    fake_llm = _ModelErrorThenRecoveredDirectLLM()
    event_bus = _NoopEventBus()
    guardrails = _NoopGuardrails()
    persisted_events: list[SessionEvent] = []

    async def record_events(**kwargs: object) -> EventAppendResult:
        persisted_events.extend(kwargs.get("events", []))  # type: ignore[arg-type]
        return EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1)

    guardrails.record_events = record_events  # type: ignore[method-assign]
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=guardrails),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=event_bus,
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-model-retry-notice",
            intaris_session_id="sess-model-retry-notice",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-model-retry-notice"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-model-retry-notice",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    notices = [
        getattr(event, "data", {})
        for event in event_bus.events
        if getattr(event, "type", None) == EventType.SYSTEM_NOTICE
    ]
    assert not any(notice.get("kind") == "model_retry" for notice in notices)
    assert any(notice.get("kind") == "model_recovery" for notice in notices)
    persisted_notice_events = [
        event
        for event in persisted_events
        if event.type == "lifecycle" and event.data.get("event") == "system_notice"
    ]
    assert len(persisted_notice_events) == 1
    persisted_notice = persisted_notice_events[0].data
    assert persisted_notice["kind"] == "model_recovery"
    assert persisted_notice["scope"] == "continuation"
    assert persisted_notice["notice_id"].endswith(":model_recovery:continuation")
    assert persisted_notice["tool_results_saved"] is True


@pytest.mark.asyncio
async def test_direct_context_overflow_compacts_rotates_and_replays() -> None:
    fake_llm = _ContextOverflowThenTextLLM()
    session_manager = _NoopSessionManager()
    compaction_strategy = _SuccessfulCompactionStrategy()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=session_manager,
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=compaction_strategy,
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-overflow",
            intaris_session_id="sess-overflow",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-overflow"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-overflow",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered after compaction."
    assert fake_llm.calls == 2
    assert compaction_strategy.calls == ["provider_context_overflow"]
    assert len(session_manager.rotations) == 1
    assert ctx.session.session_id == "sess-rotated"
    assert ctx.runtime_info["provider_overflow_recoveries"] == 1


@pytest.mark.asyncio
async def test_stream_provider_context_overflow_compacts_without_mid_stream_retry() -> None:
    fake_llm = _StreamProviderContextOverflowThenTextLLM()
    session_manager = _NoopSessionManager()
    compaction_strategy = _SuccessfulCompactionStrategy()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=session_manager,
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=compaction_strategy,
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=3,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-provider-overflow",
            intaris_session_id="sess-provider-overflow",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-provider-overflow"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-provider-overflow",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered after compaction."
    assert fake_llm.calls == 2
    assert compaction_strategy.calls == ["provider_context_overflow"]
    assert len(session_manager.rotations) == 1
    assert ctx.runtime_info["provider_overflow_recoveries"] == 1


@pytest.mark.asyncio
async def test_stream_failure_chunk_context_overflow_compacts_without_mid_stream_retry() -> None:
    fake_llm = _StreamFailureChunkContextOverflowThenTextLLM()
    session_manager = _NoopSessionManager()
    compaction_strategy = _SuccessfulCompactionStrategy()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=session_manager,
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=compaction_strategy,
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=3,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-chunk-overflow",
            intaris_session_id="sess-chunk-overflow",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-chunk-overflow"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-chunk-overflow",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered after compaction."
    assert fake_llm.calls == 2
    assert compaction_strategy.calls == ["provider_context_overflow"]
    assert len(session_manager.rotations) == 1
    assert ctx.runtime_info["provider_overflow_recoveries"] == 1


@pytest.mark.asyncio
async def test_direct_token_callback_errors_are_not_retried_as_model_errors() -> None:
    fake_llm = _EmptyThenTextDirectLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-token-error",
            intaris_session_id="sess-token-error",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-token-error"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=CHAT_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-token-error",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    async def _on_token(_token: str) -> None:
        raise RuntimeError("websocket send failed")

    output = await agent_loop.run_step(ctx, on_token=_on_token)

    assert output is not None
    assert output.error == "RuntimeError: websocket send failed"
    assert len(fake_llm.calls) == 2


def test_get_incomplete_todos_treats_done_as_completed() -> None:
    ctx = SimpleNamespace(
        todos=[
            {"content": "legacy", "status": "done"},
            {"content": "current", "status": "completed"},
            {"content": "pending", "status": "pending"},
        ]
    )

    incomplete = AgentLoop._get_incomplete_todos(ctx)

    assert incomplete == [{"content": "pending", "status": "pending"}]


def test_todo_schema_uses_completed_status() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="step-a", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        todos=[],
        policy=WORKFLOW_POLICY,
        user_message="",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id="sr-1",
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    tools = loop._build_controller_tool_schemas(ctx)
    write_deliverable_tool = next(
        tool for tool in tools if tool["function"]["name"] == "write_deliverable"
    )
    todo_tool = next(tool for tool in tools if tool["function"]["name"] == "step_todo_write")
    statuses = todo_tool["function"]["parameters"]["properties"]["todos"]["items"]["properties"][
        "status"
    ]["enum"]

    assert statuses == ["pending", "in_progress", "completed", "cancelled"]
    # Schema now sourced from the registry (cognis/tools/builtin/workflow.py)
    # so description drift is impossible — just assert it mentions todos.
    assert "todos" in todo_tool["function"]["description"].lower()
    # Item-level required fields are enforced by the registry schema,
    # matching what the validator checks against.
    items = todo_tool["function"]["parameters"]["properties"]["todos"]["items"]
    assert items["required"] == ["content", "status"]
    assert "canonical workflow artifact" in write_deliverable_tool["function"]["description"]


def test_step_complete_metadata_array_schema_includes_items() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(
            name="plan",
            type="run",
            prompt="",
            metadata_contract=StepCompletionContract(
                fields=[
                    StepCompletionMetadataField(
                        name="source_strategy", type="array", required=False
                    ),
                    StepCompletionMetadataField(
                        name="scope_contract",
                        type="array",
                        required=True,
                        description="Array of objects. Scope items with acceptance evidence.",
                    ),
                    StepCompletionMetadataField(name="open_questions", type="array", required=True),
                ]
            ),
        ),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        step_run_id="sr-1",
    )

    tools = loop._build_controller_tool_schemas(ctx)
    step_complete_tool = next(tool for tool in tools if tool["function"]["name"] == "step_complete")
    metadata_properties = step_complete_tool["function"]["parameters"]["properties"]["metadata"][
        "properties"
    ]

    assert metadata_properties["source_strategy"]["items"] == {"type": "string"}
    assert metadata_properties["scope_contract"]["items"] == {"type": "object"}
    assert metadata_properties["open_questions"]["items"] == {"type": "string"}


def test_delegation_schema_exposes_write_deliverable_for_workflow_backed_child() -> None:
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt=""),
        session=SimpleNamespace(session_id="child", intaris_session_id="child"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=DELEGATION_POLICY,
        deliverable_step_run_id="sr-parent",
    )

    tools = loop._build_controller_tool_schemas(ctx)

    assert any(tool["function"]["name"] == "write_deliverable" for tool in tools)


@pytest.mark.asyncio
async def test_workflow_backed_delegation_can_write_parent_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_FinalAssistantContentLLM(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    captured: dict[str, object] = {}

    async def _write_deliverable(ctx: StepContext, **kwargs: object) -> Deliverable:
        captured["owner_step_run_id"] = agent_loop._deliverable_owner_step_run_id(ctx)
        captured.update(kwargs)
        deliverable = Deliverable(
            deliverable_id="dlv-child",
            step_run_id="sr-parent",
            version=1,
            content=str(kwargs["content"]),
            format="markdown",
            title="Delegated result",
            outputs={},
        )
        return agent_loop._cache_deliverable(ctx, deliverable)

    monkeypatch.setattr(agent_loop, "_write_step_deliverable", _write_deliverable)
    ctx = StepContext(
        step_definition=StepDefinition(
            name="delegation",
            type="run",
            prompt="Investigate the issue.",
            require_deliverable=False,
        ),
        session=SimpleNamespace(
            session_id="child-1",
            intaris_session_id="child-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=DELEGATION_POLICY,
        user_message="Investigate the issue.",
        deliverable_step_run_id="sr-parent",
        system_initiated=True,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.deliverable_id == "dlv-child"
    assert captured["owner_step_run_id"] == "sr-parent"
    assert captured["content"] == "Final clean briefing text."


def test_secondary_agent_delegation_uses_slim_policy_not_is_system() -> None:
    """User-managed secondary agents must receive SECONDARY_AGENT_DELEGATION_POLICY,
    not DELEGATION_POLICY.  The gate is agent_type=='secondary', not is_system."""
    # User-defined secondary agent (not is_system)
    user_secondary = AgentDefinition(
        agent_id="custom-reviewer",
        owner_email="user@example.com",
        name="My Reviewer",
        agent_type="secondary",
        is_system=False,
    )
    ctx_secondary = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt=""),
        session=SimpleNamespace(session_id="child", intaris_session_id="child"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=user_secondary,
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
    )
    # step_complete must NOT be required for secondary agent delegations
    assert not ctx_secondary.policy.require_step_complete
    # Memory must be skipped for secondary agent delegations
    assert ctx_secondary.policy.skip_memory

    # A shipped system agent is also secondary — same policy applies
    system_explore = AgentDefinition(
        agent_id="system:explore",
        owner_email="system@cognis.local",
        name="Explore",
        agent_type="secondary",
        is_system=True,
    )
    ctx_system = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt=""),
        session=SimpleNamespace(session_id="child2", intaris_session_id="child2"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=system_explore,
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
    )
    assert not ctx_system.policy.require_step_complete
    assert ctx_system.policy.skip_memory

    # A primary agent delegation keeps the full policy
    primary_agent = AgentDefinition(
        agent_id="riker",
        owner_email="user@example.com",
        name="Riker",
        agent_type="primary",
        is_system=False,
    )
    ctx_primary = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt=""),
        session=SimpleNamespace(session_id="child3", intaris_session_id="child3"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=primary_agent,
        policy=DELEGATION_POLICY,
    )
    assert ctx_primary.policy.require_step_complete
    assert not ctx_primary.policy.skip_memory


def test_secondary_agent_delegation_slim_prompt_has_minimal_completion_hint() -> None:
    """_build_step_prompt for a secondary-agent delegation emits the slim
    completion hint, not the heavy write_deliverable/step_complete block."""
    loop = object.__new__(AgentLoop)
    user_secondary = AgentDefinition(
        agent_id="custom-reviewer",
        owner_email="user@example.com",
        name="My Reviewer",
        agent_type="secondary",
        is_system=False,
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="delegation",
            type="run",
            prompt="Review the changes in this PR.",
            require_deliverable=False,
        ),
        session=SimpleNamespace(session_id="child", intaris_session_id="child"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=user_secondary,
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
        user_message="Review the changes in this PR.",
    )
    prompt = loop._build_step_prompt(ctx)
    # Must use the slim completion hint
    assert "write your findings as a final assistant message" in prompt
    # Must NOT contain the heavy deliverable contract
    assert "write_deliverable with the canonical user-facing artifact" not in prompt
    assert "Required Completion Actions" not in prompt


def test_select_delegation_result_falls_back_to_step_output_when_no_messages() -> None:
    """When no assistant messages exist, use StepOutput.content as fallback."""
    agent_loop = object.__new__(AgentLoop)
    agent_loop.providers = SimpleNamespace(
        guardrails=SimpleNamespace(
            read_events=lambda **_: SimpleNamespace(
                events=[], last_seq=0, has_more=False, missing_stream_fallback_used=False
            )
        )
    )  # type: ignore[attr-defined]
    child = SimpleNamespace(session_id="child-1", intaris_session_id="child-1")
    substantive = (
        "Investigated the queued chat handling code. "
        "Found that ui/src/lib/chat.ts:520 reconciles user messages by id, "
        "but the optimistic id from the form is not the same as the persisted id."
    )

    async def _run() -> object:
        return await agent_loop._select_delegation_result_content(
            child_session=child, step_output=StepOutput(summary="done", content=substantive)
        )

    result = asyncio.run(_run())
    assert result.content == substantive
    assert result.source == "step_output"


def test_select_delegation_result_walks_events_when_step_output_is_meta_complaint() -> None:
    """Aggregate child assistant messages chronologically so cleanup tails do not replace reports.

    This regresses the 'tool budget reached' bug where the parent received
    only the budget-exhaustion message, despite the substantive findings being
    in an earlier turn of the same sub-session."""
    agent_loop = object.__new__(AgentLoop)

    substantive = (
        "Investigation results: the duplicate-render bug lives in "
        "ui/src/routes/(app)/chat/[conversationId]/+page.svelte:1614 where "
        "queued messages are appended without dedup against the optimistic "
        "pending list. Recommend: switch to id-keyed merge."
    )
    meta_complaint = (
        "The investigation result was already delivered. I can't call tools "
        "anymore in this sub-session because the tool budget is exhausted, "
        "so I can't update the stale todo state."
    )

    fake_events = [
        {"seq": 10, "type": "assistant_thinking", "data": {"content": "thinking..."}},
        {"seq": 11, "type": "assistant_message", "data": {"content": substantive}},
        {"seq": 14, "type": "tool_call", "data": {"name": "read"}},
        {"seq": 15, "type": "tool_result", "data": {"is_error": False}},
        {"seq": 18, "type": "assistant_message", "data": {"content": meta_complaint}},
    ]

    class _FakeGuardrails:
        async def read_events(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                events=fake_events,
                last_seq=18,
                has_more=False,
                missing_stream_fallback_used=False,
            )

    agent_loop.providers = SimpleNamespace(guardrails=_FakeGuardrails())  # type: ignore[attr-defined]
    child = SimpleNamespace(session_id="child-1", intaris_session_id="child-1")

    async def _run() -> object:
        return await agent_loop._select_delegation_result_content(
            child_session=child, step_output=StepOutput(summary="done", content=meta_complaint)
        )

    result = asyncio.run(_run())
    assert result.source == "assistant_messages"
    assert substantive in result.content
    assert meta_complaint in result.content
    assert result.content.index(substantive) < result.content.index(meta_complaint)
    assert "--- Assistant message 1 ---" in result.content
    assert "--- Assistant message 2 ---" in result.content
    assert [anchor["anchor"] for anchor in result.anchors] == ["message:1", "message:2"]


def test_select_delegation_result_reads_paginated_child_events() -> None:
    agent_loop = object.__new__(AgentLoop)
    first_page_events = [
        {"seq": index, "type": "tool_call", "data": {"name": "read"}} for index in range(1, 501)
    ]
    report = "Full report from the second page must survive pagination."
    cleanup = "Todo cleanup finalization message."
    second_page_events = [
        {"seq": 501, "type": "assistant_message", "data": {"content": report}},
        {"seq": 502, "type": "assistant_message", "data": {"content": cleanup}},
    ]

    class _FakeGuardrails:
        def __init__(self) -> None:
            self.after_seqs: list[int] = []

        async def read_events(self, **kwargs: object) -> SimpleNamespace:
            after_seq = int(kwargs["after_seq"])
            self.after_seqs.append(after_seq)
            if after_seq == 0:
                return SimpleNamespace(
                    events=first_page_events,
                    last_seq=500,
                    has_more=True,
                    missing_stream_fallback_used=False,
                )
            return SimpleNamespace(
                events=second_page_events,
                last_seq=502,
                has_more=False,
                missing_stream_fallback_used=False,
            )

    guardrails = _FakeGuardrails()
    agent_loop.providers = SimpleNamespace(guardrails=guardrails)  # type: ignore[attr-defined]
    child = SimpleNamespace(session_id="child-1", intaris_session_id="child-1")

    async def _run() -> object:
        return await agent_loop._select_delegation_result_content(
            child_session=child, step_output=StepOutput(summary="done", content=cleanup)
        )

    result = asyncio.run(_run())
    assert guardrails.after_seqs == [0, 500]
    assert report in result.content
    assert cleanup in result.content
    assert result.content.index(report) < result.content.index(cleanup)


def test_delegation_result_anchors_match_truncated_content() -> None:
    first = "A" * (_DELEGATION_RESULT_MAX_CHARS + 1_000)
    second = "This second message is omitted by truncation."

    result = _build_delegation_message_result([first, second])

    assert result.truncated
    assert second not in result.content
    assert "message:1" in result.content
    assert "message:2" not in result.content
    assert [anchor["anchor"] for anchor in result.anchors] == ["message:1"]
    sections = _result_sections_from_content(result.content, result.anchors)
    assert [section["anchor"] for section in sections] == ["message:1"]
    assert sections[0]["content"].startswith("[[message:1]]")


def test_delegation_result_adds_markdown_heading_anchors() -> None:
    result = _build_delegation_message_result(
        ["### Summary\nDone\n\n### Must Fix\n- Repair the anchor path."]
    )

    assert [anchor["anchor"] for anchor in result.anchors] == [
        "message:1",
        "heading:summary",
        "heading:must-fix",
    ]
    assert result.anchors[1]["kind"] == "markdown_heading"
    sections = _result_sections_from_content(result.content, result.anchors)
    must_fix = next(section for section in sections if section["anchor"] == "heading:must-fix")
    assert "### Must Fix" in must_fix["content"]
    assert "Repair the anchor path" in must_fix["content"]


def test_select_delegation_result_aggregates_all_meta_messages() -> None:
    """Even meta-only messages are preserved in chronological order."""
    agent_loop = object.__new__(AgentLoop)

    short_meta = "Tool budget reached."
    longer_meta = (
        "The tool budget for this sub-session is exhausted. I am unable to "
        "call any further tools. No substantive result was produced."
    )

    fake_events = [
        {"seq": 5, "type": "assistant_message", "data": {"content": short_meta}},
        {"seq": 9, "type": "assistant_message", "data": {"content": longer_meta}},
    ]

    class _FakeGuardrails:
        async def read_events(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                events=fake_events,
                last_seq=9,
                has_more=False,
                missing_stream_fallback_used=False,
            )

    agent_loop.providers = SimpleNamespace(guardrails=_FakeGuardrails())  # type: ignore[attr-defined]
    child = SimpleNamespace(session_id="child-1", intaris_session_id="child-1")

    async def _run() -> object:
        return await agent_loop._select_delegation_result_content(
            child_session=child, step_output=StepOutput(summary="done", content=short_meta)
        )

    result = asyncio.run(_run())
    assert short_meta in result.content
    assert longer_meta in result.content
    assert result.content.index(short_meta) < result.content.index(longer_meta)


def test_select_delegation_result_handles_read_events_failure() -> None:
    """If reading events fails (Intaris down, etc.), fall back to whatever
    StepOutput.content is so the parent gets something rather than nothing."""
    agent_loop = object.__new__(AgentLoop)

    class _FailingGuardrails:
        async def read_events(self, **_: object) -> SimpleNamespace:
            raise RuntimeError("Intaris unavailable")

    agent_loop.providers = SimpleNamespace(guardrails=_FailingGuardrails())  # type: ignore[attr-defined]
    child = SimpleNamespace(session_id="child-1", intaris_session_id="child-1")
    fallback = "I cannot call tools."  # short meta-complaint

    async def _run() -> object:
        return await agent_loop._select_delegation_result_content(
            child_session=child, step_output=StepOutput(summary="done", content=fallback)
        )

    result = asyncio.run(_run())
    assert result.content == fallback
    assert result.source == "step_output"


def test_select_delegation_result_prefers_deliverable_content() -> None:
    agent_loop = object.__new__(AgentLoop)

    class _SessionFactory:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    agent_loop.session_manager = SimpleNamespace(  # type: ignore[attr-defined]
        session_factory=lambda: _SessionFactory()
    )
    agent_loop.providers = SimpleNamespace(  # type: ignore[attr-defined]
        guardrails=SimpleNamespace(
            read_events=lambda **_: SimpleNamespace(
                events=[{"type": "assistant_message", "data": {"content": "assistant chatter"}}],
                last_seq=1,
                has_more=False,
                missing_stream_fallback_used=False,
            )
        )
    )
    child = SimpleNamespace(session_id="child-1", intaris_session_id="child-1")

    async def _get_deliverable(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(content="Canonical deliverable body", status="approved")

    async def _run(monkeypatch: pytest.MonkeyPatch) -> object:
        monkeypatch.setattr("cognis.core.agent_loop.get_deliverable", _get_deliverable)
        return await agent_loop._select_delegation_result_content(
            child_session=child,
            step_output=StepOutput(
                summary="summary",
                content="assistant chatter",
                deliverable_id="dlv_real",
                deliverable_title="Report",
            ),
        )

    monkeypatch = pytest.MonkeyPatch()
    try:
        result = asyncio.run(_run(monkeypatch))
    finally:
        monkeypatch.undo()
    assert result.content == "Canonical deliverable body"
    assert result.source == "deliverable"
    assert [anchor["anchor"] for anchor in result.anchors] == ["deliverable"]


def test_select_delegation_result_preserves_deliverable_and_markdown_anchors() -> None:
    agent_loop = object.__new__(AgentLoop)

    class _SessionFactory:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    agent_loop.session_manager = SimpleNamespace(  # type: ignore[attr-defined]
        session_factory=lambda: _SessionFactory()
    )
    child = SimpleNamespace(session_id="child-1", intaris_session_id="child-1")

    async def _get_deliverable(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            content="## Summary\nCanonical deliverable body\n\n## Verdict\nApproved",
            status="approved",
        )

    async def _run(monkeypatch: pytest.MonkeyPatch) -> object:
        monkeypatch.setattr("cognis.core.agent_loop.get_deliverable", _get_deliverable)
        return await agent_loop._select_delegation_result_content(
            child_session=child,
            step_output=StepOutput(
                summary="summary",
                content="fallback",
                deliverable_id="dlv_real",
                deliverable_title="Report",
            ),
        )

    monkeypatch = pytest.MonkeyPatch()
    try:
        result = asyncio.run(_run(monkeypatch))
    finally:
        monkeypatch.undo()
    assert result.source == "deliverable"
    assert [anchor["anchor"] for anchor in result.anchors] == [
        "deliverable",
        "heading:summary",
        "heading:verdict",
    ]


def test_truncated_delegation_result_only_advertises_retained_markdown_headings() -> None:
    content = "## Visible\n" + ("x" * 130) + "\n## Hidden\n" + ("y" * 200)

    truncated, anchors, was_truncated, _original_length = (
        AgentLoop._truncate_delegation_result_content(content, [], max_chars=180)
    )

    assert was_truncated
    assert "## Visible" in truncated
    assert "## Hidden" not in truncated
    assert [anchor["anchor"] for anchor in anchors] == ["heading:visible"]


def test_get_subsession_returns_durable_result_content(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_loop = object.__new__(AgentLoop)

    class _SessionFactory:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    agent_loop.session_manager = SimpleNamespace(session_factory=lambda: _SessionFactory())  # type: ignore[attr-defined]
    ctx = SimpleNamespace(session=SimpleNamespace(session_id="parent-1"))
    row = SimpleNamespace(
        session_id="child-1",
        parent_session_id="parent-1",
        agent_id="system:explore",
        status="completed",
        delegation_task="Investigate",
        result_summary="Done",
        result_content="[assistant_message:1]\nFull report",
        started_at=None,
        completed_at=None,
    )

    async def _fake_get_session_row(_db: object, session_id: str) -> object:
        assert session_id == "child-1"
        return row

    monkeypatch.setattr("cognis.store.queries.get_session_row", _fake_get_session_row)

    async def _run():
        return await agent_loop._handle_subsession_management(
            ToolCall(
                call_id="call-get",
                name="get_subsession",
                arguments={"session_id": "child-1"},
            ),
            ctx=ctx,  # type: ignore[arg-type]
        )

    tool_result = asyncio.run(_run())
    payload = json.loads(tool_result.output)
    assert payload["result_content"] == "[assistant_message:1]\nFull report"
    assert payload["result_anchors"][0]["anchor"] == "assistant_message:1"
    assert payload["result_sections"][0]["content"].startswith("[assistant_message:1]")


def test_looks_like_meta_complaint_detects_known_patterns() -> None:
    """The meta-complaint heuristic flags short messages that contain the
    well-known phrases the model uses when the controller cuts it off."""
    assert AgentLoop._looks_like_meta_complaint(
        "I can't call tools anymore in this sub-session because the tool budget "
        "is exhausted, so I can't update the stale todo state."
    )
    assert AgentLoop._looks_like_meta_complaint("Tool budget reached. Tools are disabled.")
    assert AgentLoop._looks_like_meta_complaint(
        "Already provided the final findings above. No additional user-facing information remains; "
        "the remaining todo state is stale."
    )
    assert AgentLoop._looks_like_meta_complaint("")
    # Substantive technical text is NOT a meta-complaint
    assert not AgentLoop._looks_like_meta_complaint(
        "The bug is in ui/src/lib/chat.ts:520 where the optimistic message id "
        "is not the same as the persisted id, causing duplicate renders. "
        "Recommend switching to id-keyed merge."
    )
    # Long messages are not flagged even if they contain a phrase, because
    # they probably embed the phrase in a substantive context.
    long_text = "The user reported that 'tool budget' messages are unhelpful. " * 30
    assert not AgentLoop._looks_like_meta_complaint(long_text)


@pytest.mark.asyncio
async def test_secondary_delegation_prefers_deliverable_when_limit_forces_tail_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(
            llm=_DelegationDeliverableThenLimitLLM(),
            guardrails=_NoopGuardrails(),
        ),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    async def _write_deliverable(ctx: StepContext, **kwargs: object) -> Deliverable:
        deliverable = Deliverable(
            deliverable_id="dlv-limit",
            step_run_id="sr-parent",
            version=1,
            content=str(kwargs["content"]),
            format="markdown",
            title="Delegated deliverable",
            outputs=dict(kwargs.get("outputs") or {}),
        )
        return agent_loop._cache_deliverable(ctx, deliverable)

    monkeypatch.setattr(agent_loop, "_write_step_deliverable", _write_deliverable)

    ctx = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt="Investigate."),
        session=SimpleNamespace(
            session_id="child-limit",
            intaris_session_id="child-limit",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="system:explore",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            execution={"steps": 2},
            is_system=True,
        ),
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
        user_message="Investigate.",
        deliverable_step_run_id="sr-parent",
        system_initiated=True,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.deliverable_id == "dlv-limit"
    assert output.content == "Deliverable output from delegated run."
    assert output.outputs == {"source": "deliverable"}


@pytest.mark.asyncio
async def test_delegation_prompt_uses_user_message_role() -> None:
    """Delegated task text must enter the child prompt as a user message.

    The child session itself is system-initiated for lifecycle/audit purposes,
    but the delegated task is the active task input. Passing it as a system
    message makes language and instruction precedence ambiguous for secondary
    agents.
    """

    context_assembler = _FakeContextAssembler()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_SingleTextLLM(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=context_assembler,
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt="Investigate."),
        session=SimpleNamespace(
            session_id="child-role",
            intaris_session_id="child-role",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="system:explore",
            parent_session_id="parent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            execution={"steps": 1},
            is_system=True,
        ),
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
        user_message="Investigate.",
        system_initiated=True,
    )
    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert context_assembler.calls[-1]["prompt_context"] is PromptContext.DELEGATION
    assert context_assembler.calls[-1]["user_message_role"] == "user"


@pytest.mark.asyncio
async def test_secondary_delegation_steps_count_llm_turns_not_tool_calls() -> None:
    fake_llm = _DelegationMultiToolThenSummaryLLM()

    class _ToolRouter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, tool_call: ToolCall, *args: object) -> ToolResult:
            del args
            self.calls.append(tool_call.call_id)
            return ToolResult(output="ok", is_error=False)

    tool_router = _ToolRouter()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=tool_router,
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt="Investigate."),
        session=SimpleNamespace(
            session_id="child-steps",
            intaris_session_id="child-steps",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="system:explore",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            execution={"steps": 2},
            is_system=True,
        ),
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
        user_message="Investigate.",
        system_initiated=True,
    )
    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Final delegated summary."
    assert fake_llm.calls == 2
    assert fake_llm.tools_by_call[1] == []


@pytest.mark.asyncio
async def test_malformed_tool_arguments_do_not_drop_valid_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_llm = _MalformedSiblingToolCallLLM()

    class _ToolRouter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, tool_call: ToolCall, *args: object) -> ToolResult:
            del args
            self.calls.append(tool_call.call_id)
            return ToolResult(output=f"ok:{tool_call.call_id}", is_error=False)

    tool_router = _ToolRouter()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=tool_router,
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    async def _not_stale(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(agent_loop, "_stale_session_step_output", _not_stale)
    ctx = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt="Investigate."),
        session=SimpleNamespace(
            session_id="malformed-sibling",
            intaris_session_id="malformed-sibling",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="system:explore",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            execution={"steps": 2},
            is_system=True,
        ),
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
        user_message="Investigate.",
        system_initiated=True,
    )
    tool_results: list[tuple[str, str, bool]] = []

    async def _capture_tool_result(
        call_id: str,
        name: str,
        output: str,
        is_error: bool,
        *_: object,
    ) -> None:
        tool_results.append((call_id, name, is_error))

    output = await agent_loop.run_step(ctx, on_tool_result=_capture_tool_result)

    assert output is not None
    assert output.content == "done"
    assert tool_results == [
        ("call_bad", "step_todo_write", True),
        ("call_valid_1", "step_todo_write", False),
        ("call_valid_2", "step_todo_write", False),
    ]
    assert fake_llm.calls == 2
    second_prompt = fake_llm.messages_by_call[1]
    assistant_with_calls = [
        message
        for message in second_prompt
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert len(assistant_with_calls) == 1
    assert [call["id"] for call in assistant_with_calls[0]["tool_calls"]] == [
        "call_valid_1",
        "call_bad",
        "call_valid_2",
    ]
    tool_messages = [message for message in second_prompt if message.get("role") == "tool"]
    assert {message["tool_call_id"] for message in tool_messages} == {
        "call_valid_1",
        "call_bad",
        "call_valid_2",
    }
    malformed_result = next(
        message for message in tool_messages if message["tool_call_id"] == "call_bad"
    )
    assert '"reason":"invalid_json"' in str(malformed_result["content"])


@pytest.mark.asyncio
async def test_secondary_delegation_max_steps_cancels_open_todos() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(
            llm=_DelegationOpenTodosThenMaxStepsLLM(),
            guardrails=_NoopGuardrails(),
        ),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="delegation", type="run", prompt="Investigate."),
        session=SimpleNamespace(
            session_id="child-todos",
            intaris_session_id="child-todos",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="system:explore",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(
            agent_id="system:explore",
            owner_email="system",
            name="Explore",
            agent_type="secondary",
            execution={"steps": 2},
            is_system=True,
        ),
        policy=SECONDARY_AGENT_DELEGATION_POLICY,
        user_message="Investigate.",
        system_initiated=True,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Maximum steps reached summary."
    assert [todo["status"] for todo in ctx.todos] == ["cancelled", "cancelled"]


@pytest.mark.asyncio
async def test_delegation_progress_callback_tolerates_variadic_on_tool_result_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delegation progress callback inside _handle_delegate must accept the
    canonical on_tool_result signature, which can have anywhere from 5 to 8
    positional args plus optional kwargs.  Regression for a runtime crash:
    'takes 6 positional arguments but 8 were given'."""
    from types import SimpleNamespace as NS

    # Capture the on_tool_result callback that _handle_delegate passes into
    # _run_child_session.
    captured_callback: list[Any] = []

    async def _fake_run_child_session(*args: Any, **kwargs: Any) -> StepOutput:
        cb = kwargs.get("on_tool_result")
        captured_callback.append(cb)
        return StepOutput(summary="ok", content="result")

    async def _fake_handle_delegate_tool_call(*args: Any, **kwargs: Any):
        return ToolResult(output=json.dumps({"status": "started"})), NS(
            session_id="child-1",
            agent_id="system:explore",
        )

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    monkeypatch.setattr(
        "cognis.core.agent_loop.handle_delegate_tool_call",
        _fake_handle_delegate_tool_call,
    )
    monkeypatch.setattr(agent_loop, "_run_child_session", _fake_run_child_session)

    # Avoid network/persistence for the started event recording
    async def _noop_record(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(agent_loop, "_record_events_strict", _noop_record)

    primary_agent = AgentDefinition(
        agent_id="riker",
        owner_email="user@example.com",
        name="Riker",
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="riker",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=primary_agent,
        policy=CHAT_POLICY,
        orchestration_mode=OrchestrationMode.FULL,
    )

    result = await agent_loop._handle_delegate(
        ToolCall(
            call_id="call-1",
            name="delegate",
            arguments={"task": "Investigate.", "agent_id": "system:explore", "wait": True},
        ),
        ctx=ctx,
        events_to_record=[],
    )
    payload = json.loads(result.output)
    assert payload["result"] == "result"
    assert "result_content" not in payload
    assert "result_sections" not in payload

    assert captured_callback, "_run_child_session was not called with on_tool_result"
    cb = captured_callback[0]
    # Must accept the longest call shape (8 args: call_id, tool_name, output,
    # is_error, duration_ms, eval_meta, attachments, file_diffs)
    await cb("c1", "read", "<out>", False, 12, {"decision": "allow"}, None, None)
    # And the shorter shape (6 args)
    await cb("c2", "grep", "<out>", False, None, None)
    # And the medium shape (7 args)
    await cb("c3", "bash", "<out>", False, 5, {"decision": "allow"}, None)


def test_step_request_questions_schema_only_exposed_for_question_enabled_steps() -> None:
    loop = object.__new__(AgentLoop)
    base = dict(
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
    )
    question_ctx = StepContext(
        **base,
        step_definition=StepDefinition(name="plan", type="run", prompt="", allow_questions=True),
        interaction_mode="step_requests",
    )
    autonomous_ctx = StepContext(
        **base,
        step_definition=StepDefinition(name="plan", type="run", prompt="", allow_questions=True),
        interaction_mode="none",
    )

    assert any(
        tool["function"]["name"] == "step_request_questions"
        for tool in loop._build_controller_tool_schemas(question_ctx)
    )
    assert not any(
        tool["function"]["name"] == "step_request_questions"
        for tool in loop._build_controller_tool_schemas(autonomous_ctx)
    )


@pytest.mark.asyncio
async def test_handle_delegate_returns_deliverable_metadata_and_clears_parent_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    primary_agent = AgentDefinition(
        agent_id="primary-1",
        owner_email="user@example.com",
        name="Primary",
        execution={"executor_id": "user-exec"},
    )
    step_agent = AgentDefinition(
        agent_id="system:research",
        owner_email="system@cognis.local",
        name="Research",
        is_system=True,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="implement", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="intaris-1",
            user_email="user@example.com",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=step_agent,
        executor_agent=primary_agent,
        policy=WORKFLOW_POLICY,
        orchestration_mode=OrchestrationMode.DELEGATE_SYNC_ONLY,
        step_run_id="sr-parent",
        current_deliverable_id="dlv-stale",
        current_deliverable_version=1,
        current_deliverable_content="stale",
        current_deliverable_format="markdown",
        current_deliverable_title="Old",
        current_deliverable_outputs={"old": True},
        current_deliverable_status="approved",
    )

    captured: dict[str, object] = {}

    async def _fake_handle_delegate_tool_call(*args: object, **kwargs: object):
        return ToolResult(output=json.dumps({"status": "started"})), SimpleNamespace(
            session_id="child-1",
            agent_id="agent-1",
        )

    async def _fake_run_child_session(**kwargs: object) -> StepOutput:
        captured.update(kwargs)
        return StepOutput(
            summary="done",
            content="Final delegated artifact",
            outputs={"files": ["a.py"]},
            deliverable_id="dlv-new",
            deliverable_version=2,
            deliverable_format="markdown",
            deliverable_title="Child result",
        )

    monkeypatch.setattr(
        "cognis.core.agent_loop.handle_delegate_tool_call",
        _fake_handle_delegate_tool_call,
    )
    monkeypatch.setattr(agent_loop, "_run_child_session", _fake_run_child_session)

    result = await agent_loop._handle_delegate(
        ToolCall(call_id="call-1", name="delegate", arguments={"task": "Investigate"}),
        ctx=ctx,
        events_to_record=[],
    )

    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert payload["deliverable_written"] is True
    assert payload["deliverable_id"] == "dlv-new"
    assert payload["deliverable_version"] == 2
    assert payload["deliverable_format"] == "markdown"
    assert payload["deliverable_title"] == "Child result"
    assert captured["agent"] is primary_agent
    assert captured["deliverable_step_run_id"] == "sr-parent"
    assert ctx.current_deliverable_id is None
    assert ctx.current_deliverable_content is None


@pytest.mark.asyncio
async def test_agent_loop_passes_routing_reminder_for_eligible_chat_turn() -> None:
    assembler = _FakeContextAssembler()
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Implement refresh token support.",
        user_attachments=[],
        system_initiated=False,
    )

    await _run_with_assembler(ctx, assembler)

    assert assembler.calls[0]["routing_reminder"] is not None
    assert "background task" in str(assembler.calls[0]["routing_reminder"])


@pytest.mark.asyncio
async def test_agent_loop_skips_routing_reminder_for_system_initiated_and_workflow_turns() -> None:
    system_assembler = _FakeContextAssembler()
    system_ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Implement refresh token support.",
        user_attachments=[],
        system_initiated=True,
    )
    await _run_with_assembler(system_ctx, system_assembler)
    assert system_assembler.calls[0]["routing_reminder"] is None

    workflow_assembler = _FakeContextAssembler()
    workflow_ctx = StepContext(
        step_definition=StepDefinition(
            name="implement", type="run", prompt="", require_deliverable=False
        ),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="Implement refresh token support.",
        user_attachments=[],
        system_initiated=False,
    )
    await _run_with_assembler(workflow_ctx, workflow_assembler)
    assert workflow_assembler.calls[0]["routing_reminder"] is None


@pytest.mark.asyncio
async def test_agent_loop_skips_routing_reminder_for_secondary_policy_turns() -> None:
    assembler = _FakeContextAssembler()
    ctx = StepContext(
        step_definition=StepDefinition(
            name="secondary", type="run", prompt="", require_deliverable=False
        ),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=SECONDARY_POLICY,
        user_message="Implement refresh token support.",
        user_attachments=[],
        system_initiated=False,
    )

    await _run_with_assembler(ctx, assembler)

    assert assembler.calls[0]["routing_reminder"] is None


@pytest.mark.asyncio
async def test_user_message_is_persisted_before_reasoning_and_tool_execution() -> None:
    order: list[str] = []

    class _Guardrails:
        async def record_events(self, **kwargs: object) -> EventAppendResult:
            events = kwargs["events"]
            order.extend(f"record:{event.type}" for event in events)
            return EventAppendResult(ok=True, count=len(events), first_seq=1, last_seq=len(events))

        async def report_reasoning(self, **_: object) -> ReasoningReportResult:
            order.append("reasoning")
            return ReasoningReportResult(
                ok=True, intention="fresh", updated_at="2026-04-12T00:00:00+00:00"
            )

        async def health(self) -> SimpleNamespace:
            return SimpleNamespace(status="healthy")

    class _LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def get_model_info(self, model: str | None) -> SimpleNamespace:
            del model
            return _test_model_info()

        def count_tokens(self, text: str, model: str | None = None) -> int:
            del model
            return len(text)

        async def stream_generate(self, messages: list[dict[str, object]], **_: object):
            del messages
            self.calls += 1
            if self.calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {
                                            "name": "bash",
                                            "arguments": '{"command":"pwd"}',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
                yield {"choices": [{"finish_reason": "tool_calls", "delta": {}}]}
                return
            yield {"choices": [{"delta": {"content": "done"}}]}

    class _ToolRouter:
        async def execute(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            order.append("execute")
            return SimpleNamespace(
                output="ok",
                is_error=False,
                duration_ms=1,
                metadata={},
                attachments=[],
            )

    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="run something",
        user_attachments=[],
        system_initiated=False,
        tool_registry=ToolRegistry(),
    )
    ctx.tool_registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="bash",
                description="Run a shell command",
                parameters={},
                source=ToolSource(type="executor"),
            ),
            handler=None,
        )
    )

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_LLM(), guardrails=_Guardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=_ToolRouter(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert order[0] == "record:user_message"
    assert order[1] == "reasoning"
    assert "record:system_message" in order
    assert order.index("record:tool_call") < order.index("record:tool_result")
    assert "record:tool_result" in order
    assert "record:assistant_message" in order


@pytest.mark.asyncio
async def test_agent_loop_retries_with_cached_openai_tool_search_fallback() -> None:
    class _FallbackLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.native_tool_search_broken = False
            self.tool_sets: list[list[str]] = []

        async def resolve_model_target(
            self,
            *,
            explicit_model: str | None,
            task_type: str,
            explicit_provider_id: str | None = None,
        ) -> tuple[str, str]:
            del explicit_model, task_type, explicit_provider_id
            return ("gpt-5.4", "proxy")

        async def get_model_info(
            self, model: str | None, provider_id: str | None = None
        ) -> SimpleNamespace:
            del model, provider_id
            return SimpleNamespace(
                max_tools=128,
                supports_parallel_tool_calls=True,
                supports_tool_choice=True,
                supports_cache_control=False,
                supports_defer_loading=False,
                supports_openai_allowed_tools=True,
                supports_openai_namespace_tools=True,
                supports_tool_search=True,
                supports_responses_api=True,
                provider="test",
            )

        def apply_tool_exposure_runtime_fallbacks(
            self,
            model_info: SimpleNamespace,
            *,
            provider_id: str | None,
            model_id: str,
        ) -> SimpleNamespace:
            del provider_id, model_id
            if not self.native_tool_search_broken:
                return model_info
            adjusted = SimpleNamespace(**model_info.__dict__)
            adjusted.supports_openai_allowed_tools = False
            adjusted.supports_openai_namespace_tools = False
            return adjusted

        def count_tokens(self, text: str, model: str | None = None) -> int:
            del model
            return len(text)

        async def stream_generate(self, messages: list[dict[str, object]], **kwargs: object):
            del messages
            self.calls += 1
            tool_names: list[str] = []
            for schema in kwargs.get("tools", []):
                if not isinstance(schema, dict):
                    continue
                if schema.get("type") == "namespace":
                    tool_names.append(str(schema.get("name") or "namespace"))
                elif schema.get("type") == "tool_search":
                    tool_names.append("tool_search")
                else:
                    function = schema.get("function")
                    if isinstance(function, dict) and isinstance(function.get("name"), str):
                        tool_names.append(function["name"])
            self.tool_sets.append(tool_names)
            if self.calls == 1:
                self.native_tool_search_broken = True
                raise OpenAIToolSearchFallbackRequired(
                    provider_id="proxy",
                    model_id="gpt-5.4",
                    reason="tool_choice_tools_unknown",
                )
            yield {"choices": [{"delta": {"content": "done"}}]}

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="read",
                description="Read",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                category="filesystem",
                read_only=True,
            ),
            handler=None,
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="mcp_github__search_issues",
                description="Search issues",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(
                    type="intaris_mcp",
                    server_name="github",
                    raw_tool_name="search/issues",
                ),
                category="mcp",
            ),
            handler=None,
        )
    )
    fake_llm = _FallbackLLM()
    session_cache = _NoopSessionCache()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=session_cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="direct",
            type="run",
            prompt="",
            step_profile_id="system:direct-default",
        ),
        session=SimpleNamespace(
            session_id="sess-fallback",
            conversation_id="conv-fallback",
            intaris_session_id="sess-fallback",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-fallback",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="Inspect the repo",
        tool_registry=registry,
        system_initiated=False,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "done"
    assert fake_llm.calls == 2
    assert "search_tools" in fake_llm.tool_sets[0]
    assert all(name != "tool_search" for name in fake_llm.tool_sets[0])
    assert "search_tools" in fake_llm.tool_sets[1]
    assert all(name != "tool_search" for name in fake_llm.tool_sets[1])
    assert session_cache.tool_runtime_info is not None
    assert session_cache.tool_runtime_info["resolved_model"] == "gpt-5.4"
    assert session_cache.tool_runtime_info["resolved_provider_id"] == "proxy"
    assert "reasoning_effort" in session_cache.tool_runtime_info
    assert session_cache.tool_runtime_info["strategy"] in {
        "generic_search_tools",
        "openai_responses_controller_search_fallback",
    }


@pytest.mark.asyncio
async def test_search_tools_discovery_is_promoted_on_next_user_turn() -> None:
    class _DiscoveryLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.tool_sets: list[list[str]] = []

        async def get_model_info(self, model: str | None) -> SimpleNamespace:
            del model
            return _test_model_info()

        def count_tokens(self, text: str, model: str | None = None) -> int:
            del model
            return len(text)

        async def stream_generate(self, messages: list[dict[str, object]], **kwargs: object):
            del messages
            self.calls += 1
            tool_names: list[str] = []
            for schema in kwargs.get("tools", []):
                if not isinstance(schema, dict):
                    continue
                function = schema.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    tool_names.append(function["name"])
            self.tool_sets.append(tool_names)
            if self.calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_search",
                                        "function": {
                                            "name": "search_tools",
                                            "arguments": json.dumps(
                                                {
                                                    "query": "Google Calendar events",
                                                    "category": "mcp",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
                return
            yield {"choices": [{"delta": {"content": f"done {self.calls}"}}]}

    get_events = ToolDefinition(
        name="mcp_googleworkspace__get_events",
        description="Get Google Calendar events from Google Workspace.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            type="intaris_mcp",
            server_name="googleworkspace",
            raw_tool_name="get_events",
        ),
        category="mcp",
        profile_group="office",
        read_only=True,
    )
    rohlik_orders = ToolDefinition(
        name="mcp_rohlik__fetch_orders",
        description="Retrieve Rohlik grocery shopping orders.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="rohlik", raw_tool_name="fetch_orders"),
        category="mcp",
        profile_group="web",
        capabilities=["read"],
        classification_source="declared",
        classification_confidence=1.0,
        read_only=True,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=get_events, handler=None))
    registry.register(RegisteredTool(definition=rohlik_orders, handler=None))
    fake_llm = _DiscoveryLLM()
    session_cache = SessionCache(_NoopGuardrails(), max_entries=10)
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=session_cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    session = SimpleNamespace(
        session_id="sess-discovery",
        conversation_id="conv-discovery",
        intaris_session_id="sess-discovery",
        mnemory_session_id=None,
        user_email="user@example.com",
        agent_id="agent-1",
    )
    conversation = SimpleNamespace(
        conversation_id="conv-discovery",
        context=SimpleNamespace(type="web", ref=None, platform_data={}),
    )
    agent = AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent")

    first_output = await agent_loop.run_step(
        StepContext(
            step_definition=StepDefinition(
                name="direct",
                type="run",
                prompt="",
                step_profile_id="system:direct-default",
            ),
            session=session,
            conversation=conversation,
            agent=agent,
            policy=CHAT_POLICY,
            user_message="Create a calendar event",
            tool_registry=registry,
            system_initiated=False,
        )
    )

    assert first_output is not None
    assert "mcp_rohlik__fetch_orders" in fake_llm.tool_sets[0]
    assert "mcp_googleworkspace__get_events" not in fake_llm.tool_sets[0]
    assert session_cache.get_discovered_tool_ids(session.session_id) == {stable_tool_id(get_events)}

    second_output = await agent_loop.run_step(
        StepContext(
            step_definition=StepDefinition(
                name="direct",
                type="run",
                prompt="",
                step_profile_id="system:direct-default",
            ),
            session=session,
            conversation=conversation,
            agent=agent,
            policy=CHAT_POLICY,
            user_message="Extend that event by 30 minutes",
            tool_registry=registry,
            system_initiated=False,
        )
    )

    assert second_output is not None
    assert "mcp_googleworkspace__get_events" in fake_llm.tool_sets[2]


@pytest.mark.asyncio
async def test_cached_discovered_tool_is_revalidated_against_permissions() -> None:
    class _CaptureLLM:
        def __init__(self) -> None:
            self.tool_sets: list[list[str]] = []

        async def get_model_info(self, model: str | None) -> SimpleNamespace:
            del model
            return _test_model_info()

        def count_tokens(self, text: str, model: str | None = None) -> int:
            del model
            return len(text)

        async def stream_generate(self, messages: list[dict[str, object]], **kwargs: object):
            del messages
            self.tool_sets.append(
                [
                    function["name"]
                    for schema in kwargs.get("tools", [])
                    if isinstance(schema, dict)
                    and isinstance((function := schema.get("function")), dict)
                    and isinstance(function.get("name"), str)
                ]
            )
            yield {"choices": [{"delta": {"content": "done"}}]}

    get_events = ToolDefinition(
        name="mcp_googleworkspace__get_events",
        description="Get Google Calendar events from Google Workspace.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            type="intaris_mcp",
            server_name="googleworkspace",
            raw_tool_name="get_events",
        ),
        category="mcp",
        profile_group="office",
        read_only=True,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=get_events, handler=None))
    session_cache = SessionCache(_NoopGuardrails(), max_entries=10)
    session = SimpleNamespace(
        session_id="sess-discovery-denied",
        conversation_id="conv-discovery-denied",
        intaris_session_id="sess-discovery-denied",
        mnemory_session_id=None,
        user_email="user@example.com",
        agent_id="agent-1",
    )
    await session_cache.append_recorded_events(
        session,
        [
            SessionEvent(
                type="lifecycle",
                data={
                    "event": "tool_discovery",
                    "handles": [
                        {
                            "tool_id": stable_tool_id(get_events),
                            "name": get_events.name,
                            "callable_name": get_events.name,
                            "scope": "session",
                        }
                    ],
                },
            )
        ],
        EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1),
    )
    fake_llm = _CaptureLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=session_cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    output = await agent_loop.run_step(
        StepContext(
            step_definition=StepDefinition(
                name="direct",
                type="run",
                prompt="",
                step_profile_id="system:direct-default",
            ),
            session=session,
            conversation=SimpleNamespace(conversation_id="conv-discovery-denied"),
            agent=AgentDefinition(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
                permissions=AgentPermissions(
                    tool_permissions={stable_tool_id(get_events): Permission.DENY}
                ),
            ),
            policy=CHAT_POLICY,
            user_message="Read my calendar",
            tool_registry=registry,
            system_initiated=False,
        )
    )

    assert output is not None
    assert stable_tool_id(get_events) in session_cache.get_discovered_tool_ids(session.session_id)
    assert get_events.name not in fake_llm.tool_sets[0]


@pytest.mark.asyncio
async def test_skill_load_classifier_activates_only_hidden_tools_for_session() -> None:
    class _ClassifierLLM:
        def __init__(self) -> None:
            self.kwargs: list[dict[str, object]] = []

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del messages
            self.kwargs.append(dict(kwargs))
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"tool_ids": ["builtin:read_tool_output", '
                                '"builtin:browser_snapshot"]}'
                            )
                        }
                    }
                ]
            }

        async def get_model_info(self, model: str | None) -> SimpleNamespace:
            del model
            return _test_model_info()

        def count_tokens(self, text: str, model: str | None = None) -> int:
            del model
            return len(text)

    hidden_tool = ToolDefinition(
        name="browser_snapshot",
        description="Capture the current browser state.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="browser",
        read_only=True,
    )
    visible_tool = ToolDefinition(
        name="read_tool_output",
        description="Read prior tool output.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="context",
        read_only=True,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=hidden_tool, handler=None))
    registry.register(RegisteredTool(definition=visible_tool, handler=None))
    session_cache = _NoopSessionCache()
    llm = _ClassifierLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=session_cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="execute",
            type="run",
            prompt="",
            step_profile_id="system:general-task",
        ),
        session=SimpleNamespace(session_id="sess-skill", intaris_session_id="sess-skill"),
        conversation=SimpleNamespace(conversation_id="conv-skill"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        tool_registry=registry,
        policy=CHAT_POLICY,
    )

    activated_tool_ids: set[str] = set()
    promoted_tool_ids: set[str] = set()
    await agent_loop._apply_skill_activation(
        ctx,
        metadata={
            "skill_activation": {
                "skill_id": "skill_daily_brief",
                "name": "daily-brief",
                "description": "Morning briefing skill",
                "instructions": "Edit an existing image for the brief hero card.",
                "content_hash": "hash-1",
            },
            "discovered_tool_ids": [],
        },
        promoted_tool_ids=promoted_tool_ids,
        activated_tool_ids=activated_tool_ids,
    )

    assert stable_tool_id(hidden_tool) in activated_tool_ids
    assert stable_tool_id(visible_tool) not in activated_tool_ids
    assert stable_tool_id(hidden_tool) in session_cache.activated_skill_tool_ids
    assert len(session_cache.skill_tool_classifications) == 1
    assert next(iter(session_cache.skill_tool_classifications.values())) == [
        stable_tool_id(hidden_tool)
    ]
    assert llm.kwargs[0]["cognis_session_id"] == "sess-skill"
    # Promoted tool ids must also include the activated tool so it surfaces next turn.
    assert stable_tool_id(hidden_tool) in promoted_tool_ids


@pytest.mark.asyncio
async def test_skill_load_classifier_chunks_large_hidden_inventory() -> None:
    class _ClassifierLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self, messages: list[dict[str, object]], **_: object
        ) -> dict[str, object]:
            self.calls += 1
            content = str(messages[-1].get("content") or "")
            if "builtin:zzz_target_tool" in content:
                return {
                    "choices": [
                        {"message": {"content": '{"tool_ids": ["builtin:zzz_target_tool"]}'}}
                    ]
                }
            return {"choices": [{"message": {"content": '{"tool_ids": []}'}}]}

        async def get_model_info(self, model: str | None) -> SimpleNamespace:
            del model
            return _test_model_info()

        def count_tokens(self, text: str, model: str | None = None) -> int:
            del model
            return len(text)

    registry = ToolRegistry()
    for index in range(204):
        registry.register(
            RegisteredTool(
                definition=ToolDefinition(
                    name=f"tool_{index:03d}",
                    description="Generic hidden tool.",
                    parameters={"type": "object", "properties": {}},
                    source=ToolSource(type="builtin"),
                    category="browser",
                    read_only=True,
                ),
                handler=None,
            )
        )
    target_tool = ToolDefinition(
        name="zzz_target_tool",
        description="Target tool in a later classifier chunk.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="browser",
        read_only=True,
    )
    registry.register(RegisteredTool(definition=target_tool, handler=None))

    fake_llm = _ClassifierLLM()
    session_cache = _NoopSessionCache()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=session_cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="execute",
            type="run",
            prompt="",
            step_profile_id="system:general-task",
        ),
        session=SimpleNamespace(
            session_id="sess-skill-chunk", intaris_session_id="sess-skill-chunk"
        ),
        conversation=SimpleNamespace(conversation_id="conv-skill-chunk"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        tool_registry=registry,
        policy=CHAT_POLICY,
    )

    activated_tool_ids: set[str] = set()
    await agent_loop._apply_skill_activation(
        ctx,
        metadata={
            "skill_activation": {
                "skill_id": "skill_chunked",
                "name": "chunked-skill",
                "description": "Skill requiring a later hidden tool.",
                "instructions": "Use the target tool if available.",
                "content_hash": "hash-chunked",
            },
            "discovered_tool_ids": [],
        },
        promoted_tool_ids=set(),
        activated_tool_ids=activated_tool_ids,
    )

    assert fake_llm.calls == 3
    assert stable_tool_id(target_tool) in activated_tool_ids
    assert len(session_cache.skill_tool_classifications) == 1
    assert next(iter(session_cache.skill_tool_classifications.values())) == [
        stable_tool_id(target_tool)
    ]


@pytest.mark.asyncio
async def test_skill_activation_cache_key_changes_when_tags_change() -> None:
    """Tag-only edits must invalidate the cached activation decision."""

    class _ClassifierLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self, messages: list[dict[str, object]], **_: object
        ) -> dict[str, object]:
            self.calls += 1
            # No activations needed — we're only checking cache behaviour.
            return {"choices": [{"message": {"content": '{"tool_ids": [], "reasons": {}}'}}]}

    hidden_tool = ToolDefinition(
        name="browser_open",
        description="Open a URL in the browser.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="browser",
        read_only=False,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=hidden_tool, handler=None))

    fake_llm = _ClassifierLLM()
    session_cache = _NoopSessionCache()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=session_cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="execute",
            type="run",
            prompt="",
            step_profile_id="system:general-task",
        ),
        session=SimpleNamespace(session_id="sess-cache", intaris_session_id="sess-cache"),
        conversation=SimpleNamespace(conversation_id="conv-cache"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        tool_registry=registry,
        policy=CHAT_POLICY,
    )

    await agent_loop._apply_skill_activation(
        ctx,
        metadata={
            "skill_activation": {
                "skill_id": "skill_cache",
                "name": "daily-brief",
                "description": "Morning briefing skill.",
                "instructions": "Open URLs in a browser.",
                "tags": ["browser"],
                "content_hash": "same-hash",
            },
            "discovered_tool_ids": [],
        },
        promoted_tool_ids=set(),
        activated_tool_ids=set(),
    )
    await agent_loop._apply_skill_activation(
        ctx,
        metadata={
            "skill_activation": {
                "skill_id": "skill_cache",
                "name": "daily-brief",
                "description": "Morning briefing skill.",
                "instructions": "Open URLs in a browser.",
                "tags": ["calendar"],
                "content_hash": "same-hash",
            },
            "discovered_tool_ids": [],
        },
        promoted_tool_ids=set(),
        activated_tool_ids=set(),
    )

    # The classifier must be invoked twice because the cache key changed.
    assert fake_llm.calls == 2
    assert len(session_cache.skill_tool_classifications) == 2


@pytest.mark.asyncio
async def test_skill_activation_classifier_scopes_to_policy_hidden_tools_only() -> None:
    """B1 — classifier candidates must exclude policy-visible tools.

    Even if a policy-visible tool is cap-hidden in practice, the classifier
    should only unlock tools that the step profile would NOT normally show.
    Cap-hidden policy-visible tools are reachable via search_tools.
    """

    class _ClassifierLLM:
        def __init__(self) -> None:
            self.received_candidates: list[str] = []

        async def generate(
            self, messages: list[dict[str, object]], **_: object
        ) -> dict[str, object]:
            user_msg = str(messages[-1].get("content") or "")
            self.received_candidates.append(user_msg)
            return {"choices": [{"message": {"content": '{"tool_ids": [], "reasons": {}}'}}]}

    # visible_tool: policy-visible under system:general-task (system group, READ).
    visible_tool = ToolDefinition(
        name="read_tool_output",
        description="Read prior tool output.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="context",
        read_only=True,
    )
    # hidden_tool: browser category — NOT in general-task matrix, so policy-hidden.
    hidden_tool = ToolDefinition(
        name="browser_open",
        description="Open a URL in the browser.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="browser",
        read_only=False,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=visible_tool, handler=None))
    registry.register(RegisteredTool(definition=hidden_tool, handler=None))

    fake_llm = _ClassifierLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="execute",
            type="run",
            prompt="",
            step_profile_id="system:general-task",
        ),
        session=SimpleNamespace(session_id="sess-b1", intaris_session_id="sess-b1"),
        conversation=SimpleNamespace(conversation_id="conv-b1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        tool_registry=registry,
        policy=CHAT_POLICY,
    )

    activated_tool_ids: set[str] = set()
    promoted_tool_ids: set[str] = set()
    await agent_loop._apply_skill_activation(
        ctx,
        metadata={
            "skill_activation": {
                "skill_id": "skill_test",
                "name": "test",
                "description": "Test skill",
                "instructions": "Test instructions",
                "tags": ["browser"],
                "content_hash": "hash-b1",
            },
            "discovered_tool_ids": [],
        },
        promoted_tool_ids=promoted_tool_ids,
        activated_tool_ids=activated_tool_ids,
    )

    # The classifier must have been called with only the policy-hidden tool.
    assert fake_llm.received_candidates, "classifier was never invoked"
    candidate_text = " ".join(fake_llm.received_candidates)
    assert "browser_open" in candidate_text
    # read_tool_output is policy-visible and must NOT appear as a candidate.
    assert "read_tool_output" not in candidate_text


@pytest.mark.asyncio
async def test_skill_activation_classifier_receives_tags_and_referenced_services() -> None:
    """A1+A3 — classifier prompt must include skill tags and referenced services."""
    from cognis.core.agent_loop import AgentLoop

    class _ClassifierLLM:
        def __init__(self) -> None:
            self.user_prompts: list[str] = []

        async def generate(
            self, messages: list[dict[str, object]], **_: object
        ) -> dict[str, object]:
            self.user_prompts.append(str(messages[-1].get("content") or ""))
            return {"choices": [{"message": {"content": '{"tool_ids": [], "reasons": {}}'}}]}

    hidden_tool = ToolDefinition(
        name="browser_open",
        description="Open a URL in the browser.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="browser",
        read_only=False,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=hidden_tool, handler=None))

    fake_llm = _ClassifierLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="execute",
            type="run",
            prompt="",
            step_profile_id="system:general-task",
        ),
        session=SimpleNamespace(session_id="sess-a1", intaris_session_id="sess-a1"),
        conversation=SimpleNamespace(conversation_id="conv-a1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        tool_registry=registry,
        policy=CHAT_POLICY,
    )

    activated_tool_ids: set[str] = set()
    promoted_tool_ids: set[str] = set()
    await agent_loop._apply_skill_activation(
        ctx,
        metadata={
            "skill_activation": {
                "skill_id": "skill_brief",
                "name": "daily-brief",
                "description": "Morning briefing with Gmail and Calendar.",
                "instructions": "Fetch calendar events from Google Calendar and inbox from Gmail.",
                "tags": ["briefing", "gmail", "calendar"],
                "content_hash": "hash-a1",
            },
            "discovered_tool_ids": [],
        },
        promoted_tool_ids=promoted_tool_ids,
        activated_tool_ids=activated_tool_ids,
    )

    assert fake_llm.user_prompts, "classifier was never invoked"
    prompt_text = " ".join(fake_llm.user_prompts)
    # Tags must appear in the prompt.
    assert "gmail" in prompt_text.lower()
    assert "calendar" in prompt_text.lower()
    # Referenced services derived from the instructions must appear.
    assert "googleworkspace" in prompt_text.lower()


@pytest.mark.asyncio
async def test_skill_activation_emits_transparency_notice_to_model() -> None:
    """B2 — a <skill_activation> system message must be injected into messages
    after classifier-based activation so the model can self-correct."""

    class _ClassifierLLM:
        async def generate(
            self, messages: list[dict[str, object]], **_: object
        ) -> dict[str, object]:
            del messages
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"tool_ids": ["builtin:browser_open"], "reasons": {"builtin:browser_open": "skill tag browser"}}'
                        }
                    }
                ]
            }

    hidden_tool = ToolDefinition(
        name="browser_open",
        description="Open a URL in the browser.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="browser",
        read_only=False,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=hidden_tool, handler=None))

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_ClassifierLLM(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="execute",
            type="run",
            prompt="",
            step_profile_id="system:general-task",
        ),
        session=SimpleNamespace(session_id="sess-b2", intaris_session_id="sess-b2"),
        conversation=SimpleNamespace(conversation_id="conv-b2"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        tool_registry=registry,
        policy=CHAT_POLICY,
    )

    activated_tool_ids: set[str] = set()
    promoted_tool_ids: set[str] = set()
    notice = await agent_loop._apply_skill_activation(
        ctx,
        metadata={
            "skill_activation": {
                "skill_id": "skill_browser",
                "name": "browser-skill",
                "description": "Browser automation skill.",
                "instructions": "Open URLs in a browser.",
                "tags": ["browser"],
                "content_hash": "hash-b2",
            },
            "discovered_tool_ids": [],
        },
        promoted_tool_ids=promoted_tool_ids,
        activated_tool_ids=activated_tool_ids,
    )

    assert notice is not None, "expected an activation notice"
    assert "<skill_activation" in notice
    assert "browser_open" in notice
    assert "search_tools" in notice  # self-correction hint


@pytest.mark.asyncio
async def test_step_complete_validation_reprompts_and_accepts_corrected_payload() -> None:
    fake_llm = _StepCompleteValidationLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="commit", type="run", prompt="", require_deliverable=False
        ),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="create a commit",
        user_attachments=[],
        system_initiated=False,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.outcome is not None
    assert output.outcome.status == "failed"
    assert output.outcome.reason == "git identity missing"
    assert len(fake_llm.calls) == 2
    second_prompt = str(fake_llm.calls[1][-1]["content"])
    assert "invalid_step_complete_arguments" in second_prompt
    assert "outcome.reason" in second_prompt


def _step_complete_test_loop(fake_llm: object) -> AgentLoop:
    return AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )


def _step_complete_test_context(step_definition: StepDefinition) -> StepContext:
    return StepContext(
        step_definition=step_definition,
        session=SimpleNamespace(
            session_id="sess-step-complete-metadata",
            intaris_session_id="sess-step-complete-metadata",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-step-complete-metadata",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="complete the step",
        user_attachments=[],
        system_initiated=False,
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family="default",
            allow_silent_completion=False,
        ),
    )


def _first_tool_result_payload(fake_llm: _StepCompleteRetryLLM) -> dict[str, object]:
    second_prompt = fake_llm.calls[1]
    tool_message = next(message for message in second_prompt if message.get("role") == "tool")
    return json.loads(str(tool_message["content"]))


def _lifecycle_strategy_step() -> StepDefinition:
    return StepDefinition(
        name="plan",
        type="run",
        prompt="",
        require_deliverable=False,
        metadata_contract=StepCompletionContract(
            fields=[
                StepCompletionMetadataField(
                    name="lifecycle_strategy",
                    type="object",
                    required=True,
                )
            ]
        ),
    )


@pytest.mark.asyncio
async def test_step_complete_missing_required_metadata_reports_metadata_reason() -> None:
    fake_llm = _StepCompleteRetryLLM(
        first_arguments={"summary": "done"},
        second_arguments={"summary": "done", "metadata": {"lifecycle_strategy": {}}},
    )
    output = await _step_complete_test_loop(fake_llm).run_step(
        _step_complete_test_context(_lifecycle_strategy_step())
    )

    payload = _first_tool_result_payload(fake_llm)
    assert output is not None
    assert output.metadata["lifecycle_strategy"] == {}
    assert payload["reason"] == "invalid_step_complete_metadata"
    assert "lifecycle_strategy" in str(payload["message"])
    assert payload["example"]["metadata"]["lifecycle_strategy"] == {}


@pytest.mark.asyncio
async def test_step_complete_wrong_metadata_object_type_reports_metadata_reason() -> None:
    fake_llm = _StepCompleteRetryLLM(
        first_arguments={"summary": "done", "metadata": {"lifecycle_strategy": "manual"}},
        second_arguments={"summary": "done", "metadata": {"lifecycle_strategy": {}}},
    )
    output = await _step_complete_test_loop(fake_llm).run_step(
        _step_complete_test_context(_lifecycle_strategy_step())
    )

    payload = _first_tool_result_payload(fake_llm)
    assert output is not None
    assert payload["reason"] == "invalid_step_complete_metadata"
    assert "must be object" in str(payload["message"])
    assert payload["example"]["metadata"]["lifecycle_strategy"] == {}


@pytest.mark.asyncio
async def test_step_complete_no_contract_validation_error_keeps_generic_example() -> None:
    fake_llm = _StepCompleteRetryLLM(
        first_arguments={"summary": "done", "outcome": {"status": "failed"}},
        second_arguments={
            "summary": "done",
            "outcome": {"status": "failed", "reason": "upstream failed"},
        },
    )
    output = await _step_complete_test_loop(fake_llm).run_step(
        _step_complete_test_context(
            StepDefinition(name="no-contract", type="run", prompt="", require_deliverable=False)
        )
    )

    payload = _first_tool_result_payload(fake_llm)
    assert output is not None
    assert payload["reason"] == "invalid_step_complete_arguments"
    assert "metadata" not in payload["example"]


def test_step_complete_runtime_notification_error_payload_is_notification_reason() -> None:
    ctx = _step_complete_test_context(
        StepDefinition(name="notify", type="run", prompt="", require_deliverable=False)
    )
    arguments = {
        "summary": "done",
        "notification": {"mode": "silent", "reason": "Nothing actionable happened."},
    }
    step_output = StepOutput(
        summary=arguments["summary"],
        notification=arguments["notification"],
    )

    with pytest.raises(ValueError) as exc_info:
        _validate_step_completion_notification(ctx, step_output)

    payload = {
        "status": "rejected",
        "reason": "invalid_step_complete_notification",
        "message": str(exc_info.value),
        "received": arguments,
    }
    assert payload["reason"] == "invalid_step_complete_notification"
    assert payload["reason"] != "invalid_step_complete_metadata"


@pytest.mark.asyncio
async def test_step_complete_must_be_last_tool_call_in_response() -> None:
    fake_llm = _StepCompleteOrderingLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="commit", type="run", prompt="", require_deliverable=False
        ),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="create a commit",
        user_attachments=[],
        system_initiated=False,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.summary == "done"
    assert len(fake_llm.calls) == 2
    second_prompt = "\n".join(str(message.get("content")) for message in fake_llm.calls[1])
    assert "step_complete_not_last_tool_call" in second_prompt
    assert "step_todo_write" in second_prompt


@pytest.mark.asyncio
async def test_terminal_todos_block_non_finalization_tool() -> None:
    fake_llm = _TerminalTodosThenExtraToolLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="plan", type="run", prompt="Plan the change.", require_deliverable=False
        ),
        session=SimpleNamespace(
            session_id="sess-finalization",
            intaris_session_id="sess-finalization",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-finalization", title=None),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="Plan the change.",
        user_attachments=[],
        system_initiated=False,
        task_id="task-finalization",
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.summary == "done"
    assert len(fake_llm.calls) == 3
    final_prompt = "\n".join(str(message.get("content")) for message in fake_llm.calls[2])
    assert "finalization_required" in final_prompt
    assert "All step todos are terminal" in final_prompt
    assert "Your next action must be step_complete" in final_prompt
    assistant_index = next(
        index
        for index, message in enumerate(fake_llm.calls[2])
        if "call_todo_list_after_done" in str(message.get("tool_calls"))
    )
    assert fake_llm.calls[2][assistant_index + 1]["role"] == "tool"
    assert fake_llm.calls[2][assistant_index + 2]["role"] == "tool"
    assert fake_llm.calls[2][assistant_index + 3]["role"] == "system"
    first_rejection = json.loads(str(fake_llm.calls[2][assistant_index + 1]["content"]))
    assert first_rejection["allowed_tools"] == ["step_complete", "step_todo_write"]


@pytest.mark.asyncio
async def test_terminal_todos_still_allow_todo_write() -> None:
    fake_llm = _TerminalTodosThenTodoCorrectionLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="plan", type="run", prompt="Plan the change.", require_deliverable=False
        ),
        session=SimpleNamespace(
            session_id="sess-todo-correction",
            intaris_session_id="sess-todo-correction",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-todo-correction", title=None),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="Plan the change.",
        user_attachments=[],
        system_initiated=False,
        task_id="task-todo-correction",
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.summary == "done"
    assert len(fake_llm.calls) == 3
    final_prompt = "\n".join(str(message.get("content")) for message in fake_llm.calls[2])
    assert "finalization_required" not in final_prompt
    assert "Next call step_complete" in final_prompt


@pytest.mark.asyncio
async def test_llm_stream_idle_timeout_after_todo_write_auto_continues() -> None:
    class _HangingAfterTodoLLM:
        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text: str, model: str | None = None) -> int:
            del model
            return len(text)

        async def get_model_info(self, model: str | None) -> SimpleNamespace:
            del model
            return _test_model_info()

        async def stream_generate(self, messages: list[dict[str, object]], **_: object):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_todos",
                                        "function": {
                                            "name": "step_todo_write",
                                            "arguments": json.dumps(
                                                {
                                                    "todos": [
                                                        {
                                                            "content": "Save each Reddit comment memory",
                                                            "status": "completed",
                                                        },
                                                        {
                                                            "content": "Attach source URL artifact",
                                                            "status": "in_progress",
                                                        },
                                                    ]
                                                }
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
                return

            if self.calls == 2:
                await asyncio.sleep(30)
                if False:  # pragma: no cover - keep this an async generator
                    yield {}
                return
            if self.calls == 3:
                assert not any(
                    message["role"] == "system"
                    and "previous model stream failed" in str(message["content"])
                    for message in messages
                )
                await asyncio.sleep(30)
                if False:  # pragma: no cover - keep this an async generator
                    yield {}
                return
            if self.calls == 4:
                assert any(
                    message["role"] == "system"
                    and "previous model stream failed" in str(message["content"])
                    for message in messages
                )
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_todos_done",
                                        "function": {
                                            "name": "step_todo_write",
                                            "arguments": json.dumps(
                                                {
                                                    "todos": [
                                                        {
                                                            "content": "Save each Reddit comment memory",
                                                            "status": "completed",
                                                        },
                                                        {
                                                            "content": "Attach source URL artifact",
                                                            "status": "completed",
                                                        },
                                                    ]
                                                }
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
                return
            if self.calls == 5:
                yield {"choices": [{"delta": {"content": "Recovered after idle timeout."}}]}
                return
            while True:
                await asyncio.sleep(30)
                yield {"choices": []}

    fake_llm = _HangingAfterTodoLLM()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        default_llm_stream_idle_timeout_seconds=1,
        default_llm_stream_max_retries=1,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-hung-llm",
            intaris_session_id="sess-hung-llm",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-hung-llm",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        user_message="save reddit comments",
        user_attachments=[],
        system_initiated=False,
    )
    tokens: list[str] = []

    async def _on_token(token: str) -> None:
        tokens.append(token)

    recorded_events: list[SessionEvent] = []

    async def _record_events_strict(
        _ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: object = None,
    ) -> bool:
        del reason, on_token
        recorded_events.extend(events)
        events.clear()
        return True

    agent_loop._record_events_strict = _record_events_strict  # type: ignore[method-assign]

    output = await agent_loop.run_step(ctx, on_token=_on_token)

    assert output is not None
    assert output.error is None
    assert output.summary == "Recovered after idle timeout."
    assert fake_llm.calls == 5
    streamed_text = "".join(tokens)
    assert "Recovered after idle timeout." in streamed_text
    assert "model did not produce output" not in streamed_text
    system_notices = [
        str(event.data.get("message", ""))
        for event in recorded_events
        if event.type == "lifecycle" and event.data.get("event") == "system_notice"
    ]
    assert any("continuing from the saved state" in notice for notice in system_notices)
    assert not any("model did not produce output" in notice for notice in system_notices)
    assert "sess-hung-llm" in agent_loop.session_lock.stale_unlocked_session_ids(max_idle_seconds=0)


def test_step_complete_rejects_silent_notification_when_not_allowed() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="check", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="check it",
        user_attachments=[],
        system_initiated=False,
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family="default",
            allow_silent_completion=False,
        ),
    )
    step_output = StepOutput(
        summary="done",
        notification={"mode": "silent", "reason": "Nothing actionable happened."},
    )

    with pytest.raises(ValueError, match="not allowed"):
        _validate_step_completion_notification(ctx, step_output)


def test_step_complete_allows_direct_notification_for_success() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="brief", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="prepare brief",
        user_attachments=[],
        system_initiated=False,
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family="default",
            allow_silent_completion=False,
        ),
    )
    step_output = StepOutput(
        summary="Prepared daily brief.",
        content="Here is the daily brief.",
        notification={"mode": "direct"},
    )

    _validate_step_completion_notification(ctx, step_output)


def test_step_complete_rejects_direct_notification_for_failed_outcome() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="brief", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="prepare brief",
        user_attachments=[],
        system_initiated=False,
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family="default",
            allow_silent_completion=False,
        ),
    )
    step_output = StepOutput(
        summary="Failed to prepare daily brief.",
        outcome={"status": "failed", "reason": "upstream API unavailable"},
        notification={"mode": "direct"},
    )

    with pytest.raises(ValueError, match="only valid for successful completion"):
        _validate_step_completion_notification(ctx, step_output)


def test_step_complete_rejects_direct_notification_without_written_deliverable() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="brief", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="prepare brief",
        user_attachments=[],
        system_initiated=False,
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family="default",
            allow_silent_completion=False,
        ),
    )
    step_output = StepOutput(
        summary="Prepared daily brief.",
        notification={"mode": "direct"},
    )

    with pytest.raises(ValueError, match="requires a non-empty deliverable"):
        _validate_step_completion_notification(ctx, step_output)


def test_build_step_prompt_includes_revision_context() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    workflow_state = WorkflowState(
        last_revision_context="The previous step `architect_review` requested revisions.\n\nReviewer Output:\nFull review text."
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="plan", type="run", prompt="Produce a plan."),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        workflow_state=workflow_state,
        workflow_steps=[StepDefinition(name="plan", type="run")],
        step_index=0,
    )

    prompt = agent_loop._build_step_prompt(ctx)

    assert "## Revision Context" in prompt
    assert "Full review text." in prompt
    assert workflow_state.last_revision_context is None


def test_build_tool_attachment_context_uses_user_blocks_for_vision_models() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=StepDefinition(name="execute", type="run", prompt="Do work"),
        session=SimpleNamespace(session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        current_model_info=SimpleNamespace(
            supports_vision=True,
            supports_pdf_input=False,
            supports_audio_input=False,
            supports_file_input=False,
        ),
    )

    message = loop._build_tool_attachment_context(
        ctx,
        ToolCall(call_id="call-1", name="browser_screenshot", arguments={}),
        [
            {
                "artifact_id": "art-1",
                "kind": "image",
                "filename": "shot.png",
                "url": "https://example.test/shot.png",
            }
        ],
    )

    assert message is not None
    assert message["role"] == "user"
    assert isinstance(message["content"], list)
    assert message["content"][0]["type"] == "text"
    assert "artifact_id=art-1" in message["content"][0]["text"]
    assert message["content"][1]["type"] == "image_url"


def test_context_pressure_exceeded_counts_exposed_tool_schemas() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.providers = SimpleNamespace(
        llm=SimpleNamespace(
            count_messages_tokens=lambda messages, model: 900,
            count_tokens=lambda text, model: len(text),
        )
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="execute", type="run", prompt="Do work"),
        session=SimpleNamespace(session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
        ),
        current_model="test-model",
        current_model_info=SimpleNamespace(max_output_tokens=50),
    )
    tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": "filesystem/read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]

    exceeded = loop._context_pressure_exceeded(
        ctx,
        messages=[{"role": "system", "content": "hello"}],
        tool_schemas=tool_schemas,
        max_context_tokens=1000,
    )

    assert exceeded is True


def test_context_pressure_snapshot_clamps_oversized_output_reserve() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.providers = SimpleNamespace(
        llm=SimpleNamespace(
            count_messages_tokens=lambda messages, model: 29_000,
            count_tokens=lambda text, model: 0,
        )
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="execute", type="run", prompt="Do work"),
        session=SimpleNamespace(session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
        ),
        current_model="gpt-5.4",
        current_model_info=SimpleNamespace(max_output_tokens=500_000),
    )

    snapshot = loop._context_pressure_snapshot(
        ctx,
        messages=[{"role": "system", "content": "hello"}],
        tool_schemas=[],
        max_context_tokens=250_000,
    )

    assert snapshot is not None
    assert snapshot.reserve_clamped is True
    assert snapshot.reserve_output_tokens == 500_000
    assert snapshot.effective_reserve_output_tokens == 31_250
    assert snapshot.available_prompt_tokens == 201_250
    assert snapshot.threshold_prompt_tokens == 191_187
    assert snapshot.exceeded is False


@pytest.mark.asyncio
async def test_tool_call_ceiling_returns_partial_step_output_without_second_llm_turn() -> None:
    fake_llm = _ToolCallCeilingLLM()

    class _ToolRouter:
        async def execute(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            return SimpleNamespace(
                output="ok",
                is_error=False,
                duration_ms=1,
                metadata={},
                attachments=[],
            )

    ctx = StepContext(
        step_definition=StepDefinition(name="execute", type="run", prompt="Do the task."),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            execution={"max_tool_calls": 1},
        ),
        policy=WORKFLOW_POLICY,
        user_message="do it",
        user_attachments=[],
        system_initiated=False,
    )

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=_ToolRouter(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.outcome is None
    assert (
        output.summary
        == "Stopped after reaching the tool-call ceiling. Partial work was preserved for evaluation."
    )
    assert fake_llm.calls == 1


@pytest.mark.asyncio
async def test_llm_cycle_ceiling_flushes_lifecycle_event_and_returns_continuation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecordingGuardrails(_NoopGuardrails):
        def __init__(self) -> None:
            self.recorded_batches: list[list[SessionEvent]] = []

        async def record_events(self, **kwargs: object) -> EventAppendResult:
            events = list(kwargs.get("events") or [])
            self.recorded_batches.append(events)
            return EventAppendResult(
                ok=True,
                count=len(events),
                first_seq=1,
                last_seq=len(events),
            )

    monkeypatch.setattr(agent_loop_module, "_MAX_LLM_CYCLES_PER_TURN", 0)
    fake_llm = _ToolCallCeilingLLM()
    guardrails = _RecordingGuardrails()
    session_cache = _NoopSessionCache()

    ctx = StepContext(
        step_definition=StepDefinition(name="execute", type="run", prompt="Do the task."),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title=None,
            title_source="unset",
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="do it",
        user_attachments=[],
        system_initiated=False,
    )
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=fake_llm, guardrails=guardrails),
        session_manager=_NoopSessionManager(),
        session_cache=session_cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.error is None
    assert output.outcome is None
    assert output.metadata["interrupted"] is True
    assert output.metadata["continuation_reason"] == LLM_CYCLE_CEILING_CONTINUATION_REASON
    assert output.metadata["cycle_count"] == 0
    assert output.metadata["max_llm_cycles"] == 0
    assert fake_llm.calls == 0
    assert len(guardrails.recorded_batches) == 2
    assert guardrails.recorded_batches[0][0].type == "user_message"
    recorded_event = guardrails.recorded_batches[1][0]
    assert recorded_event.type == "lifecycle"
    assert recorded_event.data["event"] == LLM_CYCLE_CEILING_CONTINUATION_REASON
    assert recorded_event.data["cycle_count"] == 0
    assert recorded_event.data["max_llm_cycles"] == 0


def test_build_step_prompt_includes_operator_instruction() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    workflow_state = WorkflowState(last_operator_instruction="Incorporate the review and continue.")
    ctx = StepContext(
        step_definition=StepDefinition(name="implement", type="run", prompt="Implement the plan."),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        workflow_state=workflow_state,
        workflow_steps=[StepDefinition(name="implement", type="run")],
        step_index=0,
    )

    prompt = agent_loop._build_step_prompt(ctx)

    assert "## Operator Instruction" in prompt
    assert "Incorporate the review and continue." in prompt


def test_filter_model_inventory_tools_hides_unattached_skill_tools_until_discovered() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        skills={"_attached_skill_tool_ids": ["skill:attached-skill:run_attached"]},
    )
    attached_tool = ToolDefinition(
        name="skill_attached-skill__run_attached",
        description="Attached",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="attached-skill", raw_tool_name="run_attached"),
        category="skill",
    )
    unattached_tool = ToolDefinition(
        name="skill_unattached-skill__run_unattached",
        description="Unattached",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            type="skill", skill_id="unattached-skill", raw_tool_name="run_unattached"
        ),
        category="skill",
    )

    filtered = _filter_model_inventory_tools(agent, [attached_tool, unattached_tool], set())
    discovered = _filter_model_inventory_tools(
        agent,
        [attached_tool, unattached_tool],
        {"skill:unattached-skill:run_unattached"},
    )
    activated = _filter_model_inventory_tools(
        agent,
        [attached_tool, unattached_tool],
        set(),
        {"skill:unattached-skill:run_unattached"},
    )

    assert [tool.name for tool in filtered] == ["skill_attached-skill__run_attached"]
    assert [tool.name for tool in discovered] == [
        "skill_attached-skill__run_attached",
        "skill_unattached-skill__run_unattached",
    ]
    assert [tool.name for tool in activated] == [
        "skill_attached-skill__run_attached",
        "skill_unattached-skill__run_unattached",
    ]


def test_initial_skill_tool_ids_include_auto_loaded_skill_tools() -> None:
    session_cache = _NoopSessionCache()
    session_cache.activated_skill_tool_ids.add("builtin:existing")
    agent_loop = AgentLoop(
        providers=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=session_cache,
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=SimpleNamespace(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            skills={
                "_attached_skill_tool_ids": ["builtin:attached"],
                "_auto_loaded_skill_tool_ids": ["builtin:auto"],
            },
        ),
    )

    assert agent_loop._get_initial_promoted_tool_ids(ctx) == {
        "builtin:attached",
        "builtin:auto",
    }
    assert agent_loop._get_initial_activated_tool_ids(ctx) == {
        "builtin:auto",
        "builtin:existing",
    }


def test_filter_model_inventory_tools_respects_legacy_skill_permission_names() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        skills={"_attached_skill_tool_ids": ["skill:attached-skill:run_attached"]},
        permissions=AgentPermissions(denied_tools=["run_attached"]),
    )
    attached_tool = ToolDefinition(
        name="skill_attached-skill__run_attached",
        description="Attached",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="attached-skill", raw_tool_name="run_attached"),
        category="skill",
    )

    filtered = _filter_model_inventory_tools(agent, [attached_tool], set())

    assert filtered == []


def test_format_prior_step_outputs_full_includes_full_content() -> None:
    agent_loop = object.__new__(AgentLoop)
    workflow_state = WorkflowState(
        step_outputs={
            "plan": StepOutput(
                summary="Plan ready",
                metadata={"scope_contract": [{"id": "backend"}]},
                claims=["Covered edge cases"],
                content="Detailed plan body",
                deliverable_id="dlv-plan",
                outputs={"files": ["a.py"]},
            ).model_dump(mode="json")
        }
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="architect_review",
            type="run",
            input=StepInputConfig(type="full", source="plan"),
        ),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        workflow_state=workflow_state,
        workflow_steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(
                name="architect_review",
                type="run",
                input=StepInputConfig(type="full", source="plan"),
            ),
        ],
        step_index=1,
    )

    text = agent_loop._format_prior_step_outputs(ctx)

    assert "Summary: Plan ready" in text
    assert "Metadata:" in text
    assert "scope_contract" in text
    assert "Claims:" in text
    assert "Deliverable:\nDetailed plan body" in text
    assert "Structured outputs:" in text


def test_format_prior_step_outputs_summary_includes_deliverable_content() -> None:
    agent_loop = object.__new__(AgentLoop)
    workflow_state = WorkflowState(
        step_outputs={
            "implement": StepOutput(
                summary="Implemented change",
                metadata={"scope_status": [{"id": "backend", "status": "completed"}]},
                claims=["Ran tests"],
                content="Long implementation details",
                deliverable_id="dlv-implement",
                outputs={"tests": ["pytest tests/unit"]},
            ).model_dump(mode="json")
        }
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="update_docs",
            type="run",
            input=StepInputConfig(type="summary", source="implement"),
        ),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        workflow_state=workflow_state,
        workflow_steps=[
            StepDefinition(name="implement", type="run"),
            StepDefinition(
                name="update_docs",
                type="run",
                input=StepInputConfig(type="summary", source="implement"),
            ),
        ],
        step_index=1,
    )

    text = agent_loop._format_prior_step_outputs(ctx)

    assert "Summary: Implemented change" in text
    assert "Metadata:" in text
    assert "scope_status" in text
    assert "Deliverable:\nLong implementation details" in text
    assert "Structured outputs:" in text
    assert "Claims:" not in text


def test_format_prior_step_outputs_last_includes_deliverable_content() -> None:
    agent_loop = object.__new__(AgentLoop)
    workflow_state = WorkflowState(
        step_outputs={
            "plan": StepOutput(
                summary="Plan ready",
                metadata={"scope_contract": [{"id": "frontend"}]},
                claims=["Reviewed dependencies"],
                content="Verbose plan details",
                deliverable_id="dlv-plan",
                outputs={"files": ["a.py", "b.py"]},
            ).model_dump(mode="json")
        }
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="last", source="plan"),
        ),
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        workflow_state=workflow_state,
        workflow_steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(
                name="implement",
                type="run",
                input=StepInputConfig(type="last", source="plan"),
            ),
        ],
        step_index=1,
    )

    text = agent_loop._format_prior_step_outputs(ctx)

    assert "Summary: Plan ready" in text
    assert "Metadata:" in text
    assert "scope_contract" in text
    assert "Claims:" in text
    assert "Deliverable:\nVerbose plan details" in text
    assert "Structured outputs:" in text


@pytest.mark.asyncio
async def test_resolve_task_pause_tool_retries_gate_with_note() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )

    class _TaskSessionManager(_NoopSessionManager):
        def session_factory(self) -> object:
            class _Dummy:
                async def __aenter__(self) -> SimpleNamespace:
                    return SimpleNamespace()

                async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                    return False

            return _Dummy()

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=pause_waiter,
    )
    agent_loop._task_queue = SimpleNamespace()
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            task_id="task-1", created_by="user@example.com", agent_id="agent-1", status="paused"
        )

    from unittest.mock import patch

    with patch("cognis.store.queries.get_task", _get_task):
        result = await agent_loop._handle_orchestration_tool(
            ToolCall(
                call_id="call-1",
                name="resolve_task_pause",
                arguments={
                    "task_id": "task-1",
                    "action": "retry",
                    "note": "Incorporate the review and continue.",
                },
            ),
            ctx=ctx,
            events_to_record=[],
        )

    payload = json.loads(result.output)
    assert payload["status"] == "retrying"
    assert payload["note_applied"] is True
    resolution = await pause_waiter.wait("gate-1", timeout=0.01)
    assert resolution.decision == "revise(plan)"
    assert resolution.data == {"note": "Incorporate the review and continue."}


@pytest.mark.asyncio
async def test_resolve_task_pause_tool_does_not_bypass_non_retryable_gate() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Continue", "action": "continue"}],
        )
    )

    class _TaskSessionManager(_NoopSessionManager):
        def session_factory(self) -> object:
            class _Dummy:
                async def __aenter__(self) -> SimpleNamespace:
                    return SimpleNamespace()

                async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                    return False

            return _Dummy()

    class _TaskQueue:
        async def retry_failed_task(self, task_id: str) -> None:
            raise AssertionError(f"retry_failed_task should not be called for {task_id}")

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=pause_waiter,
    )
    agent_loop._task_queue = _TaskQueue()
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            task_id="task-1", created_by="user@example.com", agent_id="agent-1", status="paused"
        )

    from unittest.mock import patch

    with patch("cognis.store.queries.get_task", _get_task):
        result = await agent_loop._handle_orchestration_tool(
            ToolCall(
                call_id="call-1",
                name="resolve_task_pause",
                arguments={"task_id": "task-1", "action": "retry"},
            ),
            ctx=ctx,
            events_to_record=[],
        )

    assert result.is_error is True
    assert "does not offer a retry action" in json.loads(result.output)["error"]


@pytest.mark.asyncio
async def test_retry_task_tool_paused_task_without_gate_returns_tool_error() -> None:
    class _TaskSessionManager(_NoopSessionManager):
        def session_factory(self) -> object:
            class _Dummy:
                async def __aenter__(self) -> SimpleNamespace:
                    return SimpleNamespace()

                async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                    return False

            return _Dummy()

    class _TaskQueue:
        def __init__(self) -> None:
            self.retry_calls: list[str] = []

        async def retry_failed_task(self, task_id: str) -> None:
            self.retry_calls.append(task_id)
            raise ValueError("Only failed tasks can be retried via retry_failed_task")

    task_queue = _TaskQueue()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    agent_loop._task_queue = task_queue
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            task_id="task-1", created_by="user@example.com", agent_id="agent-1", status="paused"
        )

    from unittest.mock import patch

    with patch("cognis.store.queries.get_task", _get_task):
        result = await agent_loop._handle_orchestration_tool(
            ToolCall(
                call_id="call-1",
                name="retry_task",
                arguments={"task_id": "task-1"},
            ),
            ctx=ctx,
            events_to_record=[],
        )

    assert result.is_error is True
    assert json.loads(result.output)["error"] == "No pending gate for task"
    assert task_queue.retry_calls == []


@pytest.mark.asyncio
async def test_respond_task_input_tool_returns_error_without_answers() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    result = await agent_loop._handle_task_tool(
        ToolCall(call_id="call-1", name="respond_task_input", arguments={"task_id": "task-1"}),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is True
    assert json.loads(result.output)["error"] == "answers must be an array."


@pytest.mark.asyncio
async def test_get_task_tool_includes_pending_pause_and_workflow_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )

    class _TaskSessionManager(_NoopSessionManager):
        def session_factory(self) -> object:
            class _Dummy:
                async def __aenter__(self) -> SimpleNamespace:
                    return SimpleNamespace()

                async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                    return False

            return _Dummy()

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=pause_waiter,
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            task_id="task-1",
            created_by="user@example.com",
            title="Task",
            description="Desc",
            expected_output=None,
            status="paused",
            priority=0,
            agent_id="agent-1",
            source_type="agent",
            source_ref="conv-1",
            delivery_mode="same_conversation",
            delivery_target=None,
            workflow_id="wf-1",
            workflow_state=WorkflowState(current_step_index=0).model_dump(mode="json"),
            queue_name="default",
            scheduled_for=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            result_summary=None,
            result_data={"foo": "bar"},
        )

    async def _list_step_runs(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        del args, kwargs
        return [
            SimpleNamespace(
                step_name="plan", status="approved", attempt=1, output={"summary": "ok"}
            )
        ]

    class _Registry:
        async def get(self, workflow_id: str) -> SimpleNamespace:
            del workflow_id
            return SimpleNamespace(steps=[SimpleNamespace(name="plan")])

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.store.queries.list_step_runs_for_task", _list_step_runs)
    monkeypatch.setattr(
        "cognis.core.workflow_registry.WorkflowRegistry", lambda session_factory: _Registry()
    )

    result = await agent_loop._handle_task_tool(
        ToolCall(call_id="call-1", name="get_task", arguments={"task_id": "task-1"}),
        ctx=ctx,
        events_to_record=[],
    )

    payload = json.loads(result.output)
    assert payload["pending_pause"]["pause_type"] == "gate"
    assert payload["workflow_run"]["current_step_name"] == "plan"
    assert payload["result_data"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_get_task_tool_allows_bound_secondary_agent_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ImplicitSystemRegistry:
        def __init__(self, session_factory: object) -> None:
            del session_factory

        async def get(self, agent_id: str) -> AgentDefinition:
            return AgentDefinition(
                agent_id=agent_id,
                owner_email="user@example.com",
                name=agent_id,
                agent_type="secondary" if agent_id.startswith("system:") else "primary",
            )

        async def is_secondary_bound(self, primary_agent_id: str, secondary_agent_id: str) -> bool:
            del primary_agent_id
            return secondary_agent_id.startswith("system:")

    class _TaskSessionManager(_NoopSessionManager):
        def __init__(self) -> None:
            self.session_factory = super().session_factory

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="review", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="system:architect", owner_email="user@example.com", name="Architect"
        ),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            task_id="task-1",
            created_by="user@example.com",
            title="Task",
            description="Desc",
            expected_output=None,
            status="paused",
            priority=0,
            agent_id="agent-1",
            source_type="agent",
            source_ref="conv-1",
            delivery_mode="same_conversation",
            delivery_target=None,
            workflow_id=None,
            workflow_state=None,
            queue_name="default",
            scheduled_for=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            result_summary=None,
            result_data=None,
        )

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.core.agent_registry.AgentRegistry", _ImplicitSystemRegistry)

    async def _list_step_runs(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        del args, kwargs
        return []

    monkeypatch.setattr("cognis.store.queries.list_step_runs_for_task", _list_step_runs)

    result = await agent_loop._handle_task_tool(
        ToolCall(call_id="call-1", name="get_task", arguments={"task_id": "task-1"}),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is False
    assert json.loads(result.output)["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_get_task_tool_allows_primary_agent_to_access_bound_secondary_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoundAgentRegistry:
        def __init__(self, session_factory: object) -> None:
            del session_factory

        async def get(self, agent_id: str) -> AgentDefinition:
            return AgentDefinition(
                agent_id=agent_id,
                owner_email="user@example.com",
                name=agent_id,
                agent_type="secondary" if agent_id.startswith("system:") else "primary",
            )

        async def is_secondary_bound(self, primary_agent_id: str, secondary_agent_id: str) -> bool:
            return primary_agent_id == "agent-1" and secondary_agent_id == "system:architect"

    class _TaskSessionManager(_NoopSessionManager):
        def __init__(self) -> None:
            self.session_factory = super().session_factory

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            task_id="task-1",
            title="Task",
            description="Desc",
            expected_output=None,
            status="paused",
            priority=0,
            created_by="user@example.com",
            agent_id="system:architect",
            source_type="agent",
            source_ref="conv-1",
            delivery_mode="same_conversation",
            delivery_target=None,
            workflow_id=None,
            workflow_state=None,
            queue_name="default",
            scheduled_for=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            result_summary=None,
            result_data=None,
        )

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.core.agent_registry.AgentRegistry", _BoundAgentRegistry)

    async def _list_step_runs(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        del args, kwargs
        return []

    monkeypatch.setattr("cognis.store.queries.list_step_runs_for_task", _list_step_runs)

    result = await agent_loop._handle_task_tool(
        ToolCall(call_id="call-1", name="get_task", arguments={"task_id": "task-1"}),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is False
    assert json.loads(result.output)["task_id"] == "task-1"


def test_tool_runtime_metadata_uses_executor_agent_for_runtime_access() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="implement", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            parent_session_id=None,
            delegation_mode=None,
        ),
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            context=SimpleNamespace(type="task", ref="task-1", platform_data={}),
        ),
        agent=AgentDefinition(
            agent_id="system:implement",
            owner_email="system@example.com",
            name="Implement",
            agent_type="secondary",
            is_system=True,
        ),
        executor_agent=AgentDefinition(
            agent_id="agent-b",
            owner_email="user@example.com",
            name="Agent B",
            agent_type="primary",
        ),
        task_id="task-1",
        policy=CHAT_POLICY,
    )

    metadata = agent_loop._tool_runtime_metadata(ctx)  # noqa: SLF001

    assert metadata["runtime_access"]["agent_id"] == "agent-b"
    assert metadata["runtime_access"]["agent_owner_email"] == "user@example.com"
    assert metadata["runtime_access"]["agent_type"] == "primary"
    assert metadata["runtime_access"]["tool_agent_id"] == "system:implement"


@pytest.mark.asyncio
async def test_get_task_tool_allows_exact_creator_agent_for_delegated_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TaskSessionManager(_NoopSessionManager):
        def __init__(self) -> None:
            self.session_factory = super().session_factory

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-2", owner_email="user@example.com", name="Creator"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            task_id="task-1",
            title="Delegated task",
            description="Desc",
            expected_output=None,
            status="paused",
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            created_by_agent_id="agent-2",
            source_type="agent",
            source_ref="conv-1",
            delivery_mode="same_conversation",
            delivery_target=None,
            workflow_id=None,
            workflow_state=None,
            queue_name="default",
            scheduled_for=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            result_summary=None,
            result_data=None,
        )

    async def _list_step_runs(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        del args, kwargs
        return []

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.store.queries.list_step_runs_for_task", _list_step_runs)

    result = await agent_loop._handle_task_tool(
        ToolCall(call_id="call-1", name="get_task", arguments={"task_id": "task-1"}),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is False
    body = json.loads(result.output)
    assert body["task_id"] == "task-1"
    assert body["created_by_agent_id"] == "agent-2"


@pytest.mark.asyncio
async def test_get_task_tool_still_rejects_unrelated_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnboundAgentRegistry:
        def __init__(self, session_factory: object) -> None:
            del session_factory

        async def get(self, agent_id: str) -> AgentDefinition:
            return AgentDefinition(
                agent_id=agent_id,
                owner_email="user@example.com",
                name=agent_id,
                agent_type="secondary" if agent_id.startswith("system:") else "primary",
            )

        async def is_secondary_bound(self, primary_agent_id: str, secondary_agent_id: str) -> bool:
            del primary_agent_id, secondary_agent_id
            return False

    class _TaskSessionManager(_NoopSessionManager):
        def __init__(self) -> None:
            self.session_factory = super().session_factory

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-2", owner_email="user@example.com", name="Other"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(task_id="task-1", created_by="user@example.com", agent_id="agent-1")

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.core.agent_registry.AgentRegistry", _UnboundAgentRegistry)

    result = await agent_loop._handle_task_tool(
        ToolCall(call_id="call-1", name="get_task", arguments={"task_id": "task-1"}),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is True
    assert json.loads(result.output)["message"] == "Task belongs to a different agent."


@pytest.mark.asyncio
async def test_get_task_tool_rejects_cross_user_task_even_for_system_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ImplicitSystemRegistry:
        def __init__(self, session_factory: object) -> None:
            del session_factory

        async def get(self, agent_id: str) -> AgentDefinition:
            return AgentDefinition(
                agent_id=agent_id,
                owner_email="user@example.com",
                name=agent_id,
                agent_type="secondary" if agent_id.startswith("system:") else "primary",
            )

        async def is_secondary_bound(self, primary_agent_id: str, secondary_agent_id: str) -> bool:
            del primary_agent_id
            return secondary_agent_id.startswith("system:")

    class _TaskSessionManager(_NoopSessionManager):
        def __init__(self) -> None:
            self.session_factory = super().session_factory

    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_TaskSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="review", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(
            agent_id="system:architect", owner_email="user@example.com", name="Architect"
        ),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(task_id="task-1", created_by="other@example.com", agent_id="agent-1")

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.core.agent_registry.AgentRegistry", _ImplicitSystemRegistry)

    result = await agent_loop._handle_task_tool(
        ToolCall(call_id="call-1", name="get_task", arguments={"task_id": "task-1"}),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is True
    assert json.loads(result.output)["message"] == "Task belongs to a different agent."


@pytest.mark.asyncio
async def test_controller_tool_output_store_persists_anchored_outputs() -> None:
    class _Store:
        def __init__(self) -> None:
            self.saved: list[tuple[str, str, list[dict[str, object]] | None]] = []

        async def save(
            self,
            call_id: str,
            output: str,
            *,
            anchors: list[dict[str, object]] | None = None,
        ) -> None:
            self.saved.append((call_id, output, anchors))

    store = _Store()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        tool_output_store=store,
    )

    result = ToolResult(
        output="preview",
        metadata={
            "stored_output": "[[overview]]\nFull output",
            "output_anchors": [
                {
                    "anchor": "overview",
                    "label": "Overview",
                    "kind": "overview",
                    "start_line": 1,
                    "end_line": 2,
                }
            ],
        },
    )

    await agent_loop._save_tool_output_if_available("call-1", result)

    assert store.saved == [
        (
            "call-1",
            "[[overview]]\nFull output",
            [
                {
                    "anchor": "overview",
                    "label": "Overview",
                    "kind": "overview",
                    "start_line": 1,
                    "end_line": 2,
                }
            ],
        )
    ]


@pytest.mark.asyncio
async def test_tool_output_artifact_uses_store_ttl_and_records_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ArtifactStore:
        def __init__(self) -> None:
            self.saved: list[tuple[str, str, str, bytes, str, str | None]] = []
            self.deleted: list[tuple[str, str]] = []

        @staticmethod
        def generate_id(prefix: str) -> str:
            return f"{prefix}_123"

        async def async_save(
            self,
            namespace: str,
            object_id: str,
            filename: str,
            content: bytes,
            content_type: str,
            owner_email: str | None = None,
        ) -> None:
            self.saved.append((namespace, object_id, filename, content, content_type, owner_email))

        async def async_delete_object(self, namespace: str, object_id: str) -> None:
            self.deleted.append((namespace, object_id))

    class _ToolOutputStore:
        ttl_seconds = 3600

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def commit(self) -> None:
            return None

    records: list[dict[str, object]] = []

    async def _create_record(session: object, **kwargs: object) -> object:
        del session
        records.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("cognis.store.queries.create_artifact_record", _create_record)
    artifact_store = _ArtifactStore()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(artifact_store=artifact_store),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        session_factory=lambda: _Session(),
        tool_output_store=_ToolOutputStore(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    artifact_id = await agent_loop._save_tool_output_artifact_if_available(
        ctx,
        "call-1",
        ToolResult(output="preview", metadata={"_raw_output": "full output"}),
    )

    assert artifact_id == "toolout_123"
    assert artifact_store.saved[0][:4] == (
        "tool-outputs",
        "toolout_123",
        "call-1.txt",
        b"full output",
    )
    record = records[0]
    assert record["artifact_id"] == "toolout_123"
    assert record["purpose"] == "tool_output"
    assert record["session_id"] == "sess-1"
    assert record["conversation_id"] == "conv-1"
    assert record["message_role"] == "tool"
    expires_in = (record["expires_at"] - datetime.now(UTC)).total_seconds()
    assert 0 < expires_in <= 3600


@pytest.mark.asyncio
async def test_tool_output_artifact_cleanup_on_record_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[tuple[str, str]] = []

    class _ArtifactStore:
        @staticmethod
        def generate_id(prefix: str) -> str:
            return f"{prefix}_orphan"

        async def async_save(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def async_delete_object(self, namespace: str, object_id: str) -> None:
            deleted.append((namespace, object_id))

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def commit(self) -> None:
            return None

    async def _create_record(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("cognis.store.queries.create_artifact_record", _create_record)
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(artifact_store=_ArtifactStore()),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        session_factory=lambda: _Session(),
        tool_output_store=SimpleNamespace(ttl_seconds=3600),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(session_id="sess-1", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    artifact_id = await agent_loop._save_tool_output_artifact_if_available(
        ctx,
        "call-1",
        ToolResult(output="preview", metadata={"_raw_output": "full output"}),
    )

    assert artifact_id is None
    assert deleted == [("tool-outputs", "toolout_orphan")]


def test_append_tool_result_event_stores_exact_agent_visible_result() -> None:
    output = "HEAD" + ("M" * 60_000) + "TAIL"
    tc = ToolCall(call_id="call-visible", name="bash", arguments={"command": "generate"})
    events: list[SessionEvent] = []

    _append_tool_result_event(
        events,
        tc,
        output,
        False,
        tool_id="builtin:bash",
        output_size=120_000,
        has_full_output=True,
        recovery_call_id="call-visible",
        agent_visible_truncated=True,
    )

    data = events[0].data
    assert data["result"] == output
    assert data["agent_visible"] is True
    assert data["view_kind"] == "model_tool_result"
    assert data["agent_visible_truncated"] is True
    assert data["output_size"] == 120_000


def test_append_tool_events_persist_assistant_phase_from_runtime_metadata() -> None:
    """tool_call/tool_result events must carry the live overlay's phase.

    The canonical projector groups tools under assistant segments by
    assistant_phase_index; without persisting it the same tool item flips
    from phase=K (live) to phase=None (reload) and regroups on refresh.
    """
    from cognis.core.agent_loop import _append_tool_call_event

    tc = ToolCall(call_id="call-phase", name="bash", arguments={"command": "ls"})
    tc.runtime_metadata["assistant_phase_index"] = 2
    tc.runtime_metadata["turn_cycle_index"] = 1
    events: list[SessionEvent] = []

    _append_tool_call_event(events, tc, "builtin:bash")
    _append_tool_result_event(events, tc, "ok", False, tool_id="builtin:bash")

    assert events[0].type == "tool_call"
    assert events[0].data["assistant_phase_index"] == 2
    assert events[0].data["turn_cycle_index"] == 1
    assert events[1].type == "tool_result"
    assert events[1].data["assistant_phase_index"] == 2


def test_append_tool_events_omit_phase_when_not_stamped() -> None:
    from cognis.core.agent_loop import _append_tool_call_event

    tc = ToolCall(call_id="call-nophase", name="bash", arguments={"command": "ls"})
    events: list[SessionEvent] = []

    _append_tool_call_event(events, tc, "builtin:bash")
    _append_tool_result_event(events, tc, "ok", False, tool_id="builtin:bash")

    assert "assistant_phase_index" not in events[0].data
    assert "assistant_phase_index" not in events[1].data


@pytest.mark.asyncio
async def test_finalize_regular_tool_result_stores_agent_visible_output_and_artifact_id() -> None:
    class _Store:
        ttl_seconds = 3600

        def __init__(self) -> None:
            self.saved: list[tuple[str, str]] = []

        async def save(
            self,
            call_id: str,
            output: str,
            *,
            anchors: list[dict[str, object]] | None = None,
        ) -> None:
            del anchors
            self.saved.append((call_id, output))

    class _ArtifactStore:
        @staticmethod
        def generate_id(prefix: str) -> str:
            return f"{prefix}_final"

        async def async_save(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def async_delete_object(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def commit(self) -> None:
            return None

    async def _create_record(session: object, **kwargs: object) -> object:
        del session, kwargs
        return SimpleNamespace()

    import cognis.store.queries as queries

    original_create_record = queries.create_artifact_record
    queries.create_artifact_record = _create_record
    recorded_events: list[SessionEvent] = []

    class _SessionCache(_NoopSessionCache):
        async def append_recorded_events(
            self,
            session: object,
            events: list[SessionEvent],
            append_result: EventAppendResult,
        ) -> None:
            del session, append_result
            recorded_events.extend(events)

    try:
        store = _Store()
        agent_loop = AgentLoop(
            providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
            session_manager=_NoopSessionManager(),
            session_cache=_SessionCache(),
            context_assembler=_FakeContextAssembler(),
            compaction_strategy=SimpleNamespace(),
            tool_router=SimpleNamespace(artifact_store=_ArtifactStore()),
            remember_queue=_NoopRememberQueue(),
            event_bus=_NoopEventBus(),
            session_lock=SessionLock(),
            pause_waiter=PauseWaiter(),
            session_factory=lambda: _Session(),
            tool_output_store=store,
        )
        ctx = StepContext(
            step_definition=StepDefinition(name="direct", type="run", prompt=""),
            session=SimpleNamespace(
                session_id="sess-1",
                intaris_session_id="sess-1",
                user_email="user@example.com",
                agent_id="agent-1",
            ),
            conversation=SimpleNamespace(conversation_id="conv-1"),
            agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
            policy=CHAT_POLICY,
        )
        raw_output = "RAW" * 30_000
        result = ToolResult(
            output="VISIBLE" * 20_000,
            metadata={"_raw_output": raw_output, "original_size": len(raw_output)},
        )
        events: list[SessionEvent] = []
        messages: list[dict[str, object]] = []

        await agent_loop._finalize_regular_tool_result(
            ctx,
            tc=ToolCall(call_id="call-final", name="bash", arguments={"command": "huge"}),
            tool_id="builtin:bash",
            result=result,
            events_to_record=events,
            messages=messages,
            collected_attachments=[],
            pending_assistant_attachments=[],
            promoted_tool_ids=set(),
            activated_tool_ids=set(),
            on_token=None,
            on_tool_result=None,
        )
    finally:
        queries.create_artifact_record = original_create_record

    event_data = recorded_events[-1].data
    assert event_data["result"] == messages[-1]["content"]
    assert event_data["result"] != raw_output
    assert "middle truncated" in event_data["result"]
    assert event_data["agent_visible"] is True
    assert event_data["agent_visible_truncated"] is True
    assert event_data["tool_output_artifact_id"] == "toolout_final"
    assert messages[-1]["_tool_output_artifact_id"] == "toolout_final"
    assert store.saved == [("call-final", raw_output)]


@pytest.mark.asyncio
async def test_tool_output_helper_results_recover_via_helper_call_id() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )
    tc = ToolCall(
        call_id="helper-call",
        name="read_tool_output",
        arguments={"call_id": "source-call", "offset": 1, "limit": 40},
    )
    result = ToolResult(
        output='<tool_result name="read_tool_output" trust="untrusted">\n1: line\n</tool_result>',
        metadata={
            "_raw_output": "1: line",
            "original_size": 7,
            "source_call_id": "source-call",
        },
    )
    events: list[SessionEvent] = []
    messages: list[dict[str, object]] = []

    await agent_loop._finalize_regular_tool_result(
        ctx,
        tc=tc,
        tool_id="builtin:read_tool_output",
        result=result,
        events_to_record=events,
        messages=messages,
        collected_attachments=[],
        pending_assistant_attachments=[],
        promoted_tool_ids=set(),
        activated_tool_ids=set(),
        on_token=None,
        on_tool_result=None,
    )

    assert messages[-1]["tool_call_id"] == "helper-call"
    assert messages[-1]["_recovery_call_id"] == "helper-call"
    assert messages[-1]["_source_call_id"] == "source-call"


@pytest.mark.asyncio
async def test_finalize_regular_tool_result_records_file_diffs() -> None:
    class _CapturingGuardrails(_NoopGuardrails):
        def __init__(self) -> None:
            self.events: list[SessionEvent] = []

        async def record_events(self, **kwargs: object) -> EventAppendResult:
            self.events.extend(kwargs.get("events", []))  # type: ignore[arg-type]
            return EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1)

    guardrails = _CapturingGuardrails()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=guardrails),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )
    tc = ToolCall(call_id="edit-call", name="edit", arguments={"file_path": "example.py"})
    file_diffs = [
        {
            "path": "example.py",
            "diff": "--- example.py\n+++ example.py\n@@ -1 +1 @@\n-old\n+new\n",
        }
    ]
    result = ToolResult(output="Replaced 1 occurrence", metadata={"file_diffs": file_diffs})
    events: list[SessionEvent] = []
    observed: list[object] = []

    async def on_tool_result(*args: object) -> None:
        observed.append(args)

    await agent_loop._finalize_regular_tool_result(
        ctx,
        tc=tc,
        tool_id="builtin:edit",
        result=result,
        events_to_record=events,
        messages=[],
        collected_attachments=[],
        pending_assistant_attachments=[],
        promoted_tool_ids=set(),
        activated_tool_ids=set(),
        on_token=None,
        on_tool_result=on_tool_result,
    )

    assert guardrails.events[-1].data["file_diffs"] == file_diffs
    assert observed[0][-2] == file_diffs


@pytest.mark.asyncio
async def test_tool_result_callback_supports_legacy_six_args_plus_cycle_keyword() -> None:
    observed: list[tuple[tuple[object, ...], int | None]] = []

    async def on_tool_result(
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, Any] | None,
        *,
        turn_cycle_index: int | None = None,
    ) -> None:
        observed.append(
            (
                (call_id, tool_name, result, is_error, duration_ms, evaluation),
                turn_cycle_index,
            )
        )

    await _emit_tool_result_callback(
        on_tool_result,
        call_id="call_1",
        tool_name="read",
        result="ok",
        is_error=False,
        duration_ms=12,
        evaluation=None,
        attachments=[{"artifact_id": "att_1"}],
        file_diffs=[{"path": "example.py"}],
        presentation={"output_size": 2},
        turn_cycle_index=5,
    )

    assert observed == [(("call_1", "read", "ok", False, 12, None), 5)]


@pytest.mark.asyncio
async def test_finalize_regular_tool_result_excludes_inspection_only_from_collected() -> None:
    """native_inspection_only attachments must not reach collected_attachments."""
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )
    # artifact_read native path: attachment marked inspection-only
    inspection_attachment = {
        "artifact_id": "att-img-1",
        "kind": "image",
        "mime_type": "image/png",
        "filename": "photo.png",
        "url": "https://cognis.example/api/v1/artifacts/content/attachments/att-img-1/photo.png",
        "native_inspection_only": True,
    }
    # A genuinely new generated artifact (no inspection flag)
    generated_attachment = {
        "artifact_id": "att-gen-1",
        "kind": "image",
        "mime_type": "image/png",
        "filename": "generated.png",
        "url": "https://cognis.example/api/v1/artifacts/content/attachments/att-gen-1/generated.png",
    }
    result = ToolResult(
        output="Prepared artifact for inspection.",
        attachments=[inspection_attachment, generated_attachment],
    )
    collected: list[dict] = []
    pending: list[dict] = []
    messages: list[dict] = []

    await agent_loop._finalize_regular_tool_result(
        ctx,
        tc=ToolCall(
            call_id="read-call", name="artifact_read", arguments={"artifact_id": "att-img-1"}
        ),
        tool_id="builtin:artifact_read",
        result=result,
        events_to_record=[],
        messages=messages,
        collected_attachments=collected,
        pending_assistant_attachments=pending,
        promoted_tool_ids=set(),
        activated_tool_ids=set(),
        on_token=None,
        on_tool_result=None,
    )

    # Inspection-only attachment must NOT be in collected (channel delivery)
    collected_ids = {a.get("artifact_id") for a in collected}
    assert "att-img-1" not in collected_ids
    # Generated attachment IS collected and promoted
    assert "att-gen-1" in collected_ids
    pending_ids = {a.get("artifact_id") for a in pending}
    assert "att-gen-1" in pending_ids
    assert "att-img-1" not in pending_ids
    # The inspection-only attachment artifact_id must still appear in the messages
    # appended by _build_tool_attachment_context so the next LLM cycle can see it.
    all_message_content = " ".join(str(m.get("content", "")) for m in messages)
    assert "att-img-1" in all_message_content


@pytest.mark.asyncio
async def test_get_task_step_output_returns_anchored_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(task_id="task-1", created_by="user@example.com", agent_id="agent-1")

    async def _list_step_runs(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        del args, kwargs
        long_content = "Detailed implementation plan. " * 80
        return [
            SimpleNamespace(
                step_run_id="sr-old",
                step_name="plan",
                status="approved",
                attempt=2,
                session_id="sess-step-old",
                conversation_id="conv-task-1",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime(2026, 1, 1, 0, 4, tzinfo=UTC),
                output={
                    "summary": "Old plan",
                    "content": "Old duplicate attempt content.",
                    "claims": ["Old claim"],
                    "outputs": {"milestones": 2},
                },
                evaluation={"decision": "approved", "feedback": None},
                todos=[{"content": "Old todo", "status": "completed"}],
            ),
            SimpleNamespace(
                step_run_id="sr-1",
                step_name="plan",
                status="approved",
                attempt=2,
                session_id="sess-step-1",
                conversation_id="conv-task-1",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
                output={
                    "summary": "Plan finished",
                    "content": long_content,
                    "claims": ["Reviewed requirements", "Defined milestones"],
                    "outputs": {"milestones": 3},
                },
                evaluation={"decision": "approved", "feedback": None},
                todos=[{"content": "Outline milestones", "status": "completed"}],
            ),
        ]

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.store.queries.list_step_runs_for_task", _list_step_runs)

    result = await agent_loop._handle_task_tool(
        ToolCall(
            call_id="call-1",
            name="get_task_step_output",
            arguments={"task_id": "task-1", "step_name": "plan", "attempt": 2},
        ),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["step_run_id"] == "sr-1"
    assert payload["content"].endswith("[snippet truncated]")
    assert "content" in payload["available_anchors"]
    assert result.metadata is not None
    assert "[[content]]" in str(result.metadata["stored_output"])
    assert "Detailed implementation plan." in str(result.metadata["stored_output"])
    assert any(anchor["anchor"] == "content" for anchor in result.metadata["output_anchors"])


@pytest.mark.asyncio
async def test_get_task_step_logs_returns_anchored_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(task_id="task-1", created_by="user@example.com", agent_id="agent-1")

    async def _list_step_runs(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        del args, kwargs
        return [
            SimpleNamespace(
                step_run_id="sr-1",
                step_name="plan",
                status="approved",
                attempt=1,
                session_id="sess-step-1",
                intaris_session_id=None,
                output={"summary": "Old attempt"},
            ),
            SimpleNamespace(
                step_run_id="sr-2",
                step_name="plan",
                status="approved",
                attempt=2,
                session_id=None,
                intaris_session_id="intaris-step-2",
                output={"summary": "Latest attempt"},
            ),
        ]

    async def _read_events(**kwargs: object) -> SimpleNamespace:
        assert kwargs["session_id"] == "intaris-step-2"
        assert kwargs["after_seq"] == 0
        return SimpleNamespace(
            events=[
                {
                    "seq": 1,
                    "type": "assistant_message",
                    "data": {"content": "Planning approach"},
                    "ts": "2026-01-01T00:00:00Z",
                },
                {
                    "seq": 2,
                    "type": "tool_call",
                    "data": {
                        "name": "bash",
                        "call_id": "tool-call-1",
                        "arguments": '{"command": "git status"}',
                    },
                    "ts": "2026-01-01T00:00:01Z",
                },
                {
                    "seq": 3,
                    "type": "tool_result",
                    "data": {
                        "name": "bash",
                        "call_id": "tool-call-1",
                        "is_error": False,
                        "duration_ms": 120,
                        "result": "On branch main",
                        "has_full_output": True,
                    },
                    "ts": "2026-01-01T00:00:02Z",
                },
            ],
            last_seq=3,
            has_more=True,
            missing_stream_fallback_used=False,
        )

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.store.queries.list_step_runs_for_task", _list_step_runs)
    monkeypatch.setattr(agent_loop.providers.guardrails, "read_events", _read_events)

    result = await agent_loop._handle_task_tool(
        ToolCall(
            call_id="call-1",
            name="get_task_step_logs",
            arguments={"task_id": "task-1", "step_name": "plan", "attempt": 2},
        ),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is False
    assert "[[overview]]" in result.output
    assert result.metadata is not None
    stored_output = str(result.metadata["stored_output"])
    assert "[[tool_call:1]]" in stored_output
    assert "tool-call-1" in stored_output
    assert any(anchor["anchor"] == "tool_result:1" for anchor in result.metadata["output_anchors"])


@pytest.mark.asyncio
async def test_get_task_step_logs_returns_structured_error_on_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(task_id="task-1", created_by="user@example.com", agent_id="agent-1")

    async def _list_step_runs(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        del args, kwargs
        return [
            SimpleNamespace(
                step_run_id="sr-2",
                step_name="plan",
                status="approved",
                attempt=2,
                session_id=None,
                intaris_session_id="intaris-step-2",
                output={"summary": "Latest attempt"},
            )
        ]

    async def _read_events(**kwargs: object) -> SimpleNamespace:
        del kwargs
        raise RuntimeError("Intaris unavailable")

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr("cognis.store.queries.list_step_runs_for_task", _list_step_runs)
    monkeypatch.setattr(agent_loop.providers.guardrails, "read_events", _read_events)

    result = await agent_loop._handle_task_tool(
        ToolCall(
            call_id="call-1",
            name="get_task_step_logs",
            arguments={"task_id": "task-1", "step_name": "plan", "attempt": 2},
        ),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is True
    assert "Failed to read step logs" in json.loads(result.output)["error"]


@pytest.mark.asyncio
async def test_workflow_tools_are_main_chat_only_in_delegate_sync_mode() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="plan", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        orchestration_mode=OrchestrationMode.DELEGATE_SYNC_ONLY,
    )

    result = await agent_loop._handle_orchestration_tool(
        ToolCall(call_id="call-1", name="list_workflows", arguments={}),
        ctx=ctx,
        events_to_record=[],
    )

    assert result.is_error is True
    assert "Only 'delegate' (sync) is available" in json.loads(result.output)["message"]


@pytest.mark.asyncio
async def test_create_workflow_tool_returns_created_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=SimpleNamespace(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-1", intaris_session_id="sess-1", user_email="user@example.com"
        ),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _create(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            workflow_id="wf-1",
            name="Workflow",
            description="desc",
            version=1,
            definition={"criteria": "", "tags": [], "interaction": {}, "defaults": {}, "steps": []},
            is_system=False,
            owner_email="user@example.com",
        )

    monkeypatch.setattr("cognis.core.workflow_management.create_user_workflow", _create)

    result = await agent_loop._handle_workflow_tool(
        ToolCall(
            call_id="call-1", name="create_workflow", arguments={"name": "Workflow", "steps": []}
        ),
        ctx=ctx,
    )

    payload = json.loads(result.output)
    assert payload["status"] == "created"
    assert payload["workflow"]["workflow_id"] == "wf-1"


@pytest.mark.asyncio
async def test_step_complete_uses_only_final_assistant_message_for_content() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_FinalAssistantContentLLM(), guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(
            name="briefing",
            type="run",
            prompt="Produce the final briefing.",
            require_deliverable=False,
        ),
        session=SimpleNamespace(
            session_id="sess-1",
            intaris_session_id="sess-1",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-1", title=None, title_source="unset"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        user_message="Create the daily briefing.",
        user_attachments=[],
        system_initiated=False,
    )

    output = await agent_loop.run_step(ctx)

    assert output is not None
    assert output.content == "Final clean briefing text."


def test_step_prompt_respects_expected_output_without_allowing_silent_completion() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=SimpleNamespace(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="summary", type="run", prompt="Write the summary."),
        session=SimpleNamespace(session_id="sess-1", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        step_run_id="sr-1",
        task_title="Daily summary",
        task_description="Summarize today.",
        task_expected_output="No assistant message.",
    )

    prompt = agent_loop._build_step_prompt(ctx)

    assert "## Workflow-Level Expected Output" in prompt
    assert "This describes the final task result" in prompt
    assert "## Current Step" in prompt
    assert "## Current Step Boundaries" in prompt
    assert "write_deliverable with the canonical user-facing artifact" in prompt


def test_plan_step_prompt_has_read_only_boundaries() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=SimpleNamespace(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="plan", type="run", prompt="Create a plan."),
        session=SimpleNamespace(session_id="sess-1", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        step_run_id="sr-1",
        task_title="Feature",
        task_description="Implement the feature and open a PR.",
    )

    prompt = agent_loop._build_step_prompt(ctx)

    assert "This is a read-only planning/review step" in prompt
    assert "do not edit files" in prompt
    assert "Complete only this current step" in prompt


def test_workflow_step_reminder_overrides_task_and_skill_instructions() -> None:
    agent_loop = AgentLoop(
        providers=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=SimpleNamespace(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="plan", type="run", prompt="Create a plan."),
        session=SimpleNamespace(session_id="sess-1", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=WORKFLOW_POLICY,
        step_run_id="sr-1",
    )

    reminder = agent_loop._build_workflow_step_reminder(ctx)

    assert reminder is not None
    assert reminder["_workflow_step_reminder"] is True
    content = str(reminder["content"])
    assert "overrides task-level finishing instructions and loaded skill instructions" in content
    assert "call write_deliverable" in content


def _post_deliverable_ctx() -> StepContext:
    return StepContext(
        step_definition=StepDefinition(name="plan", type="run", prompt="Plan."),
        session=SimpleNamespace(session_id="sess-1", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="A"),
        policy=WORKFLOW_POLICY,
        step_run_id="sr-1",
    )


def _post_deliverable_loop() -> AgentLoop:
    return AgentLoop(
        providers=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=SimpleNamespace(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )


def test_post_deliverable_reminder_emits_when_armed_and_todos_terminal() -> None:
    agent_loop = _post_deliverable_loop()
    ctx = _post_deliverable_ctx()
    ctx.post_deliverable_pending = True
    ctx.todos = [{"content": "do x", "status": "completed"}]
    messages: list[dict] = []

    agent_loop._maybe_inject_post_deliverable_reminder(ctx, messages)

    assert len(messages) == 1
    msg = messages[0]
    assert msg["role"] == "system"
    assert msg.get("_post_deliverable_reminder") is True
    assert "step_complete" in msg["content"]
    assert ctx.post_deliverable_reminders_sent == 1


def test_post_deliverable_reminder_skipped_when_todos_pending() -> None:
    agent_loop = _post_deliverable_loop()
    ctx = _post_deliverable_ctx()
    ctx.post_deliverable_pending = True
    ctx.todos = [{"content": "still working", "status": "in_progress"}]
    messages: list[dict] = []

    agent_loop._maybe_inject_post_deliverable_reminder(ctx, messages)

    assert messages == []
    # The flag should remain armed for the cycle when todos eventually terminate.
    assert ctx.post_deliverable_pending is True
    assert ctx.post_deliverable_reminders_sent == 0


def test_post_deliverable_reminder_caps_at_two() -> None:
    agent_loop = _post_deliverable_loop()
    ctx = _post_deliverable_ctx()
    ctx.post_deliverable_pending = True
    ctx.todos = [{"content": "done", "status": "completed"}]
    messages: list[dict] = []

    for _ in range(5):
        agent_loop._maybe_inject_post_deliverable_reminder(ctx, messages)

    assert len(messages) == 2
    assert ctx.post_deliverable_reminders_sent == 2


def test_post_deliverable_reminder_disarms_when_step_complete_not_required() -> None:
    agent_loop = _post_deliverable_loop()
    ctx = _post_deliverable_ctx()
    ctx.policy = CHAT_POLICY  # require_step_complete=False
    ctx.post_deliverable_pending = True
    messages: list[dict] = []

    agent_loop._maybe_inject_post_deliverable_reminder(ctx, messages)

    assert messages == []
    assert ctx.post_deliverable_pending is False


@pytest.mark.asyncio
async def test_parallel_delegate_batches_run_concurrently() -> None:
    """Multiple consecutive ``delegate`` calls in one turn must fan out via gather."""

    started: list[str] = []
    in_flight: list[str] = []
    peak_concurrent = 0

    agent_loop = _post_deliverable_loop()

    async def fake_handle_orchestration_tool(
        tc: ToolCall,
        *,
        ctx,
        events_to_record,
        on_token=None,
        on_tool_call=None,
        on_tool_result=None,
    ) -> ToolResult:
        nonlocal peak_concurrent
        in_flight.append(tc.call_id)
        peak_concurrent = max(peak_concurrent, len(in_flight))
        started.append(tc.call_id)
        # Yield control so siblings actually overlap when fanned out.
        await asyncio.sleep(0.01)
        in_flight.remove(tc.call_id)
        return ToolResult(output=f"done-{tc.call_id}")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    delegates = [
        ToolCall(call_id=f"d{i}", name="delegate", arguments={"task": f"explore {i}"})
        for i in range(3)
    ]
    events: list = []

    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        delegates,
        events_to_record=events,
    )

    # Every delegate index has a precomputed result.
    assert {0, 1, 2} == set(results.keys())
    for idx, call_id in enumerate(("d0", "d1", "d2")):
        assert results[idx].output == f"done-{call_id}"
    # Calls actually overlapped — sequential execution would peak at 1.
    assert peak_concurrent >= 2
    # Tool-call events are recorded in input order before fanning out so
    # the in-flight stream matches the model's emitted order.
    tool_call_event_ids = [
        getattr(event, "data", {}).get("call_id")
        for event in events
        if getattr(event, "type", None) == "tool_call"
    ]
    assert tool_call_event_ids == ["d0", "d1", "d2"]


@pytest.mark.asyncio
async def test_parallel_delegate_batches_skip_single_call() -> None:
    """A single delegate call should bypass the gather path."""

    agent_loop = _post_deliverable_loop()
    invoked = []

    async def fake_handle_orchestration_tool(*args, **kwargs):
        invoked.append(args)
        return ToolResult(output="ok")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        [ToolCall(call_id="d0", name="delegate", arguments={"task": "solo"})],
        events_to_record=[],
    )

    assert results == {}
    assert invoked == []  # Sequential path handles the lone delegate.


@pytest.mark.asyncio
async def test_parallel_managed_conversation_batches_run_independent_targets_concurrently() -> None:
    """Managed conversation calls targeting different conversations should fan out."""

    agent_loop = _post_deliverable_loop()
    in_flight: list[str] = []
    peak_concurrent = 0

    async def fake_handle_orchestration_tool(
        tc: ToolCall,
        *,
        ctx,
        events_to_record,
        on_token=None,
        on_tool_call=None,
        on_tool_result=None,
    ) -> ToolResult:
        nonlocal peak_concurrent
        in_flight.append(tc.call_id)
        peak_concurrent = max(peak_concurrent, len(in_flight))
        await asyncio.sleep(0.01)
        in_flight.remove(tc.call_id)
        return ToolResult(output=f"done-{tc.call_id}")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    calls = [
        ToolCall(
            call_id="m0",
            name="agent_conversation_send",
            arguments={"conversation_id": "conv-a", "message": "work a"},
        ),
        ToolCall(
            call_id="m1",
            name="agent_conversation_wait",
            arguments={"conversation_id": "conv-b"},
        ),
        ToolCall(
            call_id="m2",
            name="agent_conversation_create",
            arguments={"agent_id": "laforge", "title": "Work C", "initial_message": "work c"},
        ),
    ]
    events: list = []

    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        calls,
        events_to_record=events,
    )

    assert {0, 1, 2} == set(results.keys())
    assert [results[index].output for index in range(3)] == ["done-m0", "done-m1", "done-m2"]
    assert peak_concurrent >= 2
    assert [
        getattr(event, "data", {}).get("call_id")
        for event in events
        if getattr(event, "type", None) == "tool_call"
    ] == ["m0", "m1", "m2"]


@pytest.mark.asyncio
async def test_parallel_managed_conversation_batches_do_not_batch_same_target() -> None:
    """Calls targeting the same managed conversation must remain sequential."""

    agent_loop = _post_deliverable_loop()
    invoked: list[str] = []

    async def fake_handle_orchestration_tool(*args, **kwargs):
        invoked.append(args[0].call_id)
        return ToolResult(output="unexpected")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    calls = [
        ToolCall(
            call_id="m0",
            name="agent_conversation_send",
            arguments={"conversation_id": "conv-a", "message": "first"},
        ),
        ToolCall(
            call_id="m1",
            name="agent_conversation_wait",
            arguments={"conversation_id": "conv-a"},
        ),
    ]

    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        calls,
        events_to_record=[],
    )

    assert results == {}
    assert invoked == []


@pytest.mark.asyncio
async def test_parallel_orchestration_batches_do_not_batch_managed_list_with_mutation() -> None:
    """Aggregate managed conversation list reads should preserve source-order visibility."""

    agent_loop = _post_deliverable_loop()
    invoked: list[str] = []

    async def fake_handle_orchestration_tool(*args, **kwargs):
        invoked.append(args[0].call_id)
        return ToolResult(output="unexpected")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    calls = [
        ToolCall(
            call_id="m0",
            name="agent_conversation_create",
            arguments={"agent_id": "laforge", "title": "Work A", "initial_message": "work a"},
        ),
        ToolCall(call_id="m1", name="agent_conversation_list", arguments={}),
    ]

    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        calls,
        events_to_record=[],
    )

    assert results == {}
    assert invoked == []


@pytest.mark.asyncio
async def test_parallel_orchestration_batches_do_not_batch_duplicate_managed_create() -> None:
    """Duplicate creates must remain sequential so the loop guard can reject repeats."""

    agent_loop = _post_deliverable_loop()
    invoked: list[str] = []

    async def fake_handle_orchestration_tool(*args, **kwargs):
        invoked.append(args[0].call_id)
        return ToolResult(output="unexpected")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    create_args = {"agent_id": "laforge", "title": "Work A", "initial_message": "work a"}
    calls = [
        ToolCall(call_id="m0", name="agent_conversation_create", arguments=dict(create_args)),
        ToolCall(call_id="m1", name="agent_conversation_create", arguments=dict(create_args)),
    ]

    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        calls,
        events_to_record=[],
    )

    assert results == {}
    assert invoked == []


@pytest.mark.asyncio
async def test_parallel_orchestration_batches_stop_after_unbatched_prefix_call() -> None:
    """Later batches must not execute before earlier sequential/controller tools."""

    agent_loop = _post_deliverable_loop()
    invoked: list[str] = []

    async def fake_handle_orchestration_tool(*args, **kwargs):
        invoked.append(args[0].call_id)
        return ToolResult(output="unexpected")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    calls = [
        ToolCall(call_id="t0", name="todo_write", arguments={"todos": []}),
        ToolCall(
            call_id="m0",
            name="agent_conversation_send",
            arguments={"conversation_id": "conv-a", "message": "work a"},
        ),
        ToolCall(
            call_id="m1",
            name="agent_conversation_send",
            arguments={"conversation_id": "conv-b", "message": "work b"},
        ),
    ]

    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        calls,
        events_to_record=[],
    )

    assert results == {}
    assert invoked == []


@pytest.mark.asyncio
async def test_parallel_orchestration_batches_preserve_batch_event_order() -> None:
    """Mixed delegate and managed-conversation batches should be recorded in source order."""

    agent_loop = _post_deliverable_loop()

    async def fake_handle_orchestration_tool(
        tc: ToolCall,
        *,
        ctx,
        events_to_record,
        on_token=None,
        on_tool_call=None,
        on_tool_result=None,
    ) -> ToolResult:
        await asyncio.sleep(0.01)
        return ToolResult(output=f"done-{tc.call_id}")

    async def noop_flush(*args, **kwargs):
        return None

    agent_loop._handle_orchestration_tool = fake_handle_orchestration_tool  # type: ignore[assignment]
    agent_loop._flush_events_incremental = noop_flush  # type: ignore[assignment]

    ctx = _post_deliverable_ctx()
    ctx.tool_registry = ToolRegistry()
    calls = [
        ToolCall(
            call_id="m0",
            name="agent_conversation_send",
            arguments={"conversation_id": "conv-a", "message": "work a"},
        ),
        ToolCall(
            call_id="m1",
            name="agent_conversation_send",
            arguments={"conversation_id": "conv-b", "message": "work b"},
        ),
        ToolCall(call_id="d0", name="delegate", arguments={"task": "explore a"}),
        ToolCall(call_id="d1", name="delegate", arguments={"task": "explore b"}),
    ]
    events: list = []

    results = await agent_loop._precompute_parallel_orchestration_batches(
        ctx,
        calls,
        events_to_record=events,
    )

    assert {0, 1, 2, 3} == set(results.keys())
    assert [
        getattr(event, "data", {}).get("call_id")
        for event in events
        if getattr(event, "type", None) == "tool_call"
    ] == ["m0", "m1", "d0", "d1"]
