"""Startup routing must be conservative, eligible and once-only."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cognis.api.models import AgentCreateRequest, AgentUpdateRequest, ConversationCreateRequest
from cognis.core.initial_profile_selection import (
    SELECTION_KEY,
    classify_initial_profile,
    initial_profile_metadata,
    initial_profile_pending,
)
from cognis.models.agent import (
    AgentDefinition,
    AgentRuntimeProfile,
    auto_profile_selection_settings,
)


def _agent(**updates: object) -> AgentDefinition:
    agent = AgentDefinition(
        agent_id="engineering",
        owner_email="owner@test.local",
        name="Engineering",
        default_agent_profile_id="routine",
        agent_profiles={
            "routine": AgentRuntimeProfile(
                description="Routine, bounded implementation and ordinary questions.",
                agent_switchable=True,
            ),
            "deep": AgentRuntimeProfile(
                description="Difficult concurrency, recovery and data-integrity root causes.",
                agent_switchable=True,
            ),
            "manual": AgentRuntimeProfile(
                description="Manual selection only; must not be exposed to classification.",
            ),
            "disabled": AgentRuntimeProfile(
                description="Disabled profile must not be exposed.",
                enabled=False,
                agent_switchable=True,
            ),
        },
    )
    return agent.model_copy(update=updates)


def _answer(choice: str = "select:deep", confidence: float = 0.9) -> dict:
    return {
        "model": "routes/classifier",
        "answers": {
            "profile": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {
                    "keep_default": confidence if choice == "keep_default" else 1 - confidence,
                    "select:deep": confidence if choice == "select:deep" else 1 - confidence,
                },
            }
        },
    }


async def _select(llm: object, agent: AgentDefinition | None = None):
    return await classify_initial_profile(
        llm,
        agent or _agent(),
        "Diagnose a persistence race and verify cancellation-safe recovery.",
        user_email="owner@test.local",
        session_id="session-routing",
    )


@pytest.mark.asyncio
async def test_routes_only_eligible_profiles_through_typed_classifier() -> None:
    llm = SimpleNamespace(classify=AsyncMock(return_value=_answer()))
    decision = await _select(llm)
    assert decision.profile_id == "deep"
    assert decision.reason == "classified"
    request = llm.classify.call_args.args[0]
    assert request.model == "routes/classifier"
    assert set(request.questions["profile"].criteria) == {"keep_default", "select:deep"}
    payload = request.model_dump_json()
    assert "Manual selection only" not in payload
    assert "Disabled profile must" not in payload
    assert "concurrency, recovery" in payload
    assert "Routine, bounded" in payload
    assert llm.classify.call_args.kwargs["acting_user_email"] == "owner@test.local"


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [0.5, 0.54, 0.549999])
async def test_low_confidence_keeps_default(confidence: float) -> None:
    decision = await _select(
        SimpleNamespace(classify=AsyncMock(return_value=_answer(confidence=confidence)))
    )
    assert decision.profile_id == "routine"
    assert decision.reason == "low_confidence"


@pytest.mark.asyncio
async def test_confidence_boundary_and_per_agent_threshold() -> None:
    llm = SimpleNamespace(classify=AsyncMock(return_value=_answer(confidence=0.55)))
    assert (await _select(llm)).profile_id == "deep"
    assert (
        await _select(
            llm,
            _agent(execution={"auto_profile_selection": {"min_confidence": 0.95}}),
        )
    ).profile_id == "routine"


@pytest.mark.asyncio
async def test_classifier_can_abstain_for_trivial_request() -> None:
    llm = SimpleNamespace(classify=AsyncMock(return_value=_answer("keep_default", 0.99)))
    result = await classify_initial_profile(
        llm, _agent(), "Hi!", user_email="owner@test.local", session_id="session-routing"
    )
    assert result.profile_id == "routine"


@pytest.mark.asyncio
async def test_tied_predictions_keep_default_even_with_high_reported_confidence() -> None:
    response = _answer(confidence=0.9)
    response["answers"]["profile"]["probabilities"] = {"keep_default": 0.5, "select:deep": 0.5}
    result = await _select(SimpleNamespace(classify=AsyncMock(return_value=response)))
    assert result.profile_id == "routine"
    assert result.reason == "ambiguous"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["synthetic", "default_only", "manual_only", "disabled"])
async def test_no_meaningful_choice_skips_classifier(kind: str) -> None:
    agent = _agent()
    if kind == "synthetic":
        agent = agent.model_copy(update={"agent_profiles": {}, "default_agent_profile_id": None})
    elif kind == "default_only":
        agent = agent.model_copy(
            update={"agent_profiles": {"routine": agent.agent_profiles["routine"]}}
        )
    elif kind == "manual_only":
        agent.agent_profiles["deep"].agent_switchable = False
    else:
        agent.execution = {"auto_profile_selection": {"enabled": False}}
    llm = SimpleNamespace(classify=AsyncMock())
    assert (await _select(llm, agent)).profile_id == (None if kind == "synthetic" else "routine")
    llm.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sole_non_default_candidate_is_not_an_automatic_upgrade() -> None:
    agent = _agent()
    agent.agent_profiles["routine"].agent_switchable = False
    llm = SimpleNamespace(classify=AsyncMock(return_value=_answer("keep_default")))
    assert (await _select(llm, agent)).profile_id == "routine"
    payload = llm.classify.call_args.args[0].model_dump_json()
    assert "Routine, bounded" not in payload
    llm.classify.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"answers": {}},
        {"answers": {"profile": {"type": "choice", "choice": "select:manual", "confidence": 1}}},
        {"answers": {"profile": {"type": "choice", "choice": "select:unknown", "confidence": 1}}},
        _answer(confidence=float("nan")),
        {
            **_answer(),
            "answers": {**_answer()["answers"], "extra": _answer()["answers"]["profile"]},
        },
    ],
)
async def test_malformed_decisions_cannot_select_a_profile(response: dict) -> None:
    decision = await _select(SimpleNamespace(classify=AsyncMock(return_value=response)))
    assert decision.profile_id == "routine"
    assert decision.reason == "classifier_failure"


@pytest.mark.asyncio
async def test_provider_failure_falls_back_but_cancellation_propagates() -> None:
    llm = SimpleNamespace(classify=AsyncMock(side_effect=TimeoutError))
    assert (await _select(llm)).profile_id == "routine"
    llm.classify.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await _select(llm)


def test_metadata_distinguishes_startup_intent_without_fake_profile_ids() -> None:
    agent = _agent()
    assert initial_profile_pending({SELECTION_KEY: initial_profile_metadata(agent, None, None)})
    assert not initial_profile_pending(
        {SELECTION_KEY: initial_profile_metadata(agent, None, "default")}
    )
    assert not initial_profile_pending(
        {SELECTION_KEY: initial_profile_metadata(agent, "manual", "auto")}
    )
    assert not initial_profile_pending({})
    assert not initial_profile_pending({SELECTION_KEY: {"mode": "auto", "status": "resolved"}})
    agent.execution = {"auto_profile_selection": {"enabled": False}}
    assert not initial_profile_pending(
        {SELECTION_KEY: initial_profile_metadata(agent, None, "auto")}
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": "false"},
        {"min_confidence": 0.49},
        {"min_confidence": 1.01},
        {"min_confidence": float("nan")},
        {"unknown": True},
    ],
)
def test_settings_validation_is_shared_by_agent_api_and_runtime(settings: dict) -> None:
    with pytest.raises(ValidationError):
        auto_profile_selection_settings({"auto_profile_selection": settings})
    for model, fields in [
        (
            AgentDefinition,
            {"agent_id": "engineering", "owner_email": "owner@test.local", "name": "Engineering"},
        ),
        (AgentCreateRequest, {"name": "Engineering"}),
        (AgentUpdateRequest, {}),
    ]:
        with pytest.raises(ValidationError):
            model.model_validate({**fields, "execution": {"auto_profile_selection": settings}})


def test_settings_defaults_and_explicit_startup_modes() -> None:
    assert auto_profile_selection_settings(None).model_dump() == {
        "enabled": True,
        "min_confidence": 0.55,
    }
    assert (
        ConversationCreateRequest(
            agent_id="engineering", agent_profile_mode="default"
        ).agent_profile_mode
        == "default"
    )
    with pytest.raises(ValidationError):
        ConversationCreateRequest(agent_id="engineering", agent_profile_mode="adaptive")


@pytest.mark.asyncio
async def test_oversized_candidate_set_keeps_default_without_inference() -> None:
    agent = _agent()
    agent.agent_profiles.update(
        {
            f"specialist_{index}": AgentRuntimeProfile(
                description="Specialized work.",
                agent_switchable=True,
            )
            for index in range(255)
        }
    )
    llm = SimpleNamespace(classify=AsyncMock())
    result = await _select(llm, agent)
    assert result.profile_id == "routine"
    assert result.reason == "too_many_candidates"
    llm.classify.assert_not_awaited()
