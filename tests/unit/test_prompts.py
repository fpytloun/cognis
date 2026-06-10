from __future__ import annotations

from cognis.core.prompts import (
    PromptContext,
    build_critical_rules,
    build_system_instructions,
    build_visible_edit_tool_guidance,
)
from cognis.core.system_skills import get_system_skill_default
from cognis.tools.builtin.orchestration import (
    AGENT_CONVERSATION_CREATE_TOOL,
    AGENT_CONVERSATION_SEND_TOOL,
    CREATE_TASK_TOOL,
    DELEGATE_TOOL,
)


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
    assert "Use `delegate(wait=true)` for joined child work" in instructions
    assert "visible tool schema" in instructions
    assert "fire-and-follow-up, not fire-and-duplicate" in instructions
    assert (
        "do not keep investigating or implementing the same scoped work in parallel" in instructions
    )
    assert "follow-up/resume notification" in instructions
    assert "Use `wait=false` by default from live/main chat" not in instructions
    assert "prefer joined delegation, managed conversations, or tasks" in instructions
    assert "Do not optimize for finishing the whole job inside the parent turn" in instructions
    assert "parent chat as the command bridge" in instructions
    assert "`delegate(wait=false)`: bounded, non-interactive worker-style lookup" in instructions
    assert "`agent_conversation_create(wait=false)`: visible iterative work loop" in instructions
    assert "`create_task`: durable workflow-shaped work with lifecycle" in instructions
    assert '`chat_mode="plan"`' in instructions
    assert '`chat_mode="build"`' in instructions


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
    assert "visible iterative work loops outside the live channel" in create_description
    assert "CI/build/deploy/debug/browser/external-system/polling workflows" in create_description
    assert 'chat_mode="plan"' in create_description
    assert 'chat_mode="build"' in create_description

    assert "fire-and-follow-up" in send_description
    assert "finish the parent turn unless independent work" in send_description
    assert "resumed or notified" in send_description


def test_chat_prompt_avoids_async_bias_for_generic_chat() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "In a live/main user conversation, keep the chat responsive" not in instructions
    assert "Split independent read-only questions into multiple delegate calls" in instructions
    assert "non-conflicting slices" in instructions
    assert "Do not try to fan out from secondary or delegated sub-sessions" in instructions


def test_task_step_prompt_disallows_async_delegation() -> None:
    instructions = build_system_instructions(PromptContext.TASK_STEP)
    assert instructions is not None
    assert "Workflow steps are execution contexts, not live/main chat" in instructions
    assert "joined child work that returns before the step continues" in instructions
    assert "delegate(wait=false)" not in instructions
    assert "orchestrating/primary step" in instructions
    assert "must be joined before completing the step" in instructions


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
    assert "If the user asks for a plan, explanation, review, or brainstorming" in instructions
    assert "Doing the work now includes the correct execution shape" in instructions


def test_chat_prompt_preserves_diacritics_in_user_facing_prose() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    rules = build_critical_rules()
    assert instructions is not None
    assert rules is not None
    assert "Preserve correct orthography and diacritics" in instructions
    assert "natural-language documents" in instructions
    assert "Preserve correct orthography and diacritics" in rules


def test_chat_prompt_includes_workspace_hygiene() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "You may be in a dirty workspace" in instructions
    assert "Never revert, overwrite, or clean up" in instructions
    assert "Do not create, amend, or push git commits" in instructions


def test_chat_prompt_defaults_review_to_findings_first() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "If the user asks for a review" in instructions
    assert "prioritize findings first" in instructions


def test_chat_prompt_prefers_dedicated_edit_tools_for_coding() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "Prefer dedicated edit tools over shell or interpreter one-liners" in instructions
    assert "Avoid using `bash` to run Python, Perl, Ruby" in instructions


def test_chat_prompt_prefers_structured_tools_for_file_inspection() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert (
        "Do not use `bash` with `rg`, `grep`, `find`, `ls`, `cat`, `head`, `tail`" in instructions
    )
    assert "when structured" in instructions
    assert (
        "Do not chain file inspection commands with `&&`, `;`, or separator output" in instructions
    )


def test_prompt_does_not_assume_patch_is_visible() -> None:
    instructions = build_system_instructions(PromptContext.CHAT, model_id="gpt-5.4")
    assert instructions is not None
    assert "actually visible" in instructions
    assert "Do not call edit tools that are not visible" in instructions
    assert "Prefer `apply_patch` over `write`, `edit`, or `multiedit`" not in instructions


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


def test_critical_rules_cover_truncated_output_recovery_and_placeholder_bleed() -> None:
    # Placeholder and tool-output recovery rules are now carried in
    # build_critical_rules() so they land at the very top of the prompt,
    # right after <identity>.
    rules = build_critical_rules()
    assert rules is not None
    assert "Tool outputs may be omitted from the prompt for space" in rules
    assert "Recover a saved output only when a specific missing detail" in rules
    assert "Do not recover old outputs just to reconfirm context" in rules
    # Placeholder guardrail.
    assert '"dummy"' in rules
    assert '"noop"' in rules
    # Pointer-style guidance in the operational instructions can now be
    # minimal; only the anchored-recovery hint stays there.
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "list_tool_output_anchors" in instructions
    assert "read_tool_output_anchor" in instructions


def test_chat_prompt_guides_tavily_query_shape() -> None:
    instructions = build_system_instructions(PromptContext.CHAT)
    assert instructions is not None
    assert "structured parameters over query" in instructions
    assert "include_domains" in instructions
    assert "exact_match" in instructions


def test_task_step_prompt_requires_todos_for_non_trivial_work() -> None:
    instructions = build_system_instructions(PromptContext.TASK_STEP)
    assert instructions is not None
    assert "For non-trivial work, first make a short execution plan" in instructions
    assert "call `write_deliverable` with the canonical" in instructions
    assert (
        "Do not call `step_complete` until every remaining todo is `done` or `cancelled`"
        in instructions
    )


def test_delegation_prompt_mentions_todos_and_questions() -> None:
    instructions = build_system_instructions(PromptContext.DELEGATION)
    assert instructions is not None
    assert "secondary (specialist) agent" in instructions
    assert "write a comprehensive final assistant message" in instructions
    assert "Do not delegate further" in instructions


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


def test_coding_skill_preserves_user_facing_diacritics_and_workspace_hygiene() -> None:
    skill = get_system_skill_default("cognis-coding")
    assert skill is not None
    content = str(skill["instructions"])
    assert "Do not force natural-language documents to ASCII" in content
    assert "Do not add backward-compatibility code unless there is a concrete need" in content
    assert "Never revert, overwrite, or clean up changes you did not make" in content


def test_coding_skill_prefers_structured_tools_for_file_inspection() -> None:
    skill = get_system_skill_default("cognis-coding")
    assert skill is not None
    content = str(skill["instructions"])
    assert "Do not use `bash` with `rg`, `grep`, `find`, `ls`, `cat`, `head`, `tail`" in content
    assert "structured tools such as `read`, `grep`, `glob`, or `list_directory`" in content
    assert "Do not chain file inspection commands with `&&`, `;`, or separator output" in content


def test_coding_skill_allows_explicit_plan_steps() -> None:
    skill = get_system_skill_default("cognis-coding")
    assert skill is not None
    content = str(skill["instructions"])
    assert (
        "Workflow step objectives and controller completion contracts override this skill"
        in content
    )
    assert "unless the user request or current workflow step explicitly asks for a plan" in content
    assert "complete only the current workflow step artifact" in content
