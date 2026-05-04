from __future__ import annotations

from cognis.core.context_projection import project_messages
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
    big_args = {"content": "x" * 2_000}
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
    assert "list_tool_output_anchors" not in str(result.messages[1]["content"])
    assistant_args = result.messages[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(assistant_args, str)
    assert "Arguments cleared -" in assistant_args
    assert result.messages[3]["content"] == "recent 1"
    assert result.messages[5]["content"] == "recent 2"


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
    assert "list_tool_output_anchors" not in placeholder


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


def test_project_messages_compacts_prior_turn_but_preserves_latest_turn() -> None:
    messages = [
        {"role": "user", "content": "old task"},
        _assistant_tool_call("old-call", "bash", {"command": "expensive"}),
        _tool_result("old-call", "old evidence" * 1000, tool_name="bash", recovery_call_id="old-call"),
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "daily brief"},
        _assistant_tool_call("agenda-call", "mcp_todoist__find-tasks-by-date", {"startDate": "today"}),
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
    messages = [
        {"role": "user", "content": "old task"},
        _assistant_tool_call("old-call", "bash", {"command": "expensive"}),
        _tool_result("old-call", "old evidence" * 1000, tool_name="bash", recovery_call_id="old-call"),
        {"role": "assistant", "content": "old answer"},
        {"role": "system", "content": "## Workflow Task\n\nDaily brief"},
        _assistant_tool_call("agenda-call", "mcp_todoist__find-tasks-by-date", {"startDate": "today"}),
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


def test_prune_tool_outputs_does_not_treat_attachment_user_as_turn_boundary() -> None:
    messages = [
        {"role": "user", "content": "daily brief"},
        _assistant_tool_call("agenda-call", "mcp_todoist__find-tasks-by-date", {"startDate": "today"}),
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
