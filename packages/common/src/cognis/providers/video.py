"""Portable native video HTTP adapters with no automatic paid-call retries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import Field

from cognis.models.video import (
    VideoCapabilities,
    VideoContract,
    VideoOutput,
    VideoRemoteResult,
    VideoRequest,
)
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_capture import UsageAttempt
from cognis.providers.video_errors import INTERACTIONS_ERROR_CODES, redact_interactions_message
from cognis.providers.video_media import mp4_stream

_JSON_RESPONSE_LIMIT = 4 * 1024 * 1024
_OMNI_JSON_RESPONSE_LIMIT = 32 * 1024 * 1024
_BASE64_CHUNK_SIZE = 256 * 1024  # Encoded characters, aligned to complete quartets.

# Only documented googleapis.com ErrorInfo reasons, never arbitrary provider text.
# https://github.com/googleapis/googleapis/blob/master/google/api/error_reason.proto
_GOOGLE_ERROR_REASONS = frozenset(
    {
        "API_KEY_INVALID",
        "API_KEY_SERVICE_BLOCKED",
        "API_KEY_HTTP_REFERRER_BLOCKED",
        "API_KEY_IP_ADDRESS_BLOCKED",
        "API_KEY_ANDROID_APP_BLOCKED",
        "API_KEY_IOS_APP_BLOCKED",
        "SERVICE_DISABLED",
        "BILLING_DISABLED",
        "RATE_LIMIT_EXCEEDED",
        "RESOURCE_QUOTA_EXCEEDED",
        "USER_PROJECT_DENIED",
        "CONSUMER_SUSPENDED",
        "CONSUMER_INVALID",
        "LOCATION_TAX_POLICY_VIOLATED",
    }
)
# Exact request-schema paths only; field names can also reflect private input.
_OMNI_ERROR_FIELDS = frozenset(
    {
        "model",
        "input",
        "store",
        "stream",
        "previous_interaction_id",
        "response_format",
        "response_format.type",
        "response_format.delivery",
        "response_format.resolution",
        "response_format.aspect_ratio",
    }
    | {
        f"input[{index}].{field}"
        for index in range(4)  # At most three references followed by one text part.
        for field in ("type", "data", "mime_type", "text")
    }
)


def _google_error_diagnostics(content: bytearray, adapter: str) -> str:
    """Return bounded schema tokens, not messages, metadata or unknown fields."""
    try:
        body = json.loads(content)
    except (ValueError, UnicodeError, RecursionError):
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    interactions = (
        f":code={code}"
        if adapter == "omni" and isinstance(code, str) and code in INTERACTIONS_ERROR_CODES
        else ""
    )
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return interactions
    reasons: set[str] = set()
    fields: set[str] = set()
    for detail in details[:16]:
        if not isinstance(detail, dict):
            continue
        if (
            detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
            and detail.get("domain") == "googleapis.com"
        ):
            reason = detail.get("reason")
            if isinstance(reason, str) and reason in _GOOGLE_ERROR_REASONS:
                reasons.add(reason)
        if adapter == "omni" and detail.get("@type") == "type.googleapis.com/google.rpc.BadRequest":
            violations = detail.get("fieldViolations")
            if not isinstance(violations, list):
                continue
            for violation in violations[:16]:
                field = violation.get("field") if isinstance(violation, dict) else None
                if isinstance(field, str) and field in _OMNI_ERROR_FIELDS:
                    fields.add(field)
    return (
        interactions
        + "".join(f":reason={reason}" for reason in sorted(reasons)[:2])
        + "".join(f":field={field}" for field in sorted(fields)[:3])
    )


class VideoProviderError(Exception):
    """Sanitized provider error: raw bodies and credential-bearing URLs stay private."""

    def __init__(
        self,
        code: str,
        *,
        rejected: bool = False,
        retry_after: int = 30,
        provider_error_message: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.rejected = rejected
        self.retry_after = retry_after
        self.provider_error_message = provider_error_message
        self.status_code = status_code


class VideoInput(VideoContract):
    role: str
    url: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    # Only supplied by the controller from a prior job's persisted output binding.
    provider_locator: str | None = None
    interaction_id: str | None = None


class VideoConnection(VideoContract):
    adapter: str
    model: str
    api_key: str = Field(repr=False)
    base_url: str


def omni_interaction_handle(connection: VideoConnection, locator: str) -> str | None:
    """Recognize only a canonical, private Omni inline-collection locator."""
    if connection.adapter != "omni":
        return None
    prefix = connection.base_url.rstrip("/") + "/interactions/"
    if not locator.startswith(prefix):
        return None
    encoded = locator[len(prefix) :]
    handle = None
    try:
        value = unquote(encoded, errors="strict")
        if value and quote(value, safe="") == encoded:
            handle = value
    except (ValueError, UnicodeError):
        pass
    if handle is None:
        raise VideoProviderError("invalid_inline_locator")
    return handle


def video_adapter_name(preset: str, model: str) -> str:
    """Select only documented native transports, not chat-compatible guesses."""
    if preset == "openrouter":
        return "openrouter"
    if preset == "gemini":
        if model in {"veo-3.1-generate-preview", "veo-3.1-fast-generate-preview"}:
            return "veo"
        if model == "gemini-omni-1.1-flash":
            return "omni"
    raise ValueError("No certified video transport for this provider/model")


def native_video_capabilities(adapter: str) -> VideoCapabilities:
    """Known executable controls; live account/model metadata may further restrict."""
    if adapter == "veo":
        return VideoCapabilities(
            operations=["generate", "extend"],
            input_roles=["first_frame", "last_frame", "reference"],
            durations=[4, 6, 8],
            resolutions=["720p", "1080p", "4k"],
            aspect_ratios=["16:9", "9:16"],
            max_references=3,
            seed=True,
            negative_prompt=True,
            person_generation=True,
            source_policy="same_provider_output",
            extension_output="combined",
            notes=[
                "Native audio is always generated.",
                "Extension requires a retained Veo output.",
            ],
        )
    if adapter == "omni":
        return VideoCapabilities(
            operations=["generate", "edit", "extend"],
            input_roles=["reference"],
            resolutions=["360p", "720p", "1080p", "4k"],
            aspect_ratios=["16:9", "9:16"],
            max_references=3,
            source_policy="same_provider_output",
            provider_error_messages=True,
            notes=[
                "Edits/extensions use stored interactions from this account.",
                "Uploaded-video editing is region-restricted and is not enabled.",
                "Duration and audio are prompt-controlled, not guaranteed numeric controls.",
            ],
        )
    return VideoCapabilities()


def validate_video_combination(adapter: str, request: VideoRequest) -> None:
    """Validate coupled controls beyond independent capability fields."""
    if adapter == "veo":
        duration = request.duration or 8
        if (
            request.operation == "extend"
            or any(ref.role == "reference" for ref in request.references)
            or request.resolution in {"1080p", "4k"}
        ) and duration != 8:
            raise ValueError("Veo references, extension and high resolution require duration=8")
        if request.operation == "extend" and (
            request.resolution not in {None, "720p"} or request.references
        ):
            raise ValueError("Veo extension requires 720p and no image references")


class VideoHTTPAdapter:
    """Small HTTP-only adapter usable on controller or executor.

    Never retries POST. The caller persists dispatch intent before calling submit.
    Input URLs are controller-issued exact-artifact transfer URLs, not user inputs.
    """

    def __init__(
        self,
        connection: VideoConnection,
        client: httpx.AsyncClient,
        *,
        usage_binding: UsageBinding | None = None,
    ):
        self.connection = connection
        self.client = client
        self.usage_binding = usage_binding
        self.base = connection.base_url.rstrip("/")
        expected = (
            "https://openrouter.ai/api/v1"
            if connection.adapter == "openrouter"
            else "https://generativelanguage.googleapis.com/v1beta"
        )
        if self.base != expected:
            raise ValueError("Native video adapters require their canonical provider endpoint")
        self.headers = (
            {"Authorization": f"Bearer {connection.api_key}"}
            if connection.adapter == "openrouter"
            else {"x-goog-api-key": connection.api_key}
        )

    async def _error_diagnostics(
        self, response: httpx.Response, payload: object, inspect_provider_error: bool
    ) -> tuple[str, str | None]:
        if self.connection.adapter not in {"omni", "veo"}:
            return "", None
        content = bytearray()
        try:
            # Diagnostics are optional. A broken/slow body must not erase a known
            # HTTP rejection or change submission/billing uncertainty.
            async with asyncio.timeout(5):
                async for chunk in response.aiter_bytes(1024):
                    if len(content) + len(chunk) > 16 * 1024:
                        return "", None
                    content.extend(chunk)
        except (httpx.HTTPError, TimeoutError):
            return "", None
        diagnostic = _google_error_diagnostics(content, self.connection.adapter)
        message = None
        if inspect_provider_error and self.connection.adapter == "omni":
            try:
                body = json.loads(content)
            except (ValueError, UnicodeError, RecursionError):
                body = None
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                message = redact_interactions_message(
                    error.get("message"), payload=payload, api_key=self.connection.api_key
                )
        return diagnostic, message

    async def _json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        inspect_provider_error: bool = False,
        max_response_bytes: int = _JSON_RESPONSE_LIMIT,
    ) -> dict[str, Any]:
        attempt = (
            self.usage_binding.begin(operation="native", streaming=False, parameters={})
            if method == "POST" and self.usage_binding is not None
            else None
        )
        try:
            result = await self._request_json(
                method,
                path,
                payload,
                inspect_provider_error=inspect_provider_error,
                max_response_bytes=max_response_bytes,
                usage_attempt=attempt,
            )
            if attempt is not None:
                attempt.observe(result, generated=False)
            return result
        except BaseException as exc:
            if attempt is not None:
                attempt.finish(error=exc)
            raise
        finally:
            if attempt is not None:
                attempt.finish()

    async def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        inspect_provider_error: bool = False,
        max_response_bytes: int = _JSON_RESPONSE_LIMIT,
        usage_attempt: UsageAttempt | None = None,
    ) -> dict[str, Any]:
        async with self.client.stream(
            method,
            f"{self.base}/{path}",
            headers=self.headers,
            json=payload,
            timeout=httpx.Timeout(600, connect=15),
            follow_redirects=False,
        ) as response:
            if usage_attempt is not None:
                usage_attempt.observe({"response_headers": response.headers}, generated=False)
            if response.status_code >= 300:
                retry = response.headers.get("retry-after", "30")
                diagnostics, message = await self._error_diagnostics(
                    response, payload, inspect_provider_error
                )
                raise VideoProviderError(
                    f"provider_http_{response.status_code}{diagnostics}",
                    rejected=400 <= response.status_code < 500 and response.status_code != 408,
                    retry_after=min(3600, int(retry))
                    if re.fullmatch(r"[0-9]{1,10}", retry)
                    else 30,
                    provider_error_message=message,
                    status_code=response.status_code,
                )
            content = bytearray()
            async for chunk in response.aiter_bytes(64 * 1024):
                if len(content) + len(chunk) > max_response_bytes:
                    raise VideoProviderError("provider_response_too_large")
                content.extend(chunk)
            try:
                result = json.loads(content)
            except (ValueError, UnicodeError, RecursionError):
                result = None
            if not isinstance(result, dict):
                raise VideoProviderError("malformed_provider_response")
            return result

    async def capabilities(self) -> VideoCapabilities:
        """Read the live OpenRouter catalog or the native adapter contract."""
        if self.connection.adapter != "openrouter":
            return native_video_capabilities(self.connection.adapter)
        data = await self._json("GET", "videos/models")
        row = next((r for r in data.get("data", []) if r.get("id") == self.connection.model), None)
        if row is None:
            raise ValueError("Video model is not available in the provider catalog")
        parameters = set(row.get("supported_parameters") or [])
        return VideoCapabilities(
            operations=["generate"],
            durations=row.get("supported_durations") or [],
            resolutions=row.get("supported_resolutions") or [],
            aspect_ratios=row.get("supported_aspect_ratios") or [],
            input_roles=(
                (["first_frame", "last_frame"] if "frame_images" in parameters else [])
                + (["reference"] if "input_references" in parameters else [])
            ),
            max_references=3
            if "input_references" in parameters
            else 2
            if "frame_images" in parameters
            else 0,
            audio="generate_audio" in parameters,
            seed="seed" in parameters,
            notes=["Account/model restrictions may further limit image and audio conditioning."],
        )

    async def _image(self, source: VideoInput) -> dict[str, Any]:
        if source.size_bytes > 8 * 1024 * 1024:
            raise ValueError("Veo reference images must be at most 8 MiB")
        content = bytearray()
        async with self.client.stream("GET", source.url, follow_redirects=False) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 8 * 1024 * 1024:
                    raise ValueError("Reference image exceeds its transfer limit")
        return {
            "mimeType": source.mime_type,
            "bytesBase64Encoded": base64.b64encode(content).decode(),
        }

    async def submit(self, request: VideoRequest, inputs: list[VideoInput]) -> VideoRemoteResult:
        """Submit exactly once; ambiguity must be settled by the caller."""
        validate_video_combination(self.connection.adapter, request)
        if self.connection.adapter == "openrouter":
            payload: dict[str, Any] = {"model": self.connection.model, "prompt": request.prompt}
            for name in ("duration", "resolution", "aspect_ratio", "generate_audio", "seed"):
                if (value := getattr(request, name)) is not None:
                    payload[name] = value
            frames, references = [], []
            for source in inputs:
                image = {"type": "image_url", "image_url": {"url": source.url}}
                if source.role in {"first_frame", "last_frame"}:
                    frames.append({**image, "frame_type": source.role})
                else:
                    references.append(image)
            if frames:
                payload["frame_images"] = frames
            if references:
                payload["input_references"] = references
            data = await self._json("POST", "videos", payload)
            return self._openrouter_result(data)
        if self.connection.adapter == "veo":
            instance: dict[str, Any] = {"prompt": request.prompt}
            references = []
            for source in inputs:
                if source.role == "source":
                    if not source.provider_locator:
                        raise ValueError("Veo extension requires a retained provider output")
                    self._output_url(source.provider_locator)
                    instance["video"] = {"uri": source.provider_locator}
                elif source.role == "reference":
                    references.append(
                        {"image": await self._image(source), "referenceType": "asset"}
                    )
                else:
                    instance[
                        "image" if source.role == "first_frame" else "lastFrame"
                    ] = await self._image(source)
            if references:
                instance["referenceImages"] = references
            parameters: dict[str, Any] = {"durationSeconds": request.duration or 8}
            for name, target in (
                ("resolution", "resolution"),
                ("aspect_ratio", "aspectRatio"),
                ("seed", "seed"),
                ("negative_prompt", "negativePrompt"),
                ("person_generation", "personGeneration"),
            ):
                if (value := getattr(request, name)) is not None:
                    parameters[target] = value
            data = await self._json(
                "POST",
                f"models/{quote(self.connection.model, safe='')}:predictLongRunning",
                {"instances": [instance], "parameters": parameters},
            )
            return self._veo_result(data)
        payload = {
            "model": self.connection.model,
            "input": request.prompt,
            "store": True,
            "stream": False,
            "response_format": {"type": "video", "delivery": "uri"},
        }
        response_format = payload["response_format"]
        for name in ("resolution", "aspect_ratio"):
            if (value := getattr(request, name)) is not None:
                response_format[name] = value
        parts: list[dict[str, Any]] = []
        for source in inputs:
            if source.role == "source":
                if not source.interaction_id:
                    raise ValueError("Omni edits require a stored same-account interaction")
                payload["previous_interaction_id"] = source.interaction_id
            else:
                image = await self._image(source)
                parts.append(
                    {
                        "type": "image",
                        "mime_type": source.mime_type,
                        "data": image["bytesBase64Encoded"],
                    }
                )
        prompt = request.prompt
        if request.operation == "extend":
            prompt = f"Extend this video at its end. {prompt}"
        payload["input"] = [*parts, {"type": "text", "text": prompt}] if parts else prompt
        return self._omni_result(
            await self._json(
                "POST",
                "interactions",
                payload,
                inspect_provider_error=request.inspect_provider_error,
                max_response_bytes=_OMNI_JSON_RESPONSE_LIMIT,
            )
        )

    async def poll(self, handle: str) -> VideoRemoteResult:
        """Read a previously bound remote operation; never resubmit."""
        if self.connection.adapter == "openrouter":
            return self._openrouter_result(
                await self._json("GET", f"videos/{quote(handle, safe='')}")
            )
        if self.connection.adapter == "veo":
            if not re.fullmatch(r"(?:models/[\w.-]+/)?operations/[\w.-]+", handle):
                raise VideoProviderError("invalid_operation_handle")
            return self._veo_result(await self._json("GET", handle))
        return self._omni_result(
            await self._json(
                "GET",
                f"interactions/{quote(handle, safe='')}",
                max_response_bytes=_OMNI_JSON_RESPONSE_LIMIT,
            ),
            expected_handle=handle,
        )

    def _openrouter_result(self, data: dict[str, Any]) -> VideoRemoteResult:
        handle = data.get("id")
        if not isinstance(handle, str) or not handle:
            raise VideoProviderError("missing_remote_handle")
        status = data.get("status")
        state = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}.get(
            status if isinstance(status, str) else "", "running"
        )
        outputs = []
        if state == "completed":
            # Construct authenticated URLs; never follow a callback-provided locator.
            count = len(data.get("unsigned_urls") or data.get("urls") or [0])
            if count > 4:
                raise VideoProviderError("unexpected_output_count")
            outputs = [
                VideoOutput(
                    locator=f"{self.base}/videos/{quote(handle, safe='')}/content?index={i}"
                )
                for i in range(count)
            ]
        cost = (data.get("usage") or {}).get("cost")
        return VideoRemoteResult(
            handle=handle,
            state=state,
            outputs=outputs,
            cost_usd=cost,
            output_duration_ms=round(Decimal(str(data["duration"])) * 1000)
            if state == "completed" and data.get("duration") is not None
            else None,
            error_code="provider_rejected" if state == "failed" else None,
        )

    def _veo_result(self, data: dict[str, Any]) -> VideoRemoteResult:
        handle = data.get("name")
        if not isinstance(handle, str) or not handle:
            raise VideoProviderError("missing_remote_handle")
        if data.get("error"):
            return VideoRemoteResult(handle=handle, state="failed", error_code="provider_rejected")
        if not data.get("done"):
            return VideoRemoteResult(handle=handle, state="running")
        samples = ((data.get("response") or {}).get("generateVideoResponse") or {}).get(
            "generatedSamples", []
        )
        outputs = [VideoOutput(locator=self._output_url(s["video"]["uri"])) for s in samples]
        return VideoRemoteResult(
            handle=handle,
            state="completed" if outputs else "failed",
            outputs=outputs,
            error_code=None if outputs else "provider_filtered_output",
        )

    def _omni_video_parts(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Keep one ordinal across every model-output video, never reflected inputs."""
        steps = data.get("steps", [])
        if not isinstance(steps, list):
            raise VideoProviderError("malformed_provider_response")
        videos = []
        for step in steps:
            if not isinstance(step, dict):
                raise VideoProviderError("malformed_provider_response")
            if step.get("type") != "model_output":
                continue
            content = step.get("content", [])
            if not isinstance(content, list):
                raise VideoProviderError("malformed_provider_response")
            for part in content:
                if not isinstance(part, dict):
                    raise VideoProviderError("malformed_provider_response")
                if part.get("type") != "video":
                    continue
                uri, encoded = part.get("uri"), part.get("data")
                if uri:
                    if not isinstance(uri, str):
                        raise VideoProviderError("malformed_video_output")
                elif not isinstance(encoded, str) or not encoded:
                    raise VideoProviderError("missing_video_output")
                elif part.get("mime_type") != "video/mp4":
                    raise VideoProviderError("unexpected_output_type")
                videos.append(part)
        return videos

    def _omni_result(
        self, data: dict[str, Any], *, expected_handle: str | None = None
    ) -> VideoRemoteResult:
        handle = data.get("id")
        if not isinstance(handle, str) or not handle:
            raise VideoProviderError("missing_remote_handle")
        if expected_handle is not None and handle != expected_handle:
            raise VideoProviderError("interaction_id_mismatch")
        parts = self._omni_video_parts(data)
        status = data.get("status")
        outputs = []
        # Inline data is never a durable/RPC result. A terminal stored interaction
        # is fetched again during collection, using its already-persisted ordinal.
        if status == "completed" or all(part.get("uri") for part in parts):
            for part in parts:
                if part.get("uri"):
                    locator = self._output_url(part["uri"])
                    if omni_interaction_handle(self.connection, locator) is not None:
                        raise VideoProviderError("invalid_video_uri")
                else:
                    locator = f"{self.base}/interactions/{quote(handle, safe='')}"
                outputs.append(VideoOutput(locator=locator))
        if status == "completed" and not outputs:
            raise VideoProviderError("missing_video_output")
        return VideoRemoteResult(
            handle=handle,
            state="completed" if outputs else ("failed" if status == "failed" else "running"),
            outputs=outputs,
            error_code="provider_rejected" if status == "failed" else None,
        )

    def _output_url(self, locator: str) -> str:
        parsed = urlsplit(locator)
        base = urlsplit(self.base)
        if (
            parsed.scheme != "https"
            or parsed.hostname != base.hostname
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise VideoProviderError("unsafe_output_origin")
        return locator

    async def _inline_stream(
        self, output: VideoOutput, handle: str, max_bytes: int, output_index: int | None
    ) -> AsyncIterator[bytes]:
        if type(output_index) is not int or output_index < 0:
            raise VideoProviderError("invalid_inline_output_index")
        if output.mime_type != "video/mp4":
            raise VideoProviderError("unexpected_output_type")
        data = await self._json(
            "GET",
            f"interactions/{quote(handle, safe='')}",
            max_response_bytes=_OMNI_JSON_RESPONSE_LIMIT,
        )
        remote = self._omni_result(data, expected_handle=handle)
        if data.get("status") != "completed" or remote.state != "completed":
            raise VideoProviderError("inline_interaction_not_completed")
        if output_index >= len(remote.outputs) or remote.outputs[output_index] != output:
            raise VideoProviderError("inline_output_changed")
        encoded = self._omni_video_parts(data)[output_index].get("data")
        del data, remote
        if not isinstance(encoded, str):
            raise VideoProviderError("inline_output_changed")
        async for chunk in mp4_stream(self._decode_inline(encoded, max_bytes)):
            yield chunk

    async def _decode_inline(self, encoded: str, max_bytes: int) -> AsyncIterator[bytes]:
        length = len(encoded)
        padding = 2 if encoded.endswith("==") else int(encoded.endswith("="))
        if not length or length % 4 or encoded.endswith("===") or not encoded.isascii():
            raise VideoProviderError("invalid_inline_video")
        if length // 4 * 3 - padding > max_bytes:
            raise VideoProviderError("output_too_large")
        total = 0
        for start in range(0, length, _BASE64_CHUNK_SIZE):
            segment = encoded[start : start + _BASE64_CHUNK_SIZE].encode("ascii")
            decoded = None
            try:
                if b"=" not in segment or start + len(segment) == length:
                    decoded = base64.b64decode(segment, validate=True)
            except binascii.Error:
                pass
            # Also reject non-canonical padding bits, without allocating a full
            # decoded video or retaining payload-bearing decoder exceptions.
            if decoded is None or base64.b64encode(decoded) != segment:
                raise VideoProviderError("invalid_inline_video")
            total += len(decoded)
            if total > max_bytes:
                raise VideoProviderError("output_too_large")
            yield decoded

    async def output_stream(
        self, output: VideoOutput, max_bytes: int, *, output_index: int | None = None
    ) -> AsyncIterator[bytes]:
        """Stream bytes with bounded size; do not forward credentials through redirects."""
        locator = self._output_url(output.locator)
        handle = omni_interaction_handle(self.connection, locator)
        if handle is not None:
            async for chunk in self._inline_stream(output, handle, max_bytes, output_index):
                yield chunk
            return
        async with self.client.stream(
            "GET", locator, headers=self.headers, follow_redirects=False, timeout=120
        ) as response:
            if response.status_code != 200:
                raise VideoProviderError(f"output_http_{response.status_code}")
            content_type = response.headers.get("content-type", "").split(";")[0]
            if content_type not in {"video/mp4", "video/webm", "application/octet-stream"}:
                raise VideoProviderError("unexpected_output_type")
            total = 0
            async for chunk in response.aiter_bytes(256 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise VideoProviderError("output_too_large")
                yield chunk
            if not total:
                raise VideoProviderError("empty_video_output")
