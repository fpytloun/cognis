"""Handoff generation through real context and tool-exposure preparation."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cognis.core.agent_loop import AgentLoop, StepContext, direct_chat_inference_contract
from cognis.core.commands import CommandDispatcher, is_system_slash_command_message
from cognis.core.context import ContextAssembler
from cognis.core.events import EventType
from cognis.core.handoff import HandoffService
from cognis.core.immutable_prefix import ImmutablePrefixEntry
from cognis.core.prompts import PromptContext
from cognis.core.session_cache import CachedEvent, CanonicalContextSnapshot, SessionCache
from cognis.models.agent import AgentDefinition
from cognis.models.config import ModelInfo
from cognis.models.session import ConversationModel, SessionModel
from cognis.models.tool import NativeToolDefinition
from cognis.tools.registry import RegisteredTool, ToolRegistry


class _LLM:
    def __init__(self) -> None:
        self.generate = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "# Handoff\nContinue the verified task."}}]
            }
        )

    async def resolve_model_target(self, **kwargs):
        return "handoff-model", "handoff-provider"

    async def get_model_info(self, *args, **kwargs):
        return ModelInfo(model_id="handoff-model", provider="openai", max_input_tokens=100_000)

    def count_tokens(self, text, model=None):
        return max(1, len(str(text)) // 4)

    def count_messages_tokens(self, messages, model=None):
        return sum(self.count_tokens(message) for message in messages)


def _setup():
    agent = AgentDefinition(
        agent_id="handoff-agent", name="Handoff agent", owner_email="owner@local.test"
    )
    conversation = ConversationModel(
        conversation_id="conv-handoff",
        user_email=agent.owner_email,
        agent_id=agent.agent_id,
        context={"type": "web"},
    )
    session = SessionModel(
        session_id="sess-handoff",
        conversation_id=conversation.conversation_id,
        agent_id=agent.agent_id,
        user_email=agent.owner_email,
    )
    snapshot = CanonicalContextSnapshot(
        events=[
            CachedEvent(
                seq=2, type="user_message", data={"content": "Investigate the connection failure."}
            ),
            CachedEvent(
                seq=3,
                type="tool_call",
                data={
                    "call_id": "call-pending-check",
                    "name": "read",
                    "arguments": {"file_path": "/workspace/log"},
                },
            ),
        ],
        prefix_entries=[
            ImmutablePrefixEntry(
                role="system", source="identity", content="Stable identity.", seq=1
            )
        ],
        last_compaction_summary=None,
        last_event_seq=3,
        last_compaction_seq=0,
        projection_revision=1,
    )
    llm = _LLM()
    guardrails = SimpleNamespace(
        record_events=AsyncMock(
            return_value=SimpleNamespace(
                ok=True,
                first_seq=4,
                last_seq=4,
            )
        )
    )
    providers = SimpleNamespace(llm=llm, guardrails=guardrails)
    cache = SessionCache(guardrails)
    cache.acquire_context_snapshot = AsyncMock(return_value=snapshot)
    cache.append_recorded_events = AsyncMock()
    assembler = ContextAssembler(
        memory=SimpleNamespace(),
        guardrails=guardrails,
        llm=llm,
        session_cache=cache,
        session_factory=None,
        session_manager=None,
        compaction_threshold=0.85,
    )
    loop = AgentLoop(
        providers=providers,
        session_manager=None,
        session_cache=cache,
        context_assembler=assembler,
        compaction_strategy=None,
        tool_router=None,
        remember_queue=None,
        event_bus=None,
        session_lock=None,
        pause_waiter=None,
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="read",
                source={"type": "executor"},
                read_only=True,
                category="filesystem",
                description="Read a file",
                parameters={"type": "object", "properties": {}},
            )
        )
    )
    cleanup = AsyncMock()
    runtime_factory = AsyncMock(
        return_value=SimpleNamespace(
            tool_registry=registry,
            executor_environment=None,
            executor_pool=None,
            refresh_executor_pool=None,
            active_executor_id=None,
            cleanup=cleanup,
        )
    )
    signals = SimpleNamespace(publish_chat_change=AsyncMock())
    event_bus = SimpleNamespace(publish=AsyncMock())
    service = HandoffService(
        agent_loop=loop,
        providers=providers,
        session_cache=cache,
        runtime_factory=runtime_factory,
        scheduler=SimpleNamespace(cluster_signals=signals),
        event_bus=event_bus,
    )
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "focus",
    [
        pytest.param(
            "Read-only lookup: find the function that assembles agent briefs in "
            "cognis/core/orchestration_policy.py. Return its name and line range.",
            id="lookup",
        ),
        pytest.param(
            "Implement readable agent briefs in an isolated worktree. Preserve context "
            "isolation and permissions. Verify assembled prompts and focused regressions; "
            "return the change and local commit. Do not push, merge, or deploy.",
            id="implementation",
        ),
        pytest.param(
            "Investigate why handoffs lose spaces. One observed model response already "
            "contained the unreadable arguments; other cases remain unexplained.\n\n"
            "Read-only. Return evidence that distinguishes hypotheses and what remains "
            "unknown. Do not implement a fix.",
            id="investigation",
        ),
    ],
)
async def test_handoff_full_preparation_retains_schemas_and_persists_only_lifecycle(focus):
    f = _setup()
    original = copy.deepcopy(f.snapshot)
    text = await f.service.generate(f.conversation, f.session, f.agent, focus=focus)
    args, kwargs = f.llm.generate.call_args
    assert text.startswith("# Handoff")
    assert kwargs["model"] == "handoff-model"
    assert kwargs["provider_id"] == "handoff-provider"
    assert kwargs["tool_choice"] == "none"
    assert any(schema.get("function", {}).get("name") == "read" for schema in kwargs["tools"])
    assert "Stable identity." in args[0][0]["content"]
    assert any(focus in (message.get("content") or "") for message in args[0])
    assert "Pending calls" in str(args[0])
    assert "A simple question needs no report template" in str(args[0])
    assert "unverified hypothesis into prescribed steps" in str(args[0])
    assert "leaving room to choose the approach" in str(args[0])
    assert "A handoff grants no" in str(args[0])
    assert "new permissions or access" in str(args[0])
    assert "Begin with a clear title" not in str(args[0])
    assert f.snapshot == original
    f.runtime_factory.assert_awaited_once_with(
        f.agent,
        f.conversation.user_email,
        conversation_id=f.conversation.conversation_id,
        prepare_only=True,
    )
    events = f.guardrails.record_events.call_args.args[1]
    assert len(events) == 1
    assert events[0].type == "lifecycle"
    assert events[0].data["event"] == "conversation_handoff"
    assert events[0].data["source_seq"] == 3
    assert events[0].data["source_session_id"] == f.session.session_id
    assert events[0].data["text"] == text
    states = [call.args[1][0].data for call in f.guardrails.record_events.call_args_list]
    assert [state["status"] for state in states] == ["running", "completed"]
    assert states[0]["request_id"] == states[1]["request_id"]
    assert states[0]["text"] == ""
    assert f.event_bus.publish.await_count == 2
    assert f.event_bus.publish.call_args.args[0].type == EventType.CONVERSATION_HANDOFF_UPDATED
    f.cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {
            "content": "Attempted output",
            "tool_calls": [{"id": "call-unexpected", "function": {"name": "read"}}],
        },
        {"content": "", "reasoning_content": "Private reasoning"},
    ],
)
async def test_handoff_rejects_tools_and_never_exposes_reasoning(message):
    f = _setup()
    f.llm.generate.return_value = {"choices": [{"message": message}]}
    with pytest.raises(RuntimeError):
        await f.service.generate(f.conversation, f.session, f.agent)
    states = [call.args[1][0].data for call in f.guardrails.record_events.call_args_list]
    assert [state["status"] for state in states] == ["running", "failed"]
    assert all(not state["text"] for state in states)
    f.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_handoff_mid_turn_uses_original_cutoff_despite_rotation():
    f = _setup()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await finish.wait()
        return {"choices": [{"message": {"content": "# Handoff\nPending investigation."}}]}

    f.llm.generate.side_effect = generate
    task = asyncio.create_task(f.service.generate(f.conversation, f.session, f.agent))
    await asyncio.wait_for(entered.wait(), 5)
    f.conversation.active_session_id = "sess-rotated-handoff"
    finish.set()
    await task
    assert f.guardrails.record_events.call_args.args[0] == "sess-handoff"
    assert f.guardrails.record_events.call_args.args[1][0].data["source_seq"] == 3


@pytest.mark.asyncio
async def test_handoff_cancellation_has_no_result_and_cleans_up():
    f = _setup()
    entered = asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    f.llm.generate.side_effect = generate
    task = asyncio.create_task(f.service.generate(f.conversation, f.session, f.agent))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert f.guardrails.record_events.call_args.args[1][0].data["status"] == "failed"
    assert f.guardrails.record_events.call_args.args[1][0].data["error"] == "Handoff cancelled."
    f.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_handoff_command_works_while_source_busy():
    f = _setup()
    service = SimpleNamespace(generate=AsyncMock())
    dispatcher = CommandDispatcher(
        providers=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        session_factory=None,
        pause_waiter=None,
        notification_service=None,
        handoff_service=service,
    )
    assert is_system_slash_command_message("/handoff connection failure")
    result = await dispatcher.dispatch(
        "/handoff connection failure",
        conversation=f.conversation,
        session=f.session,
        agent=f.agent,
        user_email=f.agent.owner_email,
        has_active_turn=True,
        has_busy_turn=True,
    )
    assert result.type == "handoff"
    service.generate.assert_awaited_once_with(
        f.conversation,
        f.session,
        f.agent,
        focus="connection failure",
    )


@pytest.mark.asyncio
async def test_handoff_preserves_compaction_and_pending_call_without_mutating_history():
    f = _setup()
    f.snapshot.prefix_entries.append(
        ImmutablePrefixEntry(
            role="developer",
            source="compaction_summary",
            content="Earlier decision: use a bounded retry.",
        )
    )
    f.snapshot.events.extend(
        [
            CachedEvent(
                seq=5,
                type="tool_call",
                data={
                    "call_id": "call-pending-read",
                    "name": "read",
                    "arguments": {"path": "/work/log"},
                },
            ),
        ]
    )
    original = copy.deepcopy(f.snapshot.events)
    await f.service.generate(f.conversation, f.session, f.agent)
    messages = f.llm.generate.call_args.args[0]
    assert "bounded retry" in str(messages)
    assert any(
        message.get("tool_call_id") == "call-pending-read"
        and "No result recorded" in message["content"]
        for message in messages
    )
    assert f.snapshot.events == original
    assert f.snapshot.events[-1].type == "tool_call"
    assert not any(event.type == "tool_result" for event in f.snapshot.events)


@pytest.mark.asyncio
async def test_handoff_missing_prefix_persists_failure_without_inference():
    f = _setup()
    f.snapshot.prefix_entries.clear()
    with pytest.raises(RuntimeError, match="not ready"):
        await f.service.generate(f.conversation, f.session, f.agent)
    f.llm.generate.assert_not_awaited()
    assert f.guardrails.record_events.call_args.args[1][0].data["status"] == "failed"


@pytest.mark.asyncio
async def test_handoff_running_card_is_persisted_and_notified_before_slow_snapshot():
    f = _setup()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def snapshot(session):
        entered.set()
        await finish.wait()
        return f.snapshot

    f.cache.acquire_context_snapshot.side_effect = snapshot
    task = asyncio.create_task(f.service.generate(f.conversation, f.session, f.agent))
    await asyncio.wait_for(entered.wait(), 5)
    assert f.guardrails.record_events.call_args.args[1][0].data["status"] == "running"
    f.event_bus.publish.assert_awaited_once()
    f.signals.publish_chat_change.assert_awaited_once()
    f.llm.generate.assert_not_awaited()
    finish.set()
    await task
    assert f.guardrails.record_events.call_args.args[1][0].data["status"] == "completed"


@pytest.mark.asyncio
async def test_handoff_recipient_link_uses_only_verified_canonical_content():
    f = _setup()
    f.snapshot.events.append(
        CachedEvent(
            seq=8,
            type="lifecycle",
            data={
                "event": "conversation_handoff",
                "text": "Verified handoff.",
                "request_id": "handoff-request",
                "status": "completed",
                "source_session_id": f.session.session_id,
                "source_seq": 3,
            },
        )
    )
    f.guardrails.read_events = AsyncMock(
        return_value=SimpleNamespace(
            events=[{"seq": 8, "type": "lifecycle", "data": f.snapshot.events[-1].data}],
        )
    )
    await f.service.link_recipient(
        f.conversation,
        f.session,
        event_seq=8,
        recipient_id="conv-recipient",
        agent_owner_email=f.agent.owner_email,
    )
    data = f.guardrails.record_events.call_args.args[1][0].data
    assert data["text"] == "Verified handoff."
    assert data["recipient_conversation_id"] == "conv-recipient"
    assert data["handoff_seq"] == 8
    key = f.guardrails.record_events.call_args.kwargs["idempotency_key"]
    await f.service.link_recipient(
        f.conversation,
        f.session,
        event_seq=8,
        recipient_id="conv-recipient",
        agent_owner_email=f.agent.owner_email,
    )
    assert f.guardrails.record_events.call_args.kwargs["idempotency_key"] == key
    with pytest.raises(ValueError):
        await f.service.link_recipient(
            f.conversation,
            f.session,
            event_seq=3,
            recipient_id="conv-recipient",
            agent_owner_email=f.agent.owner_email,
        )


@pytest.mark.asyncio
async def test_handoff_shared_agent_link_signs_read_and_append_with_actual_owner():
    import httpx

    from cognis.providers.guardrails.intaris import IntarisProvider

    f = _setup()
    signer = Mock(return_value="signed-handoff-authority")
    provider = IntarisProvider("http://intaris.test", SimpleNamespace(sign_service_jwt=signer))

    async def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "last_seq": 8,
                    "has_more": False,
                    "events": [
                        {
                            "seq": 8,
                            "type": "lifecycle",
                            "data": {"event": "conversation_handoff", "text": "Verified brief."},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"ok": True, "count": 1, "first_seq": 9, "last_seq": 9})

    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    f.providers.guardrails = provider
    try:
        await f.service.link_recipient(
            f.conversation,
            f.session,
            event_seq=8,
            recipient_id="conv-recipient",
            agent_owner_email="shared-agent-owner@local.test",
        )
    finally:
        await provider.client.aclose()
    assert signer.call_count == 2
    for call in signer.call_args_list:
        assert call.args[:2] == (f.conversation.user_email, f.conversation.agent_id)
        assert call.kwargs["agent_owner_email"] == "shared-agent-owner@local.test"


@pytest.mark.asyncio
async def test_handoff_matches_direct_chat_controller_schemas_and_system_prefix():
    f = _setup()
    await f.service.generate(f.conversation, f.session, f.agent)
    handoff_messages = f.llm.generate.call_args.args[0]
    handoff_tools = f.llm.generate.call_args.kwargs["tools"]
    direct_step, surface = direct_chat_inference_contract({})
    direct_ctx = StepContext(
        step_definition=direct_step,
        controller_tool_surface=surface,
        interaction_mode="step_requests",
        session=f.session,
        conversation=f.conversation,
        agent=f.agent,
        tool_registry=f.registry,
    )
    controller = f.loop._build_controller_tool_exposure(direct_ctx)
    exposure, *_ = await f.loop.prepare_auxiliary_tools(
        direct_ctx,
        model="handoff-model",
        provider_id="handoff-provider",
        model_info=await f.llm.get_model_info(),
        controller_tool_schemas=controller.schemas,
        authorized_deferred_controller_tools=controller.deferred_definitions,
        promoted_tool_ids=set(),
        activated_tool_ids=set(),
    )
    assert handoff_tools == exposure.tools
    names = {schema.get("function", {}).get("name") for schema in handoff_tools}
    assert {"todo_write", "todo_list", "write_deliverable"} <= names
    assert "step_todo_write" not in names
    f.cache.refresh = AsyncMock()
    f.assembler._ensure_immutable_prefix = AsyncMock(return_value=f.snapshot.prefix_entries)
    f.assembler._record_assembled_history = Mock()
    normal = await f.assembler.assemble(
        session=f.session,
        conversation=f.conversation,
        agent=f.agent,
        user_message="Continue",
        skip_memory=True,
        prompt_context=PromptContext.CHAT,
        tool_schema_tokens_override=sum(len(str(schema)) // 4 for schema in handoff_tools),
    )
    assert [m["content"] for m in handoff_messages if m["role"] == "system"] == [
        m["content"] for m in normal.messages if m["role"] == "system"
    ]


@pytest.mark.asyncio
async def test_handoff_retains_anthropic_native_search_chain_and_replay():
    from cognis.core.agent_loop import ANTHROPIC_NATIVE_ENVELOPE_INTERNAL_FIELD
    from cognis.core.tool_exposure import LLMApiMode, ToolDiscoveryMode, ToolExposureContract
    from cognis.providers.llm.anthropic.contracts import (
        AnthropicContinuationStatus,
        AnthropicNativeEnvelope,
    )
    from cognis.providers.llm.anthropic.integration import NATIVE_CONTINUATION_REQUIRED_KWARG

    f = _setup()
    contract = ToolExposureContract(
        llm_api=LLMApiMode.CHAT_COMPLETIONS,
        discovery_mode=ToolDiscoveryMode.ANTHROPIC_NATIVE_SEARCH,
        anthropic_schema_compatible=True,
        anthropic_native_tool_search=True,
    )
    f.llm.resolve_tool_exposure_contract = AsyncMock(return_value=contract)
    f.llm.prepare_anthropic_native_chain = AsyncMock(return_value={})
    envelope = AnthropicNativeEnvelope(
        native_blocks=({"type": "tool_use", "id": "toolu_read", "name": "read", "input": {}},),
        stop_reason="tool_use",
        stop_details={},
        usage={},
        pending_client_message_id=None,
        pending_server_message_id="msg_read",
        bundle_fingerprint="read-bundle",
        provider_fingerprint="anthropic-provider",
        model_fingerprint="anthropic-model",
        thinking_fingerprint="disabled",
        continuation_status=AnthropicContinuationStatus.CONTINUABLE,
    ).to_dict()
    f.snapshot.events[-1].data["anthropic_native_envelope"] = envelope
    await f.service.generate(f.conversation, f.session, f.agent)
    request = f.llm.generate.call_args
    assert request.kwargs["tool_choice"] == "none"
    assert request.kwargs[NATIVE_CONTINUATION_REQUIRED_KWARG] is False
    assert request.kwargs["tools"] == f.llm.prepare_anthropic_native_chain.call_args.kwargs["tools"]
    assert any(ANTHROPIC_NATIVE_ENVELOPE_INTERNAL_FIELD in message for message in request.args[0])
    f.llm.prepare_anthropic_native_chain.assert_awaited_once()
