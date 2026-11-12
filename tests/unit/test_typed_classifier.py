"""Typed classifier routing, recovery, and skill activation boundary coverage."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.api.serializers import agent_to_response
from cognis.core.agent_loop import AgentLoop
from cognis.core.agent_management import _agent_settings_payload
from cognis.core.routing_management import route_model_is_eligible
from cognis.models.agent import AgentDefinition
from cognis.models.config import ModelInfo
from cognis.models.tool import NativeToolDefinition, ToolSource, stable_tool_id
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.providers.llm.recovery_policy import NamedRouteConfig
from cognis.providers.llm.route_store import InferenceRouteStore
from cognis.providers.llm.service import LLMService
from cognis.providers.llm.systemone import SystemOneRequest
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_llm_provider,
    update_agent,
    update_agent_if_updated_at,
    upsert_model_routing,
)


def _request() -> SystemOneRequest:
    return SystemOneRequest(
        model="routes/classifier",
        state="Read source code.",
        questions={
            "group": {
                "type": "choice",
                "instructions": "Which profile group fits?",
                "criteria": {"filesystem": "Read files.", "shell": "Run commands."},
            },
            "required": {"type": "noul", "instructions": "Is file reading required?"},
        },
    )


def _response() -> dict:
    return {
        "answers": {
            "group": {
                "type": "choice",
                "choice": "filesystem",
                "confidence": 0.9,
                "probabilities": {"filesystem": 0.9, "shell": 0.1},
            },
            "required": {"type": "noul", "noul": 0.95},
        },
    }


@pytest_asyncio.fixture
async def classifier(tmp_path, usage_capture):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/classifier.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        await create_llm_provider(
            session,
            provider_id="decision",
            display_name="Decisions",
            location="controller",
            backend="litellm",
            owner_email=SYSTEM_USER_EMAIL,
            config={
                "preset": "typesafe",
                "models": [{"model_id": "jev/decision", "model_mode": "systemone"}],
            },
        )
        await create_llm_provider(
            session,
            provider_id="chat",
            display_name="Chat",
            location="controller",
            backend="litellm",
            owner_email=SYSTEM_USER_EMAIL,
            config={"preset": "openai", "models": [{"model_id": "gpt-4o-mini"}]},
        )
        await upsert_model_routing(
            session, task_type="classifier", model="cognis/decisions", provider_id=None
        )
        await session.commit()
    await InferenceRouteStore(factory).save(
        NamedRouteConfig(
            name="decisions",
            targets=[
                {"provider_id": "decision", "model": "jev/decision", "priority": 0},
                {"provider_id": "chat", "model": "gpt-4o-mini", "priority": 1},
            ],
            recovery={"max_retries_per_target": 0},
        ),
        owner=SYSTEM_USER_EMAIL,
    )
    yield LLMService(factory, usage_capture=usage_capture)
    await engine.dispose()


@pytest.mark.asyncio
async def test_native_classifier_uses_route_and_never_chat(classifier, monkeypatch):
    native = AsyncMock(return_value=_response())
    chat = AsyncMock()
    monkeypatch.setattr(classifier, "_system_one_for_provider", native)
    monkeypatch.setattr(LiteLLMProvider, "generate", chat)
    response = await classifier.classify(_request(), acting_user_email=SYSTEM_USER_EMAIL)
    assert response["answers"] == _response()["answers"]
    request, provider = native.await_args.args
    assert request.model == "jev/decision"
    assert provider.provider_id == "decision"
    assert native.await_count == 1
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_classifier_recovers_from_native_to_chat(classifier, monkeypatch):
    native = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    chat = AsyncMock(
        return_value={
            "choices": [{"message": {"content": json.dumps(_response())}}],
            "usage": {},
        }
    )
    monkeypatch.setattr(classifier, "_system_one_for_provider", native)
    monkeypatch.setattr(LiteLLMProvider, "generate", chat)
    response = await classifier.classify(_request(), acting_user_email=SYSTEM_USER_EMAIL)
    assert response["answers"] == _response()["answers"]
    assert response["model"] == "gpt-4o-mini"
    assert native.await_count == chat.await_count == 1
    assert chat.await_args.kwargs["provider_id"] == "chat"
    assert chat.await_args.kwargs["acting_user_email"] == SYSTEM_USER_EMAIL
    state = json.loads(chat.await_args.args[0][1]["content"])
    assert state["questions"] == _request().model_dump()["questions"]


@pytest.mark.asyncio
async def test_classifier_recovers_from_chat_to_native(classifier, monkeypatch):
    store = InferenceRouteStore(classifier.session_factory)
    previous = await store.get("cognis/decisions", SYSTEM_USER_EMAIL)
    await store.save(
        NamedRouteConfig(
            name="decisions",
            targets=[
                {"provider_id": "chat", "model": "gpt-4o-mini", "priority": 0},
                {"provider_id": "decision", "model": "jev/decision", "priority": 1},
            ],
            recovery={"max_retries_per_target": 0},
        ),
        owner=SYSTEM_USER_EMAIL,
        route_id=previous.route_id,
        expected_revision=previous.revision,
    )
    native = AsyncMock(return_value=_response())
    chat = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(classifier, "_system_one_for_provider", native)
    monkeypatch.setattr(LiteLLMProvider, "generate", chat)
    response = await classifier.classify(_request(), acting_user_email=SYSTEM_USER_EMAIL)
    assert response["answers"] == _response()["answers"]
    assert native.await_count == chat.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        '{"answers":{}}',
        '{"answers":{"required":{"type":"noul","noul":1.1}}}',
    ],
)
async def test_chat_classifier_rejects_malformed_answers(classifier, monkeypatch, content):
    monkeypatch.setattr(
        LiteLLMProvider,
        "generate",
        AsyncMock(return_value={"choices": [{"message": {"content": content}}]}),
    )
    with pytest.raises(ValueError):
        await classifier._classify_once(
            _request(),
            provider_id="chat",
            model="gpt-4o-mini",
            acting_user_email=SYSTEM_USER_EMAIL,
        )


def test_only_decision_routes_allow_systemone():
    info = ModelInfo(model_id="jev/decision", model_mode="systemone")
    assert route_model_is_eligible("classifier", model_id=info.model_id, model_info=info)
    assert route_model_is_eligible("intaris_evaluation", model_id=info.model_id, model_info=info)
    for route in ("default", "auxiliary_generation", "compaction", "evaluator"):
        assert not route_model_is_eligible(route, model_id=info.model_id, model_info=info)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_skill_activation_only_accepts_confident_candidate_answers(malformed):
    candidates = [
        NativeToolDefinition(
            name=name,
            description=f"{name} repository files",
            parameters={"type": "object"},
            source=ToolSource(type="builtin"),
            category="filesystem",
            read_only=name == "read",
        )
        for name in ("read", "write")
    ]
    tool_ids = [stable_tool_id(tool) for tool in candidates]
    answers = {
        tool_ids[0]: {"type": "noul", "noul": 0.95},
        tool_ids[1]: {"type": "noul", "noul": 0.6},
    }
    if malformed:
        answers["unrequested-tool"] = {"type": "noul", "noul": 1.0}
    llm = SimpleNamespace(
        classify=AsyncMock(return_value={"model": "jev/decision", "answers": answers})
    )
    cache: dict[str, list[str]] = {}
    loop = object.__new__(AgentLoop)
    loop.providers = SimpleNamespace(llm=llm)
    loop.session_cache = SimpleNamespace(
        get_skill_tool_classification=lambda _session, key: cache.get(key),
        set_skill_tool_classification=lambda _session, key, ids: cache.update({key: ids}),
    )
    ctx = SimpleNamespace(
        session=SimpleNamespace(session_id="session-classification", user_email="owner@example.org")
    )
    result = await loop._classify_skill_activation_tool_ids(
        ctx,
        activation={
            "skill_id": "cognis-coding",
            "name": "Inspect source",
            "instructions": "Read files",
        },
        candidate_tools=candidates,
    )
    assert result == (set() if malformed else {tool_ids[0]})
    assert llm.classify.await_args.kwargs["acting_user_email"] == "owner@example.org"
    llm.classify.reset_mock()
    repeated = await loop._classify_skill_activation_tool_ids(
        ctx,
        activation={
            "skill_id": "cognis-coding",
            "name": "Inspect source",
            "instructions": "Read files",
        },
        candidate_tools=candidates[:1],
    )
    assert repeated == result
    if malformed:
        llm.classify.assert_awaited_once()
    else:
        llm.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_retired_agent_settings_are_removed_without_losing_active_settings(classifier):
    active = {"executor_id": "fixture-executor", "step_agent_overrides": {"review": "reviewer"}}
    legacy = {
        **active,
        "available_workflow_ids": ["system:general-task"],
        "default_workflow_id": "system:general-task",
        "workflow_selection_mode": "automatic",
    }
    definition = AgentDefinition(
        agent_id="agent-retirement",
        owner_email=SYSTEM_USER_EMAIL,
        name="Agent",
        llm_config={"model": "gpt-4o-mini"},
        execution=legacy,
    )
    assert definition.execution == active
    async with classifier.session_factory() as session:
        row = await create_agent(
            session,
            agent_id=definition.agent_id,
            owner_email=SYSTEM_USER_EMAIL,
            name=definition.name,
            execution=legacy,
        )
        assert row.execution == active
        # Simulate an execution JSON value persisted before this feature removal.
        row.execution = legacy
        await session.commit()
        assert agent_to_response(row).execution == active
        assert _agent_settings_payload(row)["execution"] == active
        assert _agent_settings_payload(row)["workflow"] == {
            "step_agent_overrides": {"review": "reviewer"}
        }
        assert await update_agent(session, row.agent_id, updates={"execution": legacy})
        assert row.execution == active
        await session.commit()
        assert await update_agent_if_updated_at(
            session, row.agent_id, expected_updated_at=row.updated_at, updates={"execution": legacy}
        )
        await session.refresh(row)
        assert row.execution == active
