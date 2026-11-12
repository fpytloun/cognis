"""Opt-in startup acceptance through the native or normal-model classifier facade."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.core.initial_profile_selection import classify_initial_profile
from cognis.models.agent import AgentDefinition
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.service import LLMService
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.store.models import Base
from cognis.store.queries import create_llm_provider, upsert_model_routing

pytestmark = pytest.mark.skipif(
    os.getenv("COGNIS_RUN_PROFILE_SELECTION_LIVE") != "1",
    reason="set COGNIS_RUN_PROFILE_SELECTION_LIVE=1 and PROFILE_CLASSIFIER_{URL,MODEL,API_KEY}",
)

PROFILES = {
    "architect": (
        "Coordinates architecture, decomposition, implementation workers, integration, and "
        "acceptance. Use for cross-component ownership and interfaces, not as an implementation "
        "worker."
    ),
    "architect-critical": (
        "Bounded architecture second opinion for exceptional unresolved risk. Select explicitly "
        "or when concrete evidence shows the standard architecture assessment is insufficient. "
        "Advisory only, not implementation or ongoing coordination."
    ),
    "developer": (
        "Default for bounded implementation with known behavior, ownership, wiring, acceptance, "
        "and verification; ordinary local implementation choices may remain. Size or test volume "
        "alone does not require senior."
    ),
    "developer-deep": (
        "Direct implementation for difficult root causes, trust boundaries, concurrency, "
        "persistence, recovery, migration, or data integrity. Select for concrete unresolved "
        "complexity or explicit user choice, not task size or precaution alone."
    ),
    "developer-senior": (
        "Choose for bounded implementation with unresolved ownership or wiring, coupled "
        "behavior, or shared lifecycle/compatibility invariants. Not a general quality tier: "
        "size, duration, production use, or test volume alone do not qualify."
    ),
    "developer-worker": (
        "Choose for explicit, low-risk implementation with behavior, owner, wiring, acceptance, "
        "and verification already specified. Not for unresolved architecture or shared invariants."
    ),
}


@pytest_asyncio.fixture
async def classifier(tmp_path):
    """Use the real classifier route and environment-bound auth in an isolated local DB."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/classifier.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    model = os.environ["PROFILE_CLASSIFIER_MODEL"]
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with factory() as db:
            await create_llm_provider(
                db,
                provider_id="profile-classifier",
                display_name="Profile classifier",
                location="controller",
                backend="litellm",
                owner_email=SYSTEM_USER_EMAIL,
                config={
                    "preset": "openrouter"
                    if os.getenv("PROFILE_CLASSIFIER_NATIVE") == "1"
                    else "openai_compatible",
                    "api_base": os.environ["PROFILE_CLASSIFIER_URL"],
                    "auth_config": {"mode": "env", "env_var": "PROFILE_CLASSIFIER_API_KEY"},
                    "models": [
                        {
                            "model_id": model,
                            "model_mode": "systemone"
                            if os.getenv("PROFILE_CLASSIFIER_NATIVE") == "1"
                            else "chat",
                        }
                    ],
                },
            )
            await upsert_model_routing(
                db, task_type="classifier", model=model, provider_id="profile-classifier"
            )
            await db.commit()
        yield LLMService(
            factory,
            usage_capture=UsageCapture(
                sink=SimpleNamespace(try_emit=lambda _event: True),
                controller_owner_id="profile-fit-live",
                coverage_epoch_id="profile-fit-live",
            ),
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_request", "expected"),
    [
        pytest.param(
            "Fix conflicts in this PR and update it.", "developer", id="reported-pr-conflicts"
        ),
        pytest.param(
            "Rebase the existing PR onto main, resolve its merge conflicts, run the existing "
            "checks, and update the PR.",
            "developer",
            id="rebase-pr",
        ),
        pytest.param(
            "Add a name filter to the existing project list using its existing API and component. "
            "Preserve current permissions and add a behavior test. Follow the current design; "
            "choose the local implementation details.",
            "developer",
            id="ordinary-implementation",
        ),
        pytest.param(
            "In src/format.ts change formatCount(0) to return '0' instead of an empty string. "
            "All callers must retain the existing signature. Add that exact case to the existing "
            "format.test.ts suite and run it. Do not change anything else.",
            "developer-worker",
            id="fully-specified-small-change",
        ),
        pytest.param(
            "Implement shared retry settings across the queue and channel adapters. Ownership "
            "and wiring are unresolved: both currently own retry decisions and their compatibility "
            "invariants are coupled. Resolve those boundaries as part of the implementation.",
            "developer-senior",
            id="coupled-implementation",
        ),
        pytest.param(
            "Fix a duplicate-write race during concurrent cancellation and crash recovery. "
            "The root cause is unknown; reproduce both transaction orderings and preserve data "
            "integrity across a restart.",
            "developer-deep",
            id="concurrency-root-cause",
        ),
        pytest.param(
            "Coordinate the implementation of a new service across the API, scheduler and UI. "
            "Define component ownership and interfaces, split independent work between workers, "
            "and own integration and acceptance. Do not implement individual components yourself.",
            "architect",
            id="cross-component-coordination",
        ),
        pytest.param("Please handle it.", "developer-senior", id="unclear-keeps-default"),
    ],
)
async def test_live_profile_fit(
    classifier: LLMService, user_request: str, expected: str, record_property: Any, caplog
) -> None:
    """Check profile fit and the real confidence gate, not mocked routing answers."""
    agent = AgentDefinition(
        agent_id="engineering",
        owner_email=SYSTEM_USER_EMAIL,
        name="Engineering",
        default_agent_profile_id="developer-senior",
        agent_profiles={
            key: {"description": description, "agent_switchable": True}
            for key, description in PROFILES.items()
        },
    )

    decision = await classify_initial_profile(
        classifier,
        agent,
        user_request,
        user_email=agent.owner_email,
        session_id="profile-fit-live",
    )
    record_property("prediction_profile_id", decision.prediction_profile_id)
    record_property("confidence", decision.confidence)
    record_property("reason", decision.reason)
    failure_types = [
        getattr(record, "extra_data", {}).get("error_type")
        for record in caplog.records
        if record.name == "cognis.core.initial_profile_selection"
    ]
    assert decision.reason != "classifier_failure", failure_types
    assert decision.profile_id == expected, decision.metadata()
