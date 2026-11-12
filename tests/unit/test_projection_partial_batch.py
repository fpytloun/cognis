"""Partial parallel-batch pruning must not rewrite accepted sibling results."""

import base64
import io
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from cognis.core.agent_loop import AgentLoop
from cognis.core.context import events_to_messages
from cognis.core.context_projection import (
    ProjectionPolicy,
    ProjectionResult,
    ProjectionTurnState,
    apply_projection_decisions,
    compacted_tool_group_anchors,
    project_messages,
    projection_decisions_from_diff,
    prune_projected_messages,
)
from cognis.core.message_markers import PROJECTED_COMPACTED, PROTECTED_TOOL_OUTPUT
from cognis.core.session_cache import SessionCache
from cognis.models.session import SessionEvent
from cognis.providers.llm.message_projection import project_messages_for_provider
from cognis.providers.llm.responses_bridge import messages_to_responses_input
from tests.unit.test_context_delivery import _EventStore, _projection_runtime

LARGE_CALL = "call_abc5ab37a31e4a28a56da007bd49a5e8"
SIBLING_CALL = "call_d40d0c4bc9f0469f90068429563816f2"
COMPACT_CALL = "call_13406d8464da4fe49e1cf1c70986a40b"
INTERRUPTED = "[No result recorded - step may have been interrupted]"
COMPACT = {"operation": "compact_result", "rendering_policy_version": 1, "parameters": {}}


@pytest.mark.parametrize("pressure", ["pressure", "critical"])
@pytest.mark.parametrize("image_last", [False, True])
def test_fresh_parallel_image_reaches_first_consuming_request(
    pressure: str, image_last: bool
) -> None:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(output, format="PNG")
    image_url = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
    messages = _batch("Sibling output\n" * 4000)[:-1]
    image_call = SIBLING_CALL if image_last else LARGE_CALL
    image_context = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": image_url}}],
        "_tool_attachment_context": True,
        "_tool_call_id": image_call,
    }
    messages.append(image_context)
    loop = object.__new__(AgentLoop)
    loop.providers = SimpleNamespace(
        llm=SimpleNamespace(count_tokens=lambda text, _model: len(text) // 4)
    )
    projected = loop._project_model_messages(
        SimpleNamespace(),
        messages=messages,
        resolved_model="gpt-6.1-sol",
        max_context_tokens=20_000,
        pressure_mode=pressure,
    )
    wire = messages_to_responses_input(projected.messages)
    images = [
        part["image_url"]
        for item in wire
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "input_image"
    ]
    assert images == [image_url]
    # The following assistant response consumes the evidence. A later tool
    # batch must not pin the image forever.
    consumed = messages + [
        {
            "role": "assistant",
            "content": "I inspected the screenshot.",
            "tool_calls": [
                {
                    "id": "next-read",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "next-read", "content": "New evidence"},
    ]
    later = loop._project_model_messages(
        SimpleNamespace(),
        messages=consumed,
        resolved_model="gpt-6.1-sol",
        max_context_tokens=20_000,
        pressure_mode=pressure,
    )
    decisions = later.decisions
    assert {
        "operation": "compact_attachment",
        "rendering_policy_version": 2,
        "parameters": {},
    } in decisions[image_call]
    replay_wire = messages_to_responses_input(apply_projection_decisions(consumed, decisions))
    assert not any(
        part.get("type") == "input_image"
        for item in replay_wire
        for part in item.get("content", [])
        if isinstance(part, dict)
    )


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("pressure", ["pressure", "critical"])
def test_accepted_attachment_rendering_survives_reprojection(version: int, pressure: str) -> None:
    original = _batch("Sibling output\n" * 4000)
    original.insert(
        4,
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}}],
            "_tool_attachment_context": True,
            "_tool_attachment_artifact_ids": ["att_replay_image"],
            "_tool_call_id": LARGE_CALL,
        },
    )
    accepted = apply_projection_decisions(
        original,
        {
            LARGE_CALL: [
                {
                    "operation": "compact_attachment",
                    "rendering_policy_version": version,
                    "parameters": {},
                }
            ]
        },
    )
    placeholder = accepted[4]["content"]
    loop = object.__new__(AgentLoop)
    loop.providers = SimpleNamespace(
        llm=SimpleNamespace(count_tokens=lambda text, _model: len(text) // 4)
    )
    projected = loop._project_model_messages(
        SimpleNamespace(),
        messages=accepted,
        resolved_model="gpt-6.1-sol",
        max_context_tokens=20_000,
        pressure_mode=pressure,
    )
    assert any(message.get("content") == placeholder for message in projected.messages)
    assert not any(
        decision.get("operation") == "compact_attachment"
        and decision.get("rendering_policy_version") != version
        for decisions in projected.decisions.values()
        for decision in decisions
    )


def _batch(sibling: str) -> list[dict]:
    return [
        {"role": "user", "content": "Inspect earlier evidence."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
                for call_id in (LARGE_CALL, SIBLING_CALL)
            ],
        },
        {
            "role": "tool",
            "tool_call_id": LARGE_CALL,
            "content": "Large output\n" * 4000,
            "_tool_name": "read",
            "_recovery_call_id": LARGE_CALL,
        },
        {"role": "tool", "tool_call_id": SIBLING_CALL, "content": sibling},
        {"role": "user", "content": "Continue with the retained evidence."},
    ]


def _state(messages: list[dict], decisions: dict | None = None) -> ProjectionTurnState:
    state = ProjectionTurnState(
        turn_id="turn_partial_batch",
        policy=ProjectionPolicy.from_budget(max_context_tokens=240_000, phase="within_turn"),
    )
    state.seed_from_cross_turn_result(
        ProjectionResult(
            messages=messages,
            mutable_start_index=0,
            decisions=decisions or {},
        ),
        messages,
    )
    return state


@pytest.mark.parametrize(
    "sibling", [INTERRUPTED, "Retained ordinary output\n" * 200], ids=["interrupted", "ordinary"]
)
@pytest.mark.parametrize("cold_seed", [False, True])
@pytest.mark.parametrize("protection", ["ordinary", "protected", "recovery_pin"])
def test_partial_batch_demotion_preserves_untouched_sibling(
    sibling: str, cold_seed: bool, protection: str
) -> None:
    original = _batch(sibling)
    if protection == "protected":
        original[3][PROTECTED_TOOL_OUTPUT] = True
    state = _state(original)
    partial = prune_projected_messages(
        original,
        protect_tokens=len(sibling) + 1,
        minimum_savings=1,
        token_counter=len,
    )
    assert partial[2][PROJECTED_COMPACTED]
    assert partial[3]["content"] == sibling
    assert not compacted_tool_group_anchors(partial), "A partly pruned batch is not fully demoted"
    if cold_seed:
        state = _state(partial)
    else:
        state.record_demotions(compacted_tool_group_anchors(partial))
    if protection == "recovery_pin":
        state.recovery_result_call_ids.add(SIBLING_CALL)
    for _ in range(3):
        result = project_messages(partial, policy=state.policy, prior_state=state)
        assert result.messages == partial
        assert not projection_decisions_from_diff(partial, result.messages)
        assert result.messages[3]["content"] == sibling
        partial = result.messages


def test_genuine_critical_pressure_can_compact_retained_sibling() -> None:
    partial = apply_projection_decisions(
        _batch("Retained ordinary output\n" * 200), {LARGE_CALL: [COMPACT]}
    )
    state = _state(partial)
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=240_000, phase="within_turn", pressure_mode="critical"
    )
    # Remove recency protection by appending a newer complete batch.
    newer = deepcopy(partial[1:3])
    newer[0]["tool_calls"] = [deepcopy(newer[0]["tool_calls"][0])]
    newer[0]["tool_calls"][0]["id"] = COMPACT_CALL
    newer[1]["tool_call_id"] = COMPACT_CALL
    newer[1]["_recovery_call_id"] = COMPACT_CALL
    result = project_messages(
        partial + newer,
        policy=policy,
        prior_state=state,
    )
    assert result.messages[3][PROJECTED_COMPACTED]


@pytest.mark.parametrize("pending_decisions", [False, True])
@pytest.mark.parametrize("raw_retry", [False, True])
@pytest.mark.parametrize("cached_estimate", [0, 112_268])
def test_below_threshold_reuses_mixed_prefix_and_pending_decisions(
    pending_decisions: bool,
    raw_retry: bool,
    cached_estimate: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    # Include a fully demoted group too: its presence must not disable the fast path.
    original = _batch(INTERRUPTED)
    older = deepcopy(original[1:3])
    older[0]["tool_calls"] = [deepcopy(older[0]["tool_calls"][0])]
    older[0]["tool_calls"][0]["id"] = COMPACT_CALL
    older[1]["tool_call_id"] = COMPACT_CALL
    messages = apply_projection_decisions(
        [original[0], *older, *original[1:]],
        {LARGE_CALL: [COMPACT], COMPACT_CALL: [COMPACT]},
    )
    decisions = {LARGE_CALL: [COMPACT]} if pending_decisions else {}
    state = _state(messages, decisions)
    state.last_projected_token_estimate = cached_estimate
    assert state.demoted_anchors
    if pending_decisions and raw_retry:
        messages = deepcopy(messages)
        next(m for m in messages if m.get("tool_call_id") == LARGE_CALL)["content"] = original[2][
            "content"
        ]
        next(m for m in messages if m.get("tool_call_id") == LARGE_CALL).pop(PROJECTED_COMPACTED)
    loop = object.__new__(AgentLoop)
    loop.providers = SimpleNamespace(
        llm=SimpleNamespace(count_tokens=lambda text, _model: len(text) // 4)
    )
    loop._context_pressure_snapshot = Mock(
        return_value=SimpleNamespace(
            prompt_tokens=112_268, available_prompt_tokens=235_904, exceeded=False
        )
    )
    loop._project_model_messages = Mock(wraps=loop._project_model_messages)
    ctx = SimpleNamespace(
        projection_state=state,
        turn_id=state.turn_id,
        current_model="gpt-6.1-sol",
        current_model_info=None,
        agent=SimpleNamespace(llm_config=None),
    )
    for _ in range(2):
        projected = loop._project_model_messages_for_budget(
            ctx,
            messages=messages,
            tool_schemas=[],
            resolved_model=ctx.current_model,
            max_context_tokens=240_000,
        )
        assert projected.mode == "normal"
        assert projected.decisions == decisions
        assert state.last_result.decisions == decisions
        assert (
            next(m for m in projected.messages if m.get("tool_call_id") == LARGE_CALL)["content"]
            != original[2]["content"]
        )
        assert (
            next(m for m in projected.messages if m.get("tool_call_id") == SIBLING_CALL)["content"]
            == INTERRUPTED
        )
    loop._project_model_messages.assert_not_called()
    logs = [r.extra_data for r in caplog.records if r.getMessage() == "Context projection decision"]
    if pending_decisions:
        assert logs
        assert all(r["projection_reason"] == "cached_rendering" for r in logs)
        assert all(r["projection_before_prompt_tokens"] == 112_268 for r in logs)
        assert all(r["projection_trigger_tokens"] is None for r in logs)
        assert all(r["projection_entry_trigger_tokens"] > 112_268 for r in logs)
        assert INTERRUPTED not in json.dumps(logs)
    else:
        assert not logs


@pytest.mark.parametrize("calibration", [0.8, 1.0, 1.2])
def test_cross_turn_projection_reports_actual_budget_reason(caplog, calibration):
    from cognis.core.context import ContextAssembler
    from tests.unit.test_context_assembler import _LLM

    caplog.set_level("INFO")
    assembler = object.__new__(ContextAssembler)
    assembler.llm = _LLM()
    messages = _batch(INTERRUPTED)
    messages[2]["content"] = "Large output\n" * 20_000
    result = assembler._finalize_assembled_messages(
        messages=messages,
        resolved_model="test-model",
        tool_schema_tokens=0,
        max_context_tokens=20_000,
        available_prompt_tokens=16_000,
        effective_input_tokens=18_000,
        calibration_ratio=calibration,
        max_prompt_tokens=16_000,
        session_id="sess_budget_diagnostics",
    )
    assert result[-1]
    logs = [r.extra_data for r in caplog.records if r.getMessage() == "Context projection decision"]
    assert logs[-1]["projection_reason"] == "cross_turn_threshold"
    assert logs[-1]["projection_phase"] == "cross_turn"
    assert logs[-1]["projection_trigger_tokens"] == 13_500
    assert logs[-1]["projection_steady_target_tokens"] == 6_300
    assert logs[-1]["projection_savings_target_tokens"] == 6_300
    assert logs[-1]["projection_before_prompt_tokens"] > logs[-1]["projection_trigger_tokens"]
    assert logs[-1]["projection_after_prompt_tokens"] < logs[-1]["projection_before_prompt_tokens"]
    assert INTERRUPTED not in json.dumps(logs)


def test_within_turn_diagnostic_distinguishes_steady_target_from_entry_trigger():
    from cognis.core.context_projection import projection_diagnostic_fields

    policy = ProjectionPolicy.from_budget(
        max_context_tokens=320_000, available_prompt_tokens=300_000, phase="within_turn"
    )
    fields = projection_diagnostic_fields(
        policy,
        reason="steady_target",
        before_prompt_tokens=215_000,
        after_prompt_tokens=112_268,
        trigger_tokens=policy.steady_target_tokens,
        entry_trigger_tokens=225_000,
    )
    assert fields["projection_trigger_tokens"] == policy.steady_target_tokens
    assert fields["projection_trigger_tokens"] != fields["projection_entry_trigger_tokens"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_marker_decision", [False, True])
async def test_partial_batch_delivery_and_cold_replay_keep_provider_prefix(
    legacy_marker_decision: bool,
) -> None:
    store = _EventStore()
    await store.record_events(
        events=[
            SessionEvent(type="user_message", data={"content": "Inspect earlier evidence."}),
            *[
                SessionEvent(
                    type="tool_call",
                    data={"call_id": cid, "name": "read", "arguments": {}},
                )
                for cid in (LARGE_CALL, SIBLING_CALL)
            ],
            SessionEvent(
                type="tool_result",
                data={
                    "call_id": LARGE_CALL,
                    "name": "read",
                    "result": "Large output\n" * 4000,
                    "recovery_call_id": LARGE_CALL,
                },
            ),
            SessionEvent(type="user_message", data={"content": "Continue."}),
        ],
        idempotency_key="partial_batch_initial",
    )
    session, agent, cache, assembler = await _projection_runtime(store)
    cold = SessionCache(store)
    try:
        decisions = {LARGE_CALL: [COMPACT]}
        if legacy_marker_decision:
            decisions[SIBLING_CALL] = [COMPACT]
        raw = events_to_messages(store.events)
        marker = next(m for m in raw if m.get("tool_call_id") == SIBLING_CALL)
        assert marker["content"] == INTERRUPTED
        assert marker[PROTECTED_TOOL_OUTPUT]
        accepted = apply_projection_decisions(raw, decisions)
        if legacy_marker_decision:
            assert (
                len(next(m for m in accepted if m.get("tool_call_id") == SIBLING_CALL)["content"])
                == 140
            )
        loop = object.__new__(AgentLoop)
        loop.context_assembler = assembler
        loop._record_events_strict = AsyncMock()
        ctx = SimpleNamespace(
            session=session,
            agent=agent,
            turn_id="turn_partial_batch",
            execution_fence=None,
            pending_events=[],
        )
        await loop._commit_context_delivery(
            ctx, audit_messages=[], projection_decisions=decisions, on_token=None
        )
        await cold.refresh(session)
        replay = events_to_messages(
            cold.get_events_since_compaction(session.session_id),
            projection_decisions=cold.get_projection_decisions(session.session_id),
        )
        state = _state(replay)
        projected = project_messages(replay, policy=state.policy, prior_state=state)
        assert not projection_decisions_from_diff(replay, projected.messages)
        for provider, api in (
            (SimpleNamespace(config={"preset": "anthropic"}), "chat"),
            (None, "responses"),
        ):
            prior = project_messages_for_provider(accepted, provider=provider, llm_api=api).messages
            following = project_messages_for_provider(
                projected.messages + [{"role": "user", "content": "Next request."}],
                provider=provider,
                llm_api=api,
            ).messages
            assert following[: len(prior)] == prior
    finally:
        await cache.aclose()
        await cold.aclose()
