"""Unit tests for web tools — fetch, search, crawl, map, research."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cognis.models.tool import ExecutorHandle, ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitState
from cognis.tools.executor.definitions import (
    executor_tool_definitions,
    executor_tool_handlers,
)
from cognis.tools.executor.web.backends import (
    available_backends,
    resolve_fetch_backend,
    resolve_search_backend,
)
from cognis.tools.executor.web.backends.direct import DirectBackend
from cognis.tools.executor.web.backends.tavily import TavilyBackend
from cognis.tools.executor.web.headers import (
    BROWSER_HEADERS,
    clamp_timeout,
    convert_html,
    format_response,
    html_to_text,
    sanitise_url,
    truncate_content,
)
from cognis.tools.registry import ToolExecutionContext

_DUMMY_CONTEXT = ToolExecutionContext(
    executor_handle=ExecutorHandle(
        executor_id="test",
        executor_type="in_process",
    )
)

_CONTEXT_WITH_TAVILY = ToolExecutionContext(
    executor_handle=ExecutorHandle(
        executor_id="test",
        executor_type="in_process",
    ),
    runtime_metadata={
        "web_backend": "tavily",
        "web_secrets": {"tavily_api_key": "tvly-test-key"},
        "web_available_backends": ["direct", "tavily"],
    },
)


class TestHeaders:
    """Test browser header generation and URL handling."""

    def test_browser_headers_has_user_agent(self) -> None:
        assert "User-Agent" in BROWSER_HEADERS
        assert "Chrome" in BROWSER_HEADERS["User-Agent"]

    def test_browser_headers_has_accept(self) -> None:
        assert "Accept" in BROWSER_HEADERS
        assert "text/html" in BROWSER_HEADERS["Accept"]

    def test_browser_headers_has_sec_fetch(self) -> None:
        assert "Sec-Fetch-Dest" in BROWSER_HEADERS
        assert "Sec-Fetch-Mode" in BROWSER_HEADERS

    def test_sanitise_url_adds_https(self) -> None:
        assert sanitise_url("example.com") == "https://example.com"

    def test_sanitise_url_upgrades_http(self) -> None:
        assert sanitise_url("http://example.com") == "https://example.com"

    def test_sanitise_url_keeps_https(self) -> None:
        assert sanitise_url("https://example.com") == "https://example.com"

    def test_clamp_timeout_default(self) -> None:
        assert clamp_timeout(None) == 30

    def test_clamp_timeout_max(self) -> None:
        assert clamp_timeout(999) == 120

    def test_clamp_timeout_min(self) -> None:
        assert clamp_timeout(-5) == 1

    def test_truncate_content_short(self) -> None:
        assert truncate_content("hello", max_size=100) == "hello"

    def test_truncate_content_long(self) -> None:
        result = truncate_content("a" * 1000, max_size=100)
        assert len(result) > 100  # includes notice
        assert "[truncated" in result


class TestHtmlConversion:
    """Test HTML to text/markdown conversion."""

    def test_html_to_text_strips_tags(self) -> None:
        result = html_to_text("<p>Hello <b>world</b></p>")
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_html_to_text_strips_scripts(self) -> None:
        html = '<script>alert("xss")</script><p>Content</p>'
        result = html_to_text(html)
        assert "alert" not in result
        assert "Content" in result

    def test_convert_html_text_format(self) -> None:
        result = convert_html("<p>Hello</p>", "text")
        assert "<" not in result

    def test_convert_html_html_format(self) -> None:
        result = convert_html("<p>Hello</p>", "html")
        assert "<p>" in result

    def test_format_response_non_html(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.headers = {"content-type": "application/json"}
        response.text = '{"key": "value"}'
        result = format_response(response, "markdown")
        assert '{"key": "value"}' in result

    def test_format_response_html(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.headers = {"content-type": "text/html; charset=utf-8"}
        response.text = "<p>Hello world</p>"
        result = format_response(response, "text")
        assert "Hello world" in result
        assert "<" not in result


class TestBackendResolution:
    """Test backend resolution from runtime metadata."""

    def test_default_is_direct(self) -> None:
        backend = resolve_fetch_backend({})
        assert isinstance(backend, DirectBackend)

    def test_direct_explicit(self) -> None:
        backend = resolve_fetch_backend({"web_backend": "direct"})
        assert isinstance(backend, DirectBackend)

    def test_tavily_without_key_falls_back(self) -> None:
        backend = resolve_fetch_backend({"web_backend": "tavily", "web_secrets": {}})
        assert isinstance(backend, DirectBackend)

    def test_tavily_with_key(self) -> None:
        backend = resolve_fetch_backend(
            {"web_backend": "tavily", "web_secrets": {"tavily_api_key": "test"}}
        )
        assert isinstance(backend, TavilyBackend)

    def test_override_takes_precedence(self) -> None:
        backend = resolve_fetch_backend(
            {"web_backend": "direct", "web_secrets": {"tavily_api_key": "test"}},
            backend_override="tavily",
        )
        assert isinstance(backend, TavilyBackend)

    def test_brave_fetch_falls_back_to_direct(self) -> None:
        # Brave has no fetch — should fall back to direct
        backend = resolve_fetch_backend(
            {"web_backend": "brave", "web_secrets": {"brave_api_key": "test"}}
        )
        assert isinstance(backend, DirectBackend)

    def test_search_default_is_direct(self) -> None:
        backend = resolve_search_backend({})
        assert isinstance(backend, DirectBackend)

    def test_available_backends_direct_only(self) -> None:
        result = available_backends({})
        assert result == ["direct"]

    def test_available_backends_with_keys(self) -> None:
        result = available_backends({"web_secrets": {"tavily_api_key": "x", "brave_api_key": "y"}})
        assert "direct" in result
        assert "tavily" in result
        assert "brave" in result


class TestDefinitions:
    """Test that web tool handlers are registered and static tools are correct."""

    def test_web_handlers_registered(self) -> None:
        handlers = executor_tool_handlers()
        for name in ("web_fetch", "web_search", "web_crawl", "web_map", "web_research"):
            assert name in handlers, f"Missing handler for {name}"

    def test_static_executor_tools_exclude_web(self) -> None:
        defs = executor_tool_definitions()
        names = {d.name for d in defs}
        assert "web_fetch" not in names
        assert "web_search" not in names
        assert {"read", "write", "edit", "multiedit", "patch", "grep", "glob", "bash"} <= names


class TestDynamicWebDefinitions:
    """Test dynamic web tool definition generation."""

    def test_direct_only_no_backend_param(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct"])
        names = {d.name for d in defs}
        assert names == {"web_fetch", "web_search"}

        for d in defs:
            props = d.parameters.get("properties", {})
            assert "backend" not in props, f"{d.name} should not have backend param"

    def test_direct_only_no_tavily_params(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct"])
        search = next(d for d in defs if d.name == "web_search")
        props = search.parameters.get("properties", {})
        assert "search_depth" not in props
        assert "include_answer" not in props
        assert "topic" not in props

    def test_tavily_adds_backend_and_params(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct", "tavily"])
        search = next(d for d in defs if d.name == "web_search")
        props = search.parameters.get("properties", {})
        assert "backend" in props
        assert "search_depth" in props
        assert "include_answer" in props
        assert "topic" in props
        backend_enum = props["backend"]["enum"]
        assert "direct" in backend_enum
        assert "tavily" in backend_enum

    def test_tavily_only_tools_included(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs_direct = web_tool_definitions(["direct"])
        defs_tavily = web_tool_definitions(["direct", "tavily"])
        names_direct = {d.name for d in defs_direct}
        names_tavily = {d.name for d in defs_tavily}
        assert "web_crawl" not in names_direct
        assert "web_map" not in names_direct
        assert "web_research" not in names_direct
        assert "web_crawl" in names_tavily
        assert "web_map" in names_tavily
        assert "web_research" in names_tavily

    def test_brave_adds_search_params(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct", "brave"])
        search = next(d for d in defs if d.name == "web_search")
        props = search.parameters.get("properties", {})
        assert "backend" in props
        assert "freshness" in props
        assert "extra_snippets" in props
        assert "safesearch" in props
        # Tavily params should NOT be present
        assert "search_depth" not in props
        assert "include_answer" not in props

    def test_brave_fetch_excludes_brave_from_backend(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct", "brave"])
        fetch = next(d for d in defs if d.name == "web_fetch")
        props = fetch.parameters.get("properties", {})
        # brave has no fetch — should not appear in fetch backend enum
        if "backend" in props:
            assert "brave" not in props["backend"].get("enum", [])

    def test_all_web_tools_are_read_only(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct", "tavily", "brave"])
        for d in defs:
            assert d.read_only, f"{d.name} should be read_only"
            assert not d.non_bypassable, f"{d.name} should not be non_bypassable"

    def test_maximal_set_has_five_tools(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct", "tavily", "brave"])
        assert len(defs) == 5


class TestWebFetchHandler:
    """Test the web_fetch handler."""

    @pytest.mark.asyncio()
    async def test_fetch_no_url(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_fetch

        result = await handle_web_fetch({"url": ""}, _DUMMY_CONTEXT)
        assert result.is_error
        assert "No URL" in result.output

    @pytest.mark.asyncio()
    async def test_fetch_dispatches_to_direct(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_fetch

        with patch("cognis.tools.executor.web.handlers.resolve_fetch_backend") as mock_resolve:
            mock_backend = AsyncMock()
            mock_backend.fetch.return_value = ToolResult(output="test content")
            mock_resolve.return_value = mock_backend

            result = await handle_web_fetch(
                {"url": "https://example.com", "format": "text"}, _DUMMY_CONTEXT
            )
            assert not result.is_error
            assert result.output == "test content"
            mock_backend.fetch.assert_awaited_once()


class TestWebSearchHandler:
    """Test the web_search handler."""

    @pytest.mark.asyncio()
    async def test_search_no_query(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        result = await handle_web_search({"query": ""}, _DUMMY_CONTEXT)
        assert result.is_error
        assert "No search query" in result.output

    @pytest.mark.asyncio()
    async def test_search_dispatches_to_backend(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock()
            mock_backend.search.return_value = ToolResult(output="search results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {"query": "test query", "num_results": 5}, _DUMMY_CONTEXT
            )
            assert not result.is_error
            assert result.output == "search results"
            mock_backend.search.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_search_passes_options(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock()
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "test",
                    "search_depth": "advanced",
                    "topic": "news",
                    "include_answer": True,
                },
                _DUMMY_CONTEXT,
            )
            call_kwargs = mock_backend.search.call_args
            options = call_kwargs.kwargs.get("options", {})
            assert options["search_depth"] == "advanced"
            assert options["topic"] == "news"
            assert options["include_answer"] is True

    @pytest.mark.asyncio()
    async def test_search_omits_empty_optional_strings(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock()
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "Lovosice weather today forecast",
                    "backend": "tavily",
                    "search_depth": "advanced",
                    "topic": "general",
                    "include_answer": True,
                    "time_range": "",
                    "country": "",
                    "include_domains": [],
                    "exclude_domains": [],
                },
                _DUMMY_CONTEXT,
            )
            call_kwargs = mock_backend.search.call_args
            options = call_kwargs.kwargs.get("options", {})
            assert options["search_depth"] == "advanced"
            assert options["topic"] == "general"
            assert options["include_answer"] is True
            assert "time_range" not in options
            assert "country" not in options
            assert "include_domains" not in options
            assert "exclude_domains" not in options

    @pytest.mark.asyncio()
    async def test_search_preserves_explicit_false_flags(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock()
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "FFIV stock price",
                    "backend": "tavily",
                    "search_depth": "advanced",
                    "topic": "finance",
                    "include_answer": False,
                    "time_range": "",
                    "country": "",
                },
                _DUMMY_CONTEXT,
            )
            call_kwargs = mock_backend.search.call_args
            options = call_kwargs.kwargs.get("options", {})
            assert options["include_answer"] is False
            assert "time_range" not in options
            assert "country" not in options


class TestTavilyRequiredTools:
    """Test tools that require Tavily backend."""

    @pytest.mark.asyncio()
    async def test_crawl_without_tavily(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_crawl

        result = await handle_web_crawl({"url": "https://example.com"}, _DUMMY_CONTEXT)
        assert result.is_error
        assert "Tavily" in result.output

    @pytest.mark.asyncio()
    async def test_map_without_tavily(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_map

        result = await handle_web_map({"url": "https://example.com"}, _DUMMY_CONTEXT)
        assert result.is_error
        assert "Tavily" in result.output

    @pytest.mark.asyncio()
    async def test_research_without_tavily(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_research

        result = await handle_web_research({"input": "test topic"}, _DUMMY_CONTEXT)
        assert result.is_error
        assert "Tavily" in result.output

    @pytest.mark.asyncio()
    async def test_research_no_query(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_research

        result = await handle_web_research({"input": ""}, _CONTEXT_WITH_TAVILY)
        assert result.is_error
        assert "No research query" in result.output


class TestDirectBackend:
    """Test the direct httpx backend."""

    @pytest.mark.asyncio()
    async def test_fetch_success(self) -> None:
        backend = DirectBackend()
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.headers = {"content-type": "text/html"}
        mock_response.text = "<p>Hello</p>"
        mock_response.status_code = 200

        with patch("cognis.tools.executor.web.backends.direct._fetch_breaker") as mock_breaker:
            mock_breaker.call = AsyncMock(return_value=mock_response)
            result = await backend.fetch("https://example.com", output_format="text")
            assert not result.is_error
            assert "Hello" in result.output

    @pytest.mark.asyncio()
    async def test_fetch_error_result(self) -> None:
        backend = DirectBackend()
        error_result = ToolResult(output="Request timed out", is_error=True)

        with patch("cognis.tools.executor.web.backends.direct._fetch_breaker") as mock_breaker:
            mock_breaker.call = AsyncMock(return_value=error_result)
            result = await backend.fetch("https://example.com")
            assert result.is_error


class TestTavilyBackend:
    """Test the Tavily backend."""

    @pytest.mark.asyncio()
    async def test_search_formats_results(self) -> None:
        from cognis.tools.executor.web.backends.tavily import _format_tavily_search

        data = {
            "answer": "Test answer",
            "results": [
                {"title": "Result 1", "url": "https://example.com", "content": "Content 1"},
            ],
        }
        result = _format_tavily_search(data)
        assert not result.is_error
        assert "Test answer" in result.output
        assert "Result 1" in result.output
        assert "https://example.com" in result.output

    @pytest.mark.asyncio()
    async def test_search_no_results(self) -> None:
        from cognis.tools.executor.web.backends.tavily import _format_tavily_search

        result = _format_tavily_search({"results": []})
        assert "No search results" in result.output

    @pytest.mark.asyncio()
    async def test_http_400_does_not_open_circuit_breaker(self) -> None:
        from cognis.tools.executor.web.backends import tavily as tavily_module

        backend = TavilyBackend(api_key="test")
        request = httpx.Request("POST", "https://api.tavily.com/search")
        response = httpx.Response(400, request=request, json={"detail": {"error": "Bad request"}})
        error = httpx.HTTPStatusError("bad request", request=request, response=response)
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=30,
            should_trip=tavily_module._should_trip_tavily,
        )

        with (
            patch.object(tavily_module, "_breaker", breaker),
            patch.object(backend, "_post", AsyncMock(side_effect=error)),
        ):
            result = await backend.search("test")

        assert result.is_error
        assert "HTTP 400" in result.output
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failures == 0


class TestBraveBackend:
    """Test the Brave backend."""

    @pytest.mark.asyncio()
    async def test_search_formats_results(self) -> None:
        from cognis.tools.executor.web.backends.brave import _format_brave_results

        data = {
            "web": {
                "results": [
                    {
                        "title": "Brave Result",
                        "url": "https://example.com",
                        "description": "A description",
                    },
                ],
            },
        }
        result = _format_brave_results(data)
        assert not result.is_error
        assert "Brave Result" in result.output
        assert "https://example.com" in result.output

    @pytest.mark.asyncio()
    async def test_search_no_results(self) -> None:
        from cognis.tools.executor.web.backends.brave import _format_brave_results

        result = _format_brave_results({"web": {"results": []}})
        assert "No search results" in result.output

    @pytest.mark.asyncio()
    async def test_search_no_query(self) -> None:
        from cognis.tools.executor.web.backends.brave import BraveBackend

        backend = BraveBackend(api_key="test")
        result = await backend.search("")
        assert result.is_error


class TestRetryLogic:
    """Test the retry and error handling in fetch_with_retry."""

    @pytest.mark.asyncio()
    async def test_429_returns_error_after_retries(self) -> None:
        from cognis.tools.executor.web.headers import fetch_with_retry

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 429
        mock_response.headers = {}

        with patch("cognis.tools.executor.web.headers.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await fetch_with_retry("https://example.com", max_retries=2, timeout=5)
            assert isinstance(result, ToolResult)
            assert result.is_error
            assert "429" in result.output

    @pytest.mark.asyncio()
    async def test_cloudflare_403_returns_actionable_error(self) -> None:
        from cognis.tools.executor.web.headers import fetch_with_retry

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 403
        mock_response.headers = {"cf-mitigated": "challenge"}
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Forbidden",
            request=MagicMock(),
            response=mock_response,
        )

        with patch("cognis.tools.executor.web.headers.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await fetch_with_retry("https://example.com", max_retries=1)
            assert isinstance(result, ToolResult)
            assert result.is_error
            assert "Cloudflare" in result.output
            assert "tavily" in result.output.lower()


class TestSettingsSchema:
    """Test web backend settings validation."""

    def test_valid_backends(self) -> None:
        from cognis.settings_schema import validate_setting_value

        for backend in ("direct", "tavily", "brave"):
            validate_setting_value("web.backend", backend)

    def test_invalid_backend_rejected(self) -> None:
        from cognis.settings_schema import validate_setting_value

        with pytest.raises(ValueError, match="must be one of"):
            validate_setting_value("web.backend", "grok")

    def test_web_backend_in_default_settings(self) -> None:
        from cognis.bootstrap import DEFAULT_SETTINGS

        assert "web.backend" in DEFAULT_SETTINGS
        category, default = DEFAULT_SETTINGS["web.backend"]
        assert category == "web"
        assert default == "direct"
