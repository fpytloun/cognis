"""Executor-side LiteLLM proxy for remote inference routing."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

import httpx
import litellm

from cognis.logging import get_logger
from cognis.providers.llm.responses_bridge import (
    messages_to_responses_input,
    responses_request_kwargs,
    responses_stream_to_chat_chunks,
    responses_to_chat_response,
)

logger = get_logger(__name__)


def _supports_image_response_format(model: str) -> bool:
    normalized = model.rsplit("/", 1)[-1].lower()
    return normalized != "gpt-image-1"


class InferenceHandler:
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

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        request_kwargs: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a completion through LiteLLM using controller-provided args."""
        request_kwargs = dict(request_kwargs)
        llm_api = str(request_kwargs.pop("cognis_llm_api", "chat_completions"))
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        response_status = "completed"
        index = 0

        try:
            if llm_api == "responses":
                request_kwargs = responses_request_kwargs(request_kwargs)
                stream = await litellm.aresponses(
                    model=model,
                    input=messages_to_responses_input(messages),
                    stream=True,
                    **request_kwargs,
                )
                async for chunk in responses_stream_to_chat_chunks(stream):
                    payload = dict(chunk)
                    if payload.get("mid_stream_failure") or payload.get("error"):
                        yield {
                            "done": True,
                            "error": str(payload.get("error") or "Responses stream failed"),
                            "finish_reason": "error",
                        }
                        return
                    if payload.get("usage"):
                        usage = payload["usage"]
                    if payload.get("response_status"):
                        response_status = str(payload["response_status"])
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
            else:
                stream = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    stream=True,
                    **request_kwargs,
                )
                async for chunk in stream:
                    payload = dict(chunk)
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
            yield {
                "done": True,
                "error": f"Inference error: {str(exc)[:500]}",
                "finish_reason": "error",
            }
            return

        yield {
            "done": True,
            "usage": usage,
            "finish_reason": finish_reason,
            "response_status": response_status,
        }

    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a non-streaming completion through LiteLLM."""
        request_kwargs = dict(request_kwargs)
        llm_api = str(request_kwargs.pop("cognis_llm_api", "chat_completions"))
        if llm_api == "responses":
            response = await litellm.aresponses(
                model=model,
                input=messages_to_responses_input(messages),
                stream=False,
                **responses_request_kwargs(request_kwargs),
            )
            return responses_to_chat_response(response.model_dump())
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
            dumped = response.model_dump()
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
        request_kwargs: dict[str, Any],
        prompt: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
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
