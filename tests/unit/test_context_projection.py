from __future__ import annotations

from cognis.core.context import _compact_oldest_droppable_tool_group
from cognis.core.context_projection import (
    CRITICAL_ESCALATE_FRACTION,
    PRESSURE_ESCALATE_FRACTION,
    PressureMode,
    PressureSnapshot,
    ProjectionPolicy,
    ProjectionResult,
    ProjectionTurnState,
    ReprojectDecision,
    decide_pressure_mode,
    project_messages,
    should_reproject,
)
from cognis.core.invariants import check_projection_monotonicity
from cognis.core.message_markers import TURN_BOUNDARY
from cognis.core.pruning import prune_tool_outputs


def _assistant_tool_call(call_id: str, name: str, arguments: object) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _tool_result(
    call_id: str,
    content: str,
    *,
    tool_name: str,
    recovery_call_id: str | None = None,
) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        "_tool_name": tool_name,
        "_recovery_call_id": recovery_call_id,
        "_output_size": len(content),
    }


def test_project_messages_compacts_older_completed_tool_groups() -> None:
    big_args = {"file_path": "a.py", "content": "x" * 7_000}
    messages = [
        _assistant_tool_call("call-1", "bash", big_args),
        _tool_result("call-1", "A" * 8_000, tool_name="bash", recovery_call_id="call-1"),
        _assistant_tool_call("call-2", "read", {"path": "a.py"}),
        _tool_result("call-2", "recent 1", tool_name="read", recovery_call_id="call-2"),
        _assistant_tool_call("call-3", "grep", {"pattern": "needle"}),
        _tool_result("call-3", "recent 2", tool_name="grep", recovery_call_id="call-3"),
    ]

    result = project_messages(messages, preserve_recent_completed_tool_groups=2)

    assert result.mutable_start_index == 2
    assert "Tool output omitted from prompt." in str(result.messages[1]["content"])
    assert "call_id 'call-1'" in str(result.messages[1]["content"])
    assert "Recover with" in str(result.messages[1]["content"])
    assert "read_tool_output(call_id='call-1')" in str(result.messages[1]["content"])
    assert "search_tool_output(call_id='call-1'" in str(result.messages[1]["content"])
    assistant_args = result.messages[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(assistant_args, str)
    assert "Arguments cleared -" in assistant_args
    assert '"file_path": "a.py"' in assistant_args
    assert '"content_preview":' in assistant_args
    assert result.messages[3]["content"] == "recent 1"
    assert result.messages[5]["content"] == "recent 2"


def test_projection_policy_scales_context_windows_sublinearly() -> None:
    small = ProjectionPolicy.from_budget(
        max_context_tokens=128_000,
        available_prompt_tokens=112_000,
        phase="within_turn",
    )
    medium = ProjectionPolicy.from_budget(
        max_context_tokens=272_000,
        available_prompt_tokens=240_000,
        phase="within_turn",
    )
    huge = ProjectionPolicy.from_budget(
        max_context_tokens=1_000_000,
        available_prompt_tokens=940_000,
        phase="within_turn",
    )

    assert small.steady_target_tokens == 95_000
    assert small.burst_target_tokens == 106_400
    assert small.within_turn_tool_budget_tokens == 40_432
    assert medium.steady_target_tokens == 180_000
    assert medium.burst_target_tokens == 228_000
    assert medium.within_turn_tool_budget_tokens == 86_640
    assert huge.steady_target_tokens == 320_000
    assert huge.burst_target_tokens == 600_000
    assert huge.within_turn_tool_budget_tokens == 228_000


def test_projection_policy_cross_turn_is_more_conservative_than_within_turn() -> None:
    cross_turn = ProjectionPolicy.from_budget(
        max_context_tokens=272_000,
        available_prompt_tokens=240_000,
        phase="cross_turn",
    )
    within_turn = ProjectionPolicy.from_budget(
        max_context_tokens=272_000,
        available_prompt_tokens=240_000,
        phase="within_turn",
    )

    assert within_turn.within_turn_tool_budget_tokens > cross_turn.cross_turn_tool_budget_tokens
    assert (
        within_turn.preserve_recent_completed_tool_bytes
        > cross_turn.preserve_recent_completed_tool_bytes
    )
    assert (
        within_turn.preserve_recent_completed_tool_groups
        > cross_turn.preserve_recent_completed_tool_groups
    )


def test_projection_policy_pressure_modes_reduce_tool_retention() -> None:
    normal = ProjectionPolicy.from_budget(
        max_context_tokens=272_000,
        available_prompt_tokens=240_000,
        phase="within_turn",
        pressure_mode="normal",
    )
    pressure = ProjectionPolicy.from_budget(
        max_context_tokens=272_000,
        available_prompt_tokens=240_000,
        phase="within_turn",
        pressure_mode="pressure",
    )
    critical = ProjectionPolicy.from_budget(
        max_context_tokens=272_000,
        available_prompt_tokens=240_000,
        phase="within_turn",
        pressure_mode="critical",
    )

    assert (
        normal.preserve_recent_completed_tool_bytes > pressure.preserve_recent_completed_tool_bytes
    )
    assert (
        pressure.preserve_recent_completed_tool_bytes
        > critical.preserve_recent_completed_tool_bytes
    )
    assert normal.prune_protect_tokens > pressure.prune_protect_tokens
    assert pressure.prune_protect_tokens > critical.prune_protect_tokens


def test_project_messages_uses_helper_recovery_call_id_for_helper_results() -> None:
    messages = [
        _assistant_tool_call("helper-call", "read_tool_output", {"call_id": "source-call"}),
        {
            **_tool_result(
                "helper-call",
                "helper output",
                tool_name="read_tool_output",
                recovery_call_id="helper-call",
            ),
            "_source_call_id": "source-call",
        },
    ]

    result = project_messages(messages, preserve_recent_completed_tool_groups=0)

    placeholder = str(result.messages[1]["content"])
    assert "call_id 'helper-call'" in placeholder
    assert "source call_id 'source-call'" in placeholder
    assert "anchor='result:1'" not in placeholder
    assert "read_tool_output(call_id='helper-call')" in placeholder


def test_project_messages_preserves_recent_groups_until_byte_budget_is_hit() -> None:
    messages = [
        _assistant_tool_call("call-1", "bash", {"command": "one"}),
        _tool_result("call-1", "A" * 8_000, tool_name="bash", recovery_call_id="call-1"),
        _assistant_tool_call("call-2", "read", {"path": "a.py"}),
        _tool_result("call-2", "B" * 8_000, tool_name="read", recovery_call_id="call-2"),
        _assistant_tool_call("call-3", "grep", {"pattern": "needle"}),
        _tool_result("call-3", "C" * 200, tool_name="grep", recovery_call_id="call-3"),
    ]

    result = project_messages(
        messages,
        preserve_recent_completed_tool_groups=3,
        preserve_recent_completed_tool_bytes=4_000,
    )

    # The oldest preserved groups are dropped until the preserved tail fits the
    # byte budget, but the newest group is always retained in full.
    assert "Tool output omitted from prompt." in str(result.messages[1]["content"])
    assert "Tool output omitted from prompt." in str(result.messages[3]["content"])
    assert result.messages[5]["content"] == "C" * 200


def test_project_messages_defaults_keep_safer_verbatim_tool_tail() -> None:
    messages: list[dict[str, object]] = []
    for index in range(12):
        call_id = f"call-{index}"
        messages.extend(
            [
                _assistant_tool_call(call_id, "read", {"path": f"file-{index}.py"}),
                _tool_result(
                    call_id, f"evidence {index}", tool_name="read", recovery_call_id=call_id
                ),
            ]
        )

    result = project_messages(messages)

    compacted_contents = [
        str(message.get("content", ""))
        for message in result.messages
        if "Tool output omitted from prompt." in str(message.get("content", ""))
    ]
    assert len(compacted_contents) == 2
    assert "call_id 'call-0'" in compacted_contents[0]
    assert "call_id 'call-1'" in compacted_contents[1]
    assert result.messages[-1]["content"] == "evidence 11"


def test_project_messages_compacts_oversized_historical_recoverable_tail() -> None:
    messages: list[dict[str, object]] = []
    for index in range(3):
        call_id = f"call-{index}"
        messages.extend(
            [
                _assistant_tool_call(call_id, "bash", {"command": f"large-{index}"}),
                _tool_result(
                    call_id,
                    "X" * 25_000,
                    tool_name="bash",
                    recovery_call_id=call_id,
                ),
            ]
        )
    messages.extend(
        [
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "continue"},
        ]
    )

    result = project_messages(
        messages,
        preserve_recent_completed_tool_groups=3,
        preserve_recent_completed_tool_bytes=200_000,
        max_historical_tool_result_bytes=20_000,
    )

    assert "Tool output omitted from prompt." in str(result.messages[1]["content"])
    assert "Tool output omitted from prompt." in str(result.messages[3]["content"])
    assert "Tool output omitted from prompt." in str(result.messages[5]["content"])
    assert "X" * 1_000 not in str(result.messages)


def test_project_messages_preserves_bounded_historical_recoverable_tail() -> None:
    messages = [
        _assistant_tool_call("call-1", "read", {"path": "small.txt"}),
        _tool_result(
            "call-1",
            "small recovered evidence",
            tool_name="read",
            recovery_call_id="call-1",
        ),
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "continue"},
    ]

    result = project_messages(
        messages,
        preserve_recent_completed_tool_groups=1,
        max_historical_tool_result_bytes=20_000,
    )

    assert result.messages[1]["content"] == "small recovered evidence"


def test_project_messages_preserves_oversized_latest_turn_tool_output() -> None:
    messages = [
        {"role": "user", "content": "summarize current logs"},
        _assistant_tool_call("call-1", "bash", {"command": "kubectl logs"}),
        _tool_result(
            "call-1",
            "L" * 25_000,
            tool_name="bash",
            recovery_call_id="call-1",
        ),
    ]

    result = project_messages(
        messages,
        preserve_recent_completed_tool_groups=0,
        max_historical_tool_result_bytes=20_000,
    )

    assert result.messages[2]["content"] == "L" * 25_000


def test_project_messages_pressure_mode_compacts_older_latest_turn_tool_outputs() -> None:
    messages = [{"role": "user", "content": "summarize current logs"}]
    for index in range(3):
        call_id = f"call-{index}"
        messages.extend(
            [
                _assistant_tool_call(call_id, "bash", {"command": f"kubectl logs {index}"}),
                _tool_result(
                    call_id,
                    f"log output {index}" * 1000,
                    tool_name="bash",
                    recovery_call_id=call_id,
                ),
            ]
        )

    result = project_messages(
        messages,
        preserve_recent_completed_tool_groups=0,
        pressure_mode="pressure",
    )

    assert "Tool output omitted from prompt." in str(result.messages[2]["content"])
    assert "call_id 'call-0'" in str(result.messages[2]["content"])
    assert "Tool output omitted from prompt." in str(result.messages[4]["content"])
    assert "call_id 'call-1'" in str(result.messages[4]["content"])
    assert result.messages[6]["content"] == "log output 2" * 1000


def test_project_messages_preserves_oversized_protected_historical_output() -> None:
    messages = [
        _assistant_tool_call("call-1", "step_todo_write", {"todos": []}),
        {
            **_tool_result(
                "call-1",
                "P" * 25_000,
                tool_name="step_todo_write",
                recovery_call_id="call-1",
            ),
            "_protected_tool_output": True,
        },
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "continue"},
    ]

    result = project_messages(
        messages,
        preserve_recent_completed_tool_groups=1,
        max_historical_tool_result_bytes=20_000,
    )

    assert result.messages[1]["content"] == "P" * 25_000


def test_prune_tool_outputs_only_modifies_mutable_tail() -> None:
    messages = [
        _assistant_tool_call("stable-call", "bash", {"command": "ls"}),
        _tool_result(
            "stable-call",
            "S" * 8_000,
            tool_name="bash",
            recovery_call_id="stable-call",
        ),
        _assistant_tool_call("tail-call", "read_tool_output", {"call_id": "source-call"}),
        _tool_result(
            "tail-call",
            "T" * 8_000,
            tool_name="read_tool_output",
            recovery_call_id="source-call",
        ),
    ]

    result = prune_tool_outputs(
        messages,
        protect_tokens=0,
        minimum_savings=1,
        min_index_to_modify=2,
        token_counter=lambda text: len(text),
    )

    assert result[1]["content"] == messages[1]["content"]
    assert "Tool output omitted from prompt." in str(result[3]["content"])
    assert "call_id 'source-call'" in str(result[3]["content"])


def test_controller_critical_tool_results_are_preserved() -> None:
    messages = [
        _assistant_tool_call("call-1", "step_todo_write", {"todos": []}),
        {
            **_tool_result(
                "call-1",
                "terminal todos",
                tool_name="step_todo_write",
                recovery_call_id="call-1",
            ),
            "_protected_tool_output": True,
        },
        _assistant_tool_call("call-2", "read", {"path": "a.py"}),
        _tool_result("call-2", "large" * 2000, tool_name="read", recovery_call_id="call-2"),
    ]

    result = project_messages(messages, preserve_recent_completed_tool_groups=0)

    assert result.messages[1]["content"] == "terminal todos"
    assert "Tool output omitted from prompt." in str(result.messages[3]["content"])


def test_project_messages_preserves_all_tool_outputs_in_latest_turn() -> None:
    messages: list[dict[str, object]] = [{"role": "user", "content": "daily brief"}]
    for index in range(15):
        call_id = f"call-{index}"
        messages.extend(
            [
                _assistant_tool_call(call_id, "read_tool_output", {"call_id": f"source-{index}"}),
                _tool_result(
                    call_id,
                    f"evidence {index}" * 100,
                    tool_name="read_tool_output",
                    recovery_call_id=call_id,
                ),
            ]
        )

    result = project_messages(messages, preserve_recent_completed_tool_groups=2)

    assert all(
        "Tool output omitted from prompt." not in str(message.get("content", ""))
        for message in result.messages
    )
    assert "evidence 0" in str(result.messages[2]["content"])
    assert "evidence 14" in str(result.messages[-1]["content"])


def test_project_messages_critical_mode_preserves_protected_and_newest_latest_turn_outputs() -> (
    None
):
    messages: list[dict[str, object]] = [{"role": "user", "content": "daily brief"}]
    messages.extend(
        [
            _assistant_tool_call("call-protected", "step_todo_write", {"todos": []}),
            {
                **_tool_result(
                    "call-protected",
                    "terminal todos",
                    tool_name="step_todo_write",
                    recovery_call_id="call-protected",
                ),
                "_protected_tool_output": True,
            },
        ]
    )
    for index in range(2):
        call_id = f"call-{index}"
        messages.extend(
            [
                _assistant_tool_call(call_id, "read", {"path": f"file-{index}.py"}),
                _tool_result(
                    call_id,
                    f"evidence {index}" * 1000,
                    tool_name="read",
                    recovery_call_id=call_id,
                ),
            ]
        )

    result = project_messages(messages, pressure_mode="critical")

    assert result.messages[2]["content"] == "terminal todos"
    assert "Tool output omitted from prompt." in str(result.messages[4]["content"])
    assert "call_id 'call-0'" in str(result.messages[4]["content"])
    assert result.messages[6]["content"] == "evidence 1" * 1000


def test_project_messages_compacts_prior_turn_but_preserves_latest_turn() -> None:
    messages = [
        {"role": "user", "content": "old task"},
        _assistant_tool_call("old-call", "bash", {"command": "expensive"}),
        _tool_result(
            "old-call", "old evidence" * 1000, tool_name="bash", recovery_call_id="old-call"
        ),
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "daily brief"},
        _assistant_tool_call(
            "agenda-call", "mcp_todoist__find-tasks-by-date", {"startDate": "today"}
        ),
        _tool_result(
            "agenda-call",
            "Todoist actual agenda: pay tax, review PR",
            tool_name="mcp_todoist__find-tasks-by-date",
            recovery_call_id="agenda-call",
        ),
    ]

    result = project_messages(messages, preserve_recent_completed_tool_groups=0)

    assert "Tool output omitted from prompt." in str(result.messages[2]["content"])
    assert result.messages[6]["content"] == "Todoist actual agenda: pay tax, review PR"


def test_project_messages_preserves_latest_system_workflow_turn() -> None:
    # Use the TURN_BOUNDARY marker (replaces the old "## Workflow Task" string sniff).
    messages = [
        {"role": "user", "content": "old task"},
        _assistant_tool_call("old-call", "bash", {"command": "expensive"}),
        _tool_result(
            "old-call", "old evidence" * 1000, tool_name="bash", recovery_call_id="old-call"
        ),
        {"role": "assistant", "content": "old answer"},
        {"role": "system", "content": "## Workflow Task\n\nDaily brief", "_turn_boundary": True},
        _assistant_tool_call(
            "agenda-call", "mcp_todoist__find-tasks-by-date", {"startDate": "today"}
        ),
        _tool_result(
            "agenda-call",
            "Todoist actual agenda from system workflow turn",
            tool_name="mcp_todoist__find-tasks-by-date",
            recovery_call_id="agenda-call",
        ),
    ]

    result = project_messages(messages, preserve_recent_completed_tool_groups=0)

    assert "Tool output omitted from prompt." in str(result.messages[2]["content"])
    assert result.messages[6]["content"] == "Todoist actual agenda from system workflow turn"


def test_project_messages_preserves_workflow_outputs_before_retry_system_directive() -> None:
    messages: list[dict[str, object]] = [{"role": "user", "content": "## Workflow Task\n\nReview"}]
    for index in range(12):
        call_id = f"call-{index}"
        messages.extend(
            [
                _assistant_tool_call(call_id, "read", {"path": f"file_{index}.py"}),
                _tool_result(
                    call_id,
                    f"prior attempt evidence {index}" * 200,
                    tool_name="read",
                    recovery_call_id=call_id,
                ),
            ]
        )
    messages.append(
        {
            "role": "system",
            "content": "The previous attempt ended before producing a final response.",
        }
    )

    result = project_messages(messages, preserve_recent_completed_tool_groups=0)

    assert all(
        "Tool output omitted from prompt." not in str(message.get("content", ""))
        for message in result.messages
    )
    assert "prior attempt evidence 0" in str(result.messages[2]["content"])
    assert "prior attempt evidence 11" in str(result.messages[-2]["content"])


def test_context_pressure_compacts_tool_group_before_drop() -> None:
    messages = [
        _assistant_tool_call("call-1", "read", {"path": "large.py"}),
        _tool_result("call-1", "large output" * 1000, tool_name="read", recovery_call_id="call-1"),
        {"role": "system", "content": "The previous attempt ended before producing output."},
    ]

    compacted = _compact_oldest_droppable_tool_group(messages)

    assert compacted is not None
    assert compacted[0]["role"] == "assistant"
    assert compacted[1]["tool_call_id"] == "call-1"
    assert "Tool output omitted from prompt." in str(compacted[1]["content"])
    assert "read_tool_output(call_id='call-1')" in str(compacted[1]["content"])


def test_prune_tool_outputs_preserves_latest_turn_recovered_evidence() -> None:
    messages = [
        {"role": "user", "content": "old task"},
        _assistant_tool_call("old-call", "bash", {"command": "expensive"}),
        _tool_result("old-call", "O" * 50_000, tool_name="bash", recovery_call_id="old-call"),
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "daily brief"},
        _assistant_tool_call("recover-call", "read_tool_output", {"call_id": "agenda-call"}),
        _tool_result(
            "recover-call",
            "Recovered Todoist actual agenda" * 2000,
            tool_name="read_tool_output",
            recovery_call_id="recover-call",
        ),
    ]

    result = prune_tool_outputs(
        messages,
        protect_tokens=0,
        minimum_savings=1,
        token_counter=lambda text: len(text),
    )

    assert "Tool output omitted from prompt." in str(result[2]["content"])
    assert result[6]["content"] == "Recovered Todoist actual agenda" * 2000


def test_prune_tool_outputs_pressure_mode_can_prune_latest_turn_recovered_evidence() -> None:
    messages = [
        {"role": "user", "content": "daily brief"},
        _assistant_tool_call("recover-call-1", "read_tool_output", {"call_id": "agenda-call"}),
        _tool_result(
            "recover-call-1",
            "Recovered old agenda" * 2000,
            tool_name="read_tool_output",
            recovery_call_id="recover-call-1",
        ),
        _assistant_tool_call("recover-call-2", "read_tool_output", {"call_id": "weather-call"}),
        _tool_result(
            "recover-call-2",
            "Recovered current weather",
            tool_name="read_tool_output",
            recovery_call_id="recover-call-2",
        ),
    ]

    result = prune_tool_outputs(
        messages,
        protect_tokens=0,
        minimum_savings=1,
        pressure_mode="pressure",
        token_counter=lambda text: len(text),
    )

    assert "Tool output omitted from prompt." in str(result[2]["content"])
    assert "call_id 'recover-call-1'" in str(result[2]["content"])
    assert result[4]["content"] == "Recovered current weather"


def test_prune_tool_outputs_pressure_mode_preserves_unresolved_latest_tool_call() -> None:
    messages = [
        {"role": "user", "content": "daily brief"},
        _assistant_tool_call("recover-call-1", "read_tool_output", {"call_id": "agenda-call"}),
        _tool_result(
            "recover-call-1",
            "Recovered old agenda" * 2000,
            tool_name="read_tool_output",
            recovery_call_id="recover-call-1",
        ),
        _assistant_tool_call("pending-call", "web_search", {"query": "weather"}),
    ]

    result = prune_tool_outputs(
        messages,
        protect_tokens=0,
        minimum_savings=1,
        pressure_mode="pressure",
        token_counter=lambda text: len(text),
    )

    assert result[2]["content"] == "Recovered old agenda" * 2000
    assert result[3] == messages[3]


def test_prune_tool_outputs_does_not_treat_attachment_user_as_turn_boundary() -> None:
    messages = [
        {"role": "user", "content": "daily brief"},
        _assistant_tool_call(
            "agenda-call", "mcp_todoist__find-tasks-by-date", {"startDate": "today"}
        ),
        _tool_result(
            "agenda-call",
            "Todoist actual agenda" * 2000,
            tool_name="mcp_todoist__find-tasks-by-date",
            recovery_call_id="agenda-call",
        ),
        {
            "role": "user",
            "content": "Tool attachment context",
            "_tool_attachment_context": True,
            "_tool_call_id": "agenda-call",
        },
    ]

    result = prune_tool_outputs(
        messages,
        protect_tokens=0,
        minimum_savings=1,
        token_counter=lambda text: len(text),
    )

    assert result[2]["content"] == "Todoist actual agenda" * 2000


# ── New tests for Slice 1 ─────────────────────────────────────────────────────


def _make_snapshot(
    prompt_tokens: int,
    available: int = 100_000,
    oversized: bool = False,
) -> PressureSnapshot:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000,
        available_prompt_tokens=available,
    )
    return PressureSnapshot(
        prompt_tokens=prompt_tokens,
        available_prompt_tokens=available,
        steady_target_tokens=policy.steady_target_tokens,
        hard_prompt_tokens=policy.hard_prompt_tokens,
        oversized_result_appended=oversized,
    )


# ── decide_pressure_mode ──────────────────────────────────────────────────────


def test_decide_pressure_mode_normal_stays_normal_below_threshold() -> None:
    snap = _make_snapshot(int(100_000 * 0.80))  # 80% — below 92%
    mode, under = decide_pressure_mode(snap, PressureMode.normal, under_threshold_cycles=0)
    assert mode == PressureMode.normal
    assert under == 1


def test_decide_pressure_mode_escalates_to_pressure_at_threshold() -> None:
    snap = _make_snapshot(int(100_000 * PRESSURE_ESCALATE_FRACTION))
    mode, under = decide_pressure_mode(snap, PressureMode.normal, under_threshold_cycles=0)
    assert mode == PressureMode.pressure
    assert under == 0


def test_decide_pressure_mode_escalates_to_critical_at_high_threshold() -> None:
    snap = _make_snapshot(int(100_000 * CRITICAL_ESCALATE_FRACTION))
    mode, under = decide_pressure_mode(snap, PressureMode.pressure, under_threshold_cycles=0)
    assert mode == PressureMode.critical


def test_decide_pressure_mode_oversized_forces_critical() -> None:
    snap = _make_snapshot(int(100_000 * 0.50), oversized=True)
    mode, _ = decide_pressure_mode(snap, PressureMode.normal, under_threshold_cycles=0)
    assert mode == PressureMode.critical


def test_decide_pressure_mode_demotion_requires_two_cycles() -> None:
    snap = _make_snapshot(int(100_000 * 0.50))  # well below threshold
    # First under-threshold cycle: still pressure
    mode, under = decide_pressure_mode(snap, PressureMode.pressure, under_threshold_cycles=0)
    assert mode == PressureMode.pressure
    assert under == 1
    # Second under-threshold cycle: demote to normal
    mode, under = decide_pressure_mode(snap, PressureMode.pressure, under_threshold_cycles=1)
    assert mode == PressureMode.normal
    assert under == 0


def test_decide_pressure_mode_critical_demotes_to_pressure_first() -> None:
    snap = _make_snapshot(int(100_000 * 0.50))
    # First cycle under threshold from critical
    mode, under = decide_pressure_mode(snap, PressureMode.critical, under_threshold_cycles=0)
    assert mode == PressureMode.critical
    assert under == 1
    # Second cycle: demote to pressure (not straight to normal)
    mode, under = decide_pressure_mode(snap, PressureMode.critical, under_threshold_cycles=1)
    assert mode == PressureMode.pressure
    assert under == 0


# ── should_reproject ──────────────────────────────────────────────────────────


def test_should_reproject_skips_when_below_steady_target() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    decision = should_reproject(
        new_message_count=10,
        last_message_count=8,
        new_token_estimate=int(policy.steady_target_tokens * 0.5),
        steady_target_tokens=policy.steady_target_tokens,
        pressure_mode=PressureMode.normal,
        prior_pressure_mode=PressureMode.normal,
        oversized_appended=False,
    )
    assert decision == ReprojectDecision.skip


def test_should_reproject_reprojects_when_crossing_steady_target() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    decision = should_reproject(
        new_message_count=10,
        last_message_count=8,
        new_token_estimate=policy.steady_target_tokens + 1,
        steady_target_tokens=policy.steady_target_tokens,
        pressure_mode=PressureMode.normal,
        prior_pressure_mode=PressureMode.normal,
        oversized_appended=False,
    )
    assert decision == ReprojectDecision.reproject


def test_should_reproject_critical_always_critical_reproject() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    decision = should_reproject(
        new_message_count=5,
        last_message_count=5,
        new_token_estimate=1_000,
        steady_target_tokens=policy.steady_target_tokens,
        pressure_mode=PressureMode.critical,
        prior_pressure_mode=PressureMode.critical,
        oversized_appended=False,
    )
    assert decision == ReprojectDecision.critical_reproject


def test_should_reproject_oversized_forces_reproject() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    decision = should_reproject(
        new_message_count=5,
        last_message_count=4,
        new_token_estimate=1_000,
        steady_target_tokens=policy.steady_target_tokens,
        pressure_mode=PressureMode.normal,
        prior_pressure_mode=PressureMode.normal,
        oversized_appended=True,
    )
    assert decision == ReprojectDecision.reproject


def test_should_reproject_mode_escalation_forces_reproject() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    decision = should_reproject(
        new_message_count=5,
        last_message_count=4,
        new_token_estimate=1_000,
        steady_target_tokens=policy.steady_target_tokens,
        pressure_mode=PressureMode.pressure,  # escalated this cycle
        prior_pressure_mode=PressureMode.normal,
        oversized_appended=False,
    )
    assert decision == ReprojectDecision.reproject


# ── ProjectionResult.append_tail ─────────────────────────────────────────────


def test_projection_result_append_tail_preserves_mutable_start_index() -> None:
    base = ProjectionResult(
        messages=[{"role": "user", "content": "hello"}],
        mutable_start_index=0,
    )
    tail = [{"role": "assistant", "content": "world"}]
    extended = base.append_tail(tail)
    assert len(extended.messages) == 2
    assert extended.mutable_start_index == 0  # unchanged
    assert extended.messages[1]["content"] == "world"


def test_projection_result_append_tail_does_not_mutate_original() -> None:
    base = ProjectionResult(
        messages=[{"role": "user", "content": "hello"}],
        mutable_start_index=0,
    )
    base.append_tail([{"role": "assistant", "content": "world"}])
    assert len(base.messages) == 1


# ── Monotonic preservation ────────────────────────────────────────────────────


def test_project_messages_respects_committed_preservations() -> None:
    """A group committed in cycle 1 must not be demoted in cycle 2 under normal pressure."""
    messages = [
        {"role": "user", "content": "task"},
        _assistant_tool_call("call-1", "read", {"path": "a.py"}),
        _tool_result("call-1", "content of a.py", tool_name="read", recovery_call_id="call-1"),
        _assistant_tool_call("call-2", "read", {"path": "b.py"}),
        _tool_result("call-2", "content of b.py", tool_name="read", recovery_call_id="call-2"),
    ]

    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000,
        available_prompt_tokens=100_000,
        phase="within_turn",
        pressure_mode=PressureMode.normal,
    )
    state = ProjectionTurnState(turn_id="t1", policy=policy)

    # Cycle 1: project with preserve_recent=1 — call-1 gets compacted, call-2 preserved.
    result1 = project_messages(
        messages,
        policy=ProjectionPolicy.from_budget(
            max_context_tokens=200_000,
            available_prompt_tokens=100_000,
            phase="within_turn",
            pressure_mode=PressureMode.normal,
        ),
        prior_state=state,
    )
    # call-2 should be preserved (most recent).
    assert "content of b.py" in str(result1.messages)

    # Cycle 2: add more messages; call-2 must remain preserved (committed).
    messages_cycle2 = list(messages) + [
        _assistant_tool_call("call-3", "read", {"path": "c.py"}),
        _tool_result("call-3", "content of c.py", tool_name="read", recovery_call_id="call-3"),
    ]
    result2 = project_messages(
        messages_cycle2,
        policy=ProjectionPolicy.from_budget(
            max_context_tokens=200_000,
            available_prompt_tokens=100_000,
            phase="within_turn",
            pressure_mode=PressureMode.normal,
        ),
        prior_state=state,
    )
    # call-2 must still be preserved (committed in cycle 1).
    assert "content of b.py" in str(result2.messages)


def test_project_messages_critical_can_demote_committed_preservations() -> None:
    """Critical pressure is allowed to demote previously committed groups.

    Setup: two turns. Turn 1 has call-1 (old). Turn 2 has call-2 (new).
    Under normal pressure, call-1 is a compaction candidate (before latest turn
    start) but is preserved by preserve_recent. It gets committed.
    Under critical pressure, call-1 should be demoted even though it was committed.
    """
    messages = [
        # Turn 1
        {"role": "user", "content": "old task"},
        _assistant_tool_call("call-1", "read", {"path": "a.py"}),
        _tool_result("call-1", "content of a.py", tool_name="read", recovery_call_id="call-1"),
        {"role": "assistant", "content": "done with turn 1"},
        # Turn 2 (latest turn boundary)
        {"role": "user", "content": "new task"},
        _assistant_tool_call("call-2", "read", {"path": "b.py"}),
        _tool_result("call-2", "content of b.py", tool_name="read", recovery_call_id="call-2"),
    ]

    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000,
        available_prompt_tokens=100_000,
        phase="within_turn",
        pressure_mode=PressureMode.normal,
    )
    state = ProjectionTurnState(turn_id="t1", policy=policy)

    # Cycle 1 under normal pressure — call-1 is before the latest turn start,
    # so it's a compaction candidate but preserved by preserve_recent.
    project_messages(
        messages,
        policy=ProjectionPolicy.from_budget(
            max_context_tokens=200_000,
            available_prompt_tokens=100_000,
            phase="within_turn",
            pressure_mode=PressureMode.normal,
        ),
        prior_state=state,
    )
    # call-1 should be committed as preserved.
    assert len(state.committed_preservations) > 0

    # Cycle 2 under critical pressure — committed groups may be demoted.
    result_critical = project_messages(
        messages,
        policy=ProjectionPolicy.from_budget(
            max_context_tokens=200_000,
            available_prompt_tokens=100_000,
            phase="within_turn",
            pressure_mode=PressureMode.critical,
        ),
        prior_state=state,
    )
    # Under critical with preserve_recent=1, only the newest same-turn group is kept.
    # call-1 should be compacted even though it was committed.
    assert "Tool output omitted" in str(result_critical.messages[2]["content"])


# ── TURN_BOUNDARY marker ──────────────────────────────────────────────────────


def test_turn_boundary_marker_replaces_workflow_task_string_sniff() -> None:
    """A system message with TURN_BOUNDARY=True is treated as a turn boundary."""
    messages = [
        {"role": "user", "content": "old task"},
        _assistant_tool_call("old-call", "bash", {"command": "expensive"}),
        _tool_result(
            "old-call", "old evidence" * 1000, tool_name="bash", recovery_call_id="old-call"
        ),
        {"role": "assistant", "content": "old answer"},
        # System message with TURN_BOUNDARY marker — NOT the old string sniff.
        {"role": "system", "content": "New workflow step", TURN_BOUNDARY: True},
        _assistant_tool_call("new-call", "read", {"path": "f.py"}),
        _tool_result("new-call", "new content", tool_name="read", recovery_call_id="new-call"),
    ]

    result = project_messages(messages, preserve_recent_completed_tool_groups=0)

    # old-call should be compacted (before the turn boundary).
    assert "Tool output omitted from prompt." in str(result.messages[2]["content"])
    # new-call should be preserved (after the turn boundary).
    assert result.messages[6]["content"] == "new content"


def test_plain_system_message_without_marker_is_not_a_turn_boundary() -> None:
    """A system message without TURN_BOUNDARY is NOT a turn boundary.

    Contrast with test_turn_boundary_marker_replaces_workflow_task_string_sniff:
    here the system message has no marker, so it does NOT protect the preceding
    tool group from compaction.  The latest real turn boundary is the user message
    at index 0, so old-call (index 1) is in the latest turn and protected under
    normal pressure.  We use pressure mode to allow same-turn compaction.
    """
    messages = [
        {"role": "user", "content": "old task"},
        _assistant_tool_call("old-call", "bash", {"command": "expensive"}),
        _tool_result(
            "old-call", "old evidence" * 1000, tool_name="bash", recovery_call_id="old-call"
        ),
        {"role": "assistant", "content": "old answer"},
        # Plain system message — no TURN_BOUNDARY marker.
        {"role": "system", "content": "Some system note"},
        _assistant_tool_call("new-call", "read", {"path": "f.py"}),
        _tool_result("new-call", "new content", tool_name="read", recovery_call_id="new-call"),
    ]

    # Under pressure with preserve_recent=0, no groups are preserved by the
    # recent-slice budget — only latest_turn_preserved_indices keeps the newest.
    # The plain system message is NOT a turn boundary, so the latest turn start
    # is the user message at index 0.  Both groups are in the latest turn; under
    # pressure, same-turn compaction is allowed.  old-call is not the newest
    # same-turn group, so it should be compacted.
    result = project_messages(
        messages,
        preserve_recent_completed_tool_groups=0,
        pressure_mode=PressureMode.pressure,
    )

    # old-call should be compacted (pressure allows same-turn compaction, no
    # turn boundary protection from the plain system message).
    assert "Tool output omitted from prompt." in str(result.messages[2]["content"])


# ── Invariant checker ─────────────────────────────────────────────────────────


def test_projection_monotonicity_invariant_passes_when_no_demotion() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    state = ProjectionTurnState(turn_id="t1", policy=policy)
    state.committed_preservations = {"abc123", "def456"}

    result = check_projection_monotonicity(
        state,
        new_preserved_anchors={"abc123", "def456", "ghi789"},
        pressure_mode=PressureMode.normal,
    )
    assert result.passed


def test_projection_monotonicity_invariant_fails_on_demotion() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    state = ProjectionTurnState(turn_id="t1", policy=policy)
    state.committed_preservations = {"abc123", "def456"}

    result = check_projection_monotonicity(
        state,
        new_preserved_anchors={"abc123"},  # def456 demoted
        pressure_mode=PressureMode.normal,
    )
    assert not result.passed
    assert "def456" in result.detail


def test_projection_monotonicity_invariant_allows_critical_demotion() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    state = ProjectionTurnState(turn_id="t1", policy=policy)
    state.committed_preservations = {"abc123", "def456"}

    result = check_projection_monotonicity(
        state,
        new_preserved_anchors={"abc123"},  # def456 demoted — but critical mode allows it
        pressure_mode=PressureMode.critical,
    )
    assert result.passed


# ── ProjectionTurnState ───────────────────────────────────────────────────────


def test_projection_turn_state_skip_count_increments() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    state = ProjectionTurnState(turn_id="t1", policy=policy)
    assert state.skip_count == 0
    assert state.reproject_count == 0

    # Simulate a skip.
    state.skip_count += 1
    assert state.skip_count == 1


def test_projection_turn_state_update_pressure_tracks_history() -> None:
    policy = ProjectionPolicy.from_budget(
        max_context_tokens=200_000, available_prompt_tokens=100_000
    )
    state = ProjectionTurnState(turn_id="t1", policy=policy)
    assert state.pressure_mode == PressureMode.normal

    # Escalate to pressure.
    snap = _make_snapshot(int(100_000 * PRESSURE_ESCALATE_FRACTION))
    state.update_pressure(snap)
    assert state.pressure_mode == PressureMode.pressure

    # Escalate to critical.
    snap_crit = _make_snapshot(int(100_000 * CRITICAL_ESCALATE_FRACTION))
    state.update_pressure(snap_crit)
    assert state.pressure_mode == PressureMode.critical


def test_project_messages_compacts_prunable_delegation_replay() -> None:
    messages = [
        {
            "role": "system",
            "content": "delegation head\n" + ("x" * 20_000),
            "_delegation_result_replay": True,
            "_prunable": True,
        }
    ]

    result = project_messages(
        messages,
        max_historical_tool_result_bytes=400,
        token_counter=lambda value: len(str(value)) // 4,
    )

    content = result.messages[0]["content"]
    assert content.startswith("<delegation_result_compacted>")
    assert "Recover the full result" in content
    assert result.messages[0]["_delegation_result_replay"] is True
