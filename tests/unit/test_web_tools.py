"""Unit tests for web tools — fetch, search, crawl, map, research."""

from __future__ import annotations

import base64
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

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
from cognis.tools.executor.web.backends.direct import (
    DirectBackend,
    _ddg_search,
    _request_error_result,
)
from cognis.tools.executor.web.backends.tavily import TavilyBackend
from cognis.tools.executor.web.handlers import (
    _collect_optional_options,
    _concurrency_controller,
    _result_is_browser_fallback_candidate,
)
from cognis.tools.executor.web.headers import (
    BROWSER_HEADERS,
    clamp_timeout,
    convert_html,
    format_response,
    format_response_result,
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


def test_web_concurrency_controller_is_shared_across_per_call_metadata() -> None:
    shared: dict[str, object] = {"web_concurrency": {"backend_caps": {"direct": 2}}}
    first = ToolExecutionContext(
        executor_handle=_DUMMY_CONTEXT.executor_handle,
        runtime_metadata={**shared},
        shared_runtime_metadata=shared,
    )
    second = ToolExecutionContext(
        executor_handle=_DUMMY_CONTEXT.executor_handle,
        runtime_metadata={**shared},
        shared_runtime_metadata=shared,
    )

    first_controller = _concurrency_controller(first)
    second_controller = _concurrency_controller(second)

    assert first_controller is second_controller
    assert first_controller.settings.cap_for("direct") == 2


def test_controller_web_metrics_preserve_each_retry_failure_category() -> None:
    from cognis.core import agent_loop

    execution_child = MagicMock()
    retry_child = MagicMock()
    execution_counter = MagicMock()
    retry_counter = MagicMock()
    search_outcome_counter = MagicMock()
    execution_counter.labels.return_value = execution_child
    retry_counter.labels.return_value = retry_child
    result = ToolResult(
        output="recovered",
        metadata={
            "backend": "direct",
            "attempts": 3,
            "retry_failure_categories": ["timeout", "rate_limited"],
        },
    )

    with (
        patch.object(agent_loop, "WEB_TOOL_EXECUTIONS", execution_counter),
        patch.object(agent_loop, "WEB_SEARCH_RETRIES", retry_counter),
        patch.object(agent_loop, "WEB_SEARCH_OUTCOMES", search_outcome_counter),
    ):
        agent_loop._observe_web_tool_execution("web_search", result, "success")

    execution_counter.labels.assert_called_once_with(
        tool_name="web_search",
        backend="direct",
        outcome="success",
        failure_category="none",
        browser_fallback="false",
    )
    assert retry_counter.labels.call_args_list == [
        call(backend="direct", failure_category="timeout"),
        call(backend="direct", failure_category="rate_limited"),
    ]
    assert retry_child.inc.call_count == 2
    search_outcome_counter.labels.assert_called_once_with(
        backend="direct",
        outcome="healthy_results",
    )


def test_any_time_range_is_omitted_from_backend_options() -> None:
    assert _collect_optional_options(
        {"time_range": "any", "result_type": "repository"},
        ("time_range", "result_type"),
    ) == {"result_type": "repository"}


def test_controller_web_search_metrics_distinguish_quality_and_empty_results() -> None:
    from cognis.core import agent_loop

    execution_counter = MagicMock()
    search_outcome_counter = MagicMock()
    with (
        patch.object(agent_loop, "WEB_TOOL_EXECUTIONS", execution_counter),
        patch.object(agent_loop, "WEB_SEARCH_OUTCOMES", search_outcome_counter),
    ):
        agent_loop._observe_web_tool_execution(
            "web_search",
            ToolResult(
                output="No search results found.",
                metadata={
                    "backend": "searxng",
                    "search_quality": "healthy",
                    "returned_result_count": 0,
                },
            ),
            "success",
        )
        agent_loop._observe_web_tool_execution(
            "web_search",
            ToolResult(
                output="[[result:1]]\n[1] recovered",
                metadata={
                    "backend": "searxng",
                    "search_quality": "degraded",
                    "returned_result_count": 1,
                },
            ),
            "success",
        )
        agent_loop._observe_web_tool_execution(
            "web_search",
            ToolResult(output="network error", is_error=True, metadata={"backend": "searxng"}),
            "error",
        )

    assert search_outcome_counter.labels.call_args_list == [
        call(backend="searxng", outcome="healthy_empty"),
        call(backend="searxng", outcome="degraded_results"),
        call(backend="searxng", outcome="failed"),
    ]


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

    def test_format_response_result_text_plain_preserves_text(self) -> None:
        response = httpx.Response(
            200,
            content=b"plain text body",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://example.com/readme.txt"),
        )

        result = format_response_result(response, "markdown")

        assert result.output == "plain text body"
        assert not result.attachments
        assert (result.metadata or {}).get("content_type") == "text/plain"

    def test_format_response_result_verification_page_is_error(self) -> None:
        response = httpx.Response(
            200,
            content=(
                b"<html><head><title>Reddit - Please wait for verification</title></head>"
                b"<body></body></html>"
            ),
            headers={"content-type": "text/html"},
            request=httpx.Request(
                "GET",
                "https://www.reddit.com/r/quails/comments/t8r4dd/example/",
            ),
        )

        result = format_response_result(response, "markdown")

        assert result.is_error
        assert "requires verification" in result.output
        assert (result.metadata or {}).get("direct_fetch_blocked") is True
        assert (result.metadata or {}).get("direct_fetch_block_signal") == "verification"

    def test_format_response_result_preserves_redirect_provenance(self) -> None:
        response = httpx.Response(
            200,
            content=(
                b"<html><title>Redirected article</title><body><article><p>"
                b"This is substantive article content with enough words to represent a real "
                b"body rather than a title or navigation shell.</p></article></body></html>"
            ),
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/final"),
        )

        result = format_response_result(
            response,
            "markdown",
            requested_url="https://example.com/start",
            source_url="https://example.com/final",
        )

        metadata = result.metadata or {}
        assert metadata["requested_url"] == "https://example.com/start"
        assert metadata["fetched_url"] == "https://example.com/final"
        document = metadata["extracted_document"]
        assert document["url_provenance"]["requested_url"] == "https://example.com/start"

    def test_format_response_result_provider_error_page_is_error(self) -> None:
        response = httpx.Response(
            200,
            content=(
                b"<html><head><title>Error Page | eBay</title></head>"
                b"<body><h1>SORRY</h1><p>Something went wrong on our end.</p>"
                b"<p>Please go back and try again or go to eBay Homepage.</p></body></html>"
            ),
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://www.ebay.com/itm/257616988765"),
        )

        result = format_response_result(response, "markdown")

        assert result.is_error
        assert "provider-generated error page" in result.output
        assert (result.metadata or {}).get("direct_fetch_blocked") is True
        assert (result.metadata or {}).get("direct_fetch_block_signal") == "provider_error_page"

    def test_format_response_result_image_attaches_binary_without_dumping_bytes(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        response = httpx.Response(
            200,
            content=image_bytes,
            headers={"content-type": "image/png"},
            request=httpx.Request("GET", "https://example.com/chart.png"),
        )

        result = format_response_result(response, "markdown")

        assert "Binary content: attached as artifact" in result.output
        assert "Use artifact_read" in result.output
        assert "\x00" not in result.output
        assert result.attachments and result.attachments[0]["filename"] == "chart.png"
        assert result.attachments[0]["mime_type"] == "image/png"
        assert base64.b64decode(str(result.attachments[0]["content_b64"])) == image_bytes
        assert (result.metadata or {}).get("binary_kind") == "image"

    def test_format_response_result_octet_stream_attaches_binary(self) -> None:
        payload = b"\x00\x01\x02binary"
        response = httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.com/file.bin"),
        )

        result = format_response_result(response, "markdown")

        assert "Binary kind: binary" in result.output
        assert result.attachments and result.attachments[0]["filename"] == "file.bin"
        assert (result.metadata or {}).get("binary_content") is True

    def test_format_response_result_pdf_extracts_text_and_attaches_original(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakePage:
            def __init__(self, text: str) -> None:
                self._text = text

            def extract_text(self) -> str:
                return self._text

        class _FakeReader:
            metadata = {"/Title": "Mini EX Manual", "/Author": "Brinsea"}
            pages = [_FakePage("Page one text"), _FakePage("Page two text")]

            def __init__(self, stream: object) -> None:
                self.stream = stream

        monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
        pdf_bytes = b"%PDF-1.5 fake content"
        response = httpx.Response(
            200,
            content=pdf_bytes,
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", "https://example.com/Mini-EX_GB.pdf"),
        )

        result = format_response_result(response, "markdown")

        assert "[[metadata]]" in result.output
        assert "Pages: 2" in result.output
        assert "[[page:1]]" in result.output
        assert "Page one text" in result.output
        assert result.attachments and result.attachments[0]["filename"] == "Mini-EX_GB.pdf"
        assert result.attachments[0]["mime_type"] == "application/pdf"
        assert (result.metadata or {}).get("binary_kind") == "pdf"

    def test_format_response_result_pdf_magic_overrides_missing_mime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeReader:
            metadata = {}
            pages: list[object] = []

            def __init__(self, stream: object) -> None:
                self.stream = stream

        monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
        response = httpx.Response(
            200,
            content=b"%PDF-1.7 fake content",
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.com/download"),
        )

        result = format_response_result(response, "markdown")

        assert "Original PDF: attached as artifact" in result.output
        assert result.attachments and result.attachments[0]["mime_type"] == "application/pdf"
        assert (result.metadata or {}).get("binary_kind") == "pdf"


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
        assert {
            "read",
            "write",
            "edit",
            "multiedit",
            "apply_patch",
            "grep",
            "glob",
            "bash",
        } <= names


class TestDynamicWebDefinitions:
    """Test dynamic web tool definition generation."""

    def test_direct_only_no_backend_param(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        # web_crawl and web_map are now always available (W3 makes them
        # backend-agnostic). web_research stays Tavily-only.
        defs = web_tool_definitions(["direct"])
        names = {d.name for d in defs}
        assert names == {"web_fetch", "web_search", "web_crawl", "web_map"}

        # No backend selector is rendered when only one backend is configured.
        fetch = next(d for d in defs if d.name == "web_fetch")
        search = next(d for d in defs if d.name == "web_search")
        assert "backend" not in fetch.parameters.get("properties", {})
        assert "backend" not in search.parameters.get("properties", {})

    def test_direct_only_no_tavily_params(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(["direct"])
        search = next(d for d in defs if d.name == "web_search")
        props = search.parameters.get("properties", {})
        assert "search_depth" not in props
        assert "include_answer" not in props
        assert "topic" not in props
        assert props["include_images"]["type"] == "boolean"
        assert props["image_limit"]["maximum"] == 50
        assert props["time_range"]["enum"] == ["any", "day", "week", "month", "year"]
        assert "default: any" in props["time_range"]["description"]
        assert props["search_mode"]["enum"] == ["web", "news", "images", "videos"]
        assert props["result_type"]["enum"] == [
            "paper",
            "repository",
            "discussion",
            "document",
        ]

    def test_tavily_adds_params_without_backend_override(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(
            ["direct", "tavily"],
            default_backend="tavily",
            available_search_backends=["direct", "tavily"],
            available_fetch_backends=["direct", "tavily"],
        )
        search = next(d for d in defs if d.name == "web_search")
        props = search.parameters.get("properties", {})
        assert "backend" not in props
        assert "search_depth" in props
        assert "include_answer" in props
        assert "include_domains" in props
        assert "exclude_domains" in props
        assert "include_raw_content" in props
        assert "chunks_per_source" in props
        assert "start_date" in props
        assert "end_date" in props
        assert "topic" in props
        assert "using Tavily" in search.description

    def test_fetch_uses_configured_default_backend_in_description(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(
            ["direct", "tavily"],
            default_backend="tavily",
            available_search_backends=["direct", "tavily"],
            available_fetch_backends=["direct", "tavily"],
            default_fetch_backend="tavily",
        )
        fetch = next(d for d in defs if d.name == "web_fetch")
        props = fetch.parameters.get("properties", {})
        assert "backend" not in props
        assert "configured tavily fetch backend" in fetch.description

    def test_invalid_default_backend_falls_back_to_first_available(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(
            ["direct", "tavily"],
            default_backend="brave",
            available_search_backends=["direct", "tavily"],
            available_fetch_backends=["direct", "tavily"],
        )
        fetch = next(d for d in defs if d.name == "web_fetch")
        search = next(d for d in defs if d.name == "web_search")
        fetch_props = fetch.parameters.get("properties", {})
        search_props = search.parameters.get("properties", {})
        # When the supplied default isn't in either axis, we land on the
        # first axis-relevant option (direct here).
        assert "backend" not in fetch_props
        assert "backend" not in search_props
        assert "configured direct fetch backend" in fetch.description
        assert "using DuckDuckGo" in search.description

    def test_fetch_brave_default_falls_back_to_direct_description(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        # Brave is search-only; supplied as a search backend, fetch only has
        # 'direct' available, so no backend selector renders for fetch.
        defs = web_tool_definitions(
            ["direct", "brave"],
            default_backend="brave",
            available_search_backends=["direct", "brave"],
            available_fetch_backends=["direct"],
        )
        fetch = next(d for d in defs if d.name == "web_fetch")
        props = fetch.parameters.get("properties", {})
        assert "backend" not in props

    def test_tavily_only_tools_included(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs_direct = web_tool_definitions(["direct"])
        defs_tavily = web_tool_definitions(
            ["direct", "tavily"],
            default_backend="tavily",
            available_search_backends=["direct", "tavily"],
            available_fetch_backends=["direct", "tavily"],
        )
        names_direct = {d.name for d in defs_direct}
        names_tavily = {d.name for d in defs_tavily}
        # Crawl/map are now always available; only research is Tavily-gated.
        assert "web_crawl" in names_direct
        assert "web_map" in names_direct
        assert "web_research" not in names_direct
        assert "web_crawl" in names_tavily
        assert "web_map" in names_tavily
        assert "web_research" in names_tavily

    def test_brave_adds_search_params(self) -> None:
        from cognis.tools.executor.web.definitions import web_tool_definitions

        defs = web_tool_definitions(
            ["direct", "brave"],
            default_search_backend="brave",
        )
        search = next(d for d in defs if d.name == "web_search")
        props = search.parameters.get("properties", {})
        assert "backend" not in props
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
            assert "test content" in result.output
            assert "[[page:1]]" in result.output
            assert (result.metadata or {}).get("stored_output")
            assert (result.metadata or {}).get("output_anchors")
            mock_backend.fetch.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_fetch_omitted_backend_uses_runtime_default(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_fetch

        with patch("cognis.tools.executor.web.handlers.resolve_fetch_backend") as mock_resolve:
            mock_backend = AsyncMock()
            mock_backend.fetch.return_value = ToolResult(output="test content")
            mock_resolve.return_value = mock_backend

            result = await handle_web_fetch(
                {"url": "https://example.com", "format": "markdown"},
                _CONTEXT_WITH_TAVILY,
            )

            assert not result.is_error
            mock_resolve.assert_called_once_with(_CONTEXT_WITH_TAVILY.runtime_metadata)
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
    async def test_search_omitted_backend_uses_runtime_default(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="search results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search({"query": "test query"}, _CONTEXT_WITH_TAVILY)

            assert not result.is_error
            mock_resolve.assert_called_once_with(_CONTEXT_WITH_TAVILY.runtime_metadata)
            mock_backend.search.assert_awaited_once()
            assert result.metadata["tavily_query_normalized"] is False

    @pytest.mark.asyncio()
    async def test_search_ignores_stale_backend_override(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="search results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {"query": "test query", "backend": "direct"},
                _CONTEXT_WITH_TAVILY,
            )

            assert not result.is_error
            mock_resolve.assert_called_once_with(_CONTEXT_WITH_TAVILY.runtime_metadata)

    @pytest.mark.asyncio()
    async def test_search_passes_options(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "test",
                    "backend": "tavily",
                    "search_depth": "advanced",
                    "topic": "news",
                    "include_answer": "advanced",
                    "include_domains": ["example.com"],
                    "chunks_per_source": 2,
                },
                _DUMMY_CONTEXT,
            )
            call_kwargs = mock_backend.search.call_args
            options = call_kwargs.kwargs.get("options", {})
            assert options["search_depth"] == "advanced"
            assert options["topic"] == "news"
            assert options["include_answer"] == "advanced"
            assert options["include_domains"] == ["example.com"]
            assert options["chunks_per_source"] == 2

    @pytest.mark.asyncio()
    async def test_search_omits_empty_optional_strings(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
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
            mock_backend = AsyncMock(spec=TavilyBackend)
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

    @pytest.mark.asyncio()
    async def test_search_drops_country_for_non_general_tavily_topics(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "Czech politics today",
                    "backend": "tavily",
                    "topic": "news",
                    "country": "Czech Republic",
                },
                _DUMMY_CONTEXT,
            )

            call_kwargs = mock_backend.search.call_args
            options = call_kwargs.kwargs.get("options", {})
            assert options["topic"] == "news"
            assert "country" not in options

    @pytest.mark.asyncio()
    async def test_search_normalizes_general_tavily_country_and_dates(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "US AI news",
                    "backend": "tavily",
                    "topic": "general",
                    "country": "United States",
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-16",
                },
                _DUMMY_CONTEXT,
            )

            call_kwargs = mock_backend.search.call_args
            options = call_kwargs.kwargs.get("options", {})
            assert options["topic"] == "general"
            assert options["country"] == "united states"
            assert options["start_date"] == "2026-04-01"
            assert options["end_date"] == "2026-04-16"

    @pytest.mark.asyncio()
    async def test_search_drops_chunks_when_tavily_depth_is_not_advanced(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "test",
                    "backend": "tavily",
                    "search_depth": "basic",
                    "chunks_per_source": 3,
                },
                _DUMMY_CONTEXT,
            )

            call_kwargs = mock_backend.search.call_args
            options = call_kwargs.kwargs.get("options", {})
            assert options["search_depth"] == "basic"
            assert "chunks_per_source" not in options

    @pytest.mark.asyncio()
    async def test_search_normalizes_tavily_site_filters_into_include_domains(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "site:example.com OR site:news.example.org policy economy",
                    "backend": "tavily",
                },
                _DUMMY_CONTEXT,
            )

            call_args = mock_backend.search.call_args
            assert call_args.args[0] == "policy economy"
            options = call_args.kwargs.get("options", {})
            assert options["include_domains"] == ["example.com", "news.example.org"]

    @pytest.mark.asyncio()
    async def test_search_merges_explicit_include_domains_with_normalized_sites(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "site:example.com security",
                    "backend": "tavily",
                    "include_domains": ["api.example.net", "example.com"],
                },
                _DUMMY_CONTEXT,
            )

            call_args = mock_backend.search.call_args
            options = call_args.kwargs.get("options", {})
            assert options["include_domains"] == ["api.example.net", "example.com"]

    @pytest.mark.asyncio()
    async def test_search_accepts_string_include_domains_for_tavily(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            await handle_web_search(
                {
                    "query": "compliance",
                    "backend": "tavily",
                    "include_domains": "example.com",
                },
                _DUMMY_CONTEXT,
            )

            call_args = mock_backend.search.call_args
            options = call_args.kwargs.get("options", {})
            assert options["include_domains"] == ["example.com"]

    @pytest.mark.asyncio()
    async def test_search_retries_empty_tavily_result_with_simpler_query(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.side_effect = [
                ToolResult(output="No search results found."),
                ToolResult(output="results", metadata={"source": "retry"}),
            ]
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {
                    "query": "site:finance.example.com quote ABC=XYZ",
                    "backend": "tavily",
                    "topic": "finance",
                },
                _DUMMY_CONTEXT,
            )

            assert not result.is_error
            assert result.output == "results"
            assert mock_backend.search.await_count == 2
            first_call = mock_backend.search.await_args_list[0]
            second_call = mock_backend.search.await_args_list[1]
            assert first_call.args[0] == "quote ABC=XYZ"
            assert second_call.args[0] == "ABC=XYZ"
            assert second_call.kwargs["options"]["exact_match"] is True
            assert result.metadata["tavily_retry_attempted"] is True
            assert result.metadata["tavily_retry_reason"] == "empty_results"

    @pytest.mark.asyncio()
    async def test_search_adds_tavily_metadata_without_retry(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {
                    "query": "site:example.com compliance",
                    "backend": "tavily",
                },
                _DUMMY_CONTEXT,
            )

            assert result.metadata["tavily_query_normalized"] is True
            assert result.metadata["tavily_retry_attempted"] is False

    @pytest.mark.asyncio()
    async def test_search_does_not_normalize_when_resolved_backend_is_not_tavily(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock()
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {
                    "query": "site:example.com compliance",
                    "backend": "tavily",
                },
                _DUMMY_CONTEXT,
            )

            call_args = mock_backend.search.call_args
            assert call_args.args[0] == "site:example.com compliance"
            assert result.metadata == {"attempts": 1, "backend": "direct"}

    @pytest.mark.asyncio()
    async def test_search_does_not_rewrite_complex_boolean_tavily_query(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {
                    "query": "site:example.com AND security OR resilience",
                    "backend": "tavily",
                },
                _DUMMY_CONTEXT,
            )

            call_args = mock_backend.search.call_args
            assert call_args.args[0] == "site:example.com AND security OR resilience"
            assert result.metadata["tavily_query_normalized"] is False

    @pytest.mark.asyncio()
    async def test_search_does_not_rewrite_mixed_or_query(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {
                    "query": "site:example.com OR apple",
                    "backend": "tavily",
                },
                _DUMMY_CONTEXT,
            )

            call_args = mock_backend.search.call_args
            assert call_args.args[0] == "site:example.com OR apple"
            assert result.metadata["tavily_query_normalized"] is False

    @pytest.mark.asyncio()
    async def test_search_does_not_lift_path_qualified_site_filter(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_backend = AsyncMock(spec=TavilyBackend)
            mock_backend.search.return_value = ToolResult(output="results")
            mock_resolve.return_value = mock_backend

            result = await handle_web_search(
                {
                    "query": "site:example.com/markets currency update",
                    "backend": "tavily",
                },
                _DUMMY_CONTEXT,
            )

            call_args = mock_backend.search.call_args
            assert call_args.args[0] == "site:example.com/markets currency update"
            options = call_args.kwargs.get("options") or {}
            assert "include_domains" not in options
            assert result.metadata["tavily_query_normalized"] is False

    @pytest.mark.asyncio()
    async def test_search_rejects_invalid_tavily_date(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_resolve.return_value = AsyncMock(spec=TavilyBackend)

            result = await handle_web_search(
                {
                    "query": "test",
                    "backend": "tavily",
                    "start_date": "16-04-2026",
                },
                _DUMMY_CONTEXT,
            )

        assert result.is_error
        assert "start_date" in result.output

    @pytest.mark.asyncio()
    async def test_search_rejects_impossible_tavily_date(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        with patch("cognis.tools.executor.web.handlers.resolve_search_backend") as mock_resolve:
            mock_resolve.return_value = AsyncMock(spec=TavilyBackend)

            result = await handle_web_search(
                {
                    "query": "test",
                    "backend": "tavily",
                    "start_date": "2026-13-40",
                },
                _DUMMY_CONTEXT,
            )

        assert result.is_error
        assert "real calendar date" in result.output

    def test_tool_output_descriptions_guide_recovery(self) -> None:
        from cognis.tools.builtin.tool_output import (
            LIST_TOOL_OUTPUT_ANCHORS,
            READ_TOOL_OUTPUT,
            READ_TOOL_OUTPUT_ANCHOR,
            SEARCH_TOOL_OUTPUT,
        )

        assert "truncated or cleared from context" in READ_TOOL_OUTPUT.description
        assert (
            "prefer list_tool_output_anchors and read_tool_output_anchor first"
            in READ_TOOL_OUTPUT.description
        )
        assert "Use this before read_tool_output" in SEARCH_TOOL_OUTPUT.description
        assert "structured sections" in LIST_TOOL_OUTPUT_ANCHORS.description
        assert "one section" in READ_TOOL_OUTPUT_ANCHOR.description


class TestTavilyRequiredTools:
    """Test tools that require Tavily backend."""

    @pytest.mark.asyncio()
    async def test_crawl_without_tavily_uses_diy_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without Tavily configured, web_crawl falls through to the in-tree
        # DIY crawler (W3). It must not raise a "Tavily required" error.
        from cognis.models.tool import ToolResult
        from cognis.tools.executor.web.handlers import handle_web_crawl

        async def _fake_crawl(**kwargs: object) -> ToolResult:
            return ToolResult(output="diy ok")

        monkeypatch.setattr("cognis.tools.executor.web.crawler.crawl_site", _fake_crawl)
        result = await handle_web_crawl({"url": "https://example.com"}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "Tavily" not in result.output

    @pytest.mark.asyncio()
    async def test_map_without_tavily_uses_sitemap_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without Tavily configured, web_map falls through to the in-tree
        # direct mapper (W3). It must not raise a "Tavily required" error.
        from cognis.tools.executor.web.handlers import handle_web_map

        async def _fake_map_site_urls(
            url: str, *, options: dict[str, object] | None = None
        ) -> tuple[list[str], str]:
            del options
            return ["https://example.com/a"], "sitemap"

        monkeypatch.setattr(
            "cognis.tools.executor.web.sitemap.map_site_urls",
            _fake_map_site_urls,
        )
        result = await handle_web_map({"url": "https://example.com"}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "Tavily" not in result.output

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

        with patch("cognis.tools.executor.web.backends.direct._origin_breaker") as breaker_factory:
            mock_breaker = MagicMock()
            mock_breaker.call = AsyncMock(return_value=mock_response)
            breaker_factory.return_value = mock_breaker
            result = await backend.fetch("https://example.com", output_format="text")
            assert not result.is_error
            assert "Hello" in result.output

    @pytest.mark.asyncio()
    async def test_fetch_reddit_thread_warms_old_reddit_json_adapter(self) -> None:
        backend = DirectBackend()
        reddit_json = [
            {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t3",
                            "data": {
                                "title": "Brooder heat plate size for coturnix?",
                                "selftext": "How many coturnix chicks fit under a heat plate?",
                                "author": "SpicySnails",
                                "subreddit_name_prefixed": "r/quails",
                                "created_utc": 1646665322,
                                "permalink": "/r/quails/comments/t8r4dd/brooder_heat_plate_size_for_coturnix/",
                            },
                        }
                    ]
                },
            },
            {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t1",
                            "data": {
                                "author": "quail_keeper",
                                "body": "A 12x12 plate should work for a small hatch.",
                            },
                        }
                    ]
                },
            },
        ]

        warmup_request = httpx.Request(
            "GET",
            "https://old.reddit.com/r/quails/comments/t8r4dd/brooder_heat_plate_size_for_coturnix/",
        )
        json_request = httpx.Request(
            "GET",
            "https://old.reddit.com/r/quails/comments/t8r4dd/brooder_heat_plate_size_for_coturnix/.json?raw_json=1",
        )
        warmup_response = httpx.Response(
            200,
            request=warmup_request,
            text="<html><title>old reddit</title></html>",
        )
        response = httpx.Response(200, request=json_request, json=reddit_json)

        with patch("cognis.tools.executor.web.backends.reddit.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.get.side_effect = [warmup_response, response]
            client_cls.return_value = client

            result = await backend.fetch(
                "https://www.reddit.com/r/quails/comments/t8r4dd/brooder_heat_plate_size_for_coturnix/"
            )

        assert not result.is_error
        assert "# Brooder heat plate size for coturnix?" in result.output
        assert "How many coturnix chicks" in result.output
        assert "A 12x12 plate should work" in result.output
        document = (result.metadata or {}).get("extracted_document")
        assert isinstance(document, dict)
        assert document.get("extractor") == "reddit_json"
        assert (result.metadata or {}).get("reddit_adapter") is True
        assert (result.metadata or {}).get("reddit_warmup_url") == str(warmup_request.url)
        assert (result.metadata or {}).get("reddit_json_url") == str(json_request.url)
        assert [str(call.args[0]) for call in client.get.await_args_list] == [
            str(warmup_request.url),
            str(json_request.url),
        ]

    @pytest.mark.asyncio()
    async def test_fetch_error_result(self) -> None:
        backend = DirectBackend()
        error_result = ToolResult(output="Request timed out", is_error=True)

        with patch("cognis.tools.executor.web.backends.direct._origin_breaker") as breaker_factory:
            mock_breaker = MagicMock()
            mock_breaker.call = AsyncMock(return_value=error_result)
            breaker_factory.return_value = mock_breaker
            result = await backend.fetch("https://example.com")
            assert result.is_error

    @pytest.mark.parametrize(
        ("message", "category", "output_fragment"),
        [
            (
                "[Errno -3] Temporary failure in name resolution",
                "dns_resolution_failed",
                "Could not resolve host historypark.cz",
            ),
            (
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "Hostname mismatch, certificate is not valid for 'www.majalandpraha.cz'",
                "tls_certificate_invalid",
                "TLS certificate validation failed for www.majalandpraha.cz",
            ),
        ],
    )
    def test_terminal_transport_failures_are_precise_and_skip_browser(
        self,
        message: str,
        category: str,
        output_fragment: str,
    ) -> None:
        request = httpx.Request("GET", f"https://{output_fragment.split()[-1]}/")
        error = httpx.ConnectError(message, request=request)
        result = _request_error_result(error, url=str(request.url), timeout=30)

        assert result.is_error
        assert output_fragment in result.output
        assert result.metadata["failure_category"] == category
        assert result.metadata["browser_fallback_recommended"] is False
        assert _result_is_browser_fallback_candidate(result) is False

    def test_generic_network_failure_still_recommends_browser(self) -> None:
        request = httpx.Request("GET", "https://example.com/")
        error = httpx.ConnectError("connection reset by peer", request=request)
        result = _request_error_result(error, url=str(request.url), timeout=30)

        assert result.metadata["failure_category"] == "network_error"
        assert result.metadata["browser_fallback_recommended"] is True
        assert _result_is_browser_fallback_candidate(result) is True

    @pytest.mark.asyncio()
    async def test_repeated_dns_failures_do_not_open_origin_breaker(self) -> None:
        from cognis.tools.executor.web.backends import direct

        direct._fetch_breakers.clear()
        request = httpx.Request("GET", "https://historypark.cz/")
        error = httpx.ConnectError(
            "[Errno -3] Temporary failure in name resolution",
            request=request,
        )
        with patch(
            "cognis.tools.executor.web.backends.direct.fetch_with_retry",
            new=AsyncMock(side_effect=error),
        ):
            for _ in range(8):
                result = await DirectBackend().fetch(str(request.url))
                assert result.metadata["failure_category"] == "dns_resolution_failed"

        breaker = direct._origin_breaker(str(request.url))
        assert breaker.failures == 0
        assert breaker.state.value == "closed"

    @pytest.mark.asyncio()
    async def test_search_defaults_to_us_en_region(self) -> None:
        backend = DirectBackend()

        async def _call(fn):
            return await fn()

        with (
            patch("cognis.tools.executor.web.backends.direct._search_breaker") as mock_breaker,
            patch(
                "cognis.tools.executor.web.backends.direct._ddg_search",
                new=AsyncMock(return_value=ToolResult(output="ok")),
            ) as mock_ddg_search,
        ):
            mock_breaker.call = AsyncMock(side_effect=_call)

            result = await backend.search("test query")

        assert not result.is_error
        mock_ddg_search.assert_awaited_once_with(
            "test query",
            max_results=8,
            region="us-en",
            safesearch="moderate",
            timelimit=None,
            include_images=False,
            mode="web",
            options={},
            preferred_type=None,
            image_limit=10,
        )
        assert result.metadata["backend"] == "direct"
        assert result.metadata["provider"] == "duckduckgo"
        assert result.metadata["requested_search_mode"] == "web"

    @pytest.mark.asyncio()
    async def test_image_search_mode_selects_direct_image_search(self) -> None:
        backend = DirectBackend()

        async def _call(fn):
            return await fn()

        with (
            patch("cognis.tools.executor.web.backends.direct._search_breaker") as mock_breaker,
            patch(
                "cognis.tools.executor.web.backends.direct._ddg_search",
                new=AsyncMock(return_value=ToolResult(output="[[media:1]]")),
            ) as mock_ddg_search,
        ):
            mock_breaker.call = AsyncMock(side_effect=_call)
            result = await backend.search(
                "mountain lake",
                options={"search_mode": "images"},
            )

        assert not result.is_error
        mock_ddg_search.assert_awaited_once_with(
            "mountain lake",
            max_results=8,
            region="us-en",
            safesearch="moderate",
            timelimit=None,
            include_images=False,
            mode="images",
            options={"search_mode": "images"},
            preferred_type=None,
            image_limit=10,
        )

    @pytest.mark.asyncio()
    async def test_search_retries_transient_ddg_failure(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        backend = AsyncMock(spec=DirectBackend)
        backend.search.side_effect = [
            ToolResult(
                output="DuckDuckGo search failed (timeout).",
                is_error=True,
                metadata={"backend": "direct", "failure_category": "timeout"},
            ),
            ToolResult(output="recovered", metadata={"backend": "direct"}),
        ]
        controller = MagicMock()
        gate = MagicMock()
        gate.__aenter__ = AsyncMock(return_value=None)
        gate.__aexit__ = AsyncMock(return_value=None)
        controller.acquire.return_value = gate

        with (
            patch(
                "cognis.tools.executor.web.handlers.resolve_search_backend", return_value=backend
            ),
            patch(
                "cognis.tools.executor.web.handlers._concurrency_controller",
                return_value=controller,
            ),
            patch(
                "cognis.tools.executor.web.handlers.asyncio.sleep",
                new=AsyncMock(),
            ) as mock_sleep,
        ):
            result = await handle_web_search({"query": "retry me"}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert backend.search.await_count == 2
        assert controller.acquire.call_count == 2
        mock_sleep.assert_awaited_once()
        assert (result.metadata or {}).get("attempts") == 2
        assert (result.metadata or {}).get("retry_failure_categories") == ["timeout"]

    @pytest.mark.asyncio()
    async def test_search_reports_typed_error_after_retries(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        backend = AsyncMock(spec=DirectBackend)
        backend.search.return_value = ToolResult(
            output="DuckDuckGo search failed (rate limited).",
            is_error=True,
            metadata={
                "backend": "direct",
                "provider": "duckduckgo",
                "failure_category": "rate_limited",
                "exception_type": "RuntimeError",
            },
        )

        with (
            patch(
                "cognis.tools.executor.web.handlers.resolve_search_backend", return_value=backend
            ),
            patch(
                "cognis.tools.executor.web.handlers.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await handle_web_search({"query": "retry me"}, _DUMMY_CONTEXT)

        assert result.is_error
        assert backend.search.await_count == 3
        assert result.metadata == {
            "backend": "direct",
            "provider": "duckduckgo",
            "attempts": 3,
            "failure_category": "rate_limited",
            "exception_type": "RuntimeError",
            "retry_failure_categories": ["rate_limited", "rate_limited"],
        }

    @pytest.mark.asyncio()
    async def test_search_does_not_retry_non_transient_ddg_failure(self) -> None:
        from cognis.tools.executor.web.handlers import handle_web_search

        backend = AsyncMock(spec=DirectBackend)
        backend.search.return_value = ToolResult(
            output="DuckDuckGo search failed (invalid response).",
            is_error=True,
            metadata={"backend": "direct", "failure_category": "invalid_response"},
        )

        with patch(
            "cognis.tools.executor.web.handlers.resolve_search_backend",
            return_value=backend,
        ):
            result = await handle_web_search({"query": "retry me"}, _DUMMY_CONTEXT)

        assert result.is_error
        backend.search.assert_awaited_once()
        assert (result.metadata or {}).get("attempts") == 1

    @pytest.mark.asyncio()
    async def test_ddg_search_uses_bounded_request_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        constructor_kwargs: list[dict[str, object]] = []

        class _FakeDDGS:
            def __init__(self, **kwargs: object) -> None:
                constructor_kwargs.append(kwargs)

            def text(self, _query: str, **_kwargs: object) -> list[dict[str, str]]:
                return []

        fake_module = MagicMock()
        fake_module.DDGS = _FakeDDGS
        monkeypatch.setitem(sys.modules, "ddgs", fake_module)

        await _ddg_search("bounded")

        assert constructor_kwargs == [{"timeout": 15}]

    @pytest.mark.asyncio()
    async def test_search_returns_lazy_artifact_candidates_for_direct_image_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor.web.backends.direct import _ddg_search

        class _FakeDDGS:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def text(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
                return [
                    {"title": "Article", "href": "https://example.com/article", "body": "Snippet"}
                ]

            def images(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
                return [
                    {
                        "image": "https://images.example.com/chart.png",
                        "title": "Revenue chart",
                        "url": "https://example.com/article",
                    }
                ]

        monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)

        result = await _ddg_search("example chart", include_images=True, mode="images")

        assert "[[media:1]]" in result.output
        assert "Article" not in result.output
        assert "tool_artifact:<tool_call_id>:media:1" in result.output
        anchors = (result.metadata or {}).get("output_anchors")
        assert isinstance(anchors, list)
        media_anchor = next(anchor for anchor in anchors if anchor["anchor"] == "media:1")
        assert media_anchor["artifact_candidate"]["url"] == "https://images.example.com/chart.png"
        assert media_anchor["artifact_candidate"]["metadata"]["source_tool"] == "web_search"


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
        assert "[[answer]]" in result.output
        assert "[[result:1]]" in result.output
        assert "Test answer" in result.output
        assert "Result 1" in result.output
        assert "https://example.com" in result.output
        anchors = result.metadata.get("output_anchors") if result.metadata else None
        assert isinstance(anchors, list)
        assert anchors[0]["anchor"] == "answer"
        assert anchors[1]["anchor"] == "result:1"
        stored_output = result.metadata.get("stored_output") if result.metadata else None
        assert isinstance(stored_output, str)
        assert "[[answer]]" in stored_output

    def test_search_scores_query_relevance_and_preserves_provider_rank(self) -> None:
        from cognis.tools.executor.web.backends.brave import _format_brave_results

        result = _format_brave_results(
            {
                "web": {
                    "results": [
                        {
                            "title": "Generic game portal",
                            "url": "https://example.com/games",
                            "description": "General gaming coverage.",
                        },
                        {
                            "title": "Gothic Remake one-handed weapon locations",
                            "url": "https://guide.example/gothic-remake/weapons",
                            "description": "Locations and stats for one-handed weapons.",
                        },
                    ]
                }
            },
            query="Gothic Remake one-handed weapon locations",
            preferred_type="document",
        )

        normalized = result.metadata.get("normalized_results") if result.metadata else None
        assert isinstance(normalized, list)
        assert normalized[0]["title"] == "Gothic Remake one-handed weapon locations"
        assert float(normalized[0]["cognis_score"]) > float(normalized[1]["cognis_score"]) > 0
        assert normalized[0]["provider_rank"] == 2

    def test_brave_normalizes_site_path_operator_without_losing_terms(self) -> None:
        from cognis.tools.executor.web.backends.brave import _normalize_brave_query

        assert (
            _normalize_brave_query("site:game8.co/games/Gothic-1-Remake/archives weapon locations")
            == "site:game8.co games Gothic Remake archives weapon locations"
        )

    def test_brave_normalized_results_match_post_freshness_output(self) -> None:
        from cognis.tools.executor.web.backends.brave import _format_brave_results

        result = _format_brave_results(
            {
                "web": {
                    "results": [
                        {
                            "title": "Old result",
                            "url": "https://example.com/old",
                            "description": "An old indexed result.",
                            "age": "5 years ago",
                        }
                    ]
                }
            },
            query="current result",
            options={"time_range": "day"},
        )

        assert "[[result:1]]" not in result.output
        assert result.metadata is not None
        assert result.metadata["normalized_results"] == []
        assert result.metadata["returned_result_count"] == 0

    def test_search_preserves_tavily_image_references_as_lazy_artifacts(self) -> None:
        from cognis.tools.executor.web.backends.tavily import _format_tavily_search

        result = _format_tavily_search(
            {
                "results": [],
                "images": [{"url": "https://cdn.example.com/chart.webp", "description": "Chart"}],
            }
        )

        assert not result.is_error
        assert "[[media:1]]" in result.output
        anchors = (result.metadata or {}).get("output_anchors")
        assert isinstance(anchors, list)
        media_anchor = next(anchor for anchor in anchors if anchor["anchor"] == "media:1")
        assert media_anchor["artifact_candidate"]["metadata"]["source_tool"] == "web_search"

    @pytest.mark.asyncio()
    async def test_image_search_mode_enables_tavily_images(self) -> None:
        backend = TavilyBackend(api_key="test")
        backend._safe_call = AsyncMock(
            return_value={
                "results": [],
                "images": [{"url": "https://cdn.example.com/image.jpg"}],
            }
        )

        result = await backend.search(
            "mountain lake",
            options={"search_mode": "images"},
        )

        assert "[[media:1]]" in result.output
        body = backend._safe_call.await_args.args[1]
        assert body["include_images"] is True

    @pytest.mark.asyncio()
    async def test_video_search_mode_reports_tavily_web_fallback(self) -> None:
        backend = TavilyBackend(api_key="test")
        backend._safe_call = AsyncMock(
            return_value={
                "results": [
                    {
                        "title": "Video result",
                        "url": "https://example.com/video",
                        "content": "Fallback result",
                    }
                ]
            }
        )

        result = await backend.search("video query", options={"search_mode": "videos"})

        assert result.metadata["requested_search_mode"] == "videos"
        assert result.metadata["effective_search_mode"] == "web"
        assert result.metadata["native_mode_support"] is False
        assert result.metadata["search_degraded"] is True

    @pytest.mark.asyncio()
    async def test_explicit_search_mode_overrides_tavily_topic(self) -> None:
        backend = TavilyBackend(api_key="test")
        backend._safe_call = AsyncMock(return_value={"results": []})

        await backend.search(
            "news",
            options={"search_mode": "news", "topic": "general"},
        )

        assert backend._safe_call.await_args.args[1]["topic"] == "news"

    @pytest.mark.asyncio()
    async def test_tavily_image_mode_preserves_metadata_when_no_images(self) -> None:
        backend = TavilyBackend(api_key="test")
        backend._safe_call = AsyncMock(
            return_value={
                "results": [{"title": "Text", "url": "https://example.com", "content": "x"}]
            }
        )

        result = await backend.search("image", options={"search_mode": "images"})

        assert result.output == "No image results found."
        assert result.metadata["requested_search_mode"] == "images"
        assert result.metadata["effective_search_mode"] == "images"

    @pytest.mark.asyncio()
    async def test_search_compacts_noisy_result_content(self) -> None:
        from cognis.tools.executor.web.backends.tavily import _format_tavily_search

        noisy = "Headline\n\n" + "Most Popular " * 150
        result = _format_tavily_search(
            {
                "results": [
                    {
                        "title": "Result 1",
                        "url": "https://example.com",
                        "content": noisy,
                        "score": 0.9,
                    }
                ]
            }
        )

        assert not result.is_error
        assert "Snippet:" in result.output
        assert "[snippet truncated]" in result.output
        assert "\n\nMost Popular" not in result.output
        stored_output = result.metadata.get("stored_output") if result.metadata else None
        assert isinstance(stored_output, str)
        assert "Most Popular Most Popular" in stored_output
        assert len(stored_output) > len(result.output)

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
        assert "[[result:1]]" in result.output
        assert "Brave Result" in result.output
        assert "https://example.com" in result.output

    @pytest.mark.asyncio()
    async def test_search_stored_output_keeps_three_extra_snippets(self) -> None:
        from cognis.tools.executor.web.backends.brave import _format_brave_results

        result = _format_brave_results(
            {
                "web": {
                    "results": [
                        {
                            "title": "Brave Result",
                            "url": "https://example.com",
                            "description": "Desc",
                            "extra_snippets": ["one", "two", "three", "four"],
                        }
                    ]
                }
            }
        )

        stored_output = result.metadata.get("stored_output") if result.metadata else None
        assert isinstance(stored_output, str)
        assert "one two three" in stored_output
        assert "four" not in stored_output

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

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        ("mode", "endpoint_suffix", "payload"),
        [
            ("web", "/web/search", {"web": {"results": []}}),
            ("news", "/news/search", {"results": []}),
            ("images", "/images/search", {"results": []}),
            ("videos", "/videos/search", {"results": []}),
        ],
    )
    async def test_search_mode_selects_native_brave_endpoint(
        self,
        mode: str,
        endpoint_suffix: str,
        payload: dict[str, object],
    ) -> None:
        from cognis.tools.executor.web.backends import brave as brave_module
        from cognis.tools.executor.web.backends.brave import BraveBackend

        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(return_value=payload)

        async def _call(fn):
            return await fn()

        with patch.object(brave_module._breaker, "call", new=AsyncMock(side_effect=_call)):
            result = await backend.search("query", options={"search_mode": mode})

        assert backend._get.await_args.args[0].endswith(endpoint_suffix)
        assert result.metadata["requested_search_mode"] == mode
        assert result.metadata["effective_search_mode"] == mode

    @pytest.mark.asyncio()
    async def test_search_retries_rejected_country_with_global_targeting(self) -> None:
        from cognis.tools.executor.web.backends import brave as brave_module
        from cognis.tools.executor.web.backends.brave import BraveBackend

        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
        rejected = httpx.Response(
            422,
            request=request,
            json={"detail": "Invalid country parameter"},
        )
        error = httpx.HTTPStatusError(
            "Unprocessable Entity",
            request=request,
            response=rejected,
        )
        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(
            side_effect=[
                error,
                {
                    "web": {
                        "results": [
                            {
                                "title": "Global result",
                                "url": "https://example.com",
                                "description": "Fallback succeeded",
                            }
                        ]
                    }
                },
            ]
        )

        async def _call(fn):
            return await fn()

        with patch.object(brave_module._breaker, "call", new=AsyncMock(side_effect=_call)):
            result = await backend.search("query", options={"country": "DE"})

        assert backend._get.await_count == 2
        assert backend._get.await_args_list[0].args[1]["country"] == "DE"
        assert backend._get.await_args_list[1].args[1]["country"] == "ALL"
        assert result.is_error is False
        assert "Search degraded" in result.output
        assert "retried with global country targeting" in result.output
        assert result.metadata["country_requested"] == "DE"
        assert result.metadata["country_effective"] == "ALL"
        assert result.metadata["country_filter_applied"] is False

    @pytest.mark.asyncio()
    async def test_brave_uses_provider_domain_constraint_and_czech_language_fallback(
        self,
    ) -> None:
        from cognis.tools.executor.web.backends.brave import BraveBackend

        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(return_value={"web": {"results": []}})
        result = await backend.search(
            "aktuální počasí",
            options={
                "country": "CZ",
                "include_domains": ["chmi.cz"],
                "search_lang": "sk",
            },
        )

        params = backend._get.await_args.args[1]
        assert "site:chmi.cz" in params["q"]
        assert "country" not in params
        assert params["search_lang"] == "sk"
        assert "ui_lang" not in params
        assert result.metadata["search_language_effective"] == "sk"

    def test_brave_domain_operator_bounds_include_and_exclude_domains(self) -> None:
        from cognis.tools.executor.web.backends.brave import _provider_query

        query = _provider_query(
            "zprávy",
            {
                "include_domains": ["a.cz", "b.cz", "c.cz", "d.cz"],
                "exclude_domains": ["x.cz", "y.cz", "z.cz", "q.cz", "r.cz", "s.cz"],
            },
        )
        assert query.count("site:") == 8
        assert "(site:a.cz OR site:b.cz OR site:c.cz)" in query
        assert "site:d.cz" not in query
        assert "-site:z.cz" not in query

    @pytest.mark.asyncio()
    async def test_brave_country_fallback_freshness_retry_reuses_effective_country(
        self,
    ) -> None:
        from cognis.tools.executor.web.backends.brave import BraveBackend

        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
        rejected = httpx.Response(
            422,
            request=request,
            json={"detail": "Invalid country parameter"},
        )
        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(
            side_effect=[
                httpx.HTTPStatusError(
                    "Unprocessable Entity",
                    request=request,
                    response=rejected,
                ),
                {"web": {"results": []}},
                {
                    "web": {
                        "results": [
                            {
                                "title": "Recovered",
                                "url": "https://example.com/recovered",
                                "description": "Recovered after bounded retry.",
                            }
                        ]
                    }
                },
            ]
        )
        result = await backend.search(
            "current news",
            options={"country": "DE", "time_range": "day"},
        )

        assert backend._get.await_count == 3
        assert backend._get.await_args_list[2].args[1]["country"] == "ALL"
        assert "freshness" not in backend._get.await_args_list[2].args[1]
        assert result.metadata["freshness_relaxed"] is True

    @pytest.mark.asyncio()
    async def test_brave_relaxes_freshness_once_after_domain_scoped_empty(self) -> None:
        from cognis.tools.executor.web.backends.brave import BraveBackend

        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(
            side_effect=[
                {"web": {"results": []}},
                {
                    "web": {
                        "results": [
                            {
                                "title": "Aktuální zpráva",
                                "url": "https://novinky.cz/clanek",
                                "description": "Aktuální česká zpráva s dostatečným popisem.",
                            }
                        ]
                    }
                },
            ]
        )
        result = await backend.search(
            "aktuální zprávy",
            options={"include_domains": ["novinky.cz"], "time_range": "day"},
        )

        assert backend._get.await_count == 2
        assert backend._get.await_args_list[0].args[1]["freshness"] == "pd"
        assert "freshness" not in backend._get.await_args_list[1].args[1]
        assert result.metadata["freshness_relaxed"] is True
        assert result.metadata["returned_result_count"] == 1

    def test_brave_news_penalizes_press_releases_and_taxonomy_pages(self) -> None:
        from cognis.tools.executor.web.backends.brave import _format_brave_results

        result = _format_brave_results(
            {
                "results": [
                    {
                        "title": "Hospital ranking press release",
                        "url": "https://prnewswire.com/news/hospitals",
                        "description": "PR Newswire press release about a commercial ranking.",
                        "age": "1 hour ago",
                    },
                    {
                        "title": "World event confirmed by officials",
                        "url": "https://news.example/world/event",
                        "description": "Officials confirmed a major world event after independent reporting.",
                        "age": "2 hours ago",
                    },
                    {
                        "title": "World topic page",
                        "url": "https://other.example/tag/world/",
                        "description": "A list of links.",
                    },
                ]
            },
            mode="news",
            query="top world news",
        )
        normalized = result.metadata["normalized_results"]
        assert normalized[0]["title"] == "World event confirmed by officials"
        assert normalized[-1]["title"] == "World topic page"

    @pytest.mark.asyncio()
    async def test_search_does_not_retry_422_unrelated_to_country(self) -> None:
        from cognis.tools.executor.web.backends import brave as brave_module
        from cognis.tools.executor.web.backends.brave import BraveBackend

        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
        rejected = httpx.Response(
            422,
            request=request,
            json={"detail": "Invalid result_filter"},
        )
        error = httpx.HTTPStatusError(
            "Unprocessable Entity",
            request=request,
            response=rejected,
        )
        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(side_effect=error)

        async def _call(fn):
            return await fn()

        with patch.object(brave_module._breaker, "call", new=AsyncMock(side_effect=_call)):
            result = await backend.search("query", options={"country": "US"})

        assert backend._get.await_count == 1
        assert result.is_error is True
        assert "Invalid result_filter" in result.output
        assert result.metadata["http_status"] == 422

    @pytest.mark.asyncio()
    async def test_search_does_not_retry_global_country(self) -> None:
        from cognis.tools.executor.web.backends import brave as brave_module
        from cognis.tools.executor.web.backends.brave import BraveBackend

        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
        rejected = httpx.Response(
            422,
            request=request,
            json={"detail": "Invalid country parameter"},
        )
        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Unprocessable Entity",
                request=request,
                response=rejected,
            )
        )

        async def _call(fn):
            return await fn()

        with patch.object(brave_module._breaker, "call", new=AsyncMock(side_effect=_call)):
            result = await backend.search("query", options={"country": "ALL"})

        assert backend._get.await_count == 1
        assert result.is_error is True
        assert result.metadata["http_status"] == 422

    @pytest.mark.asyncio()
    async def test_search_country_fallback_retries_only_once(self) -> None:
        from cognis.tools.executor.web.backends import brave as brave_module
        from cognis.tools.executor.web.backends.brave import BraveBackend

        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
        rejected = httpx.Response(
            422,
            request=request,
            json={"detail": "Invalid country parameter"},
        )
        error = httpx.HTTPStatusError(
            "Unprocessable Entity",
            request=request,
            response=rejected,
        )
        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(side_effect=[error, error])

        async def _call(fn):
            return await fn()

        with patch.object(brave_module._breaker, "call", new=AsyncMock(side_effect=_call)):
            result = await backend.search("query", options={"country": "DE"})

        assert backend._get.await_count == 2
        assert backend._get.await_args_list[1].args[1]["country"] == "ALL"
        assert result.is_error is True
        assert result.metadata["http_status"] == 422

    def test_brave_rate_limit_error_keeps_status_and_bounded_detail(self) -> None:
        from cognis.tools.executor.web.backends.brave import _brave_http_error_result

        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
        response = httpx.Response(
            429,
            request=request,
            json={"detail": "x" * 400},
        )
        result = _brave_http_error_result(
            httpx.HTTPStatusError(
                "Too Many Requests",
                request=request,
                response=response,
            )
        )

        assert result.is_error is True
        assert result.output.startswith("Brave Search rate limit exceeded.")
        assert result.metadata["http_status"] == 429
        assert (
            len(result.output) <= len("Brave Search rate limit exceeded. Try again later. — ") + 300
        )

    @pytest.mark.asyncio()
    async def test_image_search_maps_shared_moderate_safesearch_to_strict(self) -> None:
        from cognis.tools.executor.web.backends import brave as brave_module
        from cognis.tools.executor.web.backends.brave import BraveBackend

        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(return_value={"results": []})

        async def _call(fn):
            return await fn()

        with patch.object(brave_module._breaker, "call", new=AsyncMock(side_effect=_call)):
            await backend.search(
                "mountain lake",
                options={"search_mode": "images", "safesearch": "moderate"},
            )

        params = backend._get.await_args.args[1]
        assert params["safesearch"] == "strict"

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("safesearch", ["off", "strict"])
    async def test_image_search_preserves_supported_safesearch(
        self,
        safesearch: str,
    ) -> None:
        from cognis.tools.executor.web.backends import brave as brave_module
        from cognis.tools.executor.web.backends.brave import BraveBackend

        backend = BraveBackend(api_key="test")
        backend._get = AsyncMock(return_value={"results": []})

        async def _call(fn):
            return await fn()

        with patch.object(brave_module._breaker, "call", new=AsyncMock(side_effect=_call)):
            await backend.search(
                "mountain lake",
                options={"search_mode": "images", "safesearch": safesearch},
            )

        params = backend._get.await_args.args[1]
        assert params["safesearch"] == safesearch

    def test_brave_image_mode_returns_media_artifact(self) -> None:
        from cognis.tools.executor.web.backends.brave import _format_brave_results

        result = _format_brave_results(
            {
                "results": [
                    {
                        "title": "Lake",
                        "url": "https://example.com/lake",
                        "properties": {"url": "https://cdn.example.com/lake.jpg"},
                    }
                ]
            },
            mode="images",
        )

        assert "[[media:1]]" in result.output
        assert "[[result:1]]" not in result.output


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
            stream_context = MagicMock()
            stream_context.__aenter__ = AsyncMock(return_value=mock_response)
            stream_context.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=stream_context)
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
            stream_context = MagicMock()
            stream_context.__aenter__ = AsyncMock(return_value=mock_response)
            stream_context.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=stream_context)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await fetch_with_retry("https://example.com", max_retries=1)
            assert isinstance(result, ToolResult)
            assert result.is_error
            assert "Cloudflare" in result.output
            assert "browser" in result.output.lower()
            assert result.metadata == {
                "cloudflare_blocked": True,
                "direct_fetch_blocked": True,
            }

    @pytest.mark.asyncio()
    async def test_streaming_fetch_rejects_oversized_content_length(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cognis.tools.executor.web.headers import fetch_with_retry

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-length": "1000"},
                request=request,
            )
        )
        client = httpx.AsyncClient(transport=transport)
        monkeypatch.setattr("cognis.tools.executor.web.headers._MAX_RESPONSE_SIZE", 100)
        with patch(
            "cognis.tools.executor.web.headers.httpx.AsyncClient",
            return_value=client,
        ):
            result = await fetch_with_retry("https://example.com", max_retries=1)
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert result.metadata["failure_category"] == "response_too_large"
        assert result.metadata["source_limit_bytes"] == 100


class TestSettingsSchema:
    """Test web backend settings validation."""

    def test_valid_backends(self) -> None:
        from cognis.settings_schema import validate_setting_value

        for backend in ("direct", "tavily", "brave"):
            validate_setting_value("web.search_backend", backend)

    def test_invalid_backend_rejected(self) -> None:
        from cognis.settings_schema import validate_setting_value

        with pytest.raises(ValueError, match="must be one of"):
            validate_setting_value("web.search_backend", "grok")

    def test_web_backend_in_default_settings(self) -> None:
        from cognis.bootstrap import DEFAULT_SETTINGS

        assert "web.backend" in DEFAULT_SETTINGS
        category, default = DEFAULT_SETTINGS["web.backend"]
        assert category == "web"
        assert default == "direct"

    def test_positive_timeout_settings_accept_positive_values(self) -> None:
        from cognis.settings_schema import validate_setting_value

        validate_setting_value("session.step_timeout_seconds", 3600)
        validate_setting_value("session.step_request_questions_timeout_seconds", 3600)
        validate_setting_value("evaluator.timeout_ms", 180000)

    def test_positive_timeout_settings_reject_zero(self) -> None:
        from cognis.settings_schema import validate_setting_value

        with pytest.raises(ValueError, match="greater than zero"):
            validate_setting_value("session.step_timeout_seconds", 0)
        with pytest.raises(ValueError, match="greater than zero"):
            validate_setting_value("session.step_request_questions_timeout_seconds", 0)
        with pytest.raises(ValueError, match="greater than zero"):
            validate_setting_value("evaluator.timeout_ms", 0)
