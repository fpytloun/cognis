"""Route LLM inference through matching remote executors."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy import or_, select

from cognis.core.executor_resolution import labels_match
from cognis.json_stream import merge_incremental_json_fragment
from cognis.models.config import ImageGenerationResult, SpeechToTextResult, TextToSpeechResult
from cognis.models.executor_inference import executor_local_inference_routable
from cognis.ownership import SYSTEM_USER_EMAIL, is_shared_owner_email
from cognis.providers.executor.websocket import ExecutorDisconnectedError, WebSocketExecutorProvider
from cognis.providers.llm.ollama import ollama_model_name
from cognis.store.models import (
    ExecutorRow,
    LocalModelDeployment,
    LocalModelTargetStatus,
    User,
)


class LocalModelRolloutUnavailableError(RuntimeError):
    """A managed model has no ready executor target."""

    def __init__(self, summary: dict[str, Any]) -> None:
        super().__init__("Managed local model rollout has no ready executor")
        self.summary = summary


def _provider_safe_kwargs(request_kwargs: dict[str, Any] | None) -> dict[str, Any]:
    """Remove controller-only executor affinity metadata before provider RPC."""
    return {
        key: value
        for key, value in (request_kwargs or {}).items()
        if key
        not in {
            "executor_id",
            "executor_labels",
            "cognis_executor_affinity",
            "_cognis_executor_affinity_id",
        }
    }


def _provider_safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (metadata or {}).items()
        if key
        not in {
            "executor_id",
            "executor_labels",
            "cognis_executor_affinity",
            "_cognis_executor_affinity_id",
        }
    }


class InferenceRouter:
    """Proxy provider calls through a selected executor."""

    def __init__(
        self,
        ws_provider: WebSocketExecutorProvider,
        session_factory: Any | None = None,
    ) -> None:
        self._ws_provider = ws_provider
        self._session_factory = session_factory
        self.last_backend_metadata: dict[str, Any] | None = None

    async def discover_models(
        self,
        *,
        preset: str,
        base_url: str,
        api_key: str = "",
        executor_id: str | None = None,
        executor_labels: dict[str, str] | None = None,
        provider_id: str | None = None,
        owner_email: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run read-only model discovery from a selected executor."""

        if not executor_id and not executor_labels:
            raise RuntimeError("No executor selector was provided for executor-routed discovery")
        conn = await self._find_executor(
            executor_id,
            executor_labels,
            readiness_aware=False,
        )
        if conn is None:
            if executor_id:
                raise RuntimeError(
                    f"No active executor matches executor_id {executor_id!r}; "
                    "ensure the executor is connected, ready, and visible to this provider"
                )
            if executor_labels:
                raise RuntimeError(
                    f"No active executor matches executor_labels {executor_labels!r}; "
                    "ensure a connected executor has matching labels and is visible to this provider"
                )
            raise RuntimeError("No executor selector was provided for executor-routed discovery")
        return cast(
            list[dict[str, Any]],
            await conn.llm_discover_models(
                preset=preset,
                base_url=base_url,
                api_key=api_key,
                provider_id=provider_id,
                owner_email=owner_email,
            ),
        )

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
        backend_metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        affinity = (request_kwargs or {}).get("_cognis_executor_affinity_id")
        if isinstance(affinity, str) and affinity.strip():
            pinned_id = affinity.strip()
            if executor_id and executor_id != pinned_id:
                yield {
                    "error": "Frozen executor affinity conflicts with configured executor_id",
                    "mid_stream_failure": True,
                }
                return
            executor_id = pinned_id
        request_kwargs = _provider_safe_kwargs(request_kwargs)
        backend_metadata = _provider_safe_metadata(backend_metadata)
        try:
            selection = await self._find_executor_selection(
                executor_id,
                executor_labels,
                model=model,
                provider_id=provider_id,
                owner_email=owner_email,
            )
            conn = selection[0] if selection is not None else None
            handle = selection[1] if selection is not None else None
        except LocalModelRolloutUnavailableError as exc:
            yield {
                "error": str(exc),
                "mid_stream_failure": True,
                "response_error": {
                    "category": "local_model_rollout_unavailable",
                    "message": str(exc),
                    "rollout": exc.summary,
                },
            }
            return
        self.last_backend_metadata = None
        if conn is None:
            yield {
                "error": "No executor matches the provider selector",
                "mid_stream_failure": True,
            }
            return
        assert handle is not None
        if isinstance(affinity, str) and affinity.strip():
            if handle.executor_id != affinity.strip():
                yield {
                    "error": "Resolved executor does not match frozen executor affinity",
                    "mid_stream_failure": True,
                }
                return
            if executor_labels:
                metadata = handle.metadata if isinstance(handle.metadata, dict) else {}
                labels = metadata.get("labels")
                if not isinstance(labels, dict) or not labels_match(labels, executor_labels):
                    yield {
                        "error": "Frozen executor fails configured executor_labels",
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
                backend_metadata=backend_metadata or {},
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
                    final_chunk = {
                        "choices": [
                            {"delta": {}, "finish_reason": chunk.get("finish_reason", "stop")}
                        ],
                        "usage": chunk.get("usage", {}),
                        "response_status": chunk.get("response_status", "completed"),
                        "anthropic_native_envelope": (
                            metadata.get("anthropic_native_envelope")
                            if isinstance(metadata, dict)
                            else None
                        ),
                    }
                    performance = (
                        metadata.get("performance") if isinstance(metadata, dict) else None
                    )
                    if isinstance(performance, dict):
                        performance = dict(performance)
                        if not performance.get("executor_id"):
                            performance["executor_id"] = handle.executor_id
                        handle_metadata = handle.metadata or {}
                        executor_name = (
                            handle_metadata.get("display_name")
                            or handle_metadata.get("name")
                            or handle_metadata.get("hostname")
                        )
                        if (
                            isinstance(executor_name, str)
                            and executor_name
                            and not performance.get("executor_name")
                        ):
                            performance["executor_name"] = executor_name
                        final_chunk["performance"] = performance
                    yield final_chunk
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
                thinking_blocks = chunk.get("provider_thinking_blocks")
                if isinstance(thinking_blocks, list):
                    delta["provider_thinking_blocks"] = thinking_blocks
                out_chunk: dict[str, Any] = {"choices": [{"delta": delta}]}
                native_events = chunk.get("anthropic_native_events")
                if isinstance(native_events, list):
                    # Native Anthropic events are continuation metadata, not
                    # compatibility text.  Preserve them unchanged for the
                    # controller-side native accumulator.
                    out_chunk["anthropic_native_events"] = native_events
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
        backend_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_summary_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        response_status = "completed"
        response_backend_metadata: dict[str, Any] | None = None
        anthropic_native_envelope: dict[str, Any] | None = None
        async for chunk in self.route_stream(
            messages=messages,
            model=model,
            executor_id=executor_id,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
            backend=backend,
            provider_id=provider_id,
            owner_email=owner_email,
            backend_metadata=backend_metadata,
        ):
            if chunk.get("mid_stream_failure"):
                from cognis.providers.llm.errors import (
                    LLMStreamProviderError,
                    MidStreamErrorCategory,
                    MidStreamErrorPayload,
                )

                details = chunk.get("response_error")
                if not isinstance(details, dict):
                    details = {
                        "category": MidStreamErrorCategory.OTHER.value,
                        "message": str(chunk.get("error") or "Inference failed"),
                    }
                raise LLMStreamProviderError(
                    str(chunk.get("error") or details.get("message") or "Inference failed"),
                    payload=cast(MidStreamErrorPayload, details),
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
            envelope = chunk.get("anthropic_native_envelope")
            if isinstance(envelope, dict):
                anthropic_native_envelope = envelope
            metadata = chunk.get("backend_metadata")
            if isinstance(metadata, dict):
                response_backend_metadata = metadata
        self.last_backend_metadata = response_backend_metadata
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
            **(
                {"anthropic_native_envelope": anthropic_native_envelope}
                if anthropic_native_envelope is not None
                else {}
            ),
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
        images: list[dict[str, str]] | None = None,
        mask: dict[str, str] | None = None,
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
                    "images": images,
                    "mask": mask,
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
        self,
        executor_id: str | None,
        executor_labels: dict[str, str] | None,
        *,
        model: str | None = None,
        provider_id: str | None = None,
        owner_email: str | None = None,
        readiness_aware: bool = True,
    ) -> Any | None:
        selection = await self._find_executor_selection(
            executor_id,
            executor_labels,
            model=model,
            provider_id=provider_id,
            owner_email=owner_email,
            readiness_aware=readiness_aware,
        )
        return selection[0] if selection is not None else None

    async def _find_executor_selection(
        self,
        executor_id: str | None,
        executor_labels: dict[str, str] | None,
        *,
        model: str | None = None,
        provider_id: str | None = None,
        owner_email: str | None = None,
        readiness_aware: bool = True,
    ) -> tuple[Any, Any] | None:
        ready_executor_ids: set[str] | None = None
        rollout_summary: dict[str, Any] | None = None
        if readiness_aware and model is not None and provider_id is not None:
            readiness = await self._managed_readiness(
                provider_id=provider_id,
                model=model,
                owner_email=owner_email,
            )
            if readiness is not None:
                ready_executor_ids, rollout_summary = readiness
        active = await self._ws_provider.list_active()
        persisted_executors: dict[str, ExecutorRow] | None = None
        if self._session_factory is not None:
            async with self._session_factory() as session:
                persisted_executors = {
                    row.executor_id: row
                    for row in (
                        await session.execute(
                            select(ExecutorRow).where(ExecutorRow.status == "active")
                        )
                    )
                    .scalars()
                    .all()
                }
        for handle in active:
            if executor_id and handle.executor_id != executor_id:
                continue
            if ready_executor_ids is not None and handle.executor_id not in ready_executor_ids:
                continue
            persisted = (
                persisted_executors.get(handle.executor_id)
                if persisted_executors is not None
                else None
            )
            if persisted_executors is not None and persisted is None:
                continue
            live_capabilities = getattr(handle, "capabilities", None)
            advertised = getattr(live_capabilities, "local_inference", None) is True
            if persisted is not None:
                if not executor_local_inference_routable(persisted, advertised=advertised):
                    continue
            elif not advertised:
                continue
            metadata = handle.metadata or {}
            if (
                ready_executor_ids is None
                and not bool(metadata.get("shared"))
                and not is_shared_owner_email(metadata.get("owner_email"))
            ):
                continue
            labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
            if executor_labels and not labels_match(labels, executor_labels):
                continue
            try:
                return await self._ws_provider.get_executor(handle), handle
            except Exception:
                continue
        if rollout_summary is not None:
            summary = dict(rollout_summary)
            summary["selector"] = {
                "executor_id": executor_id,
                "executor_labels": executor_labels or {},
            }
            summary["reason"] = (
                "selector_has_no_ready_target" if ready_executor_ids else "no_ready_target"
            )
            summary["action"] = {
                "type": "open_executor_settings",
                "path": "/settings?tab=executors",
                "label": "Review executor local inference settings",
            }
            raise LocalModelRolloutUnavailableError(summary)
        return None

    async def _managed_readiness(
        self,
        *,
        provider_id: str,
        model: str,
        owner_email: str | None,
    ) -> tuple[set[str], dict[str, Any] | None] | None:
        """Return ready exact targets, or None for unmanaged/override routing."""

        if self._session_factory is None:
            return None
        runtime_name = ollama_model_name(model)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(LocalModelDeployment, LocalModelTargetStatus)
                    .join(
                        LocalModelTargetStatus,
                        LocalModelTargetStatus.deployment_id == LocalModelDeployment.deployment_id,
                    )
                    .where(
                        LocalModelDeployment.provider_id == provider_id,
                        (
                            LocalModelDeployment.owner_email == SYSTEM_USER_EMAIL
                            if owner_email is None
                            else or_(
                                LocalModelDeployment.owner_email == owner_email,
                                LocalModelDeployment.owner_email == SYSTEM_USER_EMAIL,
                            )
                        ),
                        LocalModelDeployment.runtime_name == runtime_name,
                        LocalModelDeployment.desired_state == "present",
                    )
                    .order_by(
                        LocalModelDeployment.deployment_id.asc(),
                        LocalModelTargetStatus.executor_id.asc(),
                    )
                )
            ).all()
            executor_ids = {target.executor_id for _deployment, target in rows}
            executors = {
                executor.executor_id: executor
                for executor in (
                    await session.execute(
                        select(ExecutorRow).where(
                            ExecutorRow.executor_id.in_(executor_ids),
                            ExecutorRow.status == "active",
                        )
                    )
                )
                .scalars()
                .all()
            }
            actor = await session.get(User, owner_email) if owner_email is not None else None
        if not rows:
            return None
        authorized_rows = [
            (deployment, target)
            for deployment, target in rows
            if (
                (executor := executors.get(target.executor_id)) is not None
                and executor_local_inference_routable(executor)
                and (
                    (
                        is_shared_owner_email(deployment.owner_email)
                        and is_shared_owner_email(executor.owner_email)
                    )
                    or (
                        deployment.owner_email == owner_email
                        and (
                            executor.owner_email == owner_email
                            or (
                                actor is not None
                                and actor.role == "admin"
                                and is_shared_owner_email(executor.owner_email)
                            )
                        )
                    )
                )
            )
        ]
        if not authorized_rows:
            unauthorized_deployment_ids = {deployment.deployment_id for deployment, _target in rows}
            return set(), {
                "provider_id": provider_id,
                "model": runtime_name,
                "deployment_ids": sorted(unauthorized_deployment_ids),
                "total_targets": len(rows),
                "state_counts": {"unauthorized": len(rows)},
                "ready_executor_ids": [],
                "targets": [],
            }
        rows = authorized_rows
        deployments = {deployment.deployment_id: deployment for deployment, _target in rows}
        overridden_executor_ids = {
            target.executor_id
            for deployment, target in rows
            if deployment.capacity_override_acknowledged
        }
        state_counts: dict[str, int] = {}
        ready_executor_ids: set[str] = set()
        targets: list[dict[str, Any]] = []
        for deployment, target in rows:
            state_counts[target.state] = state_counts.get(target.state, 0) + 1
            ready = (
                target.state == "ready"
                and target.generation == deployment.generation
                and target.observed_generation == deployment.generation
            )
            if ready:
                ready_executor_ids.add(target.executor_id)
            targets.append(
                {
                    "deployment_id": deployment.deployment_id,
                    "executor_id": target.executor_id,
                    "generation": deployment.generation,
                    "observed_generation": target.observed_generation,
                    "state": target.state,
                    "ready": ready,
                }
            )
        if overridden_executor_ids:
            return ready_executor_ids | overridden_executor_ids, None
        summary = {
            "provider_id": provider_id,
            "model": runtime_name,
            "deployment_ids": sorted(deployments),
            "total_targets": len(targets),
            "state_counts": state_counts,
            "ready_executor_ids": sorted(ready_executor_ids),
            "targets": targets[:100],
        }
        return ready_executor_ids, summary


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
