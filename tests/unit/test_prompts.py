from __future__ import annotations

from cognis.core.prompts import PromptContext, build_system_instructions


def test_chat_prompt_describes_turn_local_todos() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "Chat todos are optional" in instructions
    assert "Do not create todos while only presenting a plan" in instructions
    assert "current turn" in instructions


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
