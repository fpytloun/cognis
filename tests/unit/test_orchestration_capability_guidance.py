from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    DIRECT_CHAT_DELEGATION_POLICY,
    SECONDARY_AGENT_DELEGATION_POLICY,
    SECONDARY_POLICY,
    WORKFLOW_POLICY,
    build_agent_work_provenance,
    prompt_context_for_step,
    replace_orchestration_capability_guidance,
)
from cognis.core.managed_conversations import (
    inherited_managed_session_policy,
    managed_link_owned_by_controller,
    managed_target_repeats_ancestry,
)
from cognis.core.orchestration_policy import (
    OrchestrationSurface,
    build_orchestration_capability_guidance,
    orchestration_surface_policy,
)
from cognis.core.prompts import PromptContext, build_system_instructions
from cognis.models.session import ConversationContext
from cognis.tools.builtin.orchestration import (
    DELEGATE_TOOL,
    OrchestrationMode,
    orchestration_tools,
)


def _guidance(
    context: ConversationContext,
    mode: OrchestrationMode,
    tools: set[str],
    *,
    async_delegate: bool = False,
    async_managed: bool = False,
) -> str | None:
    return build_orchestration_capability_guidance(
        policy=orchestration_surface_policy(context),
        orchestration_mode=mode,
        visible_tool_names=tools,
        async_delegate_visible=async_delegate,
        async_managed_visible=async_managed,
    )


@pytest.mark.parametrize(
    ("context", "expected_surface"),
    [
        (
            ConversationContext(
                type="web",
                ref="web:agent_direct:user@example.com:agent",
                platform_data={"kind": "agent_direct"},
            ),
            OrchestrationSurface.WEB_MAIN_CHAT,
        ),
        (ConversationContext(type="signal"), OrchestrationSurface.CHANNEL),
        (ConversationContext(type="web"), OrchestrationSurface.WEB_TOPIC),
        (
            ConversationContext(type="agent_work", platform_data={"kind": "agent_work"}),
            OrchestrationSurface.MANAGED_AGENT_CONVERSATION,
        ),
        (ConversationContext(type="task"), OrchestrationSurface.TASK),
    ],
)
def test_surface_matrix_is_deterministic(
    context: ConversationContext, expected_surface: OrchestrationSurface
) -> None:
    assert orchestration_surface_policy(context).surface is expected_surface


def test_main_and_channel_describe_visible_async_actions_only() -> None:
    tools = {"delegate", "agent_conversation_create", "create_task"}
    for context in (
        ConversationContext(
            type="web",
            ref="web:agent_direct:user@example.com:agent",
            platform_data={"kind": "agent_direct"},
        ),
        ConversationContext(type="signal"),
    ):
        content = _guidance(
            context,
            OrchestrationMode.FULL,
            tools,
            async_delegate=True,
            async_managed=True,
        )
        assert content is not None
        assert "asynchronously or joined" in content
        assert "primary or user agents" in content
        assert "secondary/system agents" in content
        assert "Create a task only when" in content
        assert "workflow authoring" not in content.lower()


def test_topic_exposes_async_managed_conversations_but_joined_delegates() -> None:
    content = _guidance(
        ConversationContext(type="web"),
        OrchestrationMode.FULL,
        {"delegate", "agent_conversation_create", "create_task"},
        async_managed=True,
    )
    assert content is not None
    assert "primary or user agents (asynchronously or joined)" in content
    assert "secondary/system agents (joined)" in content
    assert "only when the user explicitly asks" in content
    assert "preferred" not in content


def test_managed_and_workflow_contexts_reject_surface_forbidden_claims() -> None:
    managed = _guidance(
        ConversationContext(type="agent_work", platform_data={"kind": "agent_work"}),
        OrchestrationMode.FULL,
        {"delegate", "agent_conversation_create", "create_task", "create_workflow"},
        async_delegate=True,
        async_managed=True,
    )
    assert managed is not None
    assert "Joined specialist delegation" in managed
    assert "Managed conversations are available" not in managed
    assert "Create a task only when" not in managed
    assert "Workflow authoring is available" not in managed
    assert "asynchronously" not in managed

    task = _guidance(
        ConversationContext(type="task"),
        OrchestrationMode.DELEGATE_SYNC_ONLY,
        {"delegate", "agent_conversation_create", "create_task"},
        async_delegate=True,
    )
    assert task is not None
    assert "only joined specialist delegation" in task
    assert "Managed conversations are available" not in task
    assert "Create a task only when" not in task
    assert "asynchronously" not in task


def test_task_primary_mode_exposes_only_restricted_orchestration() -> None:
    names = {tool.name for tool in orchestration_tools(OrchestrationMode.TASK_PRIMARY)}

    assert names == {
        "delegate",
        "agent_conversation_create",
        "agent_conversation_send",
        "agent_conversation_wait",
        "agent_conversation_interrupt",
        "agent_conversation_retry",
        "agent_conversation_fork",
        "agent_conversation_close",
        "agent_conversation_list",
        "agent_conversation_get",
    }
    guidance = _guidance(
        ConversationContext(type="task"),
        OrchestrationMode.TASK_PRIMARY,
        names,
    )
    assert guidance is not None
    assert "Task-owned managed conversations" in guidance
    assert "Task and workflow creation" in guidance


def test_policy_allows_but_hidden_tools_are_not_claimed() -> None:
    assert (
        _guidance(
            ConversationContext(type="web"),
            OrchestrationMode.FULL,
            set(),
        )
        is None
    )


@pytest.mark.parametrize(
    "tools",
    [
        {"delegate"},
        {"agent_conversation_create"},
        {"agent_conversation_send"},
        {"create_task", "create_workflow"},
    ],
)
def test_message_readability_guidance_is_scoped_to_inter_agent_tools(tools: set[str]) -> None:
    guidance = _guidance(
        ConversationContext(type="web"),
        OrchestrationMode.FULL,
        tools,
    )

    assert guidance is not None
    if "create_task" in tools:
        assert "## Agent briefs" not in guidance
    else:
        assert guidance.count("## Agent briefs") == 1
        assert "restrictions exactly" in guidance


@pytest.mark.parametrize(
    ("context", "prompt_context", "mode"),
    [
        (ConversationContext(type="web"), PromptContext.CHAT, OrchestrationMode.FULL),
        (ConversationContext(type="signal"), PromptContext.CHAT, OrchestrationMode.FULL),
        (
            ConversationContext(type="agent_work", platform_data={"managed_depth": 1}),
            PromptContext.CHAT,
            OrchestrationMode.FULL,
        ),
        (
            ConversationContext(type="task"),
            PromptContext.TASK_STEP,
            OrchestrationMode.TASK_PRIMARY,
        ),
        (
            ConversationContext(type="task"),
            PromptContext.TASK_STEP,
            OrchestrationMode.DELEGATE_SYNC_ONLY,
        ),
    ],
)
def test_assembled_guidance_has_one_brief_across_surfaces_and_loaded_skills(
    context: ConversationContext, prompt_context: PromptContext, mode: OrchestrationMode
) -> None:
    from cognis.core.system_skills import get_system_skill_default

    tools = orchestration_tools(mode, expose_managed_conversation_wait_option=False)
    guidance = _guidance(context, mode, {tool.name for tool in tools})
    assert guidance is not None
    messages = [
        {"role": "system", "content": build_system_instructions(prompt_context)},
    ]
    replace_orchestration_capability_guidance(messages, guidance)
    replace_orchestration_capability_guidance(messages, guidance)
    assembled = "\n".join(str(message["content"]) for message in messages)
    for skill_id in ("cognis-coding", "cognis-orchestrator"):
        skill = get_system_skill_default(skill_id)
        assert skill is not None
        assembled += "\n" + str(skill["instructions"])
    assembled += "\n" + "\n".join(f"{tool.description}\n{tool.parameters}" for tool in tools)

    assert assembled.count("## Agent briefs") == 1
    for obsolete in (
        "compact contract",
        "Required return contract",
        "status, results/findings, changed references",
        "Objective — the bounded outcome",
        "The contract must include",
    ):
        assert obsolete not in assembled


def test_sync_and_async_delegate_fields_share_one_source() -> None:
    sync_delegate = next(
        tool
        for tool in orchestration_tools(OrchestrationMode.DELEGATE_SYNC_ONLY)
        if tool.name == "delegate"
    )
    for name in ("task", "agent_id", "context", "expected_output"):
        assert (
            sync_delegate.parameters["properties"][name]
            == DELEGATE_TOOL.parameters["properties"][name]
        )


def test_delegate_guidance_reuses_existing_child_context() -> None:
    description = DELEGATE_TOOL.description
    assert "before creating a fresh child" in description.lower()
    assert "follow_up_subsession" in description
    assert "fork_subsession" in description
    assert "same bounded problem" in description
    assert "Follow-up and fork preserve" in description
    assert "fresh specialist when compatibility changes" in description


def test_capability_guidance_isolates_fresh_children_and_reviews() -> None:
    guidance = _guidance(
        ConversationContext(type="web"),
        OrchestrationMode.FULL,
        {"delegate", "follow_up_subsession", "fork_subsession"},
    )

    assert guidance is not None
    flat = " ".join(guidance.split())
    assert flat.count("Start an initial independent review") == 1
    assert "does not inherit the parent conversation" in flat
    assert "never an implementation reasoning transcript" in flat
    assert "Never include secrets" in flat


def test_delegate_only_guidance_does_not_claim_hidden_continuation_or_fork_tools() -> None:
    guidance = _guidance(
        ConversationContext(type="web"),
        OrchestrationMode.FULL,
        {"delegate"},
    )

    assert guidance is not None
    assert "same bounded problem" not in guidance
    assert "follow_up_subsession" not in guidance
    assert "Fork only" not in guidance


def test_managed_create_only_guidance_does_not_claim_hidden_continuation_or_fork_tools() -> None:
    guidance = _guidance(
        ConversationContext(type="web"),
        OrchestrationMode.FULL,
        {"agent_conversation_create"},
    )

    assert guidance is not None
    assert "same bounded problem" not in guidance
    assert "agent_conversation_send" not in guidance
    assert "Fork only" not in guidance


def test_sync_managed_tool_descriptions_keep_context_isolation_and_review_routing() -> None:
    tools = {
        tool.name: tool
        for tool in orchestration_tools(
            OrchestrationMode.FULL,
            expose_managed_conversation_wait_option=False,
        )
    }
    create = tools["agent_conversation_create"].description
    send = tools["agent_conversation_send"].description
    fork = tools["agent_conversation_fork"].description

    assert "does not inherit parent history" in create
    assert "initial_message and attached artifacts supply its brief" in create
    assert "compact contract" not in create
    assert "return status and evidence" not in create
    assert "Working mode:" not in create
    assert "initial independent review in a fresh conversation" in send
    assert "re-review only when" in send
    assert "Create a fresh isolated child when compatibility or useful context is absent" in send
    assert "independent branch requiring inherited context" in fork
    assert "initial independent review fresh" in fork
    assert "ordinary handoff" in fork
    assert "fork for re-review" not in fork
    assert "this fork for re-review" not in fork
    assert "re-review fresh when that context is not useful" in fork


def test_managed_guidance_uses_future_compatible_nested_wording() -> None:
    guidance = _guidance(
        ConversationContext(type="agent_work", platform_data={"kind": "agent_work"}),
        OrchestrationMode.FULL,
        {"delegate"},
    )
    assert guidance is not None
    assert "maximum managed depth" in guidance
    assert "unavailable in this execution context" in guidance
    assert "Joined specialist delegation is available" in guidance
    assert "do not start nested" not in guidance


def test_depth_one_managed_guidance_describes_joined_nested_work_only() -> None:
    guidance = _guidance(
        ConversationContext(
            type="agent_work",
            platform_data={"kind": "agent_work", "managed_depth": 1},
        ),
        OrchestrationMode.FULL,
        {"delegate", "agent_conversation_create", "agent_conversation_send"},
    )
    assert guidance is not None
    assert "Joined nested managed conversations are available" in guidance
    assert "asynchronous nested work is unavailable" in guidance


def test_managed_work_provenance_is_factual_without_unavailable_options() -> None:
    content = build_agent_work_provenance(
        controller_agent_id="controller",
        controller_conversation_id="conv-1",
        controller_session_id="sess-1",
    )
    assert "Complete the assigned scope with the capabilities available" in content
    assert "Return unavailable coordination needs to the controller" in content
    assert "create_task" not in content
    assert "delegate" not in content
    assert "managed conversation" not in content.lower()


def test_managed_link_control_requires_agent_and_conversation_ownership() -> None:
    link = SimpleNamespace(
        controller_agent_id="controller",
        controller_conversation_id="conv-1",
    )
    assert managed_link_owned_by_controller(
        link,
        controller_agent_id="controller",
        controller_conversation_id="conv-1",
    )
    assert not managed_link_owned_by_controller(
        link,
        controller_agent_id="other-controller",
        controller_conversation_id="conv-1",
    )
    assert not managed_link_owned_by_controller(
        link,
        controller_agent_id="controller",
        controller_conversation_id="conv-2",
    )


def test_nested_target_rejects_repeated_agent_ancestry() -> None:
    ancestry = [
        SimpleNamespace(target_agent_id="depth-one"),
        SimpleNamespace(
            target_agent_id="root-agent",
            controller_agent_id="root-controller",
        ),
    ]
    assert managed_target_repeats_ancestry("root-agent", ancestry)
    assert managed_target_repeats_ancestry("depth-one", ancestry)
    assert managed_target_repeats_ancestry("root-controller", ancestry)
    assert not managed_target_repeats_ancestry("new-primary", ancestry)


def test_nested_session_policy_uses_explicit_inheritance_without_widening() -> None:
    parent_policy = {
        "allow_policies": ["read-only"],
        "deny_policies": ["destructive"],
    }
    ambient_broader = {"allow_policies": ["read-only", "write"]}
    assert (
        inherited_managed_session_policy(
            {"managed_session_policy": parent_policy},
            ambient_broader,
        )
        == parent_policy
    )


def test_mutable_guidance_is_audit_marked_after_prefix_and_replaced_without_duplicates() -> None:
    messages = [
        {"role": "system", "content": "immutable", "_immutable_prefix": True},
        {"role": "user", "content": "request"},
    ]
    replace_orchestration_capability_guidance(messages, "first capabilities")

    assert messages[-1] == {
        "role": "system",
        "content": "first capabilities",
        "_orchestration_capability_guidance": True,
        "_audit_source": "orchestration_capability_guidance",
        "_audit_role": "developer",
    }
    assert messages[0]["content"] == "immutable"

    # Delivered notices are append-only: changes add a superseding notice, and an
    # unchanged notice is not repeated.
    assert replace_orchestration_capability_guidance(messages, "updated capabilities")
    assert not replace_orchestration_capability_guidance(messages, "updated capabilities")
    capability_messages = [
        message for message in messages if message.get("_orchestration_capability_guidance")
    ]
    assert [message["content"] for message in capability_messages] == [
        "first capabilities",
        "updated capabilities",
    ]

    assert replace_orchestration_capability_guidance(messages, None)
    assert "no longer available" in messages[-1]["content"]
    assert messages[0]["content"] == "immutable"


@pytest.mark.parametrize(
    "policy",
    [DIRECT_CHAT_DELEGATION_POLICY, SECONDARY_AGENT_DELEGATION_POLICY],
)
def test_every_none_mode_child_uses_delegation_prompt(policy: object) -> None:
    ctx = SimpleNamespace(policy=policy, orchestration_mode=OrchestrationMode.NONE)
    assert prompt_context_for_step(ctx) is PromptContext.DELEGATION
    instructions = build_system_instructions(prompt_context_for_step(ctx))
    assert instructions is not None
    assert "## Sub-session" in instructions
    assert "## Work routing" not in instructions
    assert "managed conversation" not in instructions.lower()
    assert "create_task" not in instructions


def test_non_child_prompt_contexts_remain_distinct() -> None:
    assert (
        prompt_context_for_step(
            SimpleNamespace(policy=WORKFLOW_POLICY, orchestration_mode=OrchestrationMode.FULL)
        )
        is PromptContext.TASK_STEP
    )
    assert (
        prompt_context_for_step(
            SimpleNamespace(policy=SECONDARY_POLICY, orchestration_mode=OrchestrationMode.NONE)
        )
        is PromptContext.TASK_STEP
    )
    secondary_instructions = build_system_instructions(PromptContext.TASK_STEP)
    assert secondary_instructions is not None
    assert "call `step_complete`" in secondary_instructions
    assert (
        prompt_context_for_step(
            SimpleNamespace(policy=CHAT_POLICY, orchestration_mode=OrchestrationMode.FULL)
        )
        is PromptContext.CHAT
    )
