"""Startup estimates reach the real system preamble without widening profile eligibility."""

import pytest

from cognis.core.context import ContextAssembler
from cognis.core.initial_profile_selection import SELECTION_KEY
from cognis.models.agent import AgentRuntimeProfile
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


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
@pytest.mark.parametrize(
    ("used", "prediction", "confidence", "outcome", "reason"),
    [
        (True, "developer", 0.59, "developer", "classified"),
        (True, "developer-deep", 0.52, "developer-senior", "low_confidence"),
        (True, "developer-senior", 0.71, "developer-senior", "default"),
        (True, None, None, "developer-senior", "classifier_failure"),
        (False, None, None, "developer-senior", "explicit_selection"),
    ],
)
async def test_startup_estimate_in_system_context(
    skip_memory, used, prediction, confidence, outcome, reason
) -> None:
    agent = _agent()
    agent.default_agent_profile_id = "developer-senior"
    agent.agent_profiles = {
        "developer": AgentRuntimeProfile(
            description="Bounded implementation.", agent_switchable=True
        ),
        "developer-senior": AgentRuntimeProfile(
            description="Coupled implementation.", agent_switchable=True
        ),
        "developer-deep": AgentRuntimeProfile(
            description="Difficult root causes.", agent_switchable=True
        ),
        "manual": AgentRuntimeProfile(description="Manual-only routing description."),
        "disabled": AgentRuntimeProfile(
            description="Disabled routing description.", enabled=False, agent_switchable=True
        ),
    }
    conversation = _conversation()
    conversation.agent_profile_id = outcome
    conversation.context.platform_data[SELECTION_KEY] = {
        "mode": "auto",
        "status": "resolved",
        "reason": reason,
        "profile_id": outcome,
        "classifier_used": used,
        "prediction_profile_id": prediction,
        "confidence": confidence,
    }
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_manager=_SessionManager(),
        session_cache=_SessionCache(),
        compaction_threshold=0.85,
    )
    result = await assembler.assemble(
        session=_session(),
        conversation=conversation,
        agent=agent,
        user_message="Fix the existing change.",
        skip_memory=skip_memory,
    )
    profiles = [
        message["content"]
        for message in result.messages
        if message.get("role") == "system"
        and "<agent_runtime_profile>" in message.get("content", "")
    ]
    assert len(profiles) == 1
    context = profiles[0]
    assert "Manual-only routing description" not in context
    assert "Disabled routing description" not in context
    if not used:
        assert "<startup_profile_selection>" not in context
        return
    expected_prediction = prediction or (
        "configured default" if confidence is not None else "unknown"
    )
    assert f"Startup Auto classifier prediction: {expected_prediction}." in context
    assert (
        f"Classifier confidence (0–1): {confidence if confidence is not None else 'unknown'}."
        in context
    )
    assert f"Startup outcome: {outcome} ({reason})." in context
    assert "currently eligible profiles listed below" in context
    assert "Respect explicit user profile choices" in context
