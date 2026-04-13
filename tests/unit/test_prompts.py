from __future__ import annotations

from cognis.core.prompts import PromptContext, build_system_instructions


def test_chat_prompt_describes_turn_local_todos() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "Chat todos are optional, rare" in instructions
    assert "Do not create todos while only presenting a plan" in instructions
    assert "current turn" in instructions
    assert "Prefer delegation for non-trivial work" in instructions
    assert '"explore", "analyze", "research", "synthesize", or "write the answer"' in instructions


def test_chat_prompt_discourages_ceremonial_todos() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "If the work is simple enough to keep in working memory" in instructions
    assert "delegate or create a task instead of using chat todos" in instructions


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
