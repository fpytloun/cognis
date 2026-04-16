from __future__ import annotations

from cognis.core.prompts import PromptContext, build_system_instructions


def test_chat_prompt_describes_turn_local_todos() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "Chat todos are optional, rare" in instructions
    assert "Do not create todos while only presenting a plan" in instructions
    assert "current turn" in instructions
    assert "Prefer specialist system agents" in instructions
    assert '"explore", "analyze", "research", "synthesize", or "write the answer"' in instructions


def test_chat_prompt_discourages_ceremonial_todos() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "If the work is simple enough to keep in working memory" in instructions
    assert "delegate or create a task instead of using chat todos" in instructions


def test_chat_prompt_sets_pragmatic_coding_expectations() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "For software engineering work" in instructions
    assert "smallest correct change" in instructions
    assert "update docs only when directly affected" in instructions


def test_chat_prompt_describes_delegate_wait_behavior() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "Use `wait=true` only when conversation continuation requires" in instructions
    assert "With `wait=false`, the conversation remains responsive" in instructions
    assert "Do not use `wait=true` by default" in instructions


def test_chat_prompt_routes_to_system_specialists_and_same_agent() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "Use `delegate` without `agent_id`" in instructions
    assert "`system:explore`" in instructions
    assert "`system:research`" in instructions
    assert "`system:code-review`" in instructions
    assert "`system:architect`" in instructions
    assert "`system:implement`" in instructions


def test_chat_prompt_has_execution_bias() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "If the user asks for actionable work" in instructions
    assert "Doing the work now includes the correct execution shape" in instructions


def test_chat_prompt_prefers_dedicated_edit_tools_for_coding() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "Prefer dedicated edit tools over shell or interpreter one-liners" in instructions
    assert "Avoid using `bash` to run Python, Perl, Ruby" in instructions


def test_chat_prompt_explains_truncated_output_recovery() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "middle truncated" in instructions
    assert "Tool result cleared" in instructions
    assert 'search_tool_output(call_id=..., pattern="error|timeout|keyword")' in instructions
    assert "read_tool_output(call_id=..., offset=..., limit=...)" in instructions


def test_task_step_prompt_requires_todos_for_non_trivial_work() -> None:
    instructions = build_system_instructions(PromptContext.TASK_STEP)
    assert instructions is not None
    assert "For non-trivial work, first make a short execution plan" in instructions
    assert (
        "Do not call `step_complete` until every remaining todo is `done` or `cancelled`"
        in instructions
    )


def test_delegation_prompt_mentions_todos_and_questions() -> None:
    instructions = build_system_instructions(PromptContext.DELEGATION)
    assert instructions is not None
    assert "create step todos" in instructions
    assert "use `step_request_input`" in instructions


def test_follow_up_integrate_prompt_marks_history_as_inactive() -> None:
    instructions = build_system_instructions(PromptContext.FOLLOW_UP_INTEGRATE)
    assert instructions is not None
    assert "historical context" in instructions
    assert "active instruction is the follow-up event block" in instructions
    assert "Do not re-answer an older user message literally" in instructions


def test_follow_up_notify_prompt_keeps_updates_separate() -> None:
    instructions = build_system_instructions(PromptContext.FOLLOW_UP_NOTIFY)
    assert instructions is not None
    assert "historical context" in instructions
    assert "separate update" in instructions
    assert "Do not resume or continue an older conversation thread" in instructions
