"""Speech policy and durable audio jobs exercised through real storage and DB."""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.artifact_revisions import ContentActor
from cognis.core.audio_context import document_audio_context
from cognis.core.audio_jobs import AudioJobService
from cognis.core.audio_planning import podcast_length_estimates
from cognis.core.audio_sources import AudioSource, source_speech_binding
from cognis.core.speech import SpeechService, SpeechUnavailable, narration_text, speech_chunks
from cognis.core.speech_cache import SpeechGenerationInterrupted
from cognis.core.speech_models import AudioJobCreate, SpeechRequest
from cognis.models.config import SpeechToTextResult, TextToSpeechResult
from cognis.models.speech import SpeechStreamEvent
from cognis.providers.llm.errors import LLMStreamProviderError
from cognis.providers.llm.routing import route_chain
from cognis.store.database import create_engine, create_session_factory
from cognis.store.deliverable_storage import store_deliverable_payload
from cognis.store.models import (
    ArtifactRecordRow,
    AudioJobRow,
    Base,
    CoordinationLeaseRow,
    DeliverableRow,
    LLMProvider,
)
from cognis.store.queries import create_user, upsert_user_ui_state
from cognis.tools.builtin.speech import handle_speech_tool, speech_tools
from cognis.tools.introspection import audit_tool_registration

OWNER = "owner@example.com"
OTHER = "other@example.com"


async def test_relative_estimates_do_not_use_models_or_voice_discovery(speech_env):
    service, stub = speech_env
    document = await source(service)
    jobs = AudioJobService(service)
    plans = await jobs.estimates(OWNER, document.deliverable_id)
    assert plans == podcast_length_estimates(
        (await jobs.source(OWNER, document.deliverable_id, load=True)).spoken_text()
    )
    stub.generate.assert_not_awaited()
    stub.discover_voices.assert_not_awaited()
    stub.synthesize.assert_not_awaited()
    with pytest.raises(PermissionError):
        await jobs.estimates(OTHER, document.deliverable_id)


@pytest.mark.parametrize("length", ["shorter", "default", "longer"])
async def test_queued_jobs_pin_the_displayed_word_budget(speech_env, length):
    service, stub = speech_env
    document = await source(service)
    jobs = AudioJobService(service)
    plan = next(
        plan
        for plan in await jobs.estimates(OWNER, document.deliverable_id)
        if plan.length == length
    )
    request = AudioJobCreate(
        content_ref=document.deliverable_id,
        mode="podcast",
        length=length,
        idempotency_key=uuid.uuid4().hex,
    )
    queued = await jobs.create(OWNER, request)
    assert queued.length == length
    assert queued.target_words == plan.target_words
    stub.generate.assert_not_awaited()
    original = jobs.estimates
    jobs.estimates = AsyncMock(side_effect=AssertionError("Idempotent retry must not replan"))
    assert (await jobs.create(OWNER, request)).job_id == queued.job_id
    jobs.estimates = original
    await jobs.tick()
    result = await jobs.view(OWNER, queued.job_id)
    assert result.status == "completed"
    assert f"{plan.target_words} spoken words" in stub.generate.call_args.args[0][0]["content"]
    assert result.target_words == plan.target_words


async def test_relative_idempotency_rejects_changed_options(speech_env):
    service, _ = speech_env
    document = await source(service)
    jobs = AudioJobService(service)
    key = uuid.uuid4().hex
    await jobs.create(
        OWNER,
        AudioJobCreate(
            content_ref=document.deliverable_id,
            mode="podcast",
            length="default",
            idempotency_key=key,
        ),
    )
    with pytest.raises(ValueError, match="different audio options"):
        await jobs.create(
            OWNER,
            AudioJobCreate(
                content_ref=document.deliverable_id,
                mode="podcast",
                length="longer",
                idempotency_key=key,
            ),
        )


async def test_explicit_minutes_remain_supported_without_new_planning(speech_env):
    service, _ = speech_env
    document = await source(service)
    jobs = AudioJobService(service)
    jobs.estimates = AsyncMock(side_effect=AssertionError("Legacy request must keep its budget"))
    result = await jobs.create(
        OWNER,
        AudioJobCreate(
            content_ref=document.deliverable_id,
            mode="podcast",
            target_minutes=3,
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    assert result.length is None and result.target_words is None
    async with service.sessions() as session:
        row = await session.get(AudioJobRow, result.job_id)
    assert row.target_minutes == 3


async def test_pre_upgrade_default_intent_reconciles_without_replanning(speech_env):
    service, _ = speech_env
    document = await source(service)
    jobs = AudioJobService(service)
    key = uuid.uuid4().hex
    legacy = await jobs.create(
        OWNER,
        AudioJobCreate(
            content_ref=document.deliverable_id,
            mode="podcast",
            target_minutes=6,
            idempotency_key=key,
        ),
    )
    jobs.estimates = AsyncMock(side_effect=AssertionError("Accepted legacy intent must not replan"))
    retry = await jobs.create(
        OWNER,
        AudioJobCreate(
            content_ref=document.deliverable_id,
            mode="podcast",
            idempotency_key=key,
        ),
    )
    assert retry.job_id == legacy.job_id
    assert retry.length is None and retry.target_words is None
    with pytest.raises(ValueError, match="different audio options"):
        await jobs.create(
            OWNER,
            AudioJobCreate(
                content_ref=document.deliverable_id,
                mode="podcast",
                length="default",
                idempotency_key=key,
            ),
        )


class SpeechStub:
    async def synthesize_stream(self, text, **kwargs):
        await self.synthesize(text, **kwargs)
        for event in (
            SpeechStreamEvent(type="start"),
            SpeechStreamEvent(type="audio", sample_offset=0, audio_base64="AAA="),
            SpeechStreamEvent(type="done", sample_offset=1),
        ):
            yield event.model_dump()

    def __init__(self):
        self.provider = "speech-provider"
        self.discover_voices = AsyncMock(
            return_value=[
                {"voice_id": name, "name": name}
                for name in (
                    "alloy",
                    "ash",
                    "coral",
                    "echo",
                    "fable",
                    "onyx",
                    "nova",
                    "sage",
                    "shimmer",
                )
            ]
        )
        self.generate = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {"content": "A grounded narrative. The value is 42."},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        self.synthesize = AsyncMock(
            return_value=TextToSpeechResult(
                audio_bytes=b"ID3audio", content_type="audio/mpeg", model="tts-1", voice="alloy"
            )
        )
        self.transcribe = AsyncMock(
            return_value=SpeechToTextResult(
                text="The value is 42.", language="en", model="whisper-1"
            )
        )

    async def resolve_model_target(self, *args, **kwargs):
        return (args[0] if args else None) or "tts-1", kwargs.get(
            "explicit_provider_id"
        ) or self.provider


@pytest.fixture
async def speech_env(tmp_path, request):
    admin = None
    schema = f"speech_{uuid.uuid4().hex}"
    if getattr(request, "param", "sqlite") == "postgresql":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("needs COGNIS_TEST_POSTGRES_URL")
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/speech.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts"),
            base_url="http://localhost:8080",
            signing_secret="speech-test-signing-key",
        )
    )
    async with factory() as db:
        for email in (OWNER, OTHER):
            await create_user(db, email=email, name=email, password_hash="test")
        db.add(
            LLMProvider(
                provider_id="speech-provider",
                display_name="Speech",
                backend="litellm",
                location="controller",
                config={"preset": "openai"},
            )
        )
        await db.commit()
    stub = SpeechStub()
    service = SpeechService(session_factory=factory, artifacts=store, llm=stub)
    try:
        yield service, stub
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


def actor(owner=OWNER):
    return ContentActor(owner, None, None, None, human=True)


async def source(service, content="# Research\n\nThe value is **42**.", **kwargs):
    deliverable_id = "dlv_" + uuid.uuid4().hex
    stored = await store_deliverable_payload(
        service.artifacts,
        deliverable_id=deliverable_id,
        content=content,
        format="markdown",
        rich_payload=None,
        outputs=None,
        owner_email=OWNER,
    )
    async with service.sessions() as db:
        row = DeliverableRow(
            deliverable_id=deliverable_id,
            owner_email=OWNER,
            content=content,
            format="markdown",
            storage_namespace=stored.namespace,
            storage_object_id=stored.object_id,
            content_key=stored.content.key,
            content_mime=stored.content.mime,
            content_size=stored.content.size,
            content_hash=stored.content.hash,
            **kwargs,
        )
        db.add(row)
        await db.commit()
    return row


async def create_job(service, deliverable, mode="podcast"):
    return await AudioJobService(service).create(
        OWNER,
        AudioJobCreate(
            deliverable_id=deliverable.deliverable_id,
            mode=mode,
            idempotency_key=uuid.uuid4().hex,
        ),
    )


async def test_long_text_and_cache_isolation(speech_env):
    service, stub = speech_env
    request = SpeechRequest(text="A sentence. " * 1000, message_id="same-message")
    first = await service.synthesize(actor(), request)
    assert len(first.parts) > 1
    assert all(len(call.args[0]) <= 3500 for call in stub.synthesize.call_args_list)
    assert not first.cached
    second = await service.synthesize(actor(), request)
    assert second.cached
    assert [p.artifact_id for p in first.parts] == [p.artifact_id for p in second.parts]
    other = await service.synthesize(actor(OTHER), request)
    assert not other.cached
    assert other.parts[0].artifact_id != first.parts[0].artifact_id
    changed = await service.synthesize(actor(), request.model_copy(update={"text": "Changed."}))
    assert not changed.cached
    for updates in ({"speed": 1.2}, {"format": "wav"}, {"voice": "nova"}, {"low_latency": True}):
        result = await service.synthesize(actor(), request.model_copy(update=updates))
        assert not result.cached
    stub.provider = "another-provider"
    assert not (await service.synthesize(actor(), request)).cached


async def test_cache_does_not_cross_agent_grants(speech_env):
    from cognis.store.queries import create_agent, create_conversation, create_session

    service, _ = speech_env
    actors = []
    async with service.sessions() as db:
        for agent_id in ("narrator-a", "narrator-b"):
            await create_agent(
                db, agent_id=agent_id, owner_email=OWNER, name=agent_id, status="active"
            )
            conversation = await create_conversation(db, OWNER, agent_id, "web")
            session = await create_session(db, conversation.conversation_id, OWNER, agent_id)
            actors.append(
                ContentActor(
                    OWNER,
                    agent_id,
                    conversation.conversation_id,
                    session.session_id,
                )
            )
        await db.commit()
    request = SpeechRequest(text="Identical text supplied independently by two agents.")
    first = await service.synthesize(actors[0], request, durable=True)
    second = await service.synthesize(actors[1], request, durable=True)
    assert not second.cached
    assert first.parts[0].artifact_id != second.parts[0].artifact_id
    await service.transcribe(actors[1], second.parts[0].artifact_id)
    with pytest.raises(PermissionError):
        await service.transcribe(actors[1], first.parts[0].artifact_id)
    again = await service.synthesize(actors[0], request, durable=True)
    assert again.cached
    assert again.parts[0].artifact_id == first.parts[0].artifact_id


async def test_preference_defaults_to_verbatim_and_opt_in_retains_script(speech_env):
    service, stub = speech_env
    request = SpeechRequest(text="**The value is 42.**", use_user_preferences=True)
    response = await service.synthesize(actor(), request)
    assert not response.adapted
    stub.generate.assert_not_awaited()
    assert stub.synthesize.call_args.args[0] == request.text
    async with service.sessions() as db:
        await upsert_user_ui_state(db, OWNER, "ui.preferences", {"chat": {"adapt_tts": True}})
        await db.commit()
    adapted = await service.synthesize(actor(), request)
    assert adapted.adapted and adapted.script_artifact_id
    assert stub.generate.call_args.kwargs["task_type"] == "audio_narrative"
    assert stub.generate.call_args.args[0][1]["content"] == request.text
    async with service.sessions() as db:
        script = await db.get(ArtifactRecordRow, adapted.script_artifact_id)
    content, _ = await service.artifacts.async_load(
        script.namespace, script.object_id, script.filename
    )
    assert content.decode() == stub.generate.return_value["choices"][0]["message"]["content"]
    assert route_chain("audio_narrative") == ("audio_narrative", "companion_lite")
    again = await service.synthesize(actor(), request)
    assert again.cached and again.script_artifact_id == adapted.script_artifact_id
    stub.generate.assert_awaited_once()


@pytest.mark.parametrize(
    "content",
    ["A grounded episode.", [{"type": "text", "text": "A grounded episode."}]],
)
async def test_script_consumes_the_real_llm_completion_contract(speech_env, content):
    service, stub = speech_env
    stub.generate.return_value = {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    }
    assert await service.script(actor(), "Source facts.", podcast=True, minutes=3) == (
        "A grounded episode."
    )


@pytest.mark.parametrize(
    ("message", "finish_reason", "code"),
    [
        ({"content": "", "reasoning_content": "Private model reasoning."}, "stop", "script_empty"),
        ({"content": "An unfinished sentence"}, "length", "script_truncated"),
        ({"content": "A blocked fragment"}, "content_filter", "script_incomplete"),
        ({"content": "A fragment"}, "tool_calls", "script_incomplete"),
        ({"content": "word " * 601}, "stop", "script_too_long"),
    ],
)
async def test_failed_scripts_never_start_speech_or_retry_automatically(
    speech_env, caplog, message, finish_reason, code
):
    service, stub = speech_env
    stub.generate.return_value = {"choices": [{"message": message, "finish_reason": finish_reason}]}
    document = await source(service)
    jobs = AudioJobService(service)
    row = await jobs.create(
        OWNER,
        AudioJobCreate(
            deliverable_id=document.deliverable_id,
            mode="podcast",
            target_minutes=3,
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    await jobs.tick()
    failed = await jobs.view(OWNER, row.job_id)
    assert failed.status == "failed"
    assert failed.error_code == code
    assert failed.script_artifact_id is None
    assert not failed.parts
    await jobs.tick()
    stub.generate.assert_awaited_once()
    stub.synthesize.assert_not_awaited()
    record = next(
        record for record in caplog.records if record.message == "Audio generation failed"
    )
    assert record.extra_data == {
        "job_id": row.job_id,
        "error_class": "AudioNarrativeError",
        "error_code": code,
    }
    assert "Private model reasoning." not in caplog.text


@pytest.mark.parametrize("mode", ["narration", "podcast"])
@pytest.mark.parametrize(
    ("category", "provider_code", "expected"),
    [
        ("other", "TypeError", "generation_internal_error"),
        ("invalid_request", "invalid_api_key", "provider_access_denied"),
        ("quota_exhausted", "quota_exceeded", "provider_quota_exhausted"),
        ("rate_limit", "rate_limit_exceeded", "provider_rate_limited"),
        ("connection", None, "provider_unavailable"),
        ("invalid_request", "voice_not_found", "provider_request_rejected"),
        ("protocol", "speech_stream_incomplete", "speech_stream_incomplete"),
        ("other", None, "generation_failed"),
    ],
)
async def test_failed_audio_persists_safe_error_categories_without_replay(
    speech_env, caplog, mode, category, provider_code, expected
):
    service, stub = speech_env
    private = "Private provider diagnostics and source content"
    stub.synthesize.side_effect = LLMStreamProviderError(
        private, payload={"category": category, "code": provider_code, "message": private}
    )
    document = await source(service)
    jobs = AudioJobService(service)
    created = await create_job(service, document, mode=mode)
    await jobs.tick()
    failed = await jobs.view(OWNER, created.job_id)
    assert failed.status == "failed" and failed.error_code == expected
    assert not failed.parts
    await jobs.tick()
    stub.synthesize.assert_awaited_once()
    assert private not in caplog.text


async def test_agent_tts_binding_is_independent_of_chat_model_and_round_trips(speech_env):
    from cognis.models.agent import AgentLLMConfig
    from cognis.store.queries import create_agent

    service, stub = speech_env
    config = AgentLLMConfig(
        model="cognis/chat",
        tts_provider_id="speech-provider",
        tts_model="gpt-4o-mini-tts",
        tts_voice="nova",
    )
    async with service.sessions() as db:
        await create_agent(
            db,
            agent_id="narrator",
            owner_email=OWNER,
            name="Narrator",
            llm_config=config.model_dump(),
            status="active",
        )
        await db.commit()
    agent_actor = ContentActor(OWNER, "narrator", None, None, human=True)
    result = await service.synthesize(
        agent_actor, SpeechRequest(text="Per-agent speech.", agent_id="narrator")
    )
    assert (result.model, result.voice) == ("gpt-4o-mini-tts", "nova")
    assert stub.synthesize.call_args.kwargs["provider_id"] == "speech-provider"
    with pytest.raises(PermissionError):
        await service.binding(actor(), None, "narrator")


@pytest.mark.parametrize("mode", ["narration", "podcast"])
@pytest.mark.parametrize("kind", ["deliverable", "artifact"])
async def test_document_audio_prefers_author_tts(speech_env, mode, kind):
    from cognis.store.queries import create_agent, create_artifact_record

    service, stub = speech_env
    author_id = uuid.uuid4().hex
    async with service.sessions() as db:
        await create_agent(
            db,
            agent_id=author_id,
            owner_email=OWNER,
            name="Author",
            llm_config={
                "tts_model": "gpt-4o-mini-tts",
                "tts_provider_id": "speech-provider",
                "tts_voice": "nova",
            },
            status="active",
        )
        await db.commit()
    if kind == "deliverable":
        document = await source(service, owner_agent_id=author_id)
        ref = document.deliverable_id
    else:
        ref = "art_" + uuid.uuid4().hex
        await service.artifacts.async_save(
            "deliverables",
            ref,
            "research.md",
            b"# Research\n\nThe value is 42.",
            content_type="text/markdown",
        )
        async with service.sessions() as db:
            await create_artifact_record(
                db,
                artifact_id=ref,
                namespace="deliverables",
                object_id=ref,
                filename="research.md",
                mime_type="text/markdown",
                size_bytes=32,
                owner_email=OWNER,
                owner_agent_id=author_id,
                status="saved",
                purpose="agent_output",
                kind="file",
            )
            await db.commit()
    jobs = AudioJobService(service)
    context = await document_audio_context(jobs, OWNER, ref, can_mutate=True)
    assert context.supported and context.can_generate
    stub.generate.assert_not_awaited()
    stub.synthesize.assert_not_awaited()
    stub.discover_voices.assert_not_awaited()
    created = await jobs.create(
        OWNER, AudioJobCreate(content_ref=ref, mode=mode, idempotency_key=uuid.uuid4().hex)
    )
    await jobs.tick()
    finished = await jobs.view(OWNER, created.job_id)
    assert finished.status == "completed"
    assert finished.content_ref == ref
    assert stub.synthesize.call_args.kwargs["voice"] == "nova"
    assert stub.synthesize.call_args.kwargs["model"] == "gpt-4o-mini-tts"
    assert stub.synthesize.call_args.kwargs["provider_id"] == "speech-provider"
    with pytest.raises(PermissionError):
        await jobs.latest(OTHER, ref)
    readonly = await document_audio_context(jobs, OWNER, ref, can_mutate=False)
    assert not readonly.can_generate
    assert readonly.jobs[0].job_id == created.job_id
    if kind == "artifact":
        async with service.sessions() as db:
            deleted = await db.get(ArtifactRecordRow, ref)
            deleted.status = "deleted"
            await db.commit()
        with pytest.raises(PermissionError):
            await jobs.view(OWNER, created.job_id)


async def test_unavailable_or_foreign_author_uses_defaults(speech_env):
    from cognis.store.queries import create_agent

    service, stub = speech_env
    author_id = uuid.uuid4().hex
    async with service.sessions() as db:
        await create_agent(
            db,
            agent_id=author_id,
            owner_email=OTHER,
            name="Private author",
            llm_config={"tts_voice": "nova", "tts_model": "private-model"},
            status="active",
        )
        await db.commit()
    document = AudioSource("dlv_" + uuid.uuid4().hex, OTHER, author_id, "plain", "Text")
    binding = await source_speech_binding(service, OWNER, document, None)
    assert (binding.model, binding.voice) == ("tts-1", "alloy")
    document.owner_email = OWNER
    binding = await source_speech_binding(service, OWNER, document, None)
    assert (binding.model, binding.voice) == ("tts-1", "alloy")
    document.agent_id = uuid.uuid4().hex
    binding = await source_speech_binding(service, OWNER, document, None)
    assert binding.voice == "alloy"
    stub.synthesize.assert_not_awaited()


@pytest.mark.parametrize("prefix", ["art_", "att_", "doc_", "img_"])
async def test_binary_artifact_capability_is_explicit_and_never_generates(speech_env, prefix):
    from cognis.store.queries import create_artifact_record

    service, stub = speech_env
    ref = prefix + uuid.uuid4().hex
    async with service.sessions() as db:
        await create_artifact_record(
            db,
            artifact_id=ref,
            namespace="documents",
            object_id=ref,
            filename="scan.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            owner_email=OWNER,
            status="saved",
            purpose="user_upload",
            kind="file",
        )
        await db.commit()
    jobs = AudioJobService(service)
    context = await document_audio_context(jobs, OWNER, ref, can_mutate=True)
    assert not context.supported and not context.can_generate
    assert "no readable text" in context.detail
    with pytest.raises(ValueError, match="no readable text"):
        await jobs.create(
            OWNER,
            AudioJobCreate(content_ref=ref, mode="narration", idempotency_key=uuid.uuid4().hex),
        )
    with pytest.raises(PermissionError):
        await document_audio_context(jobs, OTHER, ref, can_mutate=True)
    stub.synthesize.assert_not_awaited()
    stub.generate.assert_not_awaited()


async def test_unavailable_owned_author_route_falls_back_before_paid_work(speech_env):
    from cognis.store.queries import create_agent

    service, stub = speech_env
    author = uuid.uuid4().hex
    async with service.sessions() as db:
        await create_agent(
            db,
            agent_id=author,
            owner_email=OWNER,
            name="Author",
            status="active",
            llm_config={"tts_model": "removed-model", "tts_provider_id": "removed-provider"},
        )
        await db.commit()
    stub.resolve_model_target = AsyncMock(
        side_effect=[ValueError("Provider is unavailable"), ("tts-1", "speech-provider")]
    )
    result = await source_speech_binding(
        service,
        OWNER,
        AudioSource("dlv_" + uuid.uuid4().hex, OWNER, author, "plain"),
        None,
        validate_voice=False,
    )
    assert result.model == "tts-1"
    assert result.voice == "alloy"
    assert any("Using the default" in warning for warning in result.warnings)
    stub.synthesize.assert_not_awaited()


def test_audio_request_aliases_pin_only_one_exact_source():
    from pydantic import ValidationError

    ref = "dlv_" + uuid.uuid4().hex
    legacy = AudioJobCreate(deliverable_id=ref, mode="narration", idempotency_key=uuid.uuid4().hex)
    current = AudioJobCreate(content_ref=ref, mode="narration", idempotency_key=uuid.uuid4().hex)
    assert legacy.source_ref == current.source_ref == ref
    with pytest.raises(ValidationError):
        AudioJobCreate(
            deliverable_id=ref,
            content_ref="art_" + uuid.uuid4().hex,
            mode="narration",
            idempotency_key=uuid.uuid4().hex,
        )


@pytest.mark.parametrize(
    "format,content,expected",
    [
        ("plain", "<config>Read literally</config>", "<config>Read literally</config>"),
        (
            "html",
            "<head><style>hidden</style></head><p>Visible</p><script>secret()</script>",
            "Visible",
        ),
        ("markdown", "# Heading\n\n**Readable** text", "Heading\n\nReadable text"),
    ],
)
def test_audio_text_projection_is_format_aware(format, content, expected):
    document = AudioSource("dlv_" + uuid.uuid4().hex, OWNER, None, format, content)
    assert document.spoken_text() == expected


async def test_supported_voice_fallback_is_visible_and_persisted(speech_env):
    service, stub = speech_env
    result = await service.synthesize(actor(), SpeechRequest(text="Read.", voice="marin"))
    assert result.voice == "alloy" and result.warnings
    assert stub.synthesize.call_args.kwargs["voice"] == "alloy"
    document = await source(service)
    jobs = AudioJobService(service)
    job = await jobs.create(
        OWNER,
        AudioJobCreate(
            deliverable_id=document.deliverable_id,
            mode="narration",
            voice="marin",
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    await jobs.tick()
    completed = await jobs.view(OWNER, job.job_id)
    assert completed.status == "completed" and completed.warnings == result.warnings


async def test_voice_fallback_ignores_malformed_legacy_system_default(speech_env):
    from cognis.store.queries import upsert_setting

    service, _ = speech_env
    async with service.sessions() as db:
        await upsert_setting(db, "tts.default_voice", {"old": "invalid"}, "tts")
        await db.commit()
    binding = await service.binding(actor(), "unavailable-voice")
    assert binding.voice == "alloy" and binding.warnings


async def test_model_voice_catalogues_and_private_provider_authorization(speech_env):
    from cognis.providers.llm.litellm import LiteLLMProvider

    service, _ = speech_env
    provider = LiteLLMProvider(service.sessions)
    mini = await provider.discover_voices("speech-provider", model="gpt-4o-mini-tts")
    classic = await provider.discover_voices("speech-provider", model="tts-1")
    assert len(mini) == 13 and len(classic) == 9
    assert "marin" in {entry["voice_id"] for entry in mini}
    assert "marin" not in {entry["voice_id"] for entry in classic}
    assert await provider.discover_voices("speech-provider", model="unpublished-tts") == []
    async with service.sessions() as db:
        row = await db.get(LLMProvider, "speech-provider")
        row.owner_email = OTHER
        await db.commit()
    service.llm = provider
    with pytest.raises(SpeechUnavailable):
        await service.binding(actor(), None, provider_id="speech-provider", model="gpt-4o-mini-tts")


@pytest.mark.parametrize("speech_env", ["sqlite", "postgresql"], indirect=True)
@pytest.mark.parametrize("cancelled", ["owner", "waiter"])
async def test_independent_controllers_coalesce_paid_speech_and_cancellation(
    speech_env, monkeypatch, cancelled
):
    from cognis.store.coordination import DatabaseLeaseStore

    service, stub = speech_env
    replica = SpeechService(session_factory=service.sessions, artifacts=service.artifacts, llm=stub)
    entered, joined, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    acquire = DatabaseLeaseStore.acquire

    async def observe(store, *args, **kwargs):
        lease = await acquire(store, *args, **kwargs)
        if lease is None:
            joined.set()
        return lease

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return TextToSpeechResult(
            audio_bytes=b"ID3shared", content_type="audio/mpeg", model="tts-1", voice="alloy"
        )

    monkeypatch.setattr(DatabaseLeaseStore, "acquire", observe)
    stub.synthesize.side_effect = blocked
    request = SpeechRequest(text="Shared speech across controllers.")
    owner = asyncio.create_task(service.synthesize(actor(), request))
    await asyncio.wait_for(entered.wait(), 5)
    waiter = asyncio.create_task(replica.synthesize(actor(), request))
    await asyncio.wait_for(joined.wait(), 5)
    if cancelled == "owner":
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        with pytest.raises(SpeechGenerationInterrupted):
            await asyncio.wait_for(waiter, 5)
        stub.synthesize.assert_awaited_once()
        return
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    first = await owner
    replay = await replica.synthesize(actor(), request)
    assert replay.cached and replay.parts[0].artifact_id == first.parts[0].artifact_id
    stub.synthesize.assert_awaited_once()


@pytest.mark.parametrize("speech_env", ["sqlite", "postgresql"], indirect=True)
async def test_lost_generation_claim_cannot_publish_or_automatically_rebill(speech_env):
    service, stub = speech_env

    async def lose_claim(*args, **kwargs):
        async with service.sessions() as db:
            for lease in await db.scalars(select(CoordinationLeaseRow)):
                lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        return TextToSpeechResult(
            audio_bytes=b"ID3unpublished", content_type="audio/mpeg", model="tts-1", voice="alloy"
        )

    stub.synthesize.side_effect = lose_claim
    with pytest.raises(SpeechGenerationInterrupted):
        await service.synthesize(actor(), SpeechRequest(text="A billed but fenced attempt."))
    async with service.sessions() as db:
        assert list(await db.scalars(select(ArtifactRecordRow))) == []
    stub.synthesize.assert_awaited_once()


async def test_http_disconnect_keeps_accepted_part_but_stops_later_chunks(speech_env):
    service, stub = speech_env
    request = SpeechRequest(text=" ".join(f"Sentence number {index}." for index in range(500)))
    disconnected = AsyncMock(side_effect=[False, False, True])
    with pytest.raises(asyncio.CancelledError):
        await service.synthesize(actor(), request, disconnected=disconnected)
    stub.synthesize.assert_awaited_once()
    await service.synthesize(actor(), request)
    assert stub.synthesize.await_count == len(speech_chunks(request.text))


async def test_durable_chunks_bypass_message_ttl_but_not_deleted_artifacts(speech_env):
    from cognis.store.queries import upsert_setting

    service, stub = speech_env
    request = SpeechRequest(text="Durable recording.")
    first = await service.synthesize(actor(), request, durable=True)
    async with service.sessions() as db:
        await upsert_setting(db, "tts.cache_ttl_days", 0, "tts")
        await db.commit()
    assert (await service.synthesize(actor(), request, durable=True)).cached
    async with service.sessions() as db:
        row = await db.get(ArtifactRecordRow, first.parts[0].artifact_id)
        row.status = "deleted"
        await db.commit()
    rebuilt = await service.synthesize(actor(), request, durable=True)
    assert not rebuilt.cached and rebuilt.parts[0].artifact_id != first.parts[0].artifact_id
    assert stub.synthesize.await_count == 2


async def test_repeated_podcast_requests_reuse_retained_script_and_recording(speech_env):
    service, stub = speech_env
    document = await source(service)
    jobs = AudioJobService(service)
    first = await create_job(service, document)
    await jobs.tick()
    second = await create_job(service, document)
    await jobs.tick()
    original = await jobs.view(OWNER, first.job_id)
    replay = await jobs.view(OWNER, second.job_id)
    assert original.status == replay.status == "completed"
    assert original.script_artifact_id == replay.script_artifact_id
    assert original.parts[0].artifact_id == replay.parts[0].artifact_id
    stub.generate.assert_awaited_once()
    stub.synthesize.assert_awaited_once()


async def test_tools_registered_and_artifact_transcription_checks_owner(speech_env):
    service, stub = speech_env
    assert all(not audit_tool_registration(tool) for tool in speech_tools())
    output = await service.synthesize(actor(), SpeechRequest(text="Read me."), durable=True)
    artifact_id = output.parts[0].artifact_id
    result = await service.transcribe(actor(), artifact_id)
    assert result.text == "The value is 42."
    with pytest.raises(PermissionError):
        await service.transcribe(actor(OTHER), artifact_id)
    stub.transcribe.assert_awaited_once()
    invalid = await handle_speech_tool(
        "speech_synthesize", {"text": "x", "unexpected": True}, service=service, actor=actor()
    )
    assert invalid.is_error
    assert stub.synthesize.await_count == 1


async def test_podcast_job_is_revision_pinned_durable_and_idempotent(speech_env):
    service, stub = speech_env
    original = await source(service)
    jobs = AudioJobService(service)
    request = AudioJobCreate(
        deliverable_id=original.deliverable_id, mode="podcast", idempotency_key=uuid.uuid4().hex
    )
    job = await jobs.create(OWNER, request)
    assert (await jobs.create(OWNER, request)).job_id == job.job_id
    with pytest.raises(ValueError):
        await jobs.create(OWNER, request.model_copy(update={"target_minutes": 10}))
    await source(service, "This unrelated newer revision says 9000.")
    await jobs.tick()
    completed = await jobs.view(OWNER, job.job_id)
    assert completed.status == "completed"
    assert completed.script_artifact_id and completed.parts
    assert "42" in stub.generate.call_args.args[0][1]["content"]
    assert "9000" not in stub.generate.call_args.args[0][1]["content"]
    assert "narrative" in stub.generate.call_args.args[0][0]["content"]
    async with service.sessions() as db:
        for part in completed.parts:
            record = await db.get(ArtifactRecordRow, part.artifact_id)
            assert record.namespace == "artifacts"
    assert (await AudioJobService(service).latest(OWNER, original.deliverable_id))[
        0
    ].job_id == job.job_id
    with pytest.raises(PermissionError):
        await jobs.view(OTHER, job.job_id)
    async with service.sessions() as db:
        row = await db.get(DeliverableRow, original.deliverable_id)
        row.status = "deleted"
        await db.commit()
    with pytest.raises(PermissionError):
        await jobs.view(OWNER, job.job_id)


async def test_job_cancel_fences_late_synthesis_and_does_not_restart(speech_env):
    from cognis.store.queries import upsert_setting

    service, stub = speech_env
    deliverable = await source(service)
    jobs = AudioJobService(service)
    job = await create_job(service, deliverable, "narration")
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return TextToSpeechResult(
            audio_bytes=b"ID3late", content_type="audio/mpeg", model="tts-1", voice="alloy"
        )

    stub.synthesize.side_effect = blocked
    worker = asyncio.create_task(jobs.tick())
    await asyncio.wait_for(entered.wait(), timeout=5)
    async with service.sessions() as db:
        await upsert_setting(db, "tts.enabled", False, "tts", updated_by=OWNER)
        await db.commit()
    context = await document_audio_context(jobs, OWNER, deliverable.deliverable_id, can_mutate=True)
    assert not context.can_generate and context.can_cancel
    readonly = await document_audio_context(
        jobs, OWNER, deliverable.deliverable_id, can_mutate=False
    )
    assert not readonly.can_cancel
    assert (await jobs.cancel(OWNER, job.job_id)).status == "cancelled"
    release.set()
    await worker
    await jobs.tick()
    state = await jobs.view(OWNER, job.job_id)
    assert state.status == "cancelled" and state.completed_parts == 0
    assert stub.synthesize.await_count == 1


async def test_lost_worker_becomes_interrupted_without_paid_replay(speech_env):
    service, stub = speech_env
    deliverable = await source(service)
    job = await create_job(service, deliverable)
    async with service.sessions() as db:
        row = await db.get(AudioJobRow, job.job_id)
        row.status = "synthesizing"
        row.updated_at = datetime.now(UTC) - timedelta(minutes=11)
        await db.commit()
    await AudioJobService(service).tick()
    assert (await AudioJobService(service).view(OWNER, job.job_id)).status == "interrupted"
    stub.generate.assert_not_awaited()
    stub.synthesize.assert_not_awaited()


async def test_two_workers_claim_only_one_attempt(speech_env):
    service, stub = speech_env
    deliverable = await source(service)
    job = await create_job(service, deliverable, "narration")
    await asyncio.gather(AudioJobService(service).tick(), AudioJobService(service).tick())
    assert (await AudioJobService(service).view(OWNER, job.job_id)).status == "completed"
    stub.synthesize.assert_awaited_once()


def test_deterministic_narration_and_chunk_boundaries():
    spoken = narration_text("# Title\n\nSee [source](https://example.com).\n\n```py\nsecret()\n```")
    assert "Title" in spoken and "source" in spoken
    assert "secret()" not in spoken
    assert "https://" not in spoken
    raw = "First sentence. " * 1000
    chunks = speech_chunks(raw)
    assert all(len(part) <= 3500 for part in chunks)
    assert " ".join(chunks) == raw.strip()


async def test_executor_elevenlabs_transcription_returns_json_contract(monkeypatch):
    from cognis.executor.backends.litellm import LiteLLMExecutorBackend

    monkeypatch.setattr(
        "cognis.executor.audio_preprocessing.prepare_audio_for_stt",
        AsyncMock(return_value=(b"audio", "audio/wav", "recording.wav")),
    )
    native = AsyncMock(
        return_value=SpeechToTextResult(text="The value is 42.", language="en", model="scribe_v1")
    )
    monkeypatch.setattr("cognis.providers.llm.elevenlabs.transcribe", native)
    result = await LiteLLMExecutorBackend().transcribe(
        audio_bytes=b"audio",
        mime_type="audio/wav",
        filename="recording.wav",
        model="scribe_v1",
        provider_preset="elevenlabs",
        request_kwargs={"api_key": "test-key"},
    )
    assert isinstance(result, dict)
    assert SpeechToTextResult.model_validate(result).text == "The value is 42."
    native.assert_awaited_once()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_audio_schema_migration_and_repeated_bootstrap(tmp_path, monkeypatch, backend):
    url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
    if backend == "postgresql" and not url:
        pytest.skip("needs COGNIS_TEST_POSTGRES_URL")
    schema = f"speech_{uuid.uuid4().hex}"
    admin = None
    if backend == "postgresql":
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/migration.db")
    migration = importlib.import_module("cognis.store.migrations.versions.171_audio_jobs")
    warnings_migration = importlib.import_module(
        "cognis.store.migrations.versions.173_audio_voice_warnings"
    )
    plan_migration = importlib.import_module(
        "cognis.store.migrations.versions.175_audio_length_plans"
    )

    def exercise(conn):
        Base.metadata.create_all(
            conn,
            tables=[table for table in Base.metadata.sorted_tables if table.name != "audio_jobs"],
        )
        assert "audio_jobs" not in inspect(conn).get_table_names()
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        migration.upgrade()
        monkeypatch.setattr(warnings_migration, "op", Operations(MigrationContext.configure(conn)))
        warnings_migration.upgrade()
        warnings_migration.upgrade()
        monkeypatch.setattr(plan_migration, "op", Operations(MigrationContext.configure(conn)))
        plan_migration.upgrade()
        plan_migration.upgrade()
        assert "audio_jobs" in inspect(conn).get_table_names()
        assert {column["name"] for column in inspect(conn).get_columns("audio_jobs")} == {
            column.name for column in AudioJobRow.__table__.columns
        }
        constraints = inspect(conn).get_unique_constraints("audio_jobs")
        assert any(
            set(item["column_names"]) == {"owner_email", "idempotency_key"} for item in constraints
        )
        migration.downgrade()
        assert "audio_jobs" not in inspect(conn).get_table_names()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(exercise)
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.begin() as conn:
            assert await conn.run_sync(lambda c: inspect(c).has_table("audio_jobs"))
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def test_elevenlabs_native_wire_contract_and_limits(monkeypatch):
    from cognis.providers.llm import elevenlabs
    from cognis.providers.llm.hosted import discover_hosted_models

    calls = []

    async def respond(request):
        calls.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json=[
                    {
                        "model_id": "eleven_multilingual_v2",
                        "name": "Multilingual",
                        "can_do_text_to_speech": True,
                    }
                ],
            )
        if request.url.path == "/v1/voices":
            return httpx.Response(
                200, json={"voices": [{"voice_id": "voice-one", "name": "Narrator"}]}
            )
        if request.url.path == "/v1/speech-to-text":
            return httpx.Response(200, json={"text": "Transcript", "language_code": "en"})
        return httpx.Response(200, content=b"ID3test")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        elevenlabs.httpx,
        "AsyncClient",
        lambda **kw: real_client(**kw, transport=httpx.MockTransport(respond)),
    )
    config = {"api_key": "test-key", "api_base": "https://speech.example"}
    async with elevenlabs.httpx.AsyncClient() as client:
        models = await discover_hosted_models(
            client, preset="elevenlabs", base_url=config["api_base"], api_key="test-key"
        )
    assert models[0]["supports_speech_synthesis"]
    assert any(model.get("supports_transcription") for model in models)
    voices = await elevenlabs.discover_voices(base_url=config["api_base"], api_key="test-key")
    assert voices == [{"voice_id": "voice-one", "name": "Narrator"}]
    audio = await elevenlabs.synthesize(
        text="Hello",
        model="elevenlabs/eleven_multilingual_v2",
        voice="voice-one",
        response_format="mp3",
        speed=1,
        request_kwargs=config,
    )
    assert audio.audio_bytes == b"ID3test"
    assert calls[-1].headers["xi-api-key"] == "test-key"
    assert b"eleven_multilingual_v2" in calls[-1].content
    transcript = await elevenlabs.transcribe(
        audio_bytes=b"data",
        mime_type="audio/wav",
        filename="sample.wav",
        model="elevenlabs/scribe_v1",
        language="en",
        prompt=None,
        request_kwargs=config,
    )
    assert transcript.text == "Transcript"
    with pytest.raises(ValueError):
        await elevenlabs.synthesize(
            text="Hello",
            model="eleven_multilingual_v2",
            voice="voice-one",
            response_format="opus",
            speed=1,
            request_kwargs=config,
        )
