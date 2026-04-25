from __future__ import annotations

from cognis.core.prompts import (
    PromptContext,
    build_critical_rules,
    build_system_instructions,
    build_visible_edit_tool_guidance,
)
from cognis.core.system_skills import get_system_skill_default


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
    assert "cleared from context" in rules
    assert "compacted" in rules
    assert "read_tool_output" in rules
    assert "list_tool_output_anchors" in rules
    assert "read_tool_output_anchor" in rules
    assert "search_tool_output" in rules
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


def test_coding_skill_preserves_user_facing_diacritics_and_workspace_hygiene() -> None:
    skill = get_system_skill_default("cognis-coding")
    assert skill is not None
    content = str(skill["instructions"])
    assert "Do not force natural-language documents to ASCII" in content
    assert "Do not add backward-compatibility code unless there is a concrete need" in content
    assert "Never revert, overwrite, or clean up changes you did not make" in content
