"""Mock LLM scenario placeholders that reuse earlier tool results."""

from __future__ import annotations

import json

from cognis.testing.mock_llm.scenarios import (
    render_chat_completion_stream,
    resolve_argument_placeholders,
)

_MESSAGES = [
    {"role": "user", "content": "scenario:x"},
    {"role": "assistant", "content": None},
    {
        "role": "tool",
        "tool_call_id": "call_v1",
        "content": json.dumps({"status": "buffered", "deliverable_id": "dlv_abc"}),
    },
    {
        "role": "tool",
        "tool_call_id": "call_wrapped",
        "content": json.dumps({"wrapped": json.dumps({"deliverable_id": "dlv_wrapped"})}),
    },
    {
        "role": "tool",
        "tool_call_id": "call_generation",
        "content": json.dumps({"kernel_generation": 987654321}),
    },
]


def test_placeholder_resolves_plain_and_escaped_tool_results() -> None:
    resolved = resolve_argument_placeholders(
        {
            "supersedes": "{{tool_result:call_v1:deliverable_id}}",
            "nested": ["{{tool_result:call_wrapped:deliverable_id}}"],
            "missing": "{{tool_result:call_absent:deliverable_id}}",
        },
        _MESSAGES,
    )
    assert resolved == {
        "supersedes": "dlv_abc",
        "nested": ["dlv_wrapped"],
        "missing": "{{tool_result:call_absent:deliverable_id}}",
    }


def test_rendered_tool_call_arguments_use_resolved_placeholder() -> None:
    scenario = {
        "turns": [
            {
                "role": "assistant",
                "steps": [
                    {
                        "type": "tool_call",
                        "call_id": "call_v2",
                        "name": "write_deliverable",
                        "arguments": {
                            "supersedes": "{{tool_result:call_v1:deliverable_id}}",
                            "expected_generation": (
                                "{{tool_result_number:call_generation:kernel_generation}}"
                            ),
                        },
                    }
                ],
            }
        ]
    }
    chunks = render_chat_completion_stream(scenario, messages=_MESSAGES)
    first = json.loads(chunks[0].removeprefix("data: "))
    arguments = first["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {
        "supersedes": "dlv_abc",
        "expected_generation": 987654321,
    }
