"""Controller protocol coverage through the agent-loop dispatch boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    AgentLoop,
    PauseWaiter,
    SessionLock,
    StepContext,
    _replayed_write_deliverable_validation_fingerprints,
    _replayed_write_deliverable_validation_receipts,
    _resolve_call_tool_envelope,
)
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.deliverable import Deliverable
from cognis.models.session import ConversationContext, ConversationModel
from cognis.models.tool import ToolCall
from cognis.models.workflow import StepDefinition
from cognis.tools.builtin.tool_search import VALIDATE_TOOL_CALL_TOOL, search_inventory
from cognis.tools.introspection import describe_available_tool
from cognis.tools.registry import ToolRegistry
from tests.unit.test_agent_loop import (
    _FakeContextAssembler,
    _NoopEventBus,
    _NoopGuardrails,
    _NoopRememberQueue,
    _NoopSessionCache,
    _NoopSessionManager,
    _SessionStub,
    _SingleTextLLM,
    _test_model_info,
)


def _context() -> StepContext:
    return StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=_SessionStub(
            session_id="session-protocol",
            intaris_session_id="session-protocol",
            mnemory_session_id=None,
            user_email="user@example.com",
            agent_id="agent-protocol",
        ),
        conversation=ConversationModel(
            conversation_id="conversation-protocol",
            user_email="user@example.com",
            agent_id="agent-protocol",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(
            agent_id="agent-protocol",
            owner_email="user@example.com",
            name="Protocol tests",
            tools={
                "builtin_tools": [
                    "search_tools",
                    "describe_tool",
                    "validate_tool_call",
                    "call_tool",
                    "write_deliverable",
                ]
            },
        ),
        policy=CHAT_POLICY,
        controller_tool_surface=CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
        tool_registry=ToolRegistry(),
    )


def _loop(llm=None) -> AgentLoop:
    loop = AgentLoop(
        providers=SimpleNamespace(llm=llm or _SingleTextLLM(), guardrails=_NoopGuardrails()),
        context_assembler=_FakeContextAssembler(),
        tool_router=SimpleNamespace(),
        session_manager=_NoopSessionManager(),
        session_cache=_NoopSessionCache(),
        compaction_strategy=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    loop._get_model_info = lambda *args, **kwargs: _test_model_info()
    loop._stale_session_step_output = AsyncMock(return_value=None)
    return loop


@pytest.mark.parametrize("restriction", ["allowed", "disabled", "denied", "hard_surface"])
def test_controller_protocol_inventory_respects_authorization(restriction):
    ctx = _context()
    if restriction == "disabled":
        ctx.agent.tools["builtin_tools"].remove("validate_tool_call")
    elif restriction == "denied":
        ctx.agent.permissions = AgentPermissions(denied_tools=["validate_tool_call"])
    loop = _loop()
    schemas = loop._build_controller_tool_exposure(ctx).schemas
    if restriction == "hard_surface":
        schemas = [s for s in schemas if s["function"]["name"] != "validate_tool_call"]
    tools = loop._introspection_tools(ctx, [], controller_tool_schemas=schemas)
    present = restriction == "allowed"
    assert (
        any(
            match["tool_id"] == "builtin:validate_tool_call"
            for match in search_inventory(tools, query="validate_tool_call", limit=5)
        )
        == present
    )
    described = describe_available_tool(tools, "builtin:validate_tool_call")
    assert described["valid"] == present
    envelope = ToolCall(
        call_id="call-validator",
        name="call_tool",
        arguments={"tool": "builtin:validate_tool_call", "arguments": {}},
    )
    assert (_resolve_call_tool_envelope(envelope, tools) is not None) == present


class _ProtocolLLM(_SingleTextLLM):
    def __init__(self, calls):
        self.calls = iter(calls)
        self.results = {}
        self.schemas = []
        self.messages = []

    async def stream_generate(self, messages, tools=None, **kwargs):
        self.messages = messages
        for message in messages:
            if message.get("role") == "tool":
                self.results[message["tool_call_id"]] = json.loads(message["content"])
        self.schemas.append(tools)
        call = next(self.calls, None)
        delta = {"tool_calls": [{"index": 0, **call}]} if call else {"content": "Done."}
        yield {"choices": [{"delta": delta}]}


def _call(call_id, name, arguments):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_discovered_controller_preflight_then_rich_write_is_session_local(wrapped):
    arguments = {
        "action": "rich",
        "payload": {
            "version": 1,
            "title": "Protocol regression",
            "blocks": [{"type": "markdown", "content": "Validated local output."}],
        },
    }
    validation = {"tool": "write_deliverable", "arguments": arguments}
    validation_call = (
        _call(
            "validate-local",
            "call_tool",
            {"tool": "builtin:validate_tool_call", "arguments": validation},
        )
        if wrapped
        else _call("validate-local", "validate_tool_call", validation)
    )
    llm = _ProtocolLLM(
        [
            _call("find-validator", "search_tools", {"query": "validate_tool_call"}),
            _call("describe-validator", "describe_tool", {"tool": "builtin:validate_tool_call"}),
            validation_call,
            _call(
                "write-local",
                "call_tool",
                {"tool": "builtin:write_deliverable", "arguments": arguments},
            ),
        ]
    )
    loop = _loop(llm)
    ctx = _context()
    writer = AsyncMock(
        return_value=Deliverable(
            deliverable_id="dlv-protocol",
            agent_id=ctx.agent.agent_id,
            conversation_id=ctx.conversation.conversation_id,
            version=1,
            content="Validated local output.",
        )
    )
    with patch.object(loop, "_write_step_deliverable", writer):
        output = await loop.run_step(ctx)
    assert output is not None and output.error is None, output
    assert "find-validator" in llm.results, (output, llm.schemas, llm.results)
    assert any(
        match["tool_id"] == "builtin:validate_tool_call"
        for match in llm.results["find-validator"]["matches"]
    )
    assert llm.results["describe-validator"]["valid"] is True
    assert llm.results["validate-local"]["valid"] is True, llm.results["validate-local"]
    assert llm.results["write-local"]["status"] == "buffered"
    writer.assert_awaited_once()
    assert _replayed_write_deliverable_validation_fingerprints(llm.messages) == (
        set(ctx.validated_write_deliverable_states)
    )
    assert _replayed_write_deliverable_validation_receipts(llm.messages) == (
        ctx.validated_write_deliverable_states
    )
    assert all(
        any(t["function"]["name"] == VALIDATE_TOOL_CALL_TOOL.name for t in schemas)
        for schemas in llm.schemas
    )

    # Even in the same conversation, another session has no local receipt.
    other = _context()
    other.session.session_id = "session-other"
    other.session.intaris_session_id = "session-other"
    other_llm = _ProtocolLLM(
        [
            _call(
                "write-other",
                "call_tool",
                {"tool": "builtin:write_deliverable", "arguments": arguments},
            ),
        ]
    )
    other_loop = _loop(other_llm)
    with patch.object(other_loop, "_write_step_deliverable", writer):
        await other_loop.run_step(other)
    assert other_llm.results["write-other"]["reason"] == "missing_validation"
    assert "missing_preflight_validation" in json.dumps(other_llm.results["write-other"])
    writer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["disabled", "denied"])
async def test_unavailable_validator_cannot_be_described_or_called_in_loop(restriction):
    ctx = _context()
    if restriction == "disabled":
        ctx.agent.tools["builtin_tools"].remove("validate_tool_call")
    else:
        ctx.agent.permissions = AgentPermissions(
            tool_permissions={"builtin:validate_tool_call": "deny"}
        )
    llm = _ProtocolLLM(
        [
            _call("find", "search_tools", {"query": "validate_tool_call"}),
            _call("describe", "describe_tool", {"tool": "builtin:validate_tool_call"}),
            _call(
                "invoke",
                "call_tool",
                {
                    "tool": "builtin:validate_tool_call",
                    "arguments": {
                        "tool": "write_deliverable",
                        "arguments": {"content": "No receipt."},
                    },
                },
            ),
        ]
    )
    output = await _loop(llm).run_step(ctx)
    assert output is not None and output.error is None
    assert all(m["tool_id"] != "builtin:validate_tool_call" for m in llm.results["find"]["matches"])
    assert llm.results["describe"]["error"] == "tool_not_available"
    assert llm.results["invoke"]["reason"] == "tool_not_available"
    assert not ctx.validated_write_deliverable_states


@pytest.mark.parametrize("target", ["builtin:validate_tool_call", "validate_tool_call", {}])
def test_wrapped_preflight_replay_requires_successful_matching_result(target):
    arguments = {"action": "rich", "payload": {"title": "Replay", "blocks": []}}
    call = _call(
        "replay-validation",
        "call_tool",
        {
            "tool": target,
            "arguments": {"tool": "write_deliverable", "arguments": arguments},
        },
    )
    messages = [
        {"role": "assistant", "tool_calls": [call]},
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps(
                {
                    "valid": True,
                    "schema_hash": "schema-hash",
                    "validation_state_fingerprint": "state-fingerprint",
                }
            ),
        },
    ]
    fingerprints = _replayed_write_deliverable_validation_fingerprints(messages)
    assert bool(fingerprints) == isinstance(target, str)
    messages[-1]["content"] = json.dumps({"valid": False})
    assert not _replayed_write_deliverable_validation_fingerprints(messages)
    assert not _replayed_write_deliverable_validation_receipts(messages)
