from __future__ import annotations

import asyncio

import httpx
import pytest

from cognis.core import local_model_catalog as catalog_module
from cognis.core.local_model_catalog import LocalModelCatalog
from cognis.models.local_models import LocalModelCatalogSource


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._content

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_curated_catalog_and_direct_reference_mapping() -> None:
    catalog = LocalModelCatalog(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            base_url="https://huggingface.co",
        )
    )

    response = await catalog.search(
        source=LocalModelCatalogSource.OLLAMA,
        query="qwen reasoning",
        cursor=None,
        limit=20,
    )
    direct = catalog.resolve_reference("qwen3:8b")
    unknown = catalog.resolve_reference("hf.co/acme/model:Q4_K_M")

    assert [item.requested_ref for item in response.items] == ["qwen3:8b"]
    assert direct.parameter_count == 8_190_000_000
    assert unknown.requested_ref == "hf.co/acme/model:Q4_K_M"
    assert unknown.source == LocalModelCatalogSource.HUGGINGFACE
    assert "unavailable" in unknown.warnings[0].lower()


@pytest.mark.asyncio
async def test_curated_filters_apply_without_a_search_query() -> None:
    catalog = LocalModelCatalog(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500))
        )
    )

    response = await catalog.search(
        source=LocalModelCatalogSource.OLLAMA,
        query="",
        cursor=None,
        limit=20,
        parameter_range="8b_14b",
        download_size_range="4gib_8gib",
        quantization="Q4_K_M",
        min_context=32768,
        include_unknown=False,
    )

    assert [item.requested_ref for item in response.items] == ["qwen3:8b"]


@pytest.mark.asyncio
async def test_huggingface_gguf_normalization_pagination_and_cache() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json=[
                {
                    "id": "acme/Small-GGUF",
                    "tags": ["gguf", "license:apache-2.0"],
                    "gguf": {"total": 3_000_000_000},
                    "siblings": [
                        {
                            "rfilename": "small.Q4_K_M.gguf",
                            "size": 2_000_000_000,
                        },
                        {
                            "rfilename": "small.Q8_0.gguf",
                            "lfs": {"size": 3_500_000_000},
                        },
                    ],
                }
            ],
            headers={"link": '<https://huggingface.co/api/models?cursor=next_123>; rel="next"'},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client, cache_ttl_seconds=60)
        first = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="small",
            cursor=None,
            limit=10,
        )
        second = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="small",
            cursor=None,
            limit=10,
        )

    assert calls == 1
    assert first.cached is False
    assert second.cached is True
    assert first.next_cursor == "next_123"
    assert first.items[0].license == "apache-2.0"
    assert first.items[0].parameter_count == 3_000_000_000
    assert [quant.name for quant in first.items[0].quantizations] == ["Q4_K_M", "Q8_0"]
    assert first.items[0].quantizations[0].requested_ref == "hf.co/acme/Small-GGUF:Q4_K_M"


@pytest.mark.asyncio
async def test_huggingface_errors_are_non_blocking_source_statuses() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"retry-after": "17"})
        ),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client)
        response = await catalog.search(
            source=None,
            query="",
            cursor=None,
            limit=20,
        )

    assert response.items
    hf_status = next(
        status
        for status in response.sources
        if status.source == LocalModelCatalogSource.HUGGINGFACE
    )
    assert hf_status.available is False
    assert hf_status.retry_after_seconds == 17
    assert "rate limit" in (hf_status.detail or "").lower()


@pytest.mark.asyncio
async def test_huggingface_decode_failure_retries_once_with_identity_and_fresh_headers() -> None:
    requests: list[httpx.Request] = []
    streams: list[_TrackedStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            stream = _TrackedStream(b'[{"id":"acme/model-GGUF","siblings":[]}]')
            streams.append(stream)
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=stream,
            )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        response = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="",
            cursor=None,
            limit=20,
        )

    assert response.sources[-1].available is True
    assert len(requests) == 2
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)
    assert all(request.headers["connection"] == "close" for request in requests)
    assert "cache-control" not in requests[0].headers
    assert requests[1].headers["cache-control"] == "no-cache"
    assert requests[1].headers["pragma"] == "no-cache"
    assert streams[0].closed is True


@pytest.mark.asyncio
async def test_repeated_huggingface_decode_failure_is_sanitized_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    streams: list[_TrackedStream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        stream = _TrackedStream(b"not-gzip")
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        with caplog.at_level("WARNING", logger=catalog_module.__name__):
            response = await catalog.search(
                source=None,
                query="",
                cursor=None,
                limit=20,
            )

    assert len(streams) == 2
    assert all(stream.closed for stream in streams)
    assert response.items
    assert all(item.source == LocalModelCatalogSource.OLLAMA for item in response.items)
    assert response.sources[-1].available is False
    assert response.sources[-1].detail == "Hugging Face returned an invalid encoded response."
    warnings = [
        record
        for record in caplog.records
        if record.message == "Hugging Face catalog response decoding failed"
    ]
    assert len(warnings) == 2
    assert warnings[-1].extra_data == {  # type: ignore[attr-defined]
        "endpoint_class": "search",
        "status": 200,
        "content_encoding": "gzip",
        "retry": False,
    }


@pytest.mark.asyncio
async def test_send_time_huggingface_decode_failure_retries_once_and_is_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.DecodingError("sensitive upstream decoder detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        with caplog.at_level("WARNING", logger=catalog_module.__name__):
            response = await catalog.search(
                source=LocalModelCatalogSource.HUGGINGFACE,
                query="",
                cursor=None,
                limit=20,
            )

    assert calls == 2
    assert response.sources[-1].available is False
    assert response.sources[-1].detail == "Hugging Face returned an invalid encoded response."
    warnings = [
        record
        for record in caplog.records
        if record.message == "Hugging Face catalog response decoding failed"
    ]
    assert len(warnings) == 2
    assert warnings[-1].extra_data == {  # type: ignore[attr-defined]
        "endpoint_class": "search",
        "status": None,
        "content_encoding": "unknown",
        "retry": False,
    }
    assert "sensitive upstream decoder detail" not in caplog.text


@pytest.mark.asyncio
async def test_huggingface_decode_retry_preserves_body_bound() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=_TrackedStream(b"not-gzip"),
            )
        return httpx.Response(
            200,
            stream=_TrackedStream(b"x" * (catalog_module.HF_MAX_RESPONSE_BYTES + 1)),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        response = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="",
            cursor=None,
            limit=20,
        )

    assert calls == 2
    assert response.sources[-1].available is False
    assert "bounded body size" in (response.sources[-1].detail or "")


@pytest.mark.asyncio
async def test_all_source_pagination_does_not_hide_huggingface_results() -> None:
    requested_limits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        requested_limits.append(limit)
        cursor = request.url.params.get("cursor")
        prefix = "next" if cursor else "first"
        return httpx.Response(
            200,
            json=[
                {
                    "id": f"acme/{prefix}-{index}-GGUF",
                    "siblings": [
                        {
                            "rfilename": "model.Q4_K_M.gguf",
                            "size": 1_000_000_000,
                        }
                    ],
                }
                for index in range(limit)
            ],
            headers={"link": '<https://huggingface.co/api/models?cursor=next_page>; rel="next"'},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client)
        first = await catalog.search(source=None, query="", cursor=None, limit=20)
        second = await catalog.search(
            source=None,
            query="",
            cursor=first.next_cursor,
            limit=20,
        )

    assert requested_limits == [15, 20]
    assert len(first.items) == 20
    assert len([item for item in first.items if item.source == "huggingface"]) == 15
    assert len(second.items) == 20
    assert all(item.source == "huggingface" for item in second.items)


def test_huggingface_sharded_quantization_sums_all_known_shards() -> None:
    quantizations = LocalModelCatalog._hf_quantizations(
        "acme/sharded-GGUF",
        [
            {"rfilename": "model-Q4_K_M-00001-of-00003.gguf", "size": 100},
            {"rfilename": "model-Q4_K_M-00002-of-00003.gguf", "size": 200},
            {"rfilename": "model-Q4_K_M-00003-of-00003.gguf", "size": 300},
        ],
    )

    assert len(quantizations) == 1
    assert quantizations[0].size_bytes == 600
    assert quantizations[0].file_name is not None
    assert quantizations[0].file_name.startswith("3 GGUF shards")


def test_huggingface_same_quant_independent_files_are_not_summed() -> None:
    quantizations = LocalModelCatalog._hf_quantizations(
        "acme/multimodal-GGUF",
        [
            {"rfilename": "model-Q8_0.gguf", "size": 8_000},
            {"rfilename": "mmproj-Q8_0.gguf", "size": 1_000},
        ],
    )

    assert len(quantizations) == 1
    assert quantizations[0].size_bytes == 8_000
    assert quantizations[0].file_name == "model-Q8_0.gguf"


def test_huggingface_single_incomplete_shard_is_never_treated_as_full_artifact() -> None:
    siblings = [
        {
            "rfilename": "model-Q4_K_M-00001-of-00003.gguf",
            "size": 100,
        }
    ]

    assert LocalModelCatalog._has_complete_gguf_sizes(siblings) is False
    quantizations = LocalModelCatalog._hf_quantizations(
        "acme/incomplete-GGUF",
        siblings,
    )
    assert len(quantizations) == 1
    assert quantizations[0].size_bytes is None
    assert quantizations[0].file_name == "Incomplete GGUF shard set (1/3 files)"


@pytest.mark.asyncio
async def test_twenty_result_search_does_not_hydrate_or_report_artificial_warning() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                {
                    "id": f"acme/model-{index}-GGUF",
                    "sha": f"{index:040x}",
                    "siblings": [{"rfilename": "model.Q4_K_M.gguf"}],
                }
                for index in range(20)
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        response = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="model",
            cursor=None,
            limit=20,
        )

    assert len(response.items) == 20
    assert calls == ["/api/models"]
    assert response.sources[-1].available is True
    assert "partial" not in (response.sources[-1].detail or "").casefold()
    assert all(item.metadata_status == "basic" for item in response.items)


@pytest.mark.asyncio
async def test_huggingface_detail_requests_are_concurrent_and_bounded() -> None:
    active = 0
    maximum_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        if request.url.path == "/api/models":
            raise AssertionError("search is not used by detail resolution")
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        repo_id = "/".join(request.url.path.split("/")[3:5])
        return httpx.Response(
            200,
            json={
                "id": repo_id,
                "sha": "a" * 40,
                "siblings": [{"rfilename": "model.Q4_K_M.gguf", "size": 1_000_000_000}],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client)
        items = await asyncio.gather(
            *[
                catalog.detail(repo_id=f"acme/model-{index}-GGUF", revision_sha=None)
                for index in range(8)
            ]
        )

    assert len(items) == 8
    assert maximum_active == 4


@pytest.mark.asyncio
async def test_huggingface_detail_is_cached_and_coalesced_by_repo_and_sha() -> None:
    calls: list[str] = []
    sha = "b" * 40

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        await asyncio.sleep(0.01)
        if request.url.path.endswith("/README.md"):
            return httpx.Response(200, text="# Model\nUseful **description**.")
        return httpx.Response(
            200,
            json={
                "id": "acme/metadata-GGUF",
                "sha": sha,
                "siblings": [{"rfilename": "model.Q4_K_M.gguf", "size": 4_000_000_000}],
                "gguf": {
                    "total": 7_000_000_000,
                    "context_length": 2_048,
                    "block_count": 32,
                    "attention_head_count_kv": 8,
                },
                "config": {
                    "hidden_size": 4_096,
                    "num_attention_heads": 32,
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client)
        first, second = await asyncio.gather(
            catalog.detail(repo_id="acme/metadata-GGUF", revision_sha=sha),
            catalog.detail(repo_id="acme/metadata-GGUF", revision_sha=sha),
        )
        third = await catalog.detail(repo_id="acme/metadata-GGUF", revision_sha=sha)

    assert calls == [
        f"/api/models/acme/metadata-GGUF/revision/{sha}",
        f"/acme/metadata-GGUF/raw/{sha}/README.md",
    ]
    assert first == second == third
    assert first.file_size_bytes == 4_000_000_000
    assert first.revision_sha == sha
    assert first.description == "Model Useful description."
    assert first.advertised_max_context == 2_048
    assert first.architecture == {
        "layer_count": 32,
        "kv_head_count": 8,
        "head_dimension": 128,
    }


@pytest.mark.asyncio
async def test_huggingface_detail_rate_limit_is_per_item_not_search_degradation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/models":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "acme/partial-GGUF",
                        "siblings": [{"rfilename": "model.Q4_K_M.gguf"}],
                    }
                ],
            )
        return httpx.Response(429, headers={"retry-after": "19"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client, cache_ttl_seconds=300)
        response = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="partial",
            cursor=None,
            limit=10,
        )
        with pytest.raises(catalog_module.CatalogUpstreamError) as exc_info:
            await catalog.detail(repo_id="acme/partial-GGUF", revision_sha=None)

    assert response.items[0].file_size_bytes is None
    assert response.sources[-1].available is True
    assert exc_info.value.retry_after_seconds == 19


@pytest.mark.asyncio
async def test_huggingface_retry_after_is_clamped_to_response_contract() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"retry-after": "999999999"})
        ),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client)
        response = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="",
            cursor=None,
            limit=10,
        )

    assert response.sources[-1].available is False
    assert response.sources[-1].retry_after_seconds == 86_400


@pytest.mark.asyncio
async def test_degraded_cache_ttl_respects_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(catalog_module, "monotonic", lambda: now)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"retry-after": "17"})
        ),
        base_url="https://huggingface.co",
    ) as client:
        catalog = LocalModelCatalog(client=client, cache_ttl_seconds=300)
        await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="",
            cursor=None,
            limit=20,
        )

    entry = next(iter(catalog._cache.values()))
    assert entry.expires_at == 117.0


def test_direct_reference_rejects_urls() -> None:
    catalog = LocalModelCatalog(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            base_url="https://huggingface.co",
        )
    )

    with pytest.raises(ValueError, match="URLs"):
        catalog.resolve_reference("https://huggingface.co/acme/model")


@pytest.mark.asyncio
async def test_huggingface_detail_rejects_ssrf_redirects_and_oversized_bodies() -> None:
    async def assert_rejected(response: httpx.Response, match: str) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: response)
        ) as client:
            catalog = LocalModelCatalog(client=client)
            with pytest.raises(catalog_module.CatalogUpstreamError, match=match):
                await catalog.detail(repo_id="acme/model-GGUF", revision_sha=None)

    with pytest.raises(ValueError, match="repository"):
        await LocalModelCatalog().detail(
            repo_id="acme/model/../../api",
            revision_sha=None,
        )
    await assert_rejected(
        httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
        "unsafe redirect",
    )
    await assert_rejected(
        httpx.Response(200, content=b"x" * (catalog_module.HF_MAX_RESPONSE_BYTES + 1)),
        "bounded body size",
    )


def test_readme_sanitization_removes_frontmatter_html_images_and_links() -> None:
    excerpt = LocalModelCatalog._sanitize_readme_excerpt(
        """---
license: apache-2.0
---
# Model
<script>alert('x')</script>
![tracker](https://evil.invalid/pixel)
Useful [documentation](javascript:alert(1)) with <b>safe text</b>.
"""
    )

    assert excerpt is not None
    assert "script" not in excerpt
    assert "javascript" not in excerpt
    assert "tracker" not in excerpt
    assert excerpt == "Model Useful documentation with safe text."


@pytest.mark.asyncio
async def test_filters_keep_unknown_by_default_and_preserve_upstream_pagination() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "acme/known-GGUF",
                    "gguf": {"total": 7_000_000_000, "context_length": 32768},
                    "siblings": [{"rfilename": "known-Q4_K_M.gguf", "size": 6 * 1024**3}],
                },
                {
                    "id": "acme/unknown-GGUF",
                    "siblings": [{"rfilename": "unknown-Q4_K_M.gguf"}],
                },
            ],
            headers={"link": '<https://huggingface.co/api/models?cursor=next_page>; rel="next"'},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        inclusive = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="",
            cursor=None,
            limit=20,
            parameter_range="4b_8b",
            download_size_range="4gib_8gib",
            quantization="Q4_K_M",
            min_context=32768,
            include_unknown=True,
        )
        strict = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="",
            cursor=None,
            limit=20,
            parameter_range="4b_8b",
            download_size_range="4gib_8gib",
            quantization="Q4_K_M",
            min_context=32768,
            include_unknown=False,
        )

    assert [item.title for item in inclusive.items] == ["known-GGUF", "unknown-GGUF"]
    assert [item.title for item in strict.items] == ["known-GGUF"]
    assert inclusive.next_cursor == strict.next_cursor == "next_page"
    assert "bounded upstream page" in (inclusive.pagination_note or "")


@pytest.mark.asyncio
async def test_cancelled_detail_waiter_does_not_cancel_coalesced_operation() -> None:
    calls = 0
    sha = "c" * 40

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/README.md"):
            return httpx.Response(404)
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            json={
                "id": "acme/model-GGUF",
                "sha": sha,
                "siblings": [{"rfilename": "model-Q4_K_M.gguf", "size": 100}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        cancelled = asyncio.create_task(catalog.detail(repo_id="acme/model-GGUF", revision_sha=sha))
        survivor = asyncio.create_task(catalog.detail(repo_id="acme/model-GGUF", revision_sha=sha))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        item = await survivor

    assert item.revision_sha == sha
    assert calls == 1
    assert not catalog._detail_inflight


@pytest.mark.asyncio
async def test_detail_task_deadline_and_queue_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_module, "HF_DETAIL_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(catalog_module, "HF_MAX_DETAIL_TASKS", 2)

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        first = asyncio.create_task(catalog.detail(repo_id="acme/one-GGUF", revision_sha=None))
        second = asyncio.create_task(catalog.detail(repo_id="acme/two-GGUF", revision_sha=None))
        await asyncio.sleep(0)
        with pytest.raises(catalog_module.CatalogUpstreamError, match="capacity"):
            await catalog.detail(repo_id="acme/three-GGUF", revision_sha=None)
        results = await asyncio.gather(first, second, return_exceptions=True)

    assert all(
        isinstance(result, catalog_module.CatalogUpstreamError) and "deadline" in str(result)
        for result in results
    )
    assert not catalog._detail_inflight


@pytest.mark.asyncio
async def test_detail_cache_stays_bounded_with_latest_revision_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_module, "DETAIL_CACHE_MAX_ENTRIES", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/README.md"):
            return httpx.Response(404)
        repo = request.url.path.rsplit("/", 1)[-1]
        sha = f"{len(repo):040x}"
        return httpx.Response(
            200,
            json={
                "id": f"acme/{repo}",
                "sha": sha,
                "siblings": [{"rfilename": "model-Q4_K_M.gguf", "size": 100}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        for repo in ("one-GGUF", "two-GGUF", "three-GGUF"):
            await catalog.detail(repo_id=f"acme/{repo}", revision_sha=None)

    assert len(catalog._detail_cache) == 2
    assert len(catalog._detail_latest) <= 2


@pytest.mark.asyncio
async def test_detail_revision_endpoint_accepts_uppercase_sha() -> None:
    sha = "D" * 40
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/README.md"):
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "id": "acme/model-GGUF",
                "sha": sha.lower(),
                "siblings": [{"rfilename": "model-Q4_K_M.gguf", "size": 100}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        item = await catalog.detail(repo_id="acme/model-GGUF", revision_sha=sha)

    assert paths[0].endswith(f"/revision/{sha.lower()}")
    assert item.revision_sha == sha.lower()


@pytest.mark.asyncio
async def test_search_has_an_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_module, "HF_SEARCH_DEADLINE_SECONDS", 0.01)

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = LocalModelCatalog(client=client)
        response = await catalog.search(
            source=LocalModelCatalogSource.HUGGINGFACE,
            query="",
            cursor=None,
            limit=20,
        )

    assert response.items == []
    assert response.sources[-1].available is False
    assert "deadline" in (response.sources[-1].detail or "")
