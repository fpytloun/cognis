"""Tests for the agent loop engine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    SECONDARY_POLICY,
    WORKFLOW_POLICY,
    AgentLoop,
    PauseResolution,
    PauseWaiter,
    PendingPause,
    SessionLock,
    StepContext,
    StreamAccumulator,
    _validate_step_completion_notification,
    _controller_builtin_enabled,
    _filter_model_inventory_tools,
)
from cognis.core.runtime import ResolvedStepRuntime, build_local_executor_environment
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import EventAppendResult, ReasoningReportResult
from cognis.models.tool import Permission, ToolCall, ToolDefinition, ToolSource
from cognis.models.workflow import (
    CompletionDeliveryPolicy,
    StepDefinition,
    StepInputConfig,
    StepOutput,
    WorkflowState,
)
from cognis.tools.builtin.orchestration import OrchestrationMode
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL

# ---------------------------------------------------------------------------
# StreamAccumulator tests
# ---------------------------------------------------------------------------


def test_stream_accumulator_collects_text() -> None:
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"content": "Hello"}}]})
    acc.feed({"choices": [{"delta": {"content": " world"}}]})

    assert acc.get_content() == "Hello world"
    assert not acc.has_tool_calls()


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


def test_stream_accumulator_collects_usage() -> None:
    acc = StreamAccumulator()
    acc.feed({"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
    assert acc.usage is not None
    assert acc.usage["total_tokens"] == 30


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

    async def _runtime_factory(*, agent: AgentDefinition, user_email: str) -> ResolvedStepRuntime:
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
            tool_registry="parent-registry",
            executor_connection="parent-executor",
        )
    finally:
        monkeypatch.undo()

    assert output is not None
    assert runtime_calls == [("agent-a", "user@example.com")]
    assert captured_tool_registry == ["child-registry"]


class _ReminderStop(RuntimeError):
    pass


class _FakeReminderLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            max_tools=None,
            supports_parallel_tool_calls=False,
            supports_tool_choice=False,
            supports_cache_control=False,
            supports_defer_loading=False,
            provider="test",
        )

    async def stream_generate(self, messages: list[dict[str, object]], **_: object):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) >= 2:
            raise _ReminderStop()
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
        return SimpleNamespace(
            max_tools=None,
            supports_parallel_tool_calls=False,
            supports_tool_choice=False,
            supports_cache_control=False,
            supports_defer_loading=False,
            provider="test",
        )

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
        return SimpleNamespace(
            max_tools=None,
            supports_parallel_tool_calls=False,
            supports_tool_choice=False,
            supports_cache_control=False,
            supports_defer_loading=False,
            provider="test",
        )

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


class _FinalAssistantContentLLM:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        del model
        return len(text)

    async def get_model_info(self, model: str | None) -> SimpleNamespace:
        del model
        return SimpleNamespace(
            max_tools=None,
            supports_parallel_tool_calls=False,
            supports_tool_choice=False,
            supports_cache_control=False,
            supports_defer_loading=False,
            provider="test",
        )

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
                                "id": "call_done",
                                "function": {
                                    "name": "step_complete",
                                    "arguments": '{"summary":"done","claims":["Delivered final text"]}',
                                },
                            }
                        ]
                    }
                }
            ]
        }
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
    def get_model_override(self, _: str) -> None:
        return None

    def get_reasoning_effort_override(self, _: str) -> None:
        return None

    def update_context_usage(self, *_: object, **__: object) -> None:
        return None

    async def append_recorded_events(self, *_: object, **__: object) -> None:
        return None

    async def update_intention(self, *_: object, **__: object) -> bool:
        return False


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
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )
    calls = await _run_reminder_capture(ctx)
    assert calls[1][-1]["role"] == "system"
    assert "incomplete todos" in str(calls[1][-1]["content"])
    assert "produce no assistant text" in str(calls[1][-1]["content"])
    assert "Do not repeat, restate, or paraphrase" in str(calls[1][-1]["content"])


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
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )
    calls = await _run_reminder_capture(ctx)
    assert calls[1][-1]["role"] == "system"
    assert "call step_complete now" in str(calls[1][-1]["content"])


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
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )

    tools = loop._build_controller_tool_schemas(ctx)
    todo_tool = next(tool for tool in tools if tool["function"]["name"] == "step_todo_write")
    statuses = todo_tool["function"]["parameters"]["properties"]["todos"]["items"]["properties"][
        "status"
    ]["enum"]

    assert statuses == ["pending", "in_progress", "completed", "cancelled"]
    assert (
        "Break substantial work into specific, actionable items"
        in todo_tool["function"]["description"]
    )


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
        step_definition=StepDefinition(name="implement", type="run", prompt=""),
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
        step_definition=StepDefinition(name="secondary", type="run", prompt=""),
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
            return SimpleNamespace(
                max_tools=None,
                supports_parallel_tool_calls=False,
                supports_tool_choice=False,
                supports_cache_control=False,
                supports_defer_loading=False,
                provider="test",
            )

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
                                        "function": {"name": "bash", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                }
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
    assert order[:4] == [
        "record:user_message",
        "reasoning",
        "record:tool_call",
        "execute",
    ]
    assert "record:tool_result" in order
    assert "record:assistant_message" in order


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
        step_definition=StepDefinition(name="commit", type="run", prompt=""),
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

    with pytest.raises(ValueError, match="requires a non-empty final assistant message"):
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

    assert [tool.name for tool in filtered] == ["skill_attached-skill__run_attached"]
    assert [tool.name for tool in discovered] == [
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
    assert "Content:\nDetailed plan body" in text
    assert "Structured outputs:" in text


def test_format_prior_step_outputs_summary_excludes_claims_and_content() -> None:
    agent_loop = object.__new__(AgentLoop)
    workflow_state = WorkflowState(
        step_outputs={
            "implement": StepOutput(
                summary="Implemented change",
                claims=["Ran tests"],
                content="Long implementation details",
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
    assert "Structured outputs:" in text
    assert "Claims:" not in text
    assert "Long implementation details" not in text


def test_format_prior_step_outputs_last_excludes_full_content() -> None:
    agent_loop = object.__new__(AgentLoop)
    workflow_state = WorkflowState(
        step_outputs={
            "plan": StepOutput(
                summary="Plan ready",
                claims=["Reviewed dependencies"],
                content="Verbose plan details",
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
    assert "Structured outputs:" in text
    assert "Verbose plan details" not in text


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
            name="briefing", type="run", prompt="Produce the final briefing."
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
        task_title="Daily summary",
        task_description="Summarize today.",
        task_expected_output="No assistant message.",
    )

    prompt = agent_loop._build_step_prompt(ctx)

    assert "Respect Expected output closely" in prompt
    assert (
        "Do not interpret Expected output alone as permission to omit the assistant deliverable entirely"
        in prompt
    )
