"""Acknowledged policy notice acceptance through actual context assembly."""

from unittest.mock import AsyncMock

import pytest

from cognis.core.context import ContextAssembler
from cognis.core.current_policy_context import is_current_policy_message, refresh_current_policy
from cognis.core.prompts import PromptContext
from cognis.models.session import IntarisSession
from tests.unit.test_context_assembler import (
    _LLM,
    _agent,
    _conversation,
    _Guardrails,
    _Memory,
    _session,
    _SessionCache,
    _SessionManager,
)


def acknowledged(session, policy, revision=7):
    return IntarisSession(
        session_id=session.intaris_session_id or session.session_id,
        user_id=session.user_email,
        agent_id=session.agent_id,
        status="active",
        policy=policy,
        policy_revision=revision,
        created_at="2026-10-11T00:00:00Z",
        updated_at="2026-10-11T00:00:00Z",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
@pytest.mark.parametrize(
    "prompt_context", [PromptContext.CHAT, PromptContext.TASK_STEP, PromptContext.DELEGATION]
)
async def test_actual_prompt_current_policy_is_not_historical_denial_authority(
    skip_memory, prompt_context
):
    session = _session()
    guardrails = _Guardrails()
    guardrails.get_session = AsyncMock(
        return_value=acknowledged(
            session,
            {
                "evaluation_policy_version": "legacy",
                "maximum_outcome": "approve",
                "minimum_outcome": "approve",
                "interaction_mode": "none",
                "system_prompt": "POLICY_PROSE_MUST_NOT_DUPLICATE",
            },
        )
    )
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    result = await assembler.assemble(
        session=session,
        conversation=_conversation(),
        agent=_agent(),
        user_message="Inspect silence renewal.",
        prior_context=[
            {
                "role": "user",
                "content": "Historical journal: prior_silence_renewal_authorization_denials_no_current_retry",
            }
        ],
        prompt_context=prompt_context,
        skip_memory=skip_memory,
    )
    notices = [r for r in result.messages if is_current_policy_message(r)]
    assert len(notices) == 1
    notice = notices[0]["content"]
    assert '"maximum_outcome": "approve"' in notice
    assert '"evaluation_mode": "legacy"' in notice
    assert '"interaction_mode": "none"' in notice
    assert '"revision": 7' in notice
    assert "not a user grant" in notice
    assert "explicit human refusals" in notice
    assert "POLICY_PROSE_MUST_NOT_DUPLICATE" not in notice
    assert len(notice.encode()) < 1800
    pruned = assembler._prune_messages(
        messages=list(result.messages),
        resolved_model="test-model",
        max_prompt_tokens=20,
        tool_schema_tokens=0,
    )
    assert any(is_current_policy_message(r) for r in pruned)
    assert any("prior_silence_renewal" in str(r["content"]) for r in result.messages)
    assert all("CURRENT POLICY SNAPSHOT" not in str(r) for r in result.audit_messages)
    # An acknowledged per-conversation override wins over agent defaults.
    agent = _agent()
    agent.capabilities.maximum_outcome = "approve"
    guardrails.get_session.return_value = acknowledged(
        session,
        {
            "evaluation_policy_version": "hybrid_v1",
            "profile": "strict",
            "maximum_outcome": "deny",
            "interaction_mode": "explicit_gates",
        },
        revision=8,
    )
    original = list(result.messages)
    await refresh_current_policy(result.messages, guardrails, session)
    notices = [r for r in result.messages if is_current_policy_message(r)]
    assert result.messages[: len(original)] == original
    assert len(notices) == 2
    assert '"maximum_outcome": "deny"' in notices[-1]["content"]
    assert '"revision": 8' in notices[-1]["content"]
    assert await refresh_current_policy(result.messages, guardrails, session) is None
    rebuilt = await assembler.assemble(
        session=session,
        conversation=_conversation(),
        agent=agent,
        user_message="Continue with current policy.",
        skip_memory=skip_memory,
        prompt_context=prompt_context,
    )
    notice = next(r["content"] for r in rebuilt.messages if is_current_policy_message(r))
    assert '"maximum_outcome": "deny"' in notice
    # Session rotation observes the new owner rather than carrying a cached policy.
    rotated = session.model_copy(update={"session_id": "rotated", "intaris_session_id": "rotated"})
    guardrails.get_session.return_value = acknowledged(rotated, {}, revision=9)
    await refresh_current_policy(result.messages, guardrails, rotated)
    notice = next(r["content"] for r in reversed(result.messages) if is_current_policy_message(r))
    assert '"maximum_outcome": "unknown"' in notice
    guardrails.get_session.assert_awaited_with("rotated")
    rebuilt = await assembler.assemble(
        session=rotated,
        conversation=_conversation(),
        agent=agent,
        user_message="Continue after compaction.",
        skip_memory=skip_memory,
        prompt_context=prompt_context,
    )
    notice = next(r["content"] for r in rebuilt.messages if is_current_policy_message(r))
    assert '"maximum_outcome": "unknown"' in notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy", [None, {"maximum_outcome": False}, {"maximum_outcome": "not-approved"}]
)
async def test_unknown_or_false_policy_never_claims_approve(policy):
    session = _session()
    guardrails = _Guardrails()
    guardrails.get_session = AsyncMock(return_value=acknowledged(session, policy))
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    result = await assembler.assemble(
        session=session,
        conversation=_conversation(),
        agent=_agent(),
        user_message="Inspect only.",
        skip_memory=True,
    )
    notice = next(r["content"] for r in result.messages if is_current_policy_message(r))
    assert '"maximum_outcome": "unknown"' in notice
    guardrails.get_session.side_effect = RuntimeError("unavailable")
    await refresh_current_policy(result.messages, guardrails, session)
    notice = next(r["content"] for r in reversed(result.messages) if is_current_policy_message(r))
    assert '"availability": "unknown"' in notice
