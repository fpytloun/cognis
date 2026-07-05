"""Route LLM inference through matching remote executors."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from cognis.core.executor_resolution import labels_match
from cognis.json_stream import merge_incremental_json_fragment
from cognis.models.config import ImageGenerationResult, SpeechToTextResult, TextToSpeechResult
from cognis.ownership import is_shared_owner_email
from cognis.providers.executor.websocket import ExecutorDisconnectedError, WebSocketExecutorProvider


class InferenceRouter:
    """Proxy provider calls through a selected executor."""

    def __init__(self, ws_provider: WebSocketExecutorProvider) -> None:
        self._ws_provider = ws_provider
        self.last_backend_metadata: dict[str, Any] | None = None

    async def route_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        executor_id: str | None = None,
        executor_labels: dict[str, str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
        backend: str | None = None,
        provider_id: str | None = None,
        owner_email: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        conn = await self._find_executor(executor_id, executor_labels)
        self.last_backend_metadata = None
        if conn is None:
            yield {
                "error": "No executor matches the provider selector",
                "mid_stream_failure": True,
            }
            return

        try:
            async for chunk in conn.llm_complete_stream(
                request_id=uuid.uuid4().hex,
                messages=messages,
                model=model,
                request_kwargs=request_kwargs or {},
                backend=backend,
                provider_id=provider_id,
                owner_email=owner_email,
            ):
                if chunk.get("error"):
                    error_chunk = {
                        "error": chunk["error"],
                        "mid_stream_failure": True,
                    }
                    response_error = chunk.get("response_error")
                    if isinstance(response_error, dict):
                        error_chunk["response_error"] = response_error
                    yield error_chunk
                    return
                if chunk.get("done"):
                    metadata = chunk.get("backend_metadata")
                    self.last_backend_metadata = metadata if isinstance(metadata, dict) else None
                    yield {
                        "choices": [
                            {"delta": {}, "finish_reason": chunk.get("finish_reason", "stop")}
                        ],
                        "usage": chunk.get("usage", {}),
                        "response_status": chunk.get("response_status", "completed"),
                    }
                    return
                delta: dict[str, Any] = {
                    "content": chunk.get("content"),
                    "tool_calls": chunk.get("tool_calls"),
                    "reasoning_content": chunk.get("reasoning_content"),
                    "reasoning": chunk.get("reasoning"),
                    "refusal": chunk.get("refusal"),
                }
                # Reconstruct structured stream fields the executor forwards:
                # thinking block boundaries (multi-block thinking), apply_patch
                # input progress, raw Responses output items (native replay),
                # and provider liveness markers (idle-timeout policy).
                boundary = chunk.get("reasoning_part_boundary")
                if isinstance(boundary, dict):
                    delta["reasoning_part_boundary"] = boundary
                tool_progress = chunk.get("tool_progress")
                if isinstance(tool_progress, dict):
                    delta["tool_progress"] = tool_progress
                out_chunk: dict[str, Any] = {"choices": [{"delta": delta}]}
                output_item = chunk.get("responses_output_item")
                if isinstance(output_item, dict):
                    out_chunk["responses_output_item"] = output_item
                provider_event_type = chunk.get("provider_event_type")
                if isinstance(provider_event_type, str) and provider_event_type:
                    out_chunk["provider_event_type"] = provider_event_type
                response_item_id = chunk.get("response_item_id")
                if isinstance(response_item_id, str) and response_item_id:
                    out_chunk["response_item_id"] = response_item_id
                content_source = chunk.get("content_source")
                if isinstance(content_source, str) and content_source:
                    out_chunk["content_source"] = content_source
                response_message_phase = chunk.get("response_message_phase")
                if isinstance(response_message_phase, str | int):
                    out_chunk["response_message_phase"] = response_message_phase
                yield out_chunk
        except ExecutorDisconnectedError:
            yield {"error": "Executor disconnected during inference", "mid_stream_failure": True}

    async def route_generate(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        executor_id: str | None = None,
        executor_labels: dict[str, str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
        backend: str | None = None,
        provider_id: str | None = None,
        owner_email: str | None = None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_summary_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        response_status = "completed"
        backend_metadata: dict[str, Any] | None = None
        async for chunk in self.route_stream(
            messages=messages,
            model=model,
            executor_id=executor_id,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
            backend=backend,
            provider_id=provider_id,
            owner_email=owner_email,
        ):
            if chunk.get("mid_stream_failure"):
                from cognis.providers.llm.errors import (
                    LLMStreamProviderError,
                    MidStreamErrorCategory,
                )

                details = chunk.get("response_error")
                if not isinstance(details, dict):
                    details = {
                        "category": MidStreamErrorCategory.OTHER.value,
                        "message": str(chunk.get("error") or "Inference failed"),
                    }
                raise LLMStreamProviderError(
                    str(chunk.get("error") or details.get("message") or "Inference failed"),
                    payload=details,
                )
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
                # Streamed tool calls arrive as index-keyed fragments (a
                # name-only fragment plus N argument fragments). Merge by
                # index so the final message carries one complete call per
                # tool instead of a list of partial fragments.
                for tool_delta in delta.get("tool_calls") or []:
                    if not isinstance(tool_delta, dict):
                        continue
                    index = int(tool_delta.get("index") or 0)
                    entry = tool_calls.setdefault(
                        index,
                        {
                            "id": str(tool_delta.get("id") or f"call_{index}"),
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if tool_delta.get("id"):
                        entry["id"] = str(tool_delta["id"])
                    function_delta = tool_delta.get("function")
                    if not isinstance(function_delta, dict):
                        continue
                    function = entry["function"]
                    if function_delta.get("name"):
                        function["name"] = str(function_delta["name"])
                    if function_delta.get("arguments"):
                        merged = merge_incremental_json_fragment(
                            str(function["arguments"]),
                            str(function_delta["arguments"]),
                        )
                        function["arguments"] = merged.merged
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("response_status"):
                response_status = str(chunk["response_status"])
            metadata = chunk.get("backend_metadata")
            if isinstance(metadata, dict):
                backend_metadata = metadata
        self.last_backend_metadata = backend_metadata
        normalized_tool_calls = [
            tool_call
            for _index, tool_call in sorted(tool_calls.items())
            if (tool_call.get("function") or {}).get("name")
        ]
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts) or None,
                        "tool_calls": normalized_tool_calls or None,
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
        executor_id: str | None = None,
        executor_labels: dict[str, str] | None = None,
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        response_format: str = "b64_json",
        image: str | None = None,
        request_kwargs: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        """Route image generation through a matching executor."""
        conn = await self._find_executor(executor_id, executor_labels)
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
        executor_id: str | None = None,
        executor_labels: dict[str, str] | None = None,
        supported_audio_mime_types: list[str] | None = None,
        request_kwargs: dict[str, Any] | None = None,
        prompt: str | None = None,
        language: str | None = None,
    ) -> SpeechToTextResult:
        conn = await self._find_executor(executor_id, executor_labels)
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

    async def route_synthesize(
        self,
        *,
        text: str,
        voice: str,
        model: str,
        provider_preset: str | None = None,
        executor_id: str | None = None,
        executor_labels: dict[str, str] | None = None,
        response_format: str = "mp3",
        speed: float = 1.0,
        request_kwargs: dict[str, Any] | None = None,
        low_latency: bool = False,
    ) -> TextToSpeechResult:
        conn = await self._find_executor(executor_id, executor_labels)
        if conn is None:
            raise RuntimeError("No executor matches the provider selector for text-to-speech")

        try:
            result = await conn.rpc_call(
                method="llm.synthesize",
                params={
                    "request_id": uuid.uuid4().hex,
                    "text": text,
                    "voice": voice,
                    "model": model,
                    "provider_preset": provider_preset,
                    "response_format": response_format,
                    "speed": speed,
                    "request_kwargs": request_kwargs or {},
                    "low_latency": low_latency,
                },
            )
        except ExecutorDisconnectedError:
            raise RuntimeError("Executor disconnected during text-to-speech") from None

        encoded = result.get("audio_hex") or result.get("audio_base64")
        encoding = result.get("audio_encoding", "hex")
        if not isinstance(encoded, str):
            raise RuntimeError("Text-to-speech executor returned no audio payload")
        if encoding != "hex":
            raise RuntimeError(f"Text-to-speech executor used unsupported encoding {encoding!r}")
        audio_bytes = bytes.fromhex(encoded)
        return TextToSpeechResult(
            audio_bytes=audio_bytes,
            content_type=str(result.get("content_type", "audio/mpeg")),
            model=str(result.get("model", model)),
            voice=str(result.get("voice", voice)),
            duration_seconds=(
                float(result["duration_seconds"])
                if isinstance(result.get("duration_seconds"), int | float)
                else None
            ),
        )

    async def _find_executor(
        self, executor_id: str | None, executor_labels: dict[str, str] | None
    ) -> Any | None:
        active = await self._ws_provider.list_active()
        for handle in active:
            if executor_id and handle.executor_id != executor_id:
                continue
            metadata = handle.metadata or {}
            if not bool(metadata.get("shared")) and not is_shared_owner_email(
                metadata.get("owner_email")
            ):
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
