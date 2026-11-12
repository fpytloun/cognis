"""HTTP adaptation for the shared speech service."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import aclosing

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from cognis.api.common import (
    api_exception,
    check_agent_access,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.core.agent_registry import AgentRegistry
from cognis.core.artifact_revisions import ContentActor
from cognis.core.speech import SpeechService, SpeechUnavailable
from cognis.core.speech_cache import SpeechGenerationInterrupted
from cognis.core.speech_models import SpeechCapabilities, SpeechVoices
from cognis.core.speech_models import (
    SpeechRequest as TtsSynthesizeRequest,
)
from cognis.core.speech_models import (
    SpeechResponse as TtsSynthesizeResponse,
)
from cognis.logging import get_logger
from cognis.store.queries import get_agent

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/tts", tags=["tts"])


@router.get("/capabilities", response_model=SpeechCapabilities)
async def speech_capabilities(request: Request, agent_id: str | None = None) -> SpeechCapabilities:
    """Check both required routes before requesting microphone access."""
    user = require_current_user(request)
    actor = await speech_actor(request, agent_id)
    ready = {}
    streaming_tts = streaming_stt = False
    sample_rate = 24_000
    for role in ("text_to_speech", "speech_to_text"):
        try:
            if role == "text_to_speech":
                binding = await speech_service(request).binding(
                    actor, None, agent_id, validate_voice=False
                )
                model, provider = binding.model, binding.provider
                streaming_tts = binding.streaming
            else:
                model, provider = await request.app.state.providers.llm.resolve_model_target(
                    None, task_type=role, acting_user_email=user.email
                )
                _, _, native = await speech_service(request).transcription_binding(actor)
                streaming_stt, sample_rate = native.stt, native.sample_rate
            ready[role] = bool(model and provider)
        except Exception:
            ready[role] = False
    tts = ready["text_to_speech"]
    stt = ready["speech_to_text"]
    return SpeechCapabilities(
        tts=tts,
        stt=stt,
        streaming_tts=streaming_tts,
        streaming_stt=streaming_stt,
        stt_sample_rate=sample_rate,
        detail=None
        if tts and stt
        else "Configure agent TTS or text_to_speech in Settings → Models, and enable speech_to_text.",
    )


def speech_service(request: Request) -> SpeechService:
    """Use the current provider facade, including test/runtime replacement."""
    return SpeechService(
        session_factory=request.app.state.session_factory,
        artifacts=request.app.state.artifact_store,
        llm=request.app.state.providers.llm,
    )


async def speech_actor(request: Request, agent_id: str | None) -> ContentActor:
    """Authorize the agent before speech policy reads its configuration."""
    user = require_current_user(request)
    if agent_id:
        registry = AgentRegistry(request.app.state.session_factory)
        if registry.get_system_agent(agent_id) is not None:
            system_agent = await registry.get_effective(agent_id, owner_email=user.email)
            if system_agent is None:
                raise api_exception(404, "not_found", "Agent not found")
            return ContentActor(user.email, agent_id, None, None, human=True)
        async with request.app.state.session_factory() as session:
            agent = await get_agent(session, agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent, required="view")
    return ContentActor(user.email, agent_id, None, None, human=True)


@router.get("/voices", response_model=SpeechVoices)
async def speech_voices(
    request: Request,
    provider_id: str | None = None,
    model: str | None = None,
) -> SpeechVoices:
    """Discover only the provider selected by this user's authorized TTS route."""
    user = require_current_user(request)
    service = speech_service(request)
    try:
        binding = await service.binding(
            ContentActor(user.email, None, None, None, human=True),
            None,
            provider_id=provider_id,
            model=model,
        )
        voices = list(binding.voices) or await service.llm.discover_voices(
            binding.provider, model=binding.model
        )
        return SpeechVoices(
            provider_id=binding.provider,
            model=binding.model,
            default_voice=binding.voice,
            voices=voices,
        )
    except Exception as exc:
        raise api_exception(
            503, "voices_unavailable", "Voice discovery is unavailable; check your TTS provider."
        ) from exc


@router.post("/synthesize", response_model=TtsSynthesizeResponse)
async def synthesize_tts(request: Request, payload: TtsSynthesizeRequest) -> TtsSynthesizeResponse:
    """Synthesize supplied text without trusting caller-supplied source identifiers."""
    forbid_mutation_for_viewer(request)
    actor = await speech_actor(request, payload.agent_id)
    try:
        return await speech_service(request).synthesize(
            actor, payload, disconnected=request.is_disconnected
        )
    except PermissionError as exc:
        raise api_exception(404, "not_found", "Speech resource not found") from exc
    except SpeechGenerationInterrupted as exc:
        raise api_exception(409, "speech_generation_interrupted", str(exc)) from exc
    except SpeechUnavailable as exc:
        raise api_exception(503, "tts_unconfigured", str(exc)) from exc
    except ValueError as exc:
        code = "tts_disabled" if str(exc) == "TTS is disabled" else "validation_error"
        raise api_exception(503 if code == "tts_disabled" else 400, code, str(exc)) from exc
    except Exception as exc:
        logger.warning("Speech synthesis failed", extra={"error_class": type(exc).__name__})
        raise api_exception(
            502, "tts_failed", "Speech synthesis failed; check the speech route."
        ) from exc


@router.post("/stream")
async def stream_tts(request: Request, payload: TtsSynthesizeRequest) -> StreamingResponse:
    """Stream disposable PCM previews and only completed artifact receipts."""
    forbid_mutation_for_viewer(request)
    actor = await speech_actor(request, payload.agent_id)
    service = speech_service(request)

    async def body() -> AsyncIterator[str]:
        try:
            async with aclosing(service.synthesis_events(actor, payload)) as events:
                async for event in events:
                    yield json.dumps(event, separators=(",", ":")) + "\n"
        except Exception as exc:
            logger.warning("Speech stream failed", extra={"error_class": type(exc).__name__})
            yield json.dumps({"type": "error", "code": "speech_stream_failed"}) + "\n"

    return StreamingResponse(
        body(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
