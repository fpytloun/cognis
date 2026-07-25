from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    AgentLoop,
    PauseWaiter,
    SessionLock,
    StepContext,
)
from cognis.core.context_projection import PressureMode
from cognis.core.message_markers import PROTECTED_TOOL_OUTPUT, TOKEN_ESTIMATE, TURN_BOUNDARY
from cognis.models.agent import AgentDefinition
from cognis.models.tool import ToolCall, ToolResult
from cognis.models.workflow import StepDefinition


class _FixedTokenLLM:
    """Deterministic token counter; no provider/model calls."""

    def count_tokens(self, text: object, _model: str | None = None) -> int:
        return len(str(text))

    def count_messages_tokens(self, messages: list[dict[str, object]], _model: str) -> int:
        total = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                total += self.count_tokens(content, _model)
            elif isinstance(content, list):
                total += self.count_tokens(json.dumps(content, default=str), _model)
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                total += self.count_tokens(json.dumps(tool_calls, default=str), _model)
        return total


class _NoopEventBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class _ToolOutputStore:
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


def _assistant_tool_call(
    call_id: str, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, sort_keys=True)},
            }
        ],
        TOKEN_ESTIMATE: 10,
    }


def _prefix_messages(projection: Any) -> list[dict[str, object]]:
    return list(projection.messages[: projection.mutable_start_index])


def _large_output(cycle: int, *, label: str = "tool") -> str:
    return f"{label}-{cycle}-" + ("X" * 70_000)


def _small_output(cycle: int) -> str:
    return f"small-{cycle}-" + ("s" * 6_000)


@pytest.mark.asyncio
async def test_projection_long_turn_integration_invariants() -> None:
    store = _ToolOutputStore()
    agent_loop = AgentLoop(
        providers=SimpleNamespace(llm=_FixedTokenLLM(), guardrails=SimpleNamespace()),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        tool_output_store=store,
    )

    async def _noop_flush(
        _ctx: StepContext,
        _events: list[object],
        *,
        reason: str = "incremental",
        on_token: object | None = None,
    ) -> None:
        del reason
        del on_token

    agent_loop._flush_events_incremental = _noop_flush  # type: ignore[method-assign]

    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id="sess-long-turn",
            intaris_session_id="sess-long-turn",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conv-long-turn"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        policy=CHAT_POLICY,
        current_model="fixed-token-model",
        current_model_info=SimpleNamespace(max_input_tokens=100_000, max_output_tokens=0),
        turn_id="turn-long",
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "stable prefix", TOKEN_ESTIMATE: 13},
        {
            "role": "user",
            "content": "Run a long tool loop.",
            TOKEN_ESTIMATE: 21,
            TURN_BOUNDARY: True,
        },
    ]

    initial = agent_loop._project_model_messages_for_budget(
        ctx,
        messages=messages,
        tool_schemas=[],
        resolved_model="fixed-token-model",
        max_context_tokens=100_000,
    )
    ctx.last_projection_snapshot = initial.snapshot

    projected_modes: list[PressureMode] = []
    state_modes: list[PressureMode] = []
    forced_counts: list[int] = []
    cap_observed: dict[PressureMode, int] = {}
    saw_critical = False
    exact_guard_candidate_used = False
    previous_prefix_messages = _prefix_messages(initial)
    previous_demotion_count = len(ctx.projection_state.demoted_anchors)

    for cycle in range(30):
        assert ctx.projection_state is not None
        mode_before_tool = PressureMode(ctx.projection_state.pressure_mode)
        call_id = f"call-{cycle}"

        is_recovery = cycle in {8, 9}
        exact_guard_candidate = (
            saw_critical
            and not exact_guard_candidate_used
            and mode_before_tool == PressureMode.normal
            and cycle >= 12
        )
        need_cap_probe = mode_before_tool not in cap_observed
        use_large_output = (
            not saw_critical or is_recovery or exact_guard_candidate or need_cap_probe
        )

        if is_recovery:
            tool_name = "read_tool_output"
            arguments = {"call_id": "call-0", "offset": 1, "limit": 200}
            raw_output = _large_output(cycle, label="recovery")
            metadata = {
                "_raw_output": raw_output,
                "original_size": len(raw_output),
                "source_call_id": "call-0",
            }
        else:
            tool_name = "bash"
            arguments = {"command": f"produce-{cycle}"}
            raw_output = _large_output(cycle) if use_large_output else _small_output(cycle)
            metadata = {"_raw_output": raw_output, "original_size": len(raw_output)}

        messages.append(_assistant_tool_call(call_id, tool_name, arguments))
        before_message_count = len(messages)
        await agent_loop._finalize_regular_tool_result(
            ctx,
            tc=ToolCall(call_id=call_id, name=tool_name, arguments=arguments),
            tool_id=f"builtin:{tool_name}",
            result=ToolResult(output=raw_output, metadata=metadata),
            events_to_record=[],
            messages=messages,
            collected_attachments=[],
            pending_assistant_attachments=[],
            promoted_tool_ids=set(),
            activated_tool_ids=set(),
            on_token=None,
            on_tool_result=None,
        )
        assert len(messages) == before_message_count + 1

        tool_message = messages[-1]
        assert tool_message["role"] == "tool"
        assert isinstance(tool_message["content"], str)
        if use_large_output:
            expected_cap = {
                PressureMode.normal: 50_000,
                PressureMode.pressure: 25_000,
                PressureMode.critical: 12_000,
            }[mode_before_tool]
            assert len(tool_message["content"]) <= expected_cap
            assert f"read_tool_output(call_id='{call_id}')" in tool_message["content"]
            cap_observed.setdefault(mode_before_tool, len(tool_message["content"]))

        if exact_guard_candidate:
            # Cheap projection estimate says "safe to skip"; exact provider
            # counting must catch the over-budget projected prompt and escalate.
            tool_message[TOKEN_ESTIMATE] = 1
            exact_guard_candidate_used = True
        elif saw_critical:
            # Keep post-critical cheap estimates under the demotion band while
            # exact counting still sees the real capped content.
            tool_message[TOKEN_ESTIMATE] = min(len(str(tool_message["content"])), 1_000)

        if is_recovery and cycle == 9:
            assert tool_message[PROTECTED_TOOL_OUTPUT] is True

        forced_before_projection = ctx.projection_state.forced_critical_count
        projected = agent_loop._project_model_messages_for_budget(
            ctx,
            messages=messages,
            tool_schemas=[],
            resolved_model="fixed-token-model",
            max_context_tokens=100_000,
        )
        ctx.last_projection_snapshot = projected.snapshot
        projected_mode = PressureMode(projected.mode)
        projected_modes.append(projected_mode)
        state_modes.append(PressureMode(ctx.projection_state.pressure_mode))
        forced_counts.append(ctx.projection_state.forced_critical_count)
        saw_critical = saw_critical or projected_mode == PressureMode.critical

        assert projected.policy is not None
        assert projected.snapshot is not None
        selected_hard_budget = min(
            projected.policy.hard_prompt_tokens,
            projected.snapshot.available_prompt_tokens,
        )
        assert projected.snapshot.prompt_tokens <= selected_hard_budget

        current_demotion_count = len(ctx.projection_state.demoted_anchors)
        current_prefix_messages = _prefix_messages(projected)
        if current_demotion_count > 0 and current_demotion_count == previous_demotion_count:
            common_prefix_len = min(len(previous_prefix_messages), len(current_prefix_messages))
            assert (
                json.dumps(
                    current_prefix_messages[:common_prefix_len],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                == json.dumps(
                    previous_prefix_messages[:common_prefix_len],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
        previous_prefix_messages = current_prefix_messages
        previous_demotion_count = current_demotion_count

        if exact_guard_candidate:
            assert ctx.projection_state.forced_critical_count == forced_before_projection + 1

    assert PressureMode.critical in projected_modes
    first_critical_index = projected_modes.index(PressureMode.critical)
    assert any(mode != PressureMode.critical for mode in state_modes[first_critical_index + 1 :])
    assert forced_counts[-1] >= 1
    assert forced_counts[-1] < len(projected_modes)

    assert ctx.projection_state is not None
    assert ctx.projection_state.recovery_per_source_counts == {"call-0": 2}
    assert max(ctx.projection_state.recovery_per_source_counts.values()) < 3
    assert ctx.projection_state.recovery_loop_detected_count == 1
    assert {"call-8", "call-9"}.issubset(ctx.projection_state.recovery_result_call_ids)

    assert PressureMode.pressure in cap_observed
    assert PressureMode.critical in cap_observed
    assert cap_observed[PressureMode.pressure] <= 25_000
    assert cap_observed[PressureMode.critical] <= 12_000
    assert all(
        saved_output.startswith(("tool-", "recovery-", "small-")) for _, saved_output in store.saved
    )
