"""Documented Omni retrieval fixtures, with synthetic media and mocked HTTP only."""

import asyncio
import base64
import json

import httpx
import pytest

from cognis.models.video import VideoOutput, VideoRequest
from cognis.providers.video import VideoConnection, VideoHTTPAdapter, VideoProviderError

BASE = "https://generativelanguage.googleapis.com/v1beta"
HANDLE = "stored-inline-interaction"
LOCATOR = f"{BASE}/interactions/{HANDLE}"
URI = f"{BASE}/files/generated:download?alt=media"
MP4 = b"\x00\x00\x00\x18ftypmp42synthetic-video"


def connection():
    return VideoConnection(
        adapter="omni",
        model="gemini-omni-1.1-flash",
        api_key="synthetic-key",
        base_url=BASE,
    )


def video_part(content=MP4):
    return {
        "type": "video",
        "mime_type": "video/mp4",
        "data": base64.b64encode(content).decode("ascii"),
    }


def interaction(parts=None, **changes):
    return {
        "id": HANDLE,
        "status": "completed",
        "steps": [
            {"type": "user_input", "content": [video_part(b"private reflected input")]},
            {"type": "thought", "content": [video_part(b"private reflected thought")]},
            {"type": "model_output", "content": parts if parts is not None else [video_part()]},
        ],
        **changes,
    }


async def collect(api, output=None, *, index=0, limit=len(MP4)):
    return b"".join(
        [
            chunk
            async for chunk in api.output_stream(
                output or VideoOutput(locator=LOCATOR), limit, output_index=index
            )
        ]
    )


@pytest.mark.asyncio
async def test_documented_create_uri_is_preferred_and_never_retrieved_as_json():
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(
                200, json=interaction([{**video_part(), "uri": URI, "data": "not base64"}])
            )
        assert str(request.url) == URI
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=MP4)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = VideoHTTPAdapter(connection(), client)
        result = await api.submit(VideoRequest(prompt="Synthetic scene"), [])
        assert result.outputs == [VideoOutput(locator=URI)]
        assert await collect(api, result.outputs[0]) == MP4
    assert [method for method, _ in calls] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_running_then_documented_get_inline_keeps_only_metadata(caplog):
    calls = []
    body = interaction()

    def handler(request):
        calls.append(request.method)
        assert request.headers["x-goog-api-key"] == "synthetic-key"
        if request.method == "POST":
            return httpx.Response(200, json=interaction(steps=[], status="in_progress"))
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = VideoHTTPAdapter(connection(), client)
        started = await api.submit(VideoRequest(prompt="Synthetic scene"), [])
        assert started.state == "running"
        remote = await api.poll(started.handle)
        assert remote.state == "completed"
        assert remote.outputs == [VideoOutput(locator=LOCATOR)]
        assert await collect(api, remote.outputs[0]) == MP4
    assert calls == ["POST", "GET", "GET"]
    metadata = remote.model_dump_json() + caplog.text
    assert body["steps"][-1]["content"][0]["data"] not in metadata
    assert "private reflected" not in metadata


@pytest.mark.asyncio
async def test_inline_payload_above_old_four_mib_limit_is_collected_in_bounded_chunks():
    media = MP4 + b"x" * (3 * 1024 * 1024)
    body = interaction([video_part(media)])
    assert len(json.dumps(body).encode()) > 4 * 1024 * 1024
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        api = VideoHTTPAdapter(connection(), client)
        remote = await api.poll(HANDLE)
        chunks = [
            chunk
            async for chunk in api.output_stream(remote.outputs[0], len(media), output_index=0)
        ]
    assert b"".join(chunks) == media
    assert max(map(len, chunks)) <= 192 * 1024
    assert len(remote.model_dump_json()) < 512


class PaddedJSON(httpx.AsyncByteStream):
    """Generate an exact envelope bound without preallocating another large fixture."""

    def __init__(self, size):
        self.prefix = b'{"padding":"'
        self.suffix = b'",' + json.dumps(interaction(), separators=(",", ":")).encode()[1:]
        self.padding = size - len(self.prefix) - len(self.suffix)
        self.closed = False

    async def __aiter__(self):
        yield self.prefix
        remaining = self.padding
        while remaining:
            amount = min(remaining, 64 * 1024)
            yield b"x" * amount
            remaining -= amount
        yield self.suffix

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_total_omni_json_envelope_bound_includes_ignored_content(extra):
    stream = PaddedJSON(32 * 1024 * 1024 + extra)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        api = VideoHTTPAdapter(connection(), client)
        if extra:
            with pytest.raises(VideoProviderError, match="^provider_response_too_large$"):
                await api.poll(HANDLE)
        else:
            assert (await api.poll(HANDLE)).outputs == [VideoOutput(locator=LOCATOR)]
    assert stream.closed


@pytest.mark.asyncio
async def test_ordinary_json_limit_is_still_four_mib():
    stream = PaddedJSON(4 * 1024 * 1024 + 1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        api = VideoHTTPAdapter(connection(), client)
        with pytest.raises(VideoProviderError, match="^provider_response_too_large$"):
            await api._json("GET", "ordinary-metadata")
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,accepted", [(len(MP4), True), (len(MP4) - 1, False)])
async def test_exact_decoded_policy_boundary(limit, accepted):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=interaction()))
    ) as client:
        api = VideoHTTPAdapter(connection(), client)
        if accepted:
            assert await collect(api, limit=limit) == MP4
        else:
            with pytest.raises(VideoProviderError, match="^output_too_large$"):
                await collect(api, limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoded",
    [
        "!!!!",
        "AAAAA",
        "AA==AA==",
        "AAAA====",
        "Zh==",
        "éééé",
        "AA A",
        "AA\nA",
        "data:video/mp4;AAAA",
    ],
)
async def test_malformed_base64_has_only_constant_errors(encoded, caplog):
    body = interaction([{**video_part(), "data": encoded}])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(VideoProviderError, match="^invalid_inline_video$") as error:
            await collect(VideoHTTPAdapter(connection(), client), limit=1024)
    assert error.value.__context__ is None
    assert not error.value.rejected
    assert encoded not in repr(error.value) + caplog.text


@pytest.mark.asyncio
async def test_padding_inside_a_decode_chunk_boundary_is_rejected(monkeypatch):
    monkeypatch.setattr("cognis.providers.video._BASE64_CHUNK_SIZE", 8)
    body = interaction([{**video_part(), "data": "AAAAAA==" + "AAAA" * 4}])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(VideoProviderError, match="^invalid_inline_video$"):
            await collect(VideoHTTPAdapter(connection(), client), limit=1024)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,error",
    [(b"not an mp4 container", "MP4"), (b"\x00\x00\x00\x18ftyp", "incomplete")],
)
async def test_non_mp4_or_truncated_container_is_not_collected(content, error):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=interaction([video_part(content)]))
        )
    ) as client:
        with pytest.raises(ValueError, match=error):
            await collect(VideoHTTPAdapter(connection(), client), limit=1024)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,index,code",
    [
        (interaction(id="different-interaction"), 0, "interaction_id_mismatch"),
        (interaction(id=None), 0, "missing_remote_handle"),
        (interaction(id=42), 0, "missing_remote_handle"),
        (interaction(status="in_progress"), 0, "inline_interaction_not_completed"),
        (interaction(status="failed"), 0, "inline_interaction_not_completed"),
        (interaction(), -1, "invalid_inline_output_index"),
        (interaction(), True, "invalid_inline_output_index"),
        (interaction(), 0.0, "invalid_inline_output_index"),
        (interaction(), "0", "invalid_inline_output_index"),
        (interaction(), None, "invalid_inline_output_index"),
        (interaction(), 1, "inline_output_changed"),
        (interaction([{**video_part(), "mime_type": "video/webm"}]), 0, "unexpected_output_type"),
        (interaction([{"type": "video", "uri": URI}]), 0, "inline_output_changed"),
        (interaction([{"type": "video", "data": ""}]), 0, "missing_video_output"),
        (interaction(steps={}), 0, "malformed_provider_response"),
        (
            interaction(steps=[{"type": "model_output", "content": {}}]),
            0,
            "malformed_provider_response",
        ),
    ],
)
async def test_changed_or_malformed_retrieval_fails_closed(body, index, code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(VideoProviderError, match=f"^{code}$"):
            await collect(VideoHTTPAdapter(connection(), client), index=index)


@pytest.mark.asyncio
async def test_mixed_multiple_outputs_keep_all_video_ordinals_and_detect_shape_reordering():
    second = MP4 + b"-second"
    original = interaction([{"type": "video", "uri": URI}, video_part(), video_part(second)])
    body = original
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        api = VideoHTTPAdapter(connection(), client)
        remote = await api.poll(HANDLE)
        assert [output.locator for output in remote.outputs] == [URI, LOCATOR, LOCATOR]
        assert await collect(api, remote.outputs[1], index=1) == MP4
        assert await collect(api, remote.outputs[2], index=2, limit=len(second)) == second
        body = interaction([video_part(), {"type": "video", "uri": URI}, video_part(second)])
        with pytest.raises(VideoProviderError, match="^inline_output_changed$"):
            await collect(api, remote.outputs[1], index=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locator",
    [
        LOCATOR + "?key=private",
        LOCATOR + "#part",
        BASE + "/interactions/%zz",
        "http://generativelanguage.googleapis.com/v1beta/interactions/test",
        "https://user@generativelanguage.googleapis.com/v1beta/interactions/test",
        "https://generativelanguage.googleapis.com.evil.test/v1beta/interactions/test",
        "https://generativelanguage.googleapis.com:444/v1beta/interactions/test",
    ],
)
async def test_inline_locator_origin_credentials_fragment_and_query_guards(locator):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        with pytest.raises(VideoProviderError):
            await collect(VideoHTTPAdapter(connection(), client), VideoOutput(locator=locator))
    assert not calls


@pytest.mark.asyncio
async def test_inline_redirect_never_forwards_credentials():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://other.test/video"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VideoProviderError, match="^provider_http_302$"):
            await collect(VideoHTTPAdapter(connection(), client))
    assert len(calls) == 1
    assert calls[0].url.host == "generativelanguage.googleapis.com"


@pytest.mark.asyncio
async def test_cancellation_closes_an_inline_json_response():
    started = asyncio.Event()

    class WaitingBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            self.closed = True

    stream = WaitingBody()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        task = asyncio.create_task(collect(VideoHTTPAdapter(connection(), client)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


@pytest.mark.asyncio
async def test_malformed_json_never_retains_payload_bearing_exception_context(caplog):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'{"private-input":"malformed')
        )
    ) as client:
        with pytest.raises(VideoProviderError, match="^malformed_provider_response$") as error:
            await collect(VideoHTTPAdapter(connection(), client))
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert "private-input" not in repr(error.value) + caplog.text


@pytest.mark.asyncio
async def test_old_collect_rpc_without_ordinal_fails_before_provider_or_transfer_io(monkeypatch):
    from cognis.executor.video import video_rpc

    calls = []
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda request: calls.append(request)), **kwargs
        ),
    )
    with pytest.raises(VideoProviderError, match="^invalid_inline_output_index$"):
        await video_rpc(
            {
                "operation": "collect",
                "connection": connection().model_dump(),
                "output": VideoOutput(locator=LOCATOR).model_dump(),
                "output_ticket": {
                    "url": "https://cognis.test/api/v1/videos/transfer",
                    "token": "synthetic-transfer-ticket",
                    "max_bytes": 1024,
                },
            }
        )
    assert not calls
