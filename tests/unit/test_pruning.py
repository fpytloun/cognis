"""Tests for per-turn pruning of old tool outputs."""

from __future__ import annotations

from cognis.core.pruning import prune_tool_outputs


def _tool_result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assistant_with_tool_calls(calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c.get("args", {})},
            }
            for c in calls
        ],
    }


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict:
    return {"role": "assistant", "content": content}


def test_no_pruning_when_under_protect_threshold() -> None:
    """Short messages stay untouched."""
    messages = [
        _user("Hello"),
        _assistant_with_tool_calls([{"id": "c1", "name": "bash"}]),
        _tool_result("c1", "short output"),
        _assistant("Done"),
    ]
    result = prune_tool_outputs(messages, protect_tokens=1000, minimum_savings=10)
    assert result[2]["content"] == "short output"


def test_old_tool_results_pruned() -> None:
    """Old results beyond protect window are replaced with cleared marker."""
    # Create messages with large tool outputs
    big = "x" * 200_000  # ~50K tokens
    small = "y" * 100  # recent, protected

    messages = [
        _user("task 1"),
        _assistant_with_tool_calls([{"id": "c1", "name": "bash"}]),
        _tool_result("c1", big),
        _assistant("result 1"),
        _user("task 2"),
        _assistant_with_tool_calls([{"id": "c2", "name": "read"}]),
        _tool_result("c2", small),
        _assistant("result 2"),
    ]

    result = prune_tool_outputs(messages, protect_tokens=1000, minimum_savings=100)
    # Old (big) result should be cleared
    assert "Tool result cleared" in result[2]["content"]
    assert "list_tool_output_anchors" in result[2]["content"]
    assert "read_tool_output_anchor" in result[2]["content"]
    assert "search_tool_output" in result[2]["content"]
    assert "read_tool_output" in result[2]["content"]
    assert "c1" in result[2]["content"]
    # tool_call_id preserved
    assert result[2]["tool_call_id"] == "c1"
    # Recent (small) result should be untouched
    assert result[6]["content"] == small


def test_does_not_mutate_input() -> None:
    messages = [
        _user("hi"),
        _tool_result("c1", "x" * 200_000),
    ]
    original_content = messages[1]["content"]
    prune_tool_outputs(messages, protect_tokens=100, minimum_savings=100)
    assert messages[1]["content"] == original_content


def test_minimum_savings_threshold() -> None:
    """If total savings < minimum_savings, skip pruning entirely."""
    messages = [
        _tool_result("c1", "x" * 4000),  # ~1000 tokens
        _tool_result("c2", "y" * 100),  # recent, protected
    ]
    result = prune_tool_outputs(messages, protect_tokens=100, minimum_savings=50_000)
    # Nothing pruned — savings too small
    assert result[0]["content"] == messages[0]["content"]


def test_large_arguments_cleared() -> None:
    """Tool call arguments exceeding threshold are cleared."""
    big_arg = "z" * 5000
    messages = [
        _user("task"),
        _assistant_with_tool_calls(
            [
                {"id": "c1", "name": "write", "args": {"content": big_arg}},
            ]
        ),
        _tool_result("c1", "x" * 200_000),
        _assistant("done"),
        _user("next"),
        _tool_result("c2", "y" * 100),
    ]

    result = prune_tool_outputs(
        messages, protect_tokens=200, minimum_savings=100, arg_clear_threshold=1000
    )
    # Tool result should be cleared
    assert "Tool result cleared" in result[2]["content"]
    assert "list_tool_output_anchors" in result[2]["content"]
    assert "search_tool_output" in result[2]["content"]
    # Tool call arguments should also be cleared (as a JSON string)
    func = result[1]["tool_calls"][0]["function"]
    assert isinstance(func["arguments"], str), "arguments must be a JSON string"
    assert "_cleared" in func["arguments"]
    assert "Arguments cleared" in func["arguments"]


def test_short_arguments_preserved() -> None:
    """Short tool call arguments are kept even when result is pruned."""
    messages = [
        _assistant_with_tool_calls(
            [
                {"id": "c1", "name": "bash", "args": {"command": "ls"}},
            ]
        ),
        _tool_result("c1", "x" * 200_000),
        _tool_result("c2", "y" * 100),
    ]
    result = prune_tool_outputs(
        messages, protect_tokens=200, minimum_savings=100, arg_clear_threshold=1000
    )
    # Arguments are short, should be preserved
    func = result[0]["tool_calls"][0]["function"]
    assert func["arguments"] == {"command": "ls"}


def test_custom_token_counter() -> None:
    """Custom token counter is used when provided."""
    calls = 0

    def counter(text: str) -> int:
        nonlocal calls
        calls += 1
        return len(text)  # 1 char = 1 token for testing

    messages = [
        _tool_result("c1", "x" * 100),
        _tool_result("c2", "y" * 10),
    ]
    prune_tool_outputs(messages, protect_tokens=50, minimum_savings=10, token_counter=counter)
    assert calls > 0


def test_tool_attachment_context_pruned_with_matching_tool_result() -> None:
    big = "x" * 200_000
    messages = [
        _assistant_with_tool_calls([{"id": "c1", "name": "browser_screenshot"}]),
        _tool_result("c1", big),
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Untrusted tool output"},
                {"type": "image_url", "image_url": {"url": "https://example.test/shot.png"}},
            ],
            "_tool_attachment_context": True,
            "_tool_call_id": "c1",
        },
        _tool_result("c2", "recent"),
    ]

    result = prune_tool_outputs(messages, protect_tokens=10, minimum_savings=100)

    assert "Tool result cleared" in result[1]["content"]
    assert result[2]["role"] == "system"
    assert "Tool attachment context cleared" in result[2]["content"]


def test_protected_tool_outputs_are_not_pruned() -> None:
    big = "x" * 200_000
    messages = [
        {
            "role": "tool",
            "tool_call_id": "skill_1",
            "content": big,
            "_protected_tool_output": True,
        },
        _tool_result("c2", "recent"),
    ]

    result = prune_tool_outputs(messages, protect_tokens=10, minimum_savings=100)

    assert result[0]["content"] == big
    assert result[1]["content"] == "recent"
