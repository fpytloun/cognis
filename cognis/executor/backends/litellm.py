"""LiteLLM executor inference backend."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import litellm

from cognis.executor.inference_types import CognisInferenceRequest, json_safe_inference_payload
from cognis.logging import get_logger
from cognis.providers.llm.errors import build_mid_stream_error_chunk
from cognis.providers.llm.responses_bridge import (
    messages_to_responses_input,
    response_model_dump,
    responses_request_kwargs,
    responses_stream_to_chat_chunks,
    responses_to_chat_response,
)

logger = get_logger(__name__)


def _apply_executor_responses_defaults(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    result = dict(request_kwargs)
    result.setdefault("store", False)
    return result


def _supports_image_response_format(model: str) -> bool:
    normalized = model.rsplit("/", 1)[-1].lower()
    return not normalized.startswith("gpt-image-")


class LiteLLMExecutorBackend:
    """Proxy LLM requests from the controller through LiteLLM.

    The controller resolves provider configuration, credentials, and model
    routing. The executor simply runs the same LiteLLM call remotely so the
    network origin is the executor host instead of the controller.
    """

    async def close(self) -> None:
        """Close any background resources.

        The current implementation uses ``litellm`` directly and has nothing
        persistent to close, but the method keeps the shutdown path uniform.
        """

    name = "litellm"

    async def stream_complete(
        self,
        request: CognisInferenceRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a completion through LiteLLM using controller-provided args."""
        model = request.model
        messages = request.messages
        request_kwargs = dict(request.request_kwargs)
        llm_api = str(request_kwargs.pop("cognis_llm_api", "chat_completions"))
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        response_status = "completed"
        index = 0

        try:
            if llm_api == "responses":
                request_kwargs = _apply_executor_responses_defaults(
                    responses_request_kwargs(request_kwargs)
                )
                stream = await litellm.aresponses(
                    model=model,
                    input=messages_to_responses_input(messages),
                    stream=True,
                    **request_kwargs,
                )
                async for chunk in responses_stream_to_chat_chunks(stream):
                    payload_raw = json_safe_inference_payload(chunk)
                    payload = payload_raw if isinstance(payload_raw, dict) else {}
                    if payload.get("mid_stream_failure") or payload.get("error"):
                        yield {
                            "done": True,
                            "error": str(payload.get("error") or "Responses stream failed"),
                            "response_error": payload.get("response_error"),
                            "finish_reason": "error",
                        }
                        return
                    if payload.get("usage"):
                        usage = payload["usage"]
                    if payload.get("response_status"):
                        response_status = str(payload["response_status"])
                    # Structured stream fields beyond the flat text/tool
                    # whitelist. Dropping these made executor-routed Responses
                    # providers diverge from controller-direct behavior:
                    # multi-block thinking collapsed into one block (no
                    # boundary markers), Responses-native replay was silently
                    # unavailable (no raw output items), apply_patch progress
                    # was invisible, and liveness markers never reached the
                    # controller's idle-timeout policy.
                    chunk_extras: dict[str, Any] = {}
                    output_item = payload.get("responses_output_item")
                    if isinstance(output_item, dict):
                        chunk_extras["responses_output_item"] = output_item
                    provider_event_type = payload.get("provider_event_type")
                    if isinstance(provider_event_type, str) and provider_event_type:
                        chunk_extras["provider_event_type"] = provider_event_type
                    response_item_id = payload.get("response_item_id")
                    if isinstance(response_item_id, str) and response_item_id:
                        chunk_extras["response_item_id"] = response_item_id
                    content_source = payload.get("content_source")
                    if isinstance(content_source, str) and content_source:
                        chunk_extras["content_source"] = content_source
                    response_message_phase = payload.get("response_message_phase")
                    if isinstance(response_message_phase, str | int):
                        chunk_extras["response_message_phase"] = response_message_phase
                    choices = payload.get("choices") or []
                    delta: dict[str, Any] = {}
                    if choices:
                        choice = choices[0]
                        raw_delta = choice.get("delta")
                        delta = raw_delta if isinstance(raw_delta, dict) else {}
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    delta_extras: dict[str, Any] = {}
                    boundary = delta.get("reasoning_part_boundary")
                    if isinstance(boundary, dict):
                        delta_extras["reasoning_part_boundary"] = boundary
                    tool_progress = delta.get("tool_progress")
                    if isinstance(tool_progress, dict):
                        delta_extras["tool_progress"] = tool_progress
                    content = delta.get("content")
                    tool_calls = delta.get("tool_calls")
                    reasoning_content = delta.get("reasoning_content")
                    reasoning = delta.get("reasoning")
                    refusal = delta.get("refusal")
                    if (
                        content is None
                        and tool_calls is None
                        and reasoning_content is None
                        and reasoning is None
                        and refusal is None
                        and not delta_extras
                        and not chunk_extras
                    ):
                        continue
                    yield {
                        "content": content,
                        "tool_calls": tool_calls,
                        "reasoning_content": reasoning_content,
                        "reasoning": reasoning,
                        "refusal": refusal,
                        "index": index,
                        **delta_extras,
                        **chunk_extras,
                    }
                    index += 1
            else:
                stream = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    stream=True,
                    **request_kwargs,
                )
                async for chunk in stream:
                    payload_raw = json_safe_inference_payload(chunk)
                    payload = payload_raw if isinstance(payload_raw, dict) else {}
                    if payload.get("usage"):
                        usage = payload["usage"]
                    choices = payload.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    content = delta.get("content")
                    tool_calls = delta.get("tool_calls")
                    reasoning_content = delta.get("reasoning_content")
                    reasoning = delta.get("reasoning")
                    refusal = delta.get("refusal")
                    if (
                        content is None
                        and tool_calls is None
                        and reasoning_content is None
                        and reasoning is None
                        and refusal is None
                    ):
                        continue
                    yield {
                        "content": content,
                        "tool_calls": tool_calls,
                        "reasoning_content": reasoning_content,
                        "reasoning": reasoning,
                        "refusal": refusal,
                        "index": index,
                    }
                    index += 1
        except Exception as exc:
            error_chunk = build_mid_stream_error_chunk(exc)
            yield {
                "done": True,
                "error": f"Inference error: {str(error_chunk.get('error') or exc)[:500]}",
                "response_error": error_chunk.get("response_error"),
                "finish_reason": "error",
            }
            return

        yield {
            "done": True,
            "usage": usage,
            "finish_reason": finish_reason,
            "response_status": response_status,
        }

    async def generate(self, request: CognisInferenceRequest) -> dict[str, Any]:
        """Run a non-streaming completion through LiteLLM."""
        model = request.model
        messages = request.messages
        request_kwargs = dict(request.request_kwargs)
        llm_api = str(request_kwargs.pop("cognis_llm_api", "chat_completions"))
        if llm_api == "responses":
            response = await litellm.aresponses(
                model=model,
                input=messages_to_responses_input(messages),
                stream=False,
                **_apply_executor_responses_defaults(responses_request_kwargs(request_kwargs)),
            )
            return responses_to_chat_response(response_model_dump(response))
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            stream=False,
            **request_kwargs,
        )
        dumped = response.model_dump()
        return dumped if isinstance(dumped, dict) else {}

    async def image_generate(
        self,
        *,
        prompt: str,
        model: str,
        strategy: str = "aimage_generation",
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        response_format: str = "b64_json",
        image: str | None = None,
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate an image through LiteLLM on the executor side."""
        gen_kwargs: dict[str, Any] = {}
        if request_kwargs.get("api_key"):
            gen_kwargs["api_key"] = request_kwargs["api_key"]
        if request_kwargs.get("api_base"):
            gen_kwargs["api_base"] = request_kwargs["api_base"]

        if strategy == "acompletion_modalities":
            content: list[dict[str, Any]] | str
            if image is not None:
                content = [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                    {"type": "text", "text": prompt},
                ]
            else:
                content = prompt
            messages = [{"role": "user", "content": content}]
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                modalities=["image", "text"],
                stream=False,
                n=n,
                **gen_kwargs,
            )
            dumped = json_safe_inference_payload(response)
            return dumped if isinstance(dumped, dict) else {}

        image_kwargs: dict[str, Any] = {}
        if _supports_image_response_format(model):
            image_kwargs["response_format"] = response_format
        response = await litellm.aimage_generation(
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            **image_kwargs,
            **gen_kwargs,
        )
        dumped = response.model_dump()
        return dumped if isinstance(dumped, dict) else {}

    async def transcribe(
        self,
        *,
        audio_bytes: bytes,
        mime_type: str,
        filename: str,
        model: str,
        provider_preset: str | None = None,
        supported_audio_mime_types: Sequence[str] | None = None,
        request_kwargs: dict[str, Any],
        prompt: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        from cognis.audio.preprocessing import prepare_audio_for_stt as _prepare_audio_for_stt

        audio_bytes, mime_type, filename = await _prepare_audio_for_stt(
            audio_bytes,
            mime_type=mime_type,
            filename=filename,
            supported_mime_types=[str(item) for item in supported_audio_mime_types or []],
        )
        api_base = request_kwargs.get("api_base") or request_kwargs.get("base_url")
        if not isinstance(api_base, str) or not api_base:
            api_base = "https://api.openai.com"
        headers: dict[str, str] = {}
        api_key = request_kwargs.get("api_key")
        if isinstance(api_key, str) and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        extra_headers = request_kwargs.get("extra_headers")
        if isinstance(extra_headers, dict):
            headers.update({str(key): str(value) for key, value in extra_headers.items()})

        wire_model = _transcription_wire_model(model, provider_preset or "")
        logger.debug(
            "executor inference: speech-to-text request prepared",
            extra={
                "extra_data": {
                    "resolved_model": model,
                    "wire_model": wire_model,
                    "provider_preset": provider_preset,
                }
            },
        )
        data: dict[str, str] = {"model": wire_model}
        if prompt:
            data["prompt"] = prompt
        if language:
            data["language"] = language

        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = filename
        async with httpx.AsyncClient(timeout=request_kwargs.get("timeout", 120)) as client:
            try:
                response = await client.post(
                    f"{api_base.rstrip('/')}/v1/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files={"file": (filename, file_obj, mime_type)},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(_sanitize_http_error_detail(exc)) from exc
        payload = response.json()
        return {
            "text": payload.get("text", ""),
            "model": model,
            "language": payload.get("language"),
            "duration_seconds": payload.get("duration"),
        }

    async def synthesize(
        self,
        *,
        text: str,
        voice: str,
        model: str,
        provider_preset: str | None = None,
        response_format: str = "mp3",
        speed: float = 1.0,
        request_kwargs: dict[str, Any],
        low_latency: bool = False,
    ) -> dict[str, Any]:
        """Run text-to-speech via LiteLLM on the executor side."""
        from cognis.providers.llm.litellm import _run_synthesize_local

        wire_model = _transcription_wire_model(model, provider_preset or "")
        if low_latency:
            configured_timeout = request_kwargs.get("timeout", 120)
            request_kwargs = dict(request_kwargs)
            request_kwargs["timeout"] = (
                min(configured_timeout, 20) if isinstance(configured_timeout, int | float) else 20
            )
        result = await _run_synthesize_local(
            text=text,
            voice=voice,
            wire_model=wire_model,
            response_format=response_format,
            speed=speed,
            request_kwargs=dict(request_kwargs),
            resolved_model=model,
            provider_preset=provider_preset or "",
            prefer_direct_http=low_latency,
        )
        return {
            "audio_hex": result.audio_bytes.hex(),
            "audio_encoding": "hex",
            "content_type": result.content_type,
            "model": result.model,
            "voice": result.voice,
            "duration_seconds": result.duration_seconds,
        }


def _transcription_wire_model(model: str, provider_preset: str) -> str:
    if "/" not in model:
        return model
    if provider_preset == "litellm_proxy":
        return model
    if provider_preset in {"openai", "openai_compatible"}:
        return model.split("/", 1)[1]
    return model


def _sanitize_http_error_detail(error: httpx.HTTPStatusError) -> str:
    detail = str(error)
    try:
        payload = error.response.json()
    except Exception:
        return detail
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            if isinstance(message, str) and message:
                return f"{detail}; provider_error={message[:250]}"
    return detail
