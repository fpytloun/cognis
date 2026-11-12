"""Exercise skill activation through the real loop and canonical replay boundary."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    AgentLoop,
    PauseWaiter,
    SessionLock,
    StepContext,
    _delegated_system_builtin_tool_ids,
)
from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.core.context import events_to_messages
from cognis.core.session_cache import SessionCache
from cognis.core.tool_exposure import LLMApiMode, ToolDiscoveryMode, ToolExposureContract
from cognis.core.tool_router import ToolRouter
from cognis.models.agent import AgentDefinition
from cognis.models.config import ModelInfo
from cognis.models.session import EventAppendResult, EventReadResult, SessionEvent
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolResult, ToolSource, stable_tool_id
from cognis.models.workflow import (
    StepDefinition,
    StepProfileConfig,
    StepProfileMode,
    StepToolOverrides,
)
from cognis.providers.llm.responses_bridge import messages_to_responses_input
from cognis.tools.builtin.skill_management import SKILL_LOAD_TOOL, materialize_loaded_skill_context
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_agent_loop import (
    _FakeContextAssembler,
    _NoopEventBus,
    _NoopRememberQueue,
    _NoopSessionManager,
    _RecordingGuardrails,
    _SessionStub,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_changed", [True, False])
@pytest.mark.parametrize("api", ["chat", "responses", "anthropic", "fallback"])
@pytest.mark.parametrize("declared,specialist", [(False, False), (True, False), (True, True)])
async def test_skill_load_preserves_complete_tools_prefix_and_cold_eligibility(
    api: str, declared: bool, specialist: bool, reload_changed: bool
) -> None:
    target = ToolDefinition(
        name="calendar_events",
        description="Read calendar events.",
        parameters={"type": "object", "properties": {}},
        source=(
            ToolSource(type="skill", skill_id="skill_calendar", raw_tool_name="events")
            if declared
            else ToolSource(type="local_mcp", server_id="calendar", raw_tool_name="events")
        ),
        category="mcp",
        profile_group="office",
        read_only=True,
    )
    target_id = stable_tool_id(target)

    class SkillLLM:
        def __init__(self) -> None:
            self.requests: list[list[dict]] = []
            self.schemas: list[list[dict]] = []

        async def get_model_info(self, *_args: object, **_kwargs: object) -> ModelInfo:
            requested_model = _args[0] if _args else _kwargs.get("model") or _kwargs.get("model_id")
            anthropic = api == "anthropic" or requested_model == "claude-fallback"
            return ModelInfo(
                model_id="claude-fallback" if anthropic else "gpt-5.4",
                context_window=128_000,
                supports_responses_api=api == "responses",
                supports_tool_search=True,
                supports_native_tool_search=anthropic,
                supports_defer_loading=anthropic,
                supports_pause_turn=anthropic,
                supports_parallel_tool_calls=True,
                supports_vision=True,
            )

        async def resolve_tool_exposure_contract(self, **_kwargs: object) -> ToolExposureContract:
            anthropic = _kwargs["model_info"].supports_native_tool_search
            return ToolExposureContract(
                llm_api=LLMApiMode.RESPONSES if api == "responses" else LLMApiMode.CHAT_COMPLETIONS,
                discovery_mode=(
                    ToolDiscoveryMode.ANTHROPIC_NATIVE_SEARCH
                    if anthropic
                    else ToolDiscoveryMode.CONTROLLER_SEARCH
                ),
                anthropic_schema_compatible=anthropic,
                anthropic_native_tool_search=anthropic,
            )

        def count_tokens(self, text: str, *_args: object) -> int:
            return len(text) // 4

        async def classify(self, request, **_kwargs: object) -> dict:
            return {
                "model": "routes/classifier",
                "answers": {
                    tool_id: {"type": "noul", "noul": 1.0 if tool_id == target_id else 0.0}
                    for tool_id in request.questions
                },
            }

        async def stream_generate(self, messages: list[dict], **kwargs: object):
            if api == "fallback":
                prepared = await kwargs["cognis_prepare_candidate"](
                    SimpleNamespace(
                        target=SimpleNamespace(
                            model="claude-fallback", provider_id="anthropic-fallback"
                        )
                    )
                )
                messages = prepared.messages
                kwargs.update(prepared.kwargs)
                yield {"_inference_selection": {"decoder": prepared.decoder}}
            self.requests.append(deepcopy(messages))
            self.schemas.append(deepcopy(kwargs.get("tools", [])))
            index = len(self.requests)
            if index > 1:
                exposed_instructions = [
                    message["content"]
                    for message in messages
                    if "<loaded_skill>" in str(message.get("content"))
                ]
                durable_content = {
                    event.data.get("content") for event in guardrails.recorded_events
                }
                assert all(content in durable_content for content in exposed_instructions)
            if index in {1, 3}:
                name, arguments = "skill_load", {"skill_id": "skill_calendar"}
            elif index == 2:
                name, arguments = "call_tool", {"tool": target_id, "arguments": {}}
            else:
                yield {"choices": [{"delta": {"content": "Done."}}]}
                return
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_{index}",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ]
                        }
                    }
                ]
            }

    def loaded_result(instructions: str, content_hash: str) -> ToolResult:
        output, metadata = materialize_loaded_skill_context(
            skill_id="skill_calendar",
            name="calendar",
            instructions=instructions,
            description="Calendar work.",
            tools=[],
            templates={},
            asset_refs=[],
            steps=[],
            tags=[],
            linked_tool_ids=[target_id] if declared else None,
            content_hash=content_hash,
        )
        return ToolResult(output=json.dumps(output), metadata=metadata)

    initial_result = loaded_result("Read the calendar.", "calendar-v1")
    skill_result = ToolResult(
        output=initial_result.output,
        attachments=[
            {
                "artifact_id": "art_0123456789abcdef0123456789abcdef",
                "kind": "image",
                "filename": "calendar.png",
                "mime_type": "image/png",
                "size_bytes": 128,
                "url": "https://media.test/calendar.png",
                "content_b64": "must-not-be-recorded",
            }
        ],
        metadata=initial_result.metadata,
    )
    registry = ToolRegistry()
    for tool in (SKILL_LOAD_TOOL, target):
        registry.register(RegisteredTool(definition=tool, handler=None))
    llm = SkillLLM()
    if api == "fallback":
        llm.inference_recovery = SimpleNamespace()

    class Guardrails(_RecordingGuardrails):
        async def record_events(self, *args: object, **kwargs: object) -> EventAppendResult:
            first = len(self.recorded_events) + 1
            result = await super().record_events(*args, **kwargs)
            return EventAppendResult(
                ok=True, count=result.count, first_seq=first, last_seq=len(self.recorded_events)
            )

        async def read_events(self, **kwargs: object) -> EventReadResult:
            after = kwargs.get("after_seq") or 0
            return EventReadResult(
                events=[
                    {
                        "seq": seq,
                        "type": event.type,
                        "data": event.data,
                        "ts": "2025-01-01T00:00:00Z",
                    }
                    for seq, event in enumerate(self.recorded_events, start=1)
                    if seq > after
                ],
                last_seq=len(self.recorded_events),
                has_more=False,
            )

    guardrails = Guardrails()
    cache = SessionCache(guardrails)
    skill_reload = loaded_result(
        "Read the refreshed calendar." if reload_changed else "Read the calendar.",
        "calendar-v2" if reload_changed else "calendar-v1",
    )
    skill_reload.attachments = deepcopy(skill_result.attachments)
    result_router = ToolRouter(guardrails=guardrails)
    router = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                result_router._sanitize_result("skill_load", skill_result, 50_000),
                ToolResult(output="Calendar evidence."),
                result_router._sanitize_result("skill_load", skill_reload, 50_000),
            ]
        ),
        finalize_skill_result=result_router.finalize_skill_result,
    )
    loop = AgentLoop(
        providers=SimpleNamespace(llm=llm, guardrails=guardrails),
        session_manager=_NoopSessionManager(),
        session_cache=cache,
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=router,
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    session = _SessionStub(
        session_id="sess-skill-prefix",
        conversation_id="conv-skill-prefix",
        intaris_session_id="sess-skill-prefix",
        user_email="user@example.com",
        agent_id="agent-1",
        mnemory_session_id=None,
    )
    agent = AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent")
    if specialist:
        agent = SYSTEM_AGENTS["system:code-review"].model_copy(deep=True)

    def context() -> StepContext:
        step = StepDefinition(
            name="direct", type="run", prompt="", step_profile_id="system:direct-default"
        )
        if specialist:
            step = StepDefinition(
                name="delegation",
                type="run",
                prompt="",
                step_profile_mode=StepProfileMode.HARD,
                step_profile=StepProfileConfig(
                    tool_overrides=StepToolOverrides(
                        include=_delegated_system_builtin_tool_ids(agent, registry)
                    ),
                    allow_tool_search=True,
                ),
            )
        return StepContext(
            step_definition=step,
            session=session,
            conversation=SimpleNamespace(
                conversation_id=session.conversation_id,
                context=SimpleNamespace(type="web", ref=None, platform_data={}),
            ),
            agent=agent,
            policy=CHAT_POLICY,
            user_message="Read my calendar.",
            tool_registry=registry,
            system_initiated=False,
        )

    assert await loop.run_step(context()) is not None
    assert router.execute.await_count == 3, [
        message for request in llm.requests for message in request if message.get("role") == "tool"
    ]
    assert len(llm.schemas) == 4
    assert all(schema == llm.schemas[0] for schema in llm.schemas)
    assert target.name not in json.dumps(llm.schemas[0])
    assert "defer_loading" not in json.dumps(llm.schemas[0])
    assert "tool_search_tool_bm25" not in json.dumps(llm.schemas[0])
    assert any("<skill_activation" in str(message.get("content")) for message in llm.requests[1])
    recorded = guardrails.recorded_events
    loaded = [
        event
        for event in recorded
        if event.type == "developer_message" and event.data.get("kind") == "loaded_skill"
    ]
    assert len(loaded) == (2 if reload_changed else 1)
    assert all(event.data["activated_tool_ids"] == [target_id] for event in loaded)
    final_results = [
        json.loads(message["content"].split("\n", 1)[1].rsplit("\n", 1)[0])
        for message in llm.requests[3]
        if message.get("role") == "tool" and message.get("tool_call_id") == "call_3"
    ]
    assert final_results[0]["status"] == ("loaded" if reload_changed else "already_loaded")
    assert final_results[0]["instructions_loaded"] is reload_changed
    assert next(
        message["content"] for message in llm.requests[3] if message.get("tool_call_id") == "call_3"
    ).startswith('<tool_result trust="untrusted">\n')
    if reload_changed:
        assert "replace the previously loaded instructions" in loaded[-1].data["content"]
        assert "Read the refreshed calendar." in loaded[-1].data["content"]
    assert not any(event.data.get("event") == "prompt_injections" for event in recorded)
    assert "must-not-be-recorded" not in json.dumps([event.data for event in recorded])
    live_aux = [
        message
        for message in llm.requests[3]
        if any(
            marker in str(message.get("content"))
            for marker in (
                "<loaded_skill>",
                "<skill_activation",
                "Untrusted tool output from skill_load",
            )
        )
    ]
    cold_aux = [
        message
        for message in events_to_messages(recorded, model_info=await llm.get_model_info())
        if message.get("_tool_context_call_id") or message.get("_tool_attachment_context")
    ]
    assert len(live_aux) == (6 if reload_changed else 5)
    assert messages_to_responses_input(cold_aux) == messages_to_responses_input(live_aux)
    if api in {"anthropic", "fallback"}:
        from cognis.providers.llm.anthropic.integration import _convert_messages

        assert _convert_messages(cold_aux) == _convert_messages(live_aux)

    # The production event consumer restores both declared and classified eligibility.
    cold = SessionCache(guardrails)
    await cold.refresh(session)
    assert cold.get_activated_skill_tool_ids(session.session_id) == {target_id}
    for active_cache in (cache, cold):
        cached_events = [
            {"seq": event.seq, "type": event.type, "data": event.data}
            for event in active_cache.get_events_since_compaction(session.session_id)
        ]
        cached_aux = [
            message
            for message in events_to_messages(cached_events, model_info=await llm.get_model_info())
            if message.get("_tool_context_call_id") or message.get("_tool_attachment_context")
        ]
        assert messages_to_responses_input(cached_aux) == messages_to_responses_input(live_aux)
    loop.session_cache = cold
    assert await loop.run_step(context()) is not None
    assert llm.schemas[-1] == llm.schemas[0]


def test_canonical_tool_notice_keeps_parallel_native_batch_wire_shape() -> None:
    from cognis.core.tool_result_context import append_tool_notice
    from tests.unit.test_native_batch_replay_grouping import _live_messages, _recorded_events

    events = _recorded_events(with_content=False)
    live = _live_messages(with_content=False)
    notice_messages: list[dict] = []
    notice_events: list[SessionEvent] = []
    append_tool_notice(
        notice_messages,
        notice_events,
        call_id="call_search",
        source="protected_tool_context",
        content="Loaded instructions.",
        prior_context=True,
    )
    first_result = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "tool_result" and event["data"]["call_id"] == "call_search"
    )
    events.insert(first_result + 1, {"type": "developer_message", "data": notice_events[0].data})
    live.insert(3, notice_messages[0])
    assert messages_to_responses_input(events_to_messages(events)) == messages_to_responses_input(
        live
    )


@pytest.mark.asyncio
async def test_notice_delivery_failure_stops_before_audit_or_projection_delivery() -> None:
    from cognis.core.tool_result_context import append_tool_notice

    pending: list[SessionEvent] = []
    append_tool_notice(
        [], pending, call_id="call_load", source="loaded_skill", content="Instructions."
    )
    loop = object.__new__(AgentLoop)
    loop._raise_if_cancelled = Mock()
    loop._flush_events_incremental = AsyncMock(
        side_effect=RuntimeError("event storage unavailable")
    )
    loop._record_outgoing_audit_messages = AsyncMock()
    loop.context_assembler = SimpleNamespace(persist_projection_decisions=AsyncMock())
    ctx = SimpleNamespace(pending_events=pending, execution_fence=None)

    with pytest.raises(RuntimeError, match="event storage unavailable"):
        await loop._commit_context_delivery(
            ctx, audit_messages=[], projection_decisions={}, on_token=None
        )
    loop._record_outgoing_audit_messages.assert_not_awaited()
    loop.context_assembler.persist_projection_decisions.assert_not_awaited()


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_tool_attachment_replay_is_forward_only_and_preserves_disabled_media(
    marked: bool, disabled: bool
) -> None:
    attachment = {
        "artifact_id": "art_0123456789abcdef0123456789abcdef",
        "kind": "image",
        "filename": "chart.png",
        "mime_type": "image/png",
        "size_bytes": 123,
        "url": "https://media.test/chart.png",
    }
    events = [
        {"type": "tool_call", "data": {"call_id": "call_chart", "name": "read", "arguments": {}}},
        {
            "type": "tool_result",
            "data": {
                "call_id": "call_chart",
                "name": "read",
                "result": "Chart.",
                "tool_attachment_context_refs": [attachment],
                **({"tool_attachment_context_version": 1} if marked else {}),
            },
        },
    ]
    replay = events_to_messages(
        events,
        model_info=ModelInfo(model_id="gpt-5.4", supports_vision=True),
        disabled_artifact_ids={attachment["artifact_id"]} if disabled else set(),
    )
    context = [message for message in replay if message.get("_tool_attachment_context")]
    assert len(context) == int(marked)
    if marked:
        assert context[0]["role"] == ("system" if disabled else "user")
        assert "untrusted" in str(context[0]["content"]).lower()


def test_native_batch_replay_keeps_call_owned_attachment_context_in_place() -> None:
    from tests.unit.test_native_batch_replay_grouping import _live_messages, _recorded_events

    events = _recorded_events(with_content=False)
    first = next(event for event in events if event["type"] == "tool_result")
    first["data"].update(
        tool_attachment_context_version=1,
        tool_attachment_context_refs=[
            {
                "artifact_id": "art_0123456789abcdef0123456789abcdef",
                "kind": "file",
                "filename": "report.txt",
                "mime_type": "text/plain",
                "size_bytes": 1,
            }
        ],
    )
    replay = events_to_messages(events)
    live = _live_messages(with_content=False)
    live.insert(3, replay[3])
    assert replay[3].get("_tool_attachment_context") is True
    assert len([message for message in replay if message.get("tool_calls")]) == 1
    assert messages_to_responses_input(replay) == messages_to_responses_input(live)
