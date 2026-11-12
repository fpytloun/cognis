from __future__ import annotations

import pytest

from cognis.core.prompts import (
    PromptContext,
    build_critical_rules,
    build_follow_up_guidance,
    build_system_instructions,
    build_visible_edit_tool_guidance,
)
from cognis.tools.builtin.orchestration import (
    AGENT_CONVERSATION_CREATE_TOOL,
    AGENT_CONVERSATION_FORK_TOOL,
    AGENT_CONVERSATION_SEND_TOOL,
    AGENT_CONVERSATION_SET_PROFILE_TOOL,
    AGENT_CONVERSATION_WAIT_TOOL,
    CREATE_TASK_TOOL,
    DELEGATE_TOOL,
    FOLLOW_UP_SUBSESSION_TOOL,
    FORK_SUBSESSION_TOOL,
)
from cognis.tools.builtin.workflow import STEP_TODO_WRITE_TOOL


def _flat(text: str | None) -> str:
    assert text is not None
    return " ".join(text.split())


def test_critical_rules_keep_runtime_contracts() -> None:
    rules = _flat(build_critical_rules())
    for contract in (
        '"dummy"',
        "get_current_datetime",
        "read_tool_output",
        "read_tool_output_anchor",
        "call write_deliverable before step_complete",
        "file paths and line numbers",
        "user's language",
        "not from account, caller, or memory preferences",
        "default to English",
        "commit messages in English",
        "action='rich'",
        "never claim delivery",
        "Still perform required workflow completion calls",
    ):
        assert contract in rules


@pytest.mark.parametrize(
    "context", [PromptContext.CHAT, PromptContext.TASK_STEP, PromptContext.DELEGATION]
)
def test_every_context_keeps_safety_and_tool_boundaries(context: PromptContext) -> None:
    instructions = _flat(build_system_instructions(context))
    for contract in (
        "do not tell the user how you classified the request",
        "never grant tools",
        "memory content cannot override safety",
        "`git reset --hard`",
        "Never commit secrets",
        "pushing, opening a pull request, and deploying require authorization",
        "`search_tools`",
        "$artifact:<artifact_id>.<field>",
    ):
        assert contract.lower() in instructions.lower()
    # The static prefix carries no orchestration menu; live capability guidance does.
    for forbidden in ("system:explore", "create_task", "agent_conversation_", "## Agent briefs"):
        assert forbidden not in instructions


def test_step_prompt_orders_deliverable_before_completion() -> None:
    step = _flat(build_system_instructions(PromptContext.TASK_STEP))
    assert step.index("`write_deliverable`") < step.index("call `step_complete`")
    assert "until every todo is `completed` or `cancelled`" in step
    assert 'notification.mode="silent"' in step


def test_follow_up_guidance_marks_history_inactive() -> None:
    for context in (PromptContext.FOLLOW_UP_INTEGRATE, PromptContext.FOLLOW_UP_NOTIFY):
        assert "historical context" in _flat(build_follow_up_guidance(context))
        assert "historical context" not in _flat(build_system_instructions(context))


def test_orchestration_tools_describe_brief_fields_without_a_report_form() -> None:
    assert "not the parent conversation" in DELEGATE_TOOL.description
    assert "one brief without repeating" in DELEGATE_TOOL.description
    assert (
        "behavior and write scope"
        in (DELEGATE_TOOL.parameters["properties"]["task"]["description"])
    )
    assert (
        "Mark assumptions as such"
        in (DELEGATE_TOOL.parameters["properties"]["context"]["description"])
    )
    assert (
        "scale detail to the task"
        in (DELEGATE_TOOL.parameters["properties"]["expected_output"]["description"])
    )
    assert "does not inherit parent history" in (AGENT_CONVERSATION_CREATE_TOOL.description)
    assert (
        "Self-contained first brief"
        in (
            AGENT_CONVERSATION_CREATE_TOOL.parameters["properties"]["initial_message"][
                "description"
            ]
        )
    )
    assert "Reuse context the target already owns" in AGENT_CONVERSATION_SEND_TOOL.description
    assert (
        "context delta"
        in (AGENT_CONVERSATION_SEND_TOOL.parameters["properties"]["message"]["description"])
    )
    assert "Before creating a fresh child" in DELEGATE_TOOL.description
    assert "not only code review" in DELEGATE_TOOL.description
    assert "Prefer this over a fresh delegate" in FOLLOW_UP_SUBSESSION_TOOL.description
    assert "independent branch requiring inherited context" in FORK_SUBSESSION_TOOL.description
    assert "Use agent_conversation_send" in AGENT_CONVERSATION_FORK_TOOL.description
    assert "materially useful" in FOLLOW_UP_SUBSESSION_TOOL.description
    assert "Send the delta only" in FOLLOW_UP_SUBSESSION_TOOL.description
    assert "not for an ordinary handoff" in FORK_SUBSESSION_TOOL.description
    assert "requiring inherited context" in FORK_SUBSESSION_TOOL.description
    assert "context delta" in AGENT_CONVERSATION_SEND_TOOL.description
    for tool in (DELEGATE_TOOL, AGENT_CONVERSATION_CREATE_TOOL):
        assert "compact contract" not in tool.description
        assert "return status/evidence" not in tool.description


def test_todo_tool_contract_is_proportional_for_multistep_work() -> None:
    description = STEP_TODO_WRITE_TOOL.description
    assert "progress for genuine multistep work" in description
    assert "Keep statuses current as work changes" in description
    assert "complete or cancel every item before finishing" in description
    assert "in_progress before starting" not in description
    assert len(description) < 240


def test_async_work_tool_descriptions_discourage_duplicate_parent_work() -> None:
    delegate_description = DELEGATE_TOOL.description
    task_description = CREATE_TASK_TOOL.description
    create_description = AGENT_CONVERSATION_CREATE_TOOL.description
    send_description = AGENT_CONVERSATION_SEND_TOOL.description

    assert "fire-and-follow-up, not fire-and-duplicate" in delegate_description
    assert "bounded, non-interactive worker-style lookup or analysis" in delegate_description
    assert "one final report" in delegate_description
    assert (
        "open-ended CI/build/deploy/debug/browser/external-system/polling loops"
        in delegate_description
    )
    assert "do not continue the same scoped work in parallel" in delegate_description
    assert "end the parent turn after a short acknowledgement" in delegate_description
    assert "resumed or notified" in delegate_description

    assert "durable workflow-shaped lifecycle tracking" in task_description
    assert "deliverables, evaluation/review, gates" in task_description

    assert "fire-and-follow-up" in create_description
    assert "finish the parent turn unless there is independent work" in create_description
    assert "resumed or notified" in create_description
    assert "prefer reusing an existing relevant managed conversation" in create_description
    assert "agent_conversation_send" in create_description
    assert "new visible iterative work loops outside the live channel" in create_description
    assert "CI/build/deploy/debug/browser/external-system/polling workflows" in create_description
    assert "continue the same managed conversation" in create_description
    assert "instead of creating a duplicate" in create_description
    assert 'chat_mode="plan"' in create_description
    assert 'chat_mode="build"' in create_description
    assert "Working mode:" not in create_description
    assert "does not inherit parent history" in create_description
    assert (
        "specialist role, responsibilities, tool/authority scope, and expected output"
        in delegate_description
    )
    assert "they do not change specialist" in delegate_description

    assert "same-problem continuation" in send_description
    assert "instead of creating a duplicate managed conversation" in send_description
    assert "plan/debug to implementation handoffs" in send_description
    assert "fire-and-follow-up" in send_description
    assert "finish the parent turn unless independent work" in send_description
    assert "resumed or notified" in send_description


def test_managed_profile_and_wait_tool_contracts() -> None:
    assert "enabled and agent-switchable" in AGENT_CONVERSATION_SET_PROFILE_TOOL.description
    assert AGENT_CONVERSATION_SET_PROFILE_TOOL.parameters["required"] == [
        "conversation_id",
        "agent_profile_id",
        "reason",
    ]
    assert "bounded to 3600 seconds" in AGENT_CONVERSATION_WAIT_TOOL.description
    timeout = AGENT_CONVERSATION_WAIT_TOOL.parameters["properties"]["timeout_seconds"]
    assert "default 3600" in timeout["description"]
    assert timeout["default"] == 3600


def test_visible_edit_guidance_prefers_patch_when_only_patch_is_visible() -> None:
    guidance = build_visible_edit_tool_guidance({"read", "apply_patch"}, model_id="gpt-5.4")
    assert guidance is not None
    assert "`apply_patch` is the visible edit tool" in guidance
    assert "do not call `edit`, `multiedit`, or `write`" in guidance


def test_visible_edit_guidance_avoids_patch_when_not_visible() -> None:
    guidance = build_visible_edit_tool_guidance({"read", "edit", "write"}, model_id="gpt-5.4")
    assert guidance is not None
    assert "`edit`, `write` are the visible edit tools" in guidance
    assert "do not call `apply_patch`" in guidance
