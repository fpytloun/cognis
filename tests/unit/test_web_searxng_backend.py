"""Tests for the SearXNG search backend."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.backends.searxng import SearxngBackend, _coerce_safesearch


def test_coerce_safesearch_handles_int_bool_string() -> None:
    assert _coerce_safesearch(0) == 0
    assert _coerce_safesearch(1) == 1
    assert _coerce_safesearch(2) == 2
    assert _coerce_safesearch(True) == 1
    assert _coerce_safesearch(False) == 0
    assert _coerce_safesearch("off") == 0
    assert _coerce_safesearch("strict") == 2
    assert _coerce_safesearch("moderate") == 1
    # Unknown values fall back to moderate.
    assert _coerce_safesearch("???") == 1
    assert _coerce_safesearch(None) == 1


@pytest.mark.asyncio
async def test_search_returns_error_when_query_blank() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    result = await backend.search("")
    assert result.is_error
    assert "No search query" in result.output


@pytest.mark.asyncio
async def test_search_returns_error_when_url_missing() -> None:
    backend = SearxngBackend(base_url="")
    result = await backend.search("python")
    assert result.is_error
    assert "no instance url" in result.output.lower()


def _build_response(json_payload: dict[str, Any], *, status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "http://localhost:8888/search")
    return httpx.Response(status_code=status, json=json_payload, request=request)


@pytest.mark.asyncio
async def test_search_parses_results_and_answer() -> None:
    backend = SearxngBackend(
        base_url="http://localhost:8888",
        engines="google,bing",
        language="en",
    )
    payload = {
        "results": [
            {
                "title": "Python (programming language)",
                "url": "https://wikipedia.org/wiki/Python",
                "content": "Python is a high-level language.",
            },
            {
                "title": "Python.org",
                "url": "https://python.org/",
                "content": "Welcome to Python.org",
            },
        ],
        "answers": ["Python is a programming language."],
    }
    mock_response = _build_response(payload)

    with patch("cognis.tools.executor.web.backends.searxng.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await backend.search("python", num_results=2)

    assert isinstance(result, ToolResult)
    assert not result.is_error
    assert "Python (programming language)" in result.output
    assert "Welcome to Python.org" in result.output

    # Verify query parameters include configured defaults.
    args, kwargs = mock_client.get.call_args
    params = kwargs["params"]
    assert params["q"] == "python"
    assert params["format"] == "json"
    assert params["engines"] == "google,bing"
    assert params["language"] == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected_category"),
    [
        ({"result_type": "repository"}, "it"),
        ({"result_type": "discussion"}, "it"),
        ({"result_type": "paper"}, "science"),
        ({"search_mode": "videos"}, "videos"),
        ({"search_mode": "images"}, "images"),
        ({"search_mode": "news"}, "news"),
    ],
)
async def test_search_maps_semantic_result_types_to_categories(
    options: dict[str, Any],
    expected_category: str,
) -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        return_value={
            "results": [
                {
                    "title": "Result",
                    "url": "https://example.com/result",
                    "content": "Relevant result.",
                    "engine": "structured",
                }
            ]
        }
    )

    await backend.search("query", options=options)

    params = backend._request.await_args.args[0]
    assert params["categories"] == expected_category
    assert "engines" not in params


@pytest.mark.asyncio
async def test_explicit_engines_suppress_automatic_category_routing() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        return_value={"results": [{"title": "Result", "url": "https://example.com/result"}]}
    )

    await backend.search(
        "query",
        options={"result_type": "paper", "engines": "custom-science"},
    )

    params = backend._request.await_args.args[0]
    assert params["engines"] == "custom-science"
    assert "categories" not in params


@pytest.mark.asyncio
async def test_explicit_category_overrides_semantic_category() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        return_value={"results": [{"title": "Result", "url": "https://example.com/result"}]}
    )

    await backend.search(
        "query",
        options={"result_type": "paper", "categories": "custom-science"},
    )

    assert backend._request.await_args.args[0]["categories"] == "custom-science"


@pytest.mark.asyncio
async def test_configured_default_category_overrides_semantic_category() -> None:
    backend = SearxngBackend(
        base_url="http://localhost:8888",
        categories="general",
    )
    backend._request = AsyncMock(
        return_value={"results": [{"title": "Result", "url": "https://example.com/result"}]}
    )

    await backend.search("query", options={"result_type": "paper"})

    assert backend._request.await_count == 1
    assert backend._request.await_args.args[0]["categories"] == "general"


@pytest.mark.asyncio
async def test_semantic_category_falls_back_to_general_and_preserves_domain_filters() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        side_effect=[
            {"results": [], "unresponsive_engines": [["github", "timeout"]]},
            {
                "results": [
                    {
                        "title": "Wrong domain",
                        "url": "https://example.com/repository",
                    },
                    {
                        "title": "pallets/flask",
                        "url": "https://github.com/pallets/flask",
                        "engine": "braveapi",
                    },
                ],
                "unresponsive_engines": [["github", "timeout"]],
            },
        ]
    )

    result = await backend.search(
        "pallets flask",
        options={"result_type": "repository", "include_domains": ["github.com"]},
    )

    assert backend._request.await_count == 2
    first_params = backend._request.await_args_list[0].args[0]
    fallback_params = backend._request.await_args_list[1].args[0]
    assert first_params["categories"] == "it"
    assert "categories" not in fallback_params
    assert "pallets/flask" in result.output
    assert "Wrong domain" not in result.output
    assert result.metadata["requested_category"] == "it"
    assert result.metadata["effective_category"] is None
    assert result.metadata["category_fallback_attempted"] is True
    assert result.metadata["category_fallback_used"] is True
    assert result.metadata["search_quality"] == "degraded"
    assert result.metadata["engine_failures"] == [["github", "timeout"]]


@pytest.mark.asyncio
async def test_category_then_freshness_fallback_stays_on_general_engines() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        side_effect=[
            {"results": [], "unresponsive_engines": []},
            {"results": [], "unresponsive_engines": []},
            {
                "results": [
                    {
                        "title": "Recovered article",
                        "url": "https://example.com/article",
                        "engine": "braveapi",
                    }
                ],
                "unresponsive_engines": [],
            },
        ]
    )

    result = await backend.search(
        "current article",
        options={"search_mode": "news", "time_range": "day"},
    )

    assert backend._request.await_count == 3
    first_params, fallback_params, relaxed_params = [
        call.args[0] for call in backend._request.await_args_list
    ]
    assert first_params["categories"] == "news"
    assert first_params["time_range"] == "day"
    assert "categories" not in fallback_params
    assert fallback_params["time_range"] == "day"
    assert "categories" not in relaxed_params
    assert "time_range" not in relaxed_params
    assert result.metadata["effective_category"] is None
    assert result.metadata["effective_time_range"] is None
    assert result.metadata["category_fallback_used"] is True
    assert result.metadata["freshness_relaxed"] is True
    assert result.metadata["requested_search_mode"] == "news"
    assert result.metadata["effective_search_mode"] == "web"
    assert "Recovered article" in result.output


@pytest.mark.asyncio
async def test_image_search_mode_returns_image_artifacts() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        return_value={
            "results": [
                {
                    "title": "Mountain lake",
                    "url": "https://allowed.example/photos/lake",
                    "img_src": "https://cdn.example.com/lake.jpg",
                    "content": "A mountain lake at sunrise.",
                    "engine": "openverse",
                    "category": "images",
                    "template": "images.html",
                },
                {
                    "title": "Excluded lake",
                    "url": "https://excluded.example/photos/lake",
                    "img_src": "https://cdn.example.com/excluded.jpg",
                    "engine": "openverse",
                    "category": "images",
                },
            ]
        }
    )

    result = await backend.search(
        "mountain lake",
        options={
            "search_mode": "images",
            "include_domains": ["allowed.example"],
            "exclude_domains": ["excluded.example"],
        },
    )

    assert backend._request.await_args.args[0]["categories"] == "images"
    assert "[[result:1]]" not in result.output
    assert "https://cdn.example.com/lake.jpg" in result.output
    assert "https://cdn.example.com/excluded.jpg" not in result.output
    assert any(anchor["anchor"] == "media:1" for anchor in result.metadata["output_anchors"])
    assert not any(anchor["anchor"] == "media:2" for anchor in result.metadata["output_anchors"])


@pytest.mark.asyncio
async def test_search_options_override_defaults() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888", engines="google")
    payload: dict[str, Any] = {"results": []}
    mock_response = _build_response(payload)

    with patch("cognis.tools.executor.web.backends.searxng.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await backend.search(
            "x",
            options={
                "engines": "duckduckgo,bing",
                "categories": "it",
                "language": "fr",
                "time_range": "month",
                "safesearch": "strict",
                "page": 2,
            },
        )

    assert mock_client.get.await_count == 2
    params = mock_client.get.await_args_list[0].kwargs["params"]
    assert params["engines"] == "duckduckgo,bing"
    assert params["categories"] == "it"
    assert params["language"] == "fr"
    assert params["time_range"] == "month"
    assert params["safesearch"] == 2
    assert params["pageno"] == 2
    assert "time_range" not in mock_client.get.await_args_list[1].kwargs["params"]


@pytest.mark.asyncio
async def test_search_handles_429_with_actionable_message() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    request = httpx.Request("GET", "http://localhost:8888/search")

    with patch("cognis.tools.executor.web.backends.searxng.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        # Make the inner raise_for_status raise via response.raise_for_status.
        bad_response = httpx.Response(429, request=request)
        mock_client.get = AsyncMock(return_value=bad_response)
        mock_client_cls.return_value = mock_client

        result = await backend.search("python")

    assert result.is_error
    assert "rate-limited" in result.output


@pytest.mark.asyncio
async def test_search_handles_403_misconfig_with_actionable_message() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    request = httpx.Request("GET", "http://localhost:8888/search")
    bad_response = httpx.Response(403, request=request)

    with patch("cognis.tools.executor.web.backends.searxng.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=bad_response)
        mock_client_cls.return_value = mock_client

        result = await backend.search("python")
    assert result.is_error
    assert "JSON output" in result.output or "refused" in result.output


@pytest.mark.asyncio
async def test_search_returns_no_results_when_payload_empty() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    payload: dict[str, Any] = {"results": []}
    mock_response = _build_response(payload)

    with patch("cognis.tools.executor.web.backends.searxng.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await backend.search("nothingmatchesthis")
    assert not result.is_error
    assert "No search results" in result.output


@pytest.mark.asyncio
async def test_search_relaxes_unsupported_freshness_after_healthy_empty_result() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        side_effect=[
            {"results": [], "unresponsive_engines": []},
            {
                "results": [
                    {
                        "title": "pallets/flask",
                        "url": "https://github.com/pallets/flask",
                        "content": "The Python micro framework.",
                        "engine": "github",
                        "score": 100,
                        "metadata": {"popularity": 72000, "license": "BSD-3-Clause"},
                    }
                ],
                "unresponsive_engines": [],
            },
        ]
    )

    result = await backend.search(
        "pallets flask",
        options={
            "include_domains": ["github.com"],
            "result_type": "document",
            "time_range": "year",
        },
    )

    assert not result.is_error
    assert "pallets/flask" in result.output
    assert "Search degraded:" in result.output
    assert "freshness filter returned no usable results" in result.output
    assert result.metadata["freshness_relaxed"] is True
    assert result.metadata["requested_time_range"] == "year"
    assert result.metadata["effective_time_range"] is None
    assert result.metadata["search_quality"] == "degraded"
    assert result.metadata["returned_result_count"] == 1
    first_params = backend._request.await_args_list[0].args[0]
    second_params = backend._request.await_args_list[1].args[0]
    assert first_params["time_range"] == "year"
    assert "time_range" not in second_params


@pytest.mark.asyncio
async def test_search_does_not_relax_freshness_when_engine_failed() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        return_value={
            "results": [],
            "unresponsive_engines": [["github", "rate limit"]],
        }
    )

    result = await backend.search(
        "pallets flask",
        options={"result_type": "repository", "time_range": "year"},
    )

    assert backend._request.await_count == 2
    assert result.metadata["search_quality"] == "degraded"
    assert result.metadata["freshness_relaxation_attempted"] is False
    assert result.metadata["category_fallback_attempted"] is True


@pytest.mark.asyncio
async def test_search_preserves_empty_result_when_relaxed_retry_returns_503() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    request = httpx.Request("GET", "http://localhost:8888/search")
    response = httpx.Response(503, request=request)
    retry_error = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=response,
    )
    backend._request = AsyncMock(
        side_effect=[
            {"results": [], "unresponsive_engines": []},
            retry_error,
        ]
    )

    result = await backend.search(
        "current news",
        options={"result_type": "document", "time_range": "day"},
    )

    assert not result.is_error
    assert backend._request.await_count == 2
    assert "No usable search results found" in result.output
    assert result.metadata["search_quality"] == "degraded"
    assert result.metadata["freshness_relaxed"] is False
    assert result.metadata["freshness_relaxation_failure"] == "HTTP 503"
    assert result.metadata["effective_time_range"] == "day"


@pytest.mark.asyncio
async def test_relaxed_recovery_reports_engine_failures_in_warning() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        side_effect=[
            {"results": [], "unresponsive_engines": []},
            {
                "results": [
                    {
                        "title": "Recovered",
                        "url": "https://example.com/recovered",
                        "engine": "bing",
                    }
                ],
                "unresponsive_engines": [["startpage", "timeout"]],
            },
        ]
    )

    result = await backend.search(
        "current news",
        options={"result_type": "document", "time_range": "day"},
    )

    assert result.metadata["freshness_relaxed"] is True
    assert len(result.metadata["engine_failures"]) == 1
    assert "1 engine failure(s) also reduced coverage" in result.output


@pytest.mark.asyncio
async def test_relaxed_retry_429_is_reported_as_degraded_empty() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    request = httpx.Request("GET", "http://localhost:8888/search")
    response = httpx.Response(429, request=request)
    backend._request = AsyncMock(
        side_effect=[
            {"results": [], "unresponsive_engines": []},
            httpx.HTTPStatusError("rate limited", request=request, response=response),
        ]
    )

    result = await backend.search(
        "current news",
        options={"result_type": "document", "time_range": "day"},
    )

    assert not result.is_error
    assert backend._request.await_count == 2
    assert result.metadata["search_quality"] == "degraded"
    assert result.metadata["freshness_relaxation_failure"] == "HTTP 429"
    assert result.metadata["effective_time_range"] == "day"


@pytest.mark.asyncio
async def test_relaxed_empty_engine_failure_is_reported_as_degraded() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    backend._request = AsyncMock(
        side_effect=[
            {"results": [], "unresponsive_engines": []},
            {
                "results": [],
                "unresponsive_engines": [["startpage", "timeout"]],
            },
        ]
    )

    result = await backend.search(
        "current news",
        options={"result_type": "document", "time_range": "day"},
    )

    assert not result.is_error
    assert result.metadata["search_quality"] == "degraded"
    assert result.metadata["freshness_relaxation_failure"] == (
        "1 engine failure(s) during relaxed retry"
    )
    assert result.metadata["engine_failures"] == [["startpage", "timeout"]]


@pytest.mark.asyncio
async def test_search_skips_results_without_url() -> None:
    backend = SearxngBackend(base_url="http://localhost:8888")
    payload = {
        "results": [
            {"title": "no url", "content": "snippet"},
            {"title": "good", "url": "https://example.com", "content": "x"},
        ],
    }
    mock_response = _build_response(payload)

    with patch("cognis.tools.executor.web.backends.searxng.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await backend.search("query")
    assert not result.is_error
    # Only the row with a URL appears.
    assert "good" in result.output
    assert "https://example.com" in result.output
    assert "no url" not in result.output
