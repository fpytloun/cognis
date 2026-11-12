from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from cognis.core.agent_management import AgentManagementDependencies
from cognis.core.artifact_revisions import ContentActor
from cognis.core.speech import SpeechService
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_llm_provider,
    create_user,
    get_agent,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL, handle_agent_management_tool
from cognis.tools.introspection import validate_available_tool_call_with_context
from cognis.tools.native_validation import NativeValidationContext

OWNER = "owner@example.com"
GUEST = "guest@example.com"
CHAT = {"provider_id": "chat", "model": "chat-model", "temperature": 0.4}
TTS = {"tts_provider_id": "speech", "tts_model": "speech-model", "tts_voice": "nova"}


@pytest_asyncio.fixture
async def deps(tmp_path: Any) -> Any:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/speech.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        for email in (OWNER, GUEST):
            await create_user(session, email=email, name=email, password_hash="test", role="admin")
        await create_agent(
            session,
            agent_id="target",
            name="Target",
            owner_email=OWNER,
            status="active",
            llm_config=CHAT,
            permissions={"allowed_credentials": ["keep"]},
            execution={"default_chat_mode": "plan"},
        )
        await create_agent_grant(
            session,
            agent_id="target",
            grantee_user_email=GUEST,
            executor_scope="owner_executor",
            granted_by=OWNER,
        )
        for provider_id, models in (
            ("chat", ["chat-model"]),
            ("speech", ["speech-model", "speech-model-2"]),
            ("other-speech", ["other-model"]),
        ):
            await create_llm_provider(
                session,
                provider_id=provider_id,
                display_name=provider_id,
                location="controller",
                backend="litellm",
                config={"models": models},
            )
        await session.commit()
    try:
        yield AgentManagementDependencies(session_factory=factory)
    finally:
        await engine.dispose()


async def call(
    deps: Any,
    action: str = "settings_update",
    *,
    settings: Any = None,
    actor: str = OWNER,
    runtime: RuntimeAccessContext | None = None,
    **extra: Any,
) -> Any:
    arguments = {"action": action, "agent_id": "target", **extra}
    if settings is not None:
        arguments["settings"] = settings
    return await handle_agent_management_tool(
        tool_name="manage_agents",
        deps=deps,
        user_email=actor,
        current_agent_id="manager",
        runtime_access=runtime
        or RuntimeAccessContext(
            user_email=actor, agent_id="manager", agent_owner_email=actor, agent_type="primary"
        ),
        arguments=arguments,
    )


async def validate(deps: Any, settings: Any, *, actor: str = OWNER) -> Any:
    return await validate_available_tool_call_with_context(
        [MANAGE_AGENTS_TOOL],
        "manage_agents",
        {"action": "settings_update", "agent_id": "target", "settings": settings},
        context=NativeValidationContext(
            actor_email=actor, current_agent_id="manager", agent_management_deps=deps
        ),
    )


async def config(deps: Any) -> dict[str, Any]:
    async with deps.session_factory() as session:
        row = await get_agent(session, "target")
        assert row is not None
        assert row.permissions == {"allowed_credentials": ["keep"]}
        assert row.execution == {"default_chat_mode": "plan"}
        assert row.status == "active"
        return dict(row.llm_config)


@pytest.mark.asyncio
async def test_native_speech_patch_persists_and_reads_back_without_chat_overwrite(
    deps: Any,
) -> None:
    assert (await validate(deps, TTS))["valid"]
    assert await config(deps) == CHAT  # Dry-run validation never writes.
    result = await call(deps, settings=TTS)
    assert not result.is_error, result.output
    assert json.loads(result.output)["settings"]["llm_config"] == {**CHAT, **TTS}
    for action in ("get", "settings_get"):
        result = await call(deps, action)
        assert not result.is_error, result.output
        payload = json.loads(result.output)
        readback = payload["agent"] if action == "get" else payload["settings"]
        assert readback["llm_config"] == {**CHAT, **TTS}
    assert await config(deps) == {**CHAT, **TTS}
    # The saved preference reaches the existing runtime resolver, not a global route write.
    llm = AsyncMock()
    llm.resolve_model_target.return_value = ("speech-model", "speech")
    service = SpeechService(session_factory=deps.session_factory, artifacts=None, llm=llm)
    binding = await service.binding(ContentActor(OWNER, "target", None, None), None, "target")
    assert (binding.provider, binding.model, binding.voice) == ("speech", "speech-model", "nova")
    llm.resolve_model_target.assert_awaited_once_with(
        "speech-model",
        task_type="text_to_speech",
        acting_user_email=OWNER,
        explicit_provider_id="speech",
    )
    llm.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_omission_reset_fallback_map_and_input_order(deps: Any) -> None:
    for settings in (
        dict(reversed(list(TTS.items()))),
        {"tts_voice": None},
        {"tts_model": "speech-model-2"},
        {"voice": "legacy", "voices": {"speech": "nova"}},
        {"tts_provider_id": None, "tts_model": None, "tts_voice": None},
    ):
        assert (await validate(deps, settings))["valid"]
        result = await call(deps, settings=settings)
        assert not result.is_error, result.output
    stored = await config(deps)
    assert stored == {
        **CHAT,
        "tts_provider_id": None,
        "tts_model": None,
        "tts_voice": None,
        "voice": "legacy",
        "voices": {"speech": "nova"},
    }
    llm = AsyncMock()
    llm.resolve_model_target.return_value = ("global-model", "speech")
    service = SpeechService(session_factory=deps.session_factory, artifacts=None, llm=llm)
    binding = await service.binding(ContentActor(OWNER, "target", None, None), None, "target")
    assert binding.voice == "nova"
    result = await call(deps, settings={"voices": {}})
    assert not result.is_error, result.output
    assert (await config(deps))["voices"] == {}
    binding = await service.binding(ContentActor(OWNER, "target", None, None), None, "target")
    assert binding.voice == "legacy"
    assert llm.resolve_model_target.call_args.args == (None,)
    assert llm.resolve_model_target.call_args.kwargs["explicit_provider_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"tts_provider_id": "missing", "tts_model": "speech-model"},
        {"tts_provider_id": "speech"},
        {"tts_provider_id": "speech", "tts_model": "other-model"},
        {"tts_voice": 12},
        {"voices": {"speech": 12}},
        {"voices": None},
    ],
)
async def test_invalid_speech_settings_are_rejected_without_writes(
    deps: Any, settings: Any
) -> None:
    assert not (await validate(deps, settings))["valid"]
    result = await call(deps, settings=settings)
    assert result.is_error
    assert await config(deps) == CHAT


@pytest.mark.asyncio
async def test_switch_provider_validates_retained_model_and_keeps_voice(deps: Any) -> None:
    assert not (await call(deps, settings=TTS)).is_error
    result = await call(deps, settings={"tts_provider_id": "other-speech"})
    assert result.is_error
    assert await config(deps) == {**CHAT, **TTS}
    result = await call(
        deps, settings={"tts_model": "other-model", "tts_provider_id": "other-speech"}
    )
    assert not result.is_error, result.output
    assert (await config(deps))["tts_voice"] == "nova"


@pytest.mark.asyncio
async def test_shared_agent_is_not_editable_even_by_admin(deps: Any) -> None:
    assert not (await validate(deps, TTS, actor=GUEST))["valid"]
    for action in ("settings_update", "settings_get", "get"):
        result = await call(deps, action, settings=TTS, actor=GUEST)
        assert result.is_error
        assert "access denied" in result.output
    assert await config(deps) == CHAT


@pytest.mark.asyncio
async def test_management_runtime_permissions_are_not_widened(deps: Any) -> None:
    runtime = RuntimeAccessContext(
        user_email=OWNER, agent_id="manager", agent_owner_email=OWNER, agent_type="secondary"
    )
    result = await call(deps, settings=TTS, runtime=runtime)
    assert result.is_error
    assert result.metadata["code"] == "agent_management_context_denied"
    assert await config(deps) == CHAT


@pytest.mark.asyncio
async def test_system_override_native_contract_supports_existing_tts_fields(deps: Any) -> None:
    arguments = {
        "action": "system_overrides_update",
        "agent_id": "system:explore",
        "overrides": {"llm_config": {**TTS, "temperature": 0.3}},
    }
    validated = await validate_available_tool_call_with_context(
        [MANAGE_AGENTS_TOOL],
        "manage_agents",
        arguments,
        context=NativeValidationContext(
            actor_email=OWNER, current_agent_id="manager", agent_management_deps=deps
        ),
    )
    assert validated["valid"], validated
    result = await call(
        deps,
        "system_overrides_update",
        agent_id="system:explore",
        overrides=arguments["overrides"],
    )
    assert not result.is_error, result.output
    result = await call(deps, "system_overrides_get", agent_id="system:explore")
    assert not result.is_error, result.output
    system = json.loads(result.output)["system_agent"]
    assert system["effective"]["llm_config"].items() >= {**TTS, "temperature": 0.3}.items()
    result = await call(
        deps,
        "system_overrides_update",
        agent_id="system:explore",
        overrides={"llm_config": None},
    )
    assert not result.is_error, result.output
    assert await config(deps) == CHAT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding",
    [
        {"tts_provider_id": "speech"},
        {"tts_provider_id": "missing", "tts_model": "speech-model"},
        {"tts_provider_id": "speech", "tts_model": "other-model"},
    ],
)
async def test_system_override_rejects_invalid_tts_pair_without_writes(
    deps: Any, binding: dict[str, Any]
) -> None:
    result = await call(
        deps,
        "system_overrides_update",
        agent_id="system:explore",
        overrides={"llm_config": binding},
    )
    assert result.is_error
    result = await call(deps, "system_overrides_get", agent_id="system:explore")
    assert not result.is_error, result.output
    assert json.loads(result.output)["system_agent"]["override"] is None
    assert await config(deps) == CHAT
