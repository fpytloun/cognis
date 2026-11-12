"""Service-boundary tests: named and direct inference share exactly one recovery."""

import asyncio
import base64
from contextlib import aclosing

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.core.speech_streaming import render_speech_stream
from cognis.models.speech import SpeechStreamEvent
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.errors import LLMStreamProviderError
from cognis.providers.llm.execution import PreparedInference
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.providers.llm.recovery import active_attempt_budget
from cognis.providers.llm.recovery_policy import NamedRouteConfig
from cognis.providers.llm.route_store import InferenceRouteStore
from cognis.providers.llm.service import LLMService
from cognis.store.models import Base
from cognis.store.queries import create_llm_provider, upsert_model_routing


def test_json_object_mode_does_not_require_schema_support():
    from cognis.models.config import ModelInfo
    from cognis.providers.llm.execution import validate_request_compatibility
    from cognis.providers.llm.recovery import IncompatibleCandidate

    info = ModelInfo(model_id="gpt-5.3-codex", supports_structured_output=False)
    validate_request_compatibility([], {"response_format": {"type": "json_object"}}, info)
    with pytest.raises(IncompatibleCandidate, match="structured output"):
        validate_request_compatibility([], {"response_format": {"type": "json_schema"}}, info)


@pytest_asyncio.fixture
async def service(tmp_path, usage_capture):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/execution.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        for provider_id in ("primary", "backup"):
            await create_llm_provider(
                session,
                provider_id=provider_id,
                display_name=provider_id,
                location="controller",
                backend="litellm",
                owner_email=SYSTEM_USER_EMAIL,
                config={
                    "preset": "openai",
                    "models": [
                        {"model_id": "tts-1"},
                        {"model_id": "gpt-4o-transcribe"},
                        {"model_id": "gpt-4o-mini", "supports_responses_api": False},
                        {"model_id": "text-embedding-3-small", "supports_embedding": True},
                    ],
                },
            )
        await upsert_model_routing(
            session, task_type="default", model="cognis/chat", provider_id=None
        )
        await upsert_model_routing(
            session, task_type="classifier", model="cognis/chat", provider_id=None
        )
        await session.commit()
    await InferenceRouteStore(factory).save(
        NamedRouteConfig(
            name="chat",
            targets=[
                {"provider_id": "primary", "model": "gpt-4o-mini", "priority": 0},
                {"provider_id": "backup", "model": "gpt-4o-mini", "priority": 1},
            ],
            recovery={"max_retries_per_target": 1},
        ),
        owner=SYSTEM_USER_EMAIL,
    )
    yield LLMService(factory, usage_capture=usage_capture)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["tts", "stt"])
@pytest.mark.parametrize("fails", [False, True])
async def test_native_speech_accepts_facade_metadata_without_forwarding_it(
    service, monkeypatch, operation, fails
):
    """Exercise the real backend signature, not a permissive backend mock."""
    calls = []

    async def physical(*args, **kwargs):
        calls.append(kwargs)
        assert not any(key.startswith("_cognis_usage") for key in kwargs["request_kwargs"])
        if fails:
            raise LLMStreamProviderError("Quota exhausted", payload={"category": "quota_exhausted"})
        yield SpeechStreamEvent(type="start")
        yield SpeechStreamEvent(type="done")

    monkeypatch.setattr("cognis.providers.llm.speech_streaming.stream_synthesis", physical)
    monkeypatch.setattr("cognis.providers.llm.speech_streaming.stream_transcription", physical)
    if operation == "tts":
        stream = service.synthesize_stream(
            "Spoken text", model="tts-1", provider_id="primary", voice="alloy"
        )
    else:

        async def audio():
            yield b"\0\0"

        stream = service.transcribe_stream(
            audio(), model="gpt-4o-transcribe", provider_id="primary"
        )
    if fails:
        with pytest.raises(LLMStreamProviderError):
            async for _ in stream:
                pass
    else:
        assert [event["type"] async for event in stream] == ["start", "done"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_native_tts_crosses_the_real_recovery_boundary(service, monkeypatch):
    events = [
        SpeechStreamEvent(type="start"),
        *[
            SpeechStreamEvent(
                type="audio",
                sample_offset=index * 16384,
                audio_base64=base64.b64encode(b"\0" * 32768).decode(),
            )
            for index in range(4)
        ],
        SpeechStreamEvent(type="done", sample_offset=65536),
    ]

    async def physical(self, *args, **kwargs):
        for event in events:
            yield event.model_dump()

    monkeypatch.setattr(LiteLLMProvider, "synthesize_stream", physical)
    actual = [
        event
        async for event in service.synthesize_stream(
            "Spoken text",
            model="tts-1",
            provider_id="primary",
            voice="alloy",
        )
    ]
    assert [event["type"] for event in actual] == [event.type for event in events]
    assert actual[1]["audio_base64"] == events[1].audio_base64

    async def recorded():
        for event in actual:
            yield event

    result = await render_speech_stream(recorded(), format="pcm", model="tts-1", voice="alloy")
    assert result.audio_bytes == b"\0" * (4 * 32768)


@pytest.mark.asyncio
async def test_live_stt_ready_precedes_microphone_input(service, monkeypatch):
    received = []

    async def audio():
        received.append(b"\0\0")
        yield b"\0\0"

    async def physical(self, frames, **kwargs):
        yield SpeechStreamEvent(type="start").model_dump()
        async for frame in frames:
            assert frame == b"\0\0"
        yield SpeechStreamEvent(type="transcript", text="A committed utterance.").model_dump()
        yield SpeechStreamEvent(type="done").model_dump()

    monkeypatch.setattr(LiteLLMProvider, "transcribe_stream", physical)
    async with asyncio.timeout(5):
        async with aclosing(
            service.transcribe_stream(
                audio(),
                model="gpt-4o-transcribe",
                provider_id="primary",
            )
        ) as stream:
            assert (await anext(stream))["type"] == "start"
            assert not received
            rest = [event async for event in stream]
    assert [event["type"] for event in rest] == ["transcript", "done"]
    assert received == [b"\0\0"]


@pytest.mark.asyncio
async def test_native_speech_does_not_replay_a_dispatched_candidate(service, monkeypatch):
    await InferenceRouteStore(service.session_factory).save(
        NamedRouteConfig(
            name="speech-no-replay",
            targets=[
                {"provider_id": "primary", "model": "tts-1", "priority": 0},
                {"provider_id": "backup", "model": "tts-1", "priority": 1},
            ],
        ),
        owner=SYSTEM_USER_EMAIL,
    )
    calls = []

    async def physical(self, *args, **kwargs):
        calls.append(kwargs["provider_id"])
        yield SpeechStreamEvent(type="start").model_dump()
        raise OSError("Connection lost after speech dispatch")

    monkeypatch.setattr(LiteLLMProvider, "synthesize_stream", physical)
    with pytest.raises(LLMStreamProviderError):
        _ = [
            event
            async for event in service.synthesize_stream(
                "Spoken text",
                model="cognis/speech-no-replay",
                voice="alloy",
                acting_user_email=SYSTEM_USER_EMAIL,
            )
        ]
    assert calls == ["primary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["fast", "tool_search"])
async def test_compatibility_fallback_signals_bypass_exhausted_transport_recovery(
    service,
    monkeypatch,
    fallback,
):
    from cognis.providers.llm.errors import (
        FastModeFallbackRequired,
        OpenAIToolSearchFallbackRequired,
    )

    await InferenceRouteStore(service.session_factory).save(
        NamedRouteConfig(
            name="compatibility",
            targets=[{"provider_id": "primary", "model": "gpt-4o-mini", "priority": 0}],
            recovery={"max_retries_per_target": 0},
        ),
        owner=SYSTEM_USER_EMAIL,
    )
    signal = (
        FastModeFallbackRequired(
            provider_id="primary", model_id="gpt-4o-mini", reason="unsupported"
        )
        if fallback == "fast"
        else OpenAIToolSearchFallbackRequired(
            provider_id="primary", model_id="gpt-4o-mini", reason="unsupported"
        )
    )
    calls = []

    async def stream(self, messages, model=None, **kwargs):
        calls.append(kwargs["provider_id"])
        raise signal
        yield

    monkeypatch.setattr(LiteLLMProvider, "stream_generate", stream)
    with pytest.raises(type(signal)) as failure:
        _ = [
            chunk
            async for chunk in service.stream_generate(
                [],
                model="cognis/compatibility",
                acting_user_email=SYSTEM_USER_EMAIL,
            )
        ]
    assert failure.value is signal
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_shared_all_target_limits_reach_stream_and_nonstream_callers(service, monkeypatch):
    from unittest.mock import AsyncMock

    from cognis.providers.llm.errors import classify_llm_exception
    from cognis.providers.llm.execution import build_inference_plan

    request_plan = await build_inference_plan(
        service,
        model="cognis/chat",
        provider_id=None,
        task_type="default",
        owner="alice@cognis.local",
        session_identity=None,
    )
    for candidate, delay in zip(request_plan.candidates, (3600, 1800), strict=True):
        await service.inference_recovery.availability.limit(
            candidate.fingerprint, block_seconds=delay
        )
    transport = AsyncMock(side_effect=AssertionError("blocked provider dispatched"))
    monkeypatch.setattr(LiteLLMProvider, "generate", transport)
    for streaming in (True, False):
        with pytest.raises(LLMStreamProviderError) as failure:
            if streaming:
                _ = [
                    chunk
                    async for chunk in service.stream_generate(
                        [], model="cognis/chat", acting_user_email="alice@cognis.local"
                    )
                ]
            else:
                await service.generate(
                    [], model="cognis/chat", acting_user_email="alice@cognis.local"
                )
        payload = classify_llm_exception(failure.value)
        assert payload["category"] == "rate_limit"
        assert payload["retry_after_seconds"] == 1800
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_stream_retry_fallback_stickiness_and_preparation(service, monkeypatch):
    calls, preparations = [], []
    monkeypatch.setattr("cognis.providers.llm.recovery.compute_delay", lambda *args: 0)

    async def stream(self, messages, model=None, **kwargs):
        assert active_attempt_budget.get() is not None
        calls.append(kwargs["provider_id"])
        assert messages[0]["content"] == kwargs["provider_id"]
        if kwargs["provider_id"] == "primary":
            raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})
        yield {"choices": [{"delta": {"content": "answer"}}]}

    async def prepare(candidate):
        preparations.append(candidate.target.provider_id)
        return PreparedInference([{"role": "user", "content": candidate.target.provider_id}])

    monkeypatch.setattr(LiteLLMProvider, "stream_generate", stream)
    kwargs = {
        "model": "cognis/chat",
        "acting_user_email": "alice@cognis.local",
        "cognis_session_id": "llm-session",
        "cognis_prepare_candidate": prepare,
    }
    result = [chunk async for chunk in service.stream_generate([], **kwargs)]
    assert calls == ["primary", "primary", "backup"]
    assert result[-1]["choices"][0]["delta"]["content"] == "answer"
    assert active_attempt_budget.get() is None
    calls.clear()
    # A later complete request preserves the successful replacement.
    result = [chunk async for chunk in service.stream_generate([], **kwargs)]
    assert calls == ["backup"]


@pytest.mark.asyncio
async def test_idle_failback_probe_failure_and_success(service, monkeypatch):
    from cognis.providers.llm.availability import InferenceAvailability

    calls = []
    healthy = False
    monkeypatch.setattr("cognis.providers.llm.recovery.compute_delay", lambda *args: 0)

    async def stream(self, messages, model=None, **kwargs):
        calls.append(kwargs["provider_id"])
        if kwargs["provider_id"] == "primary" and not healthy:
            raise LLMStreamProviderError("busy", payload={"category": "provider_5xx"})
        yield {"choices": [{"delta": {"content": "ok"}}]}

    monkeypatch.setattr(LiteLLMProvider, "stream_generate", stream)
    kwargs = {
        "model": "cognis/chat",
        "acting_user_email": "alice@cognis.local",
        "cognis_session_id": "idle-session",
    }

    async def run():
        return [chunk async for chunk in service.stream_generate([], **kwargs)]

    await run()
    calls.clear()
    for state in service.inference_activity._states.values():
        state["last"] -= 3601
    await run()
    assert calls == ["primary", "backup"]
    calls.clear()
    await run()
    assert calls == ["backup"]
    healthy = True
    calls.clear()
    # Clear only the test circuit; real recovery waits for its cooldown.
    service.inference_recovery.availability = InferenceAvailability()
    for state in service.inference_activity._states.values():
        state["last"] -= 3601
    await run()
    assert calls == ["primary"]
    calls.clear()
    await run()
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_direct_nonstream_retry_uses_same_budget(service, monkeypatch):
    calls = []
    monkeypatch.setattr("cognis.providers.llm.recovery.compute_delay", lambda *args: 0)

    async def generate(self, messages, **kwargs):
        calls.append(kwargs["provider_id"])
        if len(calls) == 1:
            raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})
        return {"choices": [{"message": {"content": "answer"}}]}

    monkeypatch.setattr(LiteLLMProvider, "generate", generate)
    result = await service.generate([], model="gpt-4o-mini", provider_id="primary", max_retries=1)
    assert calls == ["primary", "primary"]
    assert result["_cognis_inference"]["provider_id"] == "primary"


@pytest.mark.asyncio
async def test_deadline_survives_separate_anext_tasks(service, monkeypatch):
    async def stream(self, messages, **kwargs):
        yield {"choices": [{"delta": {"content": "partial"}}]}
        await asyncio.sleep(10)
        yield {"choices": [{"delta": {"content": "late"}}]}

    monkeypatch.setattr(LiteLLMProvider, "stream_generate", stream)
    iterator = service.stream_generate([], model="cognis/chat", timeout=0.1)
    assert (await asyncio.create_task(anext(iterator)))["choices"]
    with pytest.raises(TimeoutError):
        await asyncio.create_task(anext(iterator))
    await iterator.aclose()


@pytest.mark.asyncio
async def test_system_role_resolves_same_named_plan(service, monkeypatch):
    calls = []

    async def generate(self, messages, **kwargs):
        calls.append(kwargs["provider_id"])
        return {"choices": []}

    monkeypatch.setattr(LiteLLMProvider, "generate", generate)
    await service.generate([], task_type="classifier")
    assert calls == ["primary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_agent_adopts_fallback_preparation_without_rewriting_configuration(
    service, monkeypatch, native
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.unit.test_agent_loop import (
        CHAT_POLICY,
        AgentDefinition,
        AgentLoop,
        PauseWaiter,
        SessionLock,
        StepContext,
        StepDefinition,
        _FakeContextAssembler,
        _NoopEventBus,
        _NoopGuardrails,
        _NoopRememberQueue,
        _NoopSessionCache,
        _NoopSessionManager,
        _SessionStub,
    )

    calls = []
    monkeypatch.setattr("cognis.providers.llm.recovery.compute_delay", lambda *args: 0)
    assembler = _FakeContextAssembler(max_context_tokens=100_000)
    if native:
        from copy import deepcopy
        from dataclasses import replace

        from cognis.core import agent_loop as loop_module

        original_exposure = loop_module.prepare_tool_exposure

        def aliased_exposure(**kwargs):
            exposure = original_exposure(**kwargs)
            tools = deepcopy(exposure.tools)
            assert tools
            name = tools[0]["function"]["name"]
            alias = "aliased_" + name
            tools[0]["function"]["name"] = alias
            return replace(
                exposure,
                tools=tools,
                alias_map={**exposure.alias_map, alias: exposure.alias_map.get(name, name)},
            )

        monkeypatch.setattr(loop_module, "prepare_tool_exposure", aliased_exposure)
        from sqlalchemy import update

        from cognis.providers.llm.anthropic.contracts import (
            AnthropicNativeEnvelope,
            CompiledAnthropicToolBundle,
            ResolvedAnthropicRequestContext,
        )
        from cognis.providers.llm.anthropic.integration import (
            NATIVE_REQUEST_CONTEXT_KWARG,
            NATIVE_TOOL_BUNDLE_KWARG,
        )
        from cognis.providers.llm.recovery_policy import RecoveryPolicy, RouteTarget
        from cognis.store.models import LLMProvider

        async with service.session_factory() as session:
            await session.execute(
                update(LLMProvider).values(
                    config={
                        "preset": "anthropic",
                        "auth_config": {"mode": "oauth"},
                        "models": [{"model_id": "claude-opus-4-6"}],
                    }
                )
            )
            await session.commit()
        store = InferenceRouteStore(service.session_factory)
        route = await store.get("cognis/chat", "alice@cognis.local")
        await store.save(
            NamedRouteConfig(
                name="chat",
                targets=[
                    RouteTarget(provider_id="primary", model="claude-opus-4-6"),
                    RouteTarget(provider_id="backup", model="claude-opus-4-6", priority=1),
                ],
                recovery=RecoveryPolicy(max_retries_per_target=0),
            ),
            route_id=route.route_id,
            expected_revision=route.revision,
            owner=SYSTEM_USER_EMAIL,
        )
        info = await service.get_model_info(
            "claude-opus-4-6", provider_id="primary", acting_user_email="alice@cognis.local"
        )
        chain = await service.prepare_anthropic_native_chain(
            model="claude-opus-4-6",
            model_info=info,
            provider_id="primary",
            acting_user_email="alice@cognis.local",
            tools=[],
            alias_map={},
            stable_id_map={},
            argument_alias_map={},
            request_kwargs={},
        )
        context = ResolvedAnthropicRequestContext.from_dict(chain[NATIVE_REQUEST_CONTEXT_KWARG])
        bundle = CompiledAnthropicToolBundle.from_dict(chain[NATIVE_TOOL_BUNDLE_KWARG])
        envelope = AnthropicNativeEnvelope(
            native_blocks=({"type": "text", "text": "Earlier response."},),
            stop_details={},
            usage={},
            pending_client_message_id=None,
            pending_server_message_id=None,
            stop_reason="end_turn",
            bundle_fingerprint=bundle.fingerprint,
            provider_fingerprint=context.chain_id,
            model_fingerprint=context.model,
            thinking_fingerprint=context.thinking_fingerprint,
        ).to_dict()
        original_assemble = assembler.assemble

        async def assemble(**kwargs):
            result = await original_assemble(**kwargs)
            result.messages.insert(
                0,
                {
                    "role": "assistant",
                    "content": "Earlier response.",
                    "_anthropic_native_envelope": envelope,
                },
            )
            return result

        assembler.assemble = assemble

    async def stream(self, messages, model=None, **kwargs):
        calls.append((kwargs["provider_id"], model, messages))
        if kwargs["provider_id"] == "primary":
            if native:
                raise LLMStreamProviderError(
                    "rate limited", payload={"category": "rate_limit", "retry_after_seconds": 3600}
                )
            raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})
        if native:
            from cognis.providers.llm.anthropic.integration import build_native_request
            from cognis.providers.llm.reasoning import apply_reasoning_config

            prepared = build_native_request(
                provider=SimpleNamespace(
                    provider_id="backup",
                    location="controller",
                    config={"preset": "anthropic", "auth_config": {"mode": "oauth"}},
                ),
                model=model,
                model_info=info,
                messages=messages,
                request_kwargs=apply_reasoning_config(
                    kwargs, model_id=model, provider_preset="anthropic", model_info=info
                ).request_kwargs,
                credential_ref="$credential:anthropic-oauth",
            )
            assert prepared is not None
            assert any(m.get("_anthropic_native_envelope") for m in messages)
        yield {"choices": [{"delta": {"content": "Fallback answer."}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    monkeypatch.setattr(LiteLLMProvider, "stream_generate", stream)
    cache = _NoopSessionCache()
    cache.persist_runtime_metadata = AsyncMock()
    loop = AgentLoop(
        providers=SimpleNamespace(llm=service, guardrails=_NoopGuardrails()),
        session_manager=_NoopSessionManager(),
        session_cache=cache,
        context_assembler=assembler,
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    agent = AgentDefinition(
        agent_id="routing-agent",
        owner_email="alice@cognis.local",
        name="Routing",
        llm_config={"model": "cognis/chat"},
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=_SessionStub(
            session_id="route-session",
            intaris_session_id="route-session",
            mnemory_session_id=None,
            user_email="alice@cognis.local",
            agent_id=agent.agent_id,
        ),
        conversation=SimpleNamespace(conversation_id="route-conversation"),
        agent=agent,
        todos=[],
        policy=CHAT_POLICY,
        user_message="Say hello.",
        user_attachments=[],
        attachment_notice=None,
        prior_context=None,
        system_initiated=True,
        is_retry=False,
        workflow_state=None,
        step_run_id=None,
        executor_environment=None,
        cancel_event=None,
        bootstrap_wait_for_intention=False,
        tool_registry=None,
        executor_connection=None,
    )
    output = await loop.run_step(ctx)
    assert output.error is None
    assert output.summary == "Fallback answer."
    assert [provider for provider, _, _ in calls] == (
        ["primary", "backup"] if native else ["primary", "primary", "backup"]
    )
    assert ctx.current_provider_id == "backup"
    assert ctx.agent.llm_config.model == "cognis/chat"
    assert ctx.runtime_info["inference_target"]["provider_id"] == "backup"
    if native:
        calls.clear()
        output = await loop.run_step(ctx)
        assert output.error is None
        assert [provider for provider, _, _ in calls] == ["backup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["transcribe", "synthesize"])
@pytest.mark.parametrize("named", [False, True])
async def test_media_service_keeps_adapter_signature_and_selected_target(
    service, monkeypatch, operation, named
):
    from unittest.mock import AsyncMock

    import httpx

    calls = []

    async def credentials(provider):
        return {"api_base": f"https://{provider.provider_id}.test", "api_key": "test-key"}

    async def post(client, url, **kwargs):
        calls.append(url)
        request = httpx.Request("POST", url)
        if operation == "transcribe":
            return httpx.Response(200, json={"text": "transcript"}, request=request)
        return httpx.Response(
            200, content=b"audio", headers={"content-type": "audio/mpeg"}, request=request
        )

    monkeypatch.setattr(service, "_resolve_provider_kwargs", credentials)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(
        "cognis.audio.preprocessing.prepare_audio_for_stt",
        AsyncMock(return_value=(b"audio", "audio/wav", "input.wav")),
    )
    kwargs = (
        {"model": "cognis/chat"} if named else {"model": "gpt-4o-mini", "provider_id": "backup"}
    )
    if operation == "transcribe":
        response = await service.transcribe(
            b"audio", mime_type="audio/wav", filename="input.wav", **kwargs
        )
        assert response.text == "transcript"
    else:
        response = await service.synthesize("hello", voice="alloy", low_latency=True, **kwargs)
        assert response.audio_bytes == b"audio"
    assert len(calls) == 1
    assert calls[0].startswith("https://primary.test" if named else "https://backup.test")


def test_stream_error_keeps_attachment_repair_evidence():
    from cognis.providers.llm.recovery import stream_failure

    payload = {
        "category": "artifact_fetch",
        "artifact_ids": ["img_failed"],
        "artifact_urls": ["https://assets.test/failed.png"],
        "param": "messages.0.content",
    }
    failure = stream_failure({"response_error": payload})
    assert failure is not None
    normalized = failure.to_payload()
    for key, value in payload.items():
        assert normalized[key] == value


@pytest.mark.asyncio
async def test_image_fallback_uses_selected_provider_credentials(service, monkeypatch):
    from types import SimpleNamespace

    import litellm

    calls = []
    monkeypatch.setattr("cognis.providers.llm.recovery.compute_delay", lambda *args: 0)

    async def credentials(provider):
        return {"api_base": f"https://{provider.provider_id}.test", "api_key": "test-key"}

    async def generate(**kwargs):
        calls.append(kwargs["api_base"])
        if kwargs["api_base"] == "https://primary.test":
            raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})
        return SimpleNamespace(data=[{"b64_json": "aW1hZ2U="}])

    monkeypatch.setattr(service, "_resolve_provider_kwargs", credentials)
    monkeypatch.setattr(litellm, "aimage_generation", generate)
    result = await service.image_generate("draw a square", model="cognis/chat")
    assert result.images
    assert calls == ["https://primary.test", "https://primary.test", "https://backup.test"]


@pytest.mark.asyncio
async def test_chat_sdk_has_no_retry_owner_inside_shared_recovery(service, monkeypatch):
    import litellm

    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ]
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    await service.generate(
        [{"role": "user", "content": "hello"}],
        model="gpt-4o-mini",
        provider_id="primary",
        timeout=3.5,
    )
    assert len(calls) == 1
    assert calls[0]["num_retries"] == 0
    assert calls[0]["max_retries"] == 0
    assert calls[0]["timeout"] <= 3.5
