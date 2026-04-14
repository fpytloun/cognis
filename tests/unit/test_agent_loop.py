"""Tests for the agent loop engine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    WORKFLOW_POLICY,
    AgentLoop,
    PauseResolution,
    PauseWaiter,
    PendingPause,
    SessionLock,
    StepContext,
    StreamAccumulator,
    _controller_builtin_enabled,
    _filter_model_inventory_tools,
)
from cognis.core.runtime import ResolvedStepRuntime, build_local_executor_environment
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import EventAppendResult, ReasoningReportResult
from cognis.models.tool import Permission, ToolCall, ToolDefinition, ToolSource
from cognis.models.workflow import StepDefinition, StepOutput, WorkflowState
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
    async def assemble(self, **kwargs: object) -> SimpleNamespace:
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
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
    )

    async def _get_task(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(task_id="task-1", agent_id="agent-1", status="paused")

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
