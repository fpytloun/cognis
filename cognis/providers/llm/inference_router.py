"""Route LLM inference through matching remote executors."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from cognis.core.executor_resolution import labels_match
from cognis.models.config import ImageGenerationResult, SpeechToTextResult
from cognis.ownership import is_shared_owner_email
from cognis.providers.executor.websocket import ExecutorDisconnectedError, WebSocketExecutorProvider


class InferenceRouter:
    """Proxy provider calls through a selected executor."""

    def __init__(self, ws_provider: WebSocketExecutorProvider) -> None:
        self._ws_provider = ws_provider

    async def route_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        executor_labels: dict[str, str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        conn = await self._find_executor(executor_labels)
        if conn is None:
            yield {"error": "No executor matches the provider selector", "mid_stream_failure": True}
            return

        try:
            async for chunk in conn.llm_complete_stream(
                request_id=uuid.uuid4().hex,
                messages=messages,
                model=model,
                request_kwargs=request_kwargs or {},
            ):
                if chunk.get("error"):
                    yield {"error": chunk["error"], "mid_stream_failure": True}
                    return
                if chunk.get("done"):
                    yield {
                        "choices": [
                            {"delta": {}, "finish_reason": chunk.get("finish_reason", "stop")}
                        ],
                        "usage": chunk.get("usage", {}),
                        "response_status": chunk.get("response_status", "completed"),
                    }
                    return
                yield {
                    "choices": [
                        {
                            "delta": {
                                "content": chunk.get("content"),
                                "tool_calls": chunk.get("tool_calls"),
                                "reasoning_content": chunk.get("reasoning_content"),
                                "reasoning": chunk.get("reasoning"),
                                "refusal": chunk.get("refusal"),
                            }
                        }
                    ]
                }
        except ExecutorDisconnectedError:
            yield {"error": "Executor disconnected during inference", "mid_stream_failure": True}

    async def route_generate(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        executor_labels: dict[str, str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_summary_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_calls: list[Any] = []
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        response_status = "completed"
        async for chunk in self.route_stream(
            messages=messages,
            model=model,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
        ):
            if chunk.get("mid_stream_failure"):
                raise RuntimeError(chunk.get("error", "Inference failed"))
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content") is not None:
                    content_parts.append(_coerce_text_field(delta.get("content")))
                if delta.get("reasoning_content") is not None:
                    reasoning_parts.append(_coerce_text_field(delta.get("reasoning_content")))
                if delta.get("reasoning") is not None:
                    reasoning_summary_parts.append(_coerce_text_field(delta.get("reasoning")))
                if delta.get("refusal") is not None:
                    refusal_parts.append(_coerce_text_field(delta.get("refusal")))
                if delta.get("tool_calls"):
                    tool_calls.extend(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("response_status"):
                response_status = str(chunk["response_status"])
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts) or None,
                        "tool_calls": tool_calls or None,
                        "reasoning_content": "".join(reasoning_parts) or None,
                        "reasoning": "".join(reasoning_summary_parts) or None,
                        "refusal": "".join(refusal_parts) or None,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
            "response_status": response_status,
        }

    async def route_image_generate(
        self,
        *,
        prompt: str,
        model: str,
        strategy: str = "aimage_generation",
        executor_labels: dict[str, str] | None = None,
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        response_format: str = "b64_json",
        image: str | None = None,
        request_kwargs: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        """Route image generation through a matching executor."""
        conn = await self._find_executor(executor_labels)
        if conn is None:
            raise RuntimeError("No executor matches the provider selector for image generation")

        try:
            result = await conn.rpc_call(
                method="llm.image_generate",
                params={
                    "request_id": uuid.uuid4().hex,
                    "prompt": prompt,
                    "model": model,
                    "strategy": strategy,
                    "n": n,
                    "size": size,
                    "quality": quality,
                    "response_format": response_format,
                    "image": image,
                    "request_kwargs": request_kwargs or {},
                },
            )
            return ImageGenerationResult.model_validate(result)
        except ExecutorDisconnectedError:
            raise RuntimeError("Executor disconnected during image generation") from None

    async def route_transcribe(
        self,
        *,
        audio_bytes: bytes,
        mime_type: str,
        filename: str,
        model: str,
        provider_preset: str | None = None,
        executor_labels: dict[str, str] | None = None,
        supported_audio_mime_types: list[str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
        prompt: str | None = None,
        language: str | None = None,
    ) -> SpeechToTextResult:
        conn = await self._find_executor(executor_labels)
        if conn is None:
            raise RuntimeError("No executor matches the provider selector for speech-to-text")

        try:
            result = await conn.rpc_call(
                method="llm.transcribe",
                params={
                    "request_id": uuid.uuid4().hex,
                    "audio_base64": audio_bytes.hex(),
                    "audio_encoding": "hex",
                    "mime_type": mime_type,
                    "filename": filename,
                    "model": model,
                    "provider_preset": provider_preset,
                    "supported_audio_mime_types": supported_audio_mime_types,
                    "prompt": prompt,
                    "language": language,
                    "request_kwargs": request_kwargs or {},
                },
            )
            return SpeechToTextResult.model_validate(result)
        except ExecutorDisconnectedError:
            raise RuntimeError("Executor disconnected during speech-to-text") from None

    async def _find_executor(self, executor_labels: dict[str, str] | None) -> Any | None:
        active = await self._ws_provider.list_active()
        for handle in active:
            metadata = handle.metadata or {}
            if not bool(metadata.get("shared")) and not is_shared_owner_email(metadata.get("owner_email")):
                continue
            labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
            if executor_labels and not labels_match(labels, executor_labels):
                continue
            try:
                return await self._ws_provider.get_executor(handle)
            except Exception:
                continue
        return None


def _coerce_text_field(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        import json

        try:
            return json.dumps(value, ensure_ascii=True, sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)
