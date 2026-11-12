"""Documented provider wire contracts, tested without billable calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.artifacts.ranges import parse_byte_range
from cognis.models.video import VideoOutput, VideoReference, VideoRequest
from cognis.providers.video import (
    VideoConnection,
    VideoHTTPAdapter,
    VideoInput,
    VideoProviderError,
    native_video_capabilities,
    validate_video_combination,
    video_adapter_name,
)


def adapter(client, kind="veo", model="veo-3.1-generate-preview"):
    return VideoHTTPAdapter(
        VideoConnection(
            adapter=kind,
            model=model,
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1"
            if kind == "openrouter"
            else "https://generativelanguage.googleapis.com/v1beta",
        ),
        client,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,field",
    [
        ("first_frame", "image"),
        ("last_frame", "lastFrame"),
        ("reference", "referenceImages"),
    ],
)
async def test_veo_image_native_wire_format(role, field):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"image")
        instance = json.loads(request.content)["instances"][0]
        image = instance[field][0]["image"] if field == "referenceImages" else instance[field]
        assert image == {"bytesBase64Encoded": "aW1hZ2U=", "mimeType": "image/png"}
        assert "inlineData" not in image
        return httpx.Response(200, json={"name": "operations/image-render"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await adapter(client).submit(
            VideoRequest(prompt="Animate", max_cost_usd=5),
            [
                VideoInput(
                    role=role, mime_type="image/png", url="https://cognis.test/image", size_bytes=5
                )
            ],
        )


@pytest.mark.asyncio
async def test_veo_text_submit_and_poll():
    def handler(request):
        if request.method == "POST":
            assert request.url.path.endswith(":predictLongRunning")
            body = json.loads(request.content)
            assert body["instances"] == [{"prompt": "Mountain sunrise"}]
            assert body["parameters"]["durationSeconds"] == 8
            return httpx.Response(200, json={"name": "operations/test-render"})
        return httpx.Response(
            200,
            json={
                "name": "operations/test-render",
                "done": True,
                "response": {
                    "generateVideoResponse": {
                        "generatedSamples": [
                            {
                                "video": {
                                    "uri": "https://generativelanguage.googleapis.com/v1beta/files/render:download"
                                }
                            }
                        ]
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = adapter(client)
        started = await api.submit(VideoRequest(prompt="Mountain sunrise", max_cost_usd=5), [])
        assert started.state == "running"
        completed = await api.poll(started.handle)
        assert completed.state == "completed" and len(completed.outputs) == 1


@pytest.mark.asyncio
async def test_omni_image_serialization_is_independent_of_veo():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"image")
        body = json.loads(request.content)
        assert body["input"][0] == {"type": "image", "mime_type": "image/png", "data": "aW1hZ2U="}
        assert body["input"][1] == {"type": "text", "text": "Animate"}
        assert body["response_format"] == {
            "type": "video",
            "delivery": "uri",
            "resolution": "1080p",
            "aspect_ratio": "16:9",
        }
        assert set(body) == {"model", "input", "store", "stream", "response_format"}
        return httpx.Response(200, json={"id": "image-interaction", "status": "in_progress"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
            VideoRequest(prompt="Animate", max_cost_usd=5, resolution="1080p", aspect_ratio="16:9"),
            [
                VideoInput(
                    role="reference",
                    mime_type="image/png",
                    url="https://cognis.test/image",
                    size_bytes=5,
                )
            ],
        )
        assert result.handle == "image-interaction"


def google_error_info(reason, **changes):
    return {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "domain": "googleapis.com",
        "reason": reason,
        **changes,
    }


def google_bad_request(*fields):
    return {
        "@type": "type.googleapis.com/google.rpc.BadRequest",
        "fieldViolations": [
            {"field": field, "description": "private echoed input"} for field in fields
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "details,suffix",
    [
        ([google_error_info("API_KEY_INVALID")], ":reason=API_KEY_INVALID"),
        ([google_error_info("BILLING_DISABLED")], ":reason=BILLING_DISABLED"),
        ([google_error_info("SERVICE_DISABLED")], ":reason=SERVICE_DISABLED"),
        ([google_bad_request("response_format.resolution")], ":field=response_format.resolution"),
        ([google_bad_request("input[0].mime_type")], ":field=input[0].mime_type"),
        (
            [
                google_error_info("API_KEY_INVALID", metadata={"key": "private-key"}),
                google_bad_request("response_format.aspect_ratio"),
            ],
            ":reason=API_KEY_INVALID:field=response_format.aspect_ratio",
        ),
    ],
)
async def test_google_http_error_preserves_only_recognized_diagnostics(details, suffix, caplog):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            400,
            headers={"retry-after": "120"},
            json={
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "private prompt https://private.test/?key=private-key aW1hZ2U=",
                    "details": details,
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VideoProviderError) as caught:
            await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
                VideoRequest(prompt="Animate", max_cost_usd=5), []
            )
    error = caught.value
    assert error.code == str(error) == f"provider_http_400{suffix}"
    assert error.rejected and error.retry_after == 120
    assert error.__cause__ is None and error.__context__ is None
    assert "private" not in repr(error) + caplog.text
    assert "aW1hZ2U=" not in repr(error) + caplog.text
    assert calls == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not JSON: API_KEY_INVALID response_format.resolution",
        b"\xff",
        b"[" * 1100 + b"]" * 1100,
        b'{"error":{"details":',
        b"[]",
        b"null",
        json.dumps({"error": "private prompt"}).encode(),
        json.dumps(
            {"error": {"status": "INVALID_ARGUMENT", "message": "API_KEY_INVALID"}}
        ).encode(),
        json.dumps({"error": {"details": {"reason": "API_KEY_INVALID"}}}).encode(),
        json.dumps(
            {
                "error": {
                    "details": [
                        None,
                        "private prompt",
                        google_error_info(["API_KEY_INVALID"]),
                        google_error_info({"private": "secret"}),
                        google_error_info("API_KEY_INVALID\nprivate-key"),
                        google_error_info("API_KEY_INVALID", domain="private.test"),
                        google_error_info("API_KEY_INVALID", **{"@type": "private.test/ErrorInfo"}),
                        google_error_info("UNRECOGNIZED_PRIVATE_REASON"),
                        google_bad_request(
                            "response_format.resolution\nprivate-key",
                            "input[0].text.private_prompt",
                            "input[99].data",
                            "https://private.test/?key=private-key",
                            "aW1hZ2U=",
                            ["input"],
                            {"field": "input"},
                        ),
                        {
                            "@type": "type.googleapis.com/google.rpc.BadRequest",
                            "fieldViolations": "response_format.resolution",
                        },
                        {"reason": "API_KEY_INVALID", "field": "response_format.resolution"},
                    ],
                    "message": "private prompt API_KEY_INVALID",
                }
            }
        ).encode(),
        json.dumps(
            {"error": {"details": [google_error_info("API_KEY_INVALID")], "message": "x" * 16384}}
        ).encode(),
    ],
)
async def test_unknown_malformed_or_reflected_error_content_stays_generic(body, caplog):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, content=body))
    ) as client:
        with pytest.raises(VideoProviderError) as caught:
            await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
                VideoRequest(prompt="Animate", max_cost_usd=5), []
            )
    assert str(caught.value) == "provider_http_400"
    assert caught.value.rejected
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private" not in repr(caught.value) + caplog.text


@pytest.mark.asyncio
async def test_error_diagnostics_are_deduplicated_and_bounded():
    details = [
        google_error_info("SERVICE_DISABLED"),
        google_error_info("BILLING_DISABLED"),
        google_error_info("API_KEY_INVALID"),
        google_error_info("API_KEY_INVALID"),
        google_bad_request("stream", "store", "input", "model", "input"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": {"details": details}})
        )
    ) as client:
        with pytest.raises(VideoProviderError) as caught:
            await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
                VideoRequest(prompt="Animate", max_cost_usd=5), []
            )
    assert str(caught.value) == (
        "provider_http_400:reason=API_KEY_INVALID:reason=BILLING_DISABLED"
        ":field=input:field=model:field=store"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["veo", "openrouter"])
async def test_google_error_schema_is_not_assumed_for_other_provider_fields(kind):
    details = [
        google_error_info("API_KEY_INVALID"),
        google_bad_request("response_format.resolution"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": {"details": details}})
        )
    ) as client:
        with pytest.raises(VideoProviderError) as caught:
            await adapter(client, kind).submit(VideoRequest(prompt="Animate", max_cost_usd=5), [])
    assert str(caught.value) == (
        "provider_http_400:reason=API_KEY_INVALID" if kind == "veo" else "provider_http_400"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadError, TimeoutError, asyncio.CancelledError])
async def test_error_body_failure_preserves_status_but_not_cancellation(failure):
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":'
            raise failure("private upstream content")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, stream=BrokenBody()))
    ) as client:
        with pytest.raises(
            asyncio.CancelledError if failure is asyncio.CancelledError else VideoProviderError
        ) as caught:
            await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
                VideoRequest(prompt="Animate", max_cost_usd=5), []
            )
    if failure is not asyncio.CancelledError:
        assert str(caught.value) == "provider_http_400"
        assert caught.value.rejected
        assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_error_body_read_stops_at_byte_limit_and_closes():
    class LargeBody(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                self.chunks += 1
                yield b"x" * 1024

        async def aclose(self):
            self.closed = True

    body = LargeBody()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, stream=body))
    ) as client:
        with pytest.raises(VideoProviderError, match="^provider_http_400$"):
            await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
                VideoRequest(prompt="Animate", max_cost_usd=5), []
            )
    assert body.chunks == 17 and body.closed


@pytest.mark.asyncio
async def test_slow_error_body_diagnostics_timeout_preserves_rejection(monkeypatch):
    timeout = asyncio.timeout
    limits = []

    def short_timeout(seconds):
        limits.append(seconds)
        return timeout(0.001)

    class SlowBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"unreachable private content"

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("cognis.providers.video.asyncio.timeout", short_timeout)
    body = SlowBody()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, stream=body))
    ) as client:
        with pytest.raises(VideoProviderError) as caught:
            await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
                VideoRequest(prompt="Animate", max_cost_usd=5), []
            )
    assert caught.value.code == "provider_http_400" and caught.value.rejected
    assert caught.value.__context__ is None
    assert limits == [5] and body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header,expected", [("30", 30), ("7200", 3600), ("²", 30), ("9" * 5000, 30), ("private", 30)]
)
async def test_untrusted_retry_after_cannot_erase_known_rejection(header, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, headers={b"retry-after": header.encode("latin-1")})
        )
    ) as client:
        with pytest.raises(VideoProviderError) as caught:
            await adapter(client).submit(VideoRequest(prompt="Animate", max_cost_usd=5), [])
    assert caught.value.code == "provider_http_400" and caught.value.rejected
    assert caught.value.retry_after == expected


@pytest.mark.asyncio
async def test_executor_rpc_keeps_existing_generic_failure_boundary(monkeypatch):
    from cognis.executor.runner import ExecutorRunner

    rpc = AsyncMock(
        side_effect=VideoProviderError("provider_http_400:reason=API_KEY_INVALID", rejected=True)
    )
    monkeypatch.setattr("cognis.executor.video.video_rpc", rpc)
    runner = SimpleNamespace(
        _inference_handler=object(), _send_rpc_result=AsyncMock(), _send_rpc_error=AsyncMock()
    )
    ws = object()
    await ExecutorRunner._handle_video(runner, ws, "video-error-test", {})
    rpc.assert_awaited_once()
    runner._send_rpc_result.assert_not_awaited()
    runner._send_rpc_error.assert_awaited_once_with(
        ws, "video-error-test", -32000, "Video operation failed; submission may be uncertain"
    )


@pytest.mark.asyncio
async def test_openrouter_native_payload_and_cost():
    def handler(request):
        body = json.loads(request.content)
        assert body["frame_images"][0]["frame_type"] == "first_frame"
        assert body["duration"] == 8
        assert "callback_url" not in body
        return httpx.Response(202, json={"id": "test-render", "status": "pending"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = adapter(client, "openrouter", "google/veo-3.1")
        await api.submit(
            VideoRequest(prompt="Animate", max_cost_usd=5, duration=8),
            [
                VideoInput(
                    role="first_frame",
                    url="https://cognis.test/signed.png",
                    mime_type="image/png",
                    size_bytes=10,
                ),
            ],
        )
        result = api._openrouter_result(
            {
                "id": "test-render",
                "status": "completed",
                "usage": {"cost": "0.25"},
                "unsigned_urls": ["https://evil.test/video"],
            }
        )
        assert str(result.cost_usd) == "0.25"
        assert result.outputs[0].locator.startswith("https://openrouter.ai/api/v1/videos/")


@pytest.mark.asyncio
async def test_omni_edit_continuation_and_uri_output():
    def handler(request):
        body = json.loads(request.content)
        assert body["previous_interaction_id"] == "interaction-prior"
        assert body["response_format"]["delivery"] == "uri"
        return httpx.Response(
            200,
            json={
                "id": "interaction-new",
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "video",
                                "uri": "https://generativelanguage.googleapis.com/v1beta/files/new:download",
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await adapter(client, "omni", "gemini-omni-1.1-flash").submit(
            VideoRequest(
                operation="edit",
                source_artifact_id="source-video",
                prompt="Change the lighting",
                max_cost_usd=5,
            ),
            [
                VideoInput(
                    role="source",
                    url="",
                    mime_type="video/mp4",
                    size_bytes=100,
                    interaction_id="interaction-prior",
                )
            ],
        )
        assert result.state == "completed"


@pytest.mark.parametrize(
    "url",
    [
        "http://generativelanguage.googleapis.com/video",
        "https://generativelanguage.googleapis.com.evil.test/video",
        "https://key@generativelanguage.googleapis.com/video",
        "https://127.0.0.1/video",
        "https://generativelanguage.googleapis.com:444/video",
    ],
)
def test_output_origins_rejected(url):
    with pytest.raises(VideoProviderError):
        adapter(httpx.AsyncClient())._output_url(url)


@pytest.mark.asyncio
async def test_download_rejects_redirect_and_bounds_bytes():
    for response in (
        httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"error"),
        httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"x" * 11),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request, response=response: response)
        ) as client:
            with pytest.raises(VideoProviderError):
                _ = [
                    chunk
                    async for chunk in adapter(client).output_stream(
                        VideoOutput(locator="https://generativelanguage.googleapis.com/video"), 10
                    )
                ]


@pytest.mark.parametrize(
    "preset,model",
    [
        ("openai", "sora-2"),
        ("anthropic", "claude"),
        ("ollama", "llama"),
        ("gemini", "gemini-2.5-pro"),
    ],
)
def test_chat_presets_do_not_imply_video_support(preset, model):
    with pytest.raises(ValueError):
        video_adapter_name(preset, model)


def test_no_silent_capability_downgrade():
    with pytest.raises(ValueError, match="audio"):
        native_video_capabilities("veo").validate_request(
            VideoRequest(prompt="Rain", max_cost_usd=5, generate_audio=False)
        )
    with pytest.raises(ValueError, match="require duration"):
        validate_video_combination(
            "veo", VideoRequest(prompt="Rain", max_cost_usd=5, duration=4, resolution="1080p")
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        VideoRequest(
            prompt="Rain",
            max_cost_usd=5,
            references=[
                VideoReference(artifact_id="first", role="first_frame"),
                VideoReference(artifact_id="style", role="reference"),
            ],
        )


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, (0, 99, False)),
        ("bytes=10-19", (10, 19, True)),
        ("bytes=90-", (90, 99, True)),
        ("bytes=-10", (90, 99, True)),
        ("bytes=0-200", (0, 99, True)),
    ],
)
def test_media_ranges(header, expected):
    assert parse_byte_range(header, 100) == expected


@pytest.mark.parametrize(
    "header", ["bytes=100-", "bytes=-0", "bytes=20-10", "bytes=1-2,5-6", "cats=0-1"]
)
def test_bad_media_ranges(header):
    with pytest.raises(ValueError):
        parse_byte_range(header, 100)
