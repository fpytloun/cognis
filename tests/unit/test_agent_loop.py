"""Tests for the agent loop engine."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    DELEGATION_POLICY,
    SECONDARY_POLICY,
    WORKFLOW_POLICY,
    AgentLoop,
    PauseResolution,
    PauseWaiter,
    PendingPause,
    PendingToolCallState,
    SessionLock,
    StepContext,
    StreamAccumulator,
    _bounded_tool_arguments,
    _controller_builtin_enabled,
    _filter_model_inventory_tools,
    _iterate_llm_stream_with_idle_timeout,
    _PreparedRegularToolCall,
    _validate_step_completion_notification,
)
from cognis.core.project_context import ProjectContextEntry
from cognis.core.runtime import ResolvedStepRuntime, build_local_executor_environment
from cognis.core.session_cache import SessionCache
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import EventAppendResult, ReasoningReportResult, SessionEvent
from cognis.models.tool import (
    Permission,
    ToolCall,
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
from cognis.providers.llm.litellm import OpenAIToolSearchFallbackRequired
from cognis.tools.builtin.orchestration import OrchestrationMode
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry

# ---------------------------------------------------------------------------
# StreamAccumulator tests
# ---------------------------------------------------------------------------


def test_stream_accumulator_collects_text() -> None:
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"content": "Hello"}}]})
    acc.feed({"choices": [{"delta": {"content": " world"}}]})

    assert acc.get_content() == "Hello world"
    assert not acc.has_tool_calls()


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
    assert "... (truncated," in str(bounded["_preview"])


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

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return _test_model_info()

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) >= 2:
            raise _ReminderStop()
        if False:
            yield {}
        return


class _EmptyThenTextDirectLLM:
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


class _FakeContextAssembler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def assemble(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        role = kwargs.get("user_message_role", "user")
        user_message = kwargs.get("user_message", "")
        return SimpleNamespace(
            messages=[{"role": role, "content": user_message}],
            resolved_model="test-model",
            cache_breakpoint_index=None,
            prompt_tokens=0,
            static_tokens=0,
            dynamic_tokens=0,
            max_context_tokens=0,
            recommend_compaction=False,
        )


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


class _NoopRememberQueue:
    async def enqueue(self, _: object) -> None:
        return None


class _NoopEventBus:
    async def publish(self, _: object) -> None:
        return None


class _NoopGuardrails:
    async def record_events(self, **_: object) -> EventAppendResult:
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


class _NoopSessionManager:
    def session_factory(self) -> object:
        class _Dummy:
            async def __aenter__(self) -> SimpleNamespace:
                return SimpleNamespace()

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        return _Dummy()


class _NoopSessionCache:
    def __init__(self) -> None:
        self.tool_runtime_info: dict[str, object] | None = None
        self.activated_skill_tool_ids: set[str] = set()
        self.skill_tool_classifications: dict[str, list[str]] = {}

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
    assert isinstance(output.error, str)
    assert len(fake_llm.calls) >= 2
    return fake_llm.calls


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

    async def execute_tool(tool_call: ToolCall, *_: object) -> ToolResult:
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

    async def execute_tool(tool_call: ToolCall, *_: object) -> ToolResult:
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

    async def execute_tool(tool_call: ToolCall, *_: object) -> ToolResult:
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

    async def execute_tool(tool_call: ToolCall, *_: object) -> ToolResult:
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
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
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
    assert consumed_reasons == ["after_assistant_message"]
    assert len(fake_llm.calls) >= 2
    assert fake_llm.calls[1][-1]["role"] == "user"
    assert fake_llm.calls[1][-1]["content"] == "Also include the deployment notes."


@pytest.mark.asyncio
async def test_step_complete_reprompt_is_system_message() -> None:
    ctx = StepContext(
        step_definition=StepDefinition(name="step-a", type="run", prompt="", allow_questions=False),
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


def test_step_request_input_schema_only_exposed_for_question_enabled_steps() -> None:
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
        tool["function"]["name"] == "step_request_input"
        for tool in loop._build_controller_tool_schemas(question_ctx)
    )
    assert not any(
        tool["function"]["name"] == "step_request_input"
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
        async def generate(
            self, messages: list[dict[str, object]], **_: object
        ) -> dict[str, object]:
            del messages
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
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_ClassifierLLM(), guardrails=_NoopGuardrails()),
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
async def test_llm_stream_idle_timeout_after_todo_write_settles_turn() -> None:
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

            await asyncio.sleep(30)
            if False:  # pragma: no cover - keep this an async generator
                yield {}

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

    output = await agent_loop.run_step(ctx, on_token=_on_token)

    assert output is None
    assert fake_llm.calls == 3
    assert "model did not produce output" in "".join(tokens)
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
    assert snapshot.available_prompt_tokens == 218_750
    assert snapshot.threshold_prompt_tokens == 207_812
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
    assert "Claims:" in text
    assert "Deliverable:\nDetailed plan body" in text
    assert "Structured outputs:" in text


def test_format_prior_step_outputs_summary_includes_deliverable_content() -> None:
    agent_loop = object.__new__(AgentLoop)
    workflow_state = WorkflowState(
        step_outputs={
            "implement": StepOutput(
                summary="Implemented change",
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
    assert "Deliverable:\nLong implementation details" in text
    assert "Structured outputs:" in text
    assert "Claims:" not in text


def test_format_prior_step_outputs_last_includes_deliverable_content() -> None:
    agent_loop = object.__new__(AgentLoop)
    workflow_state = WorkflowState(
        step_outputs={
            "plan": StepOutput(
                summary="Plan ready",
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
async def test_respond_task_input_tool_returns_error_without_response() -> None:
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
    assert json.loads(result.output)["error"] == "response is required."


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
    assert observed[0][-1] == file_diffs


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

    results = await agent_loop._precompute_parallel_delegate_batches(
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
    results = await agent_loop._precompute_parallel_delegate_batches(
        ctx,
        [ToolCall(call_id="d0", name="delegate", arguments={"task": "solo"})],
        events_to_record=[],
    )

    assert results == {}
    assert invoked == []  # Sequential path handles the lone delegate.
