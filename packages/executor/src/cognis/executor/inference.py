"""Executor-side inference routing and media helpers."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any

import httpx
import litellm

from cognis.executor.backends.registry import ExecutorBackendRegistry, resolve_backend_name
from cognis.executor.backends.tts import run_synthesize_local
from cognis.executor.inference_types import CognisInferenceRequest, image_generation_result_payload
from cognis.executor.providers.llm.ollama import discover_ollama_models
from cognis.executor.usage_observer import ExecutorUsageObserver, executor_observer
from cognis.logging import get_logger
from cognis.models.config import ImageInput
from cognis.providers.llm.bedrock import discover_bedrock_models
from cognis.providers.llm.errors import build_mid_stream_error_chunk
from cognis.providers.llm.hosted import (
    PROVIDER_BASES,
    discover_hosted_models,
    is_openrouter_catalog,
)
from cognis.providers.llm.image_api import image_api_kwargs
from cognis.providers.llm.usage_binding import UsageBinding, observed_sdk
from cognis.providers.llm.usage_http import observed_http

logger = get_logger(__name__)


class InferenceHandler:
    """Route executor-side LLM requests through transport backends.

    The controller resolves provider configuration, credentials, and model
    routing. The executor simply runs the same LiteLLM call remotely so the
    network origin is the executor host instead of the controller.
    """

    def __init__(self, registry: ExecutorBackendRegistry | None = None) -> None:
        self._registry = registry or ExecutorBackendRegistry()
        self.usage_context_gaps = 0

    async def close(self) -> None:
        """Close any backend background resources."""
        await self._registry.close()

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        request_kwargs: dict[str, Any],
        request_id: str | None = None,
        backend: str | None = None,
        provider_id: str | None = None,
        owner_email: str | None = None,
        backend_metadata: dict[str, Any] | None = None,
        usage_observer: ExecutorUsageObserver | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        request_kwargs = dict(request_kwargs)
        observer = usage_observer
        try:
            request = CognisInferenceRequest(
                request_id=request_id,
                model=model,
                messages=messages,
                request_kwargs=request_kwargs,
                backend=resolve_backend_name(request_kwargs, backend),
                provider_id=provider_id,
                owner_email=owner_email,
                backend_metadata=backend_metadata or {},
            )
            selected = self._registry.select(request)
            observer = observer or executor_observer(backend_metadata)
            if observer is None:
                self.usage_context_gaps += 1
            else:
                request = replace(request, usage_binding=observer.binding)
            async for chunk in selected.stream_complete(request):
                if observer is not None:
                    chunk = observer.attach(chunk)
                yield chunk
        except Exception as exc:
            error_chunk = build_mid_stream_error_chunk(exc)
            result = {
                "done": True,
                "error": f"Inference error: {str(error_chunk.get('error') or exc)[:500]}",
                "response_error": error_chunk.get("response_error"),
                "finish_reason": "error",
            }
            yield observer.attach(result) if observer is not None else result
            return

    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        request_kwargs: dict[str, Any],
        request_id: str | None = None,
        backend: str | None = None,
        provider_id: str | None = None,
        owner_email: str | None = None,
        backend_metadata: dict[str, Any] | None = None,
        usage_observer: ExecutorUsageObserver | None = None,
    ) -> dict[str, Any]:
        request_kwargs = dict(request_kwargs)
        request = CognisInferenceRequest(
            request_id=request_id,
            model=model,
            messages=messages,
            request_kwargs=request_kwargs,
            backend=resolve_backend_name(request_kwargs, backend),
            stream=False,
            provider_id=provider_id,
            owner_email=owner_email,
            backend_metadata=backend_metadata or {},
        )
        selected = self._registry.select(request)
        observer = usage_observer or executor_observer(backend_metadata)
        if observer is None:
            self.usage_context_gaps += 1
        else:
            request = replace(request, usage_binding=observer.binding)
        result = await selected.generate(request)
        return observer.attach(result) if observer is not None else result

    async def discover_models(
        self,
        *,
        preset: str,
        base_url: str,
        api_key: str = "",
        aws_config: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover read-only catalogs from the configured runtime location."""
        if preset == "bedrock":
            config = aws_config or {}
            return await discover_bedrock_models(
                region=config.get("aws_region_name"), profile=config.get("aws_profile_name")
            )
        if preset in PROVIDER_BASES or is_openrouter_catalog(preset, base_url):
            async with httpx.AsyncClient(timeout=15) as client:
                return await discover_hosted_models(
                    client,
                    preset=preset if preset in PROVIDER_BASES else "openrouter",
                    base_url=base_url,
                    api_key=api_key,
                )
        if preset.strip().lower() != "ollama":
            raise ValueError("Executor-side model discovery unsupported for this preset")
        return await discover_ollama_models(base_url=base_url, api_key=api_key, logger=logger)

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
        images: list[ImageInput] | None = None,
        mask: ImageInput | None = None,
        request_kwargs: dict[str, Any],
        usage_binding: UsageBinding | None = None,
    ) -> dict[str, Any]:
        """Generate an image through LiteLLM on the executor side."""
        gen_kwargs: dict[str, Any] = {
            "timeout": request_kwargs.get("timeout", 120),
            "num_retries": 0,
            "max_retries": 0,
        }
        if request_kwargs.get("api_key"):
            gen_kwargs["api_key"] = request_kwargs["api_key"]
        if request_kwargs.get("api_base"):
            gen_kwargs["api_base"] = request_kwargs["api_base"]

        if strategy == "acompletion_modalities":
            content: list[dict[str, Any]] | str
            if images is not None:
                content = [
                    *[
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.content_type};base64,{image.b64_json}"
                            },
                        }
                        for image in images
                    ],
                    {"type": "text", "text": prompt},
                ]
            else:
                content = prompt
            if mask is not None:
                raise ValueError(
                    "Masked image editing is not supported by Gemini image generation."
                )
            messages = [{"role": "user", "content": content}]
            response = await observed_sdk(litellm.acompletion, usage_binding, operation="chat")(
                model=model,
                messages=messages,
                modalities=["image", "text"],
                stream=False,
                n=n,
                **gen_kwargs,
            )
            return image_generation_result_payload(response, model)

        image_kwargs = image_api_kwargs(
            model, response_format=response_format, images=images, mask=mask
        )
        response = await observed_sdk(
            litellm.aimage_edit if images is not None else litellm.aimage_generation,
            usage_binding,
            operation="native",
        )(
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            **image_kwargs,
            **gen_kwargs,
        )
        return image_generation_result_payload(response, model)

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
        usage_binding: UsageBinding | None = None,
    ) -> dict[str, Any]:
        from cognis.executor.audio_preprocessing import (
            prepare_audio_for_stt as _prepare_audio_for_stt,
        )

        audio_bytes, mime_type, filename = await _prepare_audio_for_stt(
            audio_bytes,
            mime_type=mime_type,
            filename=filename,
            supported_mime_types=[str(item) for item in supported_audio_mime_types or []],
        )
        if provider_preset == "elevenlabs":
            from cognis.providers.llm.elevenlabs import transcribe

            result = await transcribe(
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                filename=filename,
                model=model,
                language=language,
                prompt=prompt,
                request_kwargs=request_kwargs,
                usage_binding=usage_binding,
            )
            return result.model_dump()
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
        if wire_model.rsplit("/", 1)[-1] == "whisper-1":
            data["response_format"] = "verbose_json"
        if prompt:
            data["prompt"] = prompt
        if language:
            data["language"] = language

        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = filename
        async with httpx.AsyncClient(timeout=request_kwargs.get("timeout", 120)) as client:
            try:
                response = await observed_http(
                    lambda: client.post(
                        f"{api_base.rstrip('/')}/v1/audio/transcriptions",
                        headers=headers,
                        data=data,
                        files={"file": (filename, file_obj, mime_type)},
                    ),
                    binding=usage_binding,
                    parameters={},
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
        usage_binding: UsageBinding | None = None,
    ) -> dict[str, Any]:
        """Run text-to-speech via LiteLLM on the executor side."""
        wire_model = _transcription_wire_model(model, provider_preset or "")
        if low_latency:
            configured_timeout = request_kwargs.get("timeout", 120)
            request_kwargs = dict(request_kwargs)
            request_kwargs["timeout"] = (
                min(configured_timeout, 20) if isinstance(configured_timeout, int | float) else 20
            )
        result = await run_synthesize_local(
            text=text,
            voice=voice,
            wire_model=wire_model,
            response_format=response_format,
            speed=speed,
            request_kwargs=dict(request_kwargs),
            resolved_model=model,
            provider_preset=provider_preset or "",
            prefer_direct_http=low_latency,
            usage_binding=usage_binding,
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
