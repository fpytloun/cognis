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

    params = mock_client.get.call_args.kwargs["params"]
    assert params["engines"] == "duckduckgo,bing"
    assert params["categories"] == "it"
    assert params["language"] == "fr"
    assert params["time_range"] == "month"
    assert params["safesearch"] == 2
    assert params["pageno"] == 2


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
