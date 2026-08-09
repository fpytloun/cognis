from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.backends.browser import _is_internal_browser_url
from cognis.tools.executor.web.backends.direct import _format_pdf_response_in_process
from cognis.tools.executor.web.headers import _looks_like_landing_redirect, fetch_with_retry


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.2/",
        "http://10.0.0.1/",
        "http://169.254.1.1/",
        "http://[::1]/",
        "http://service.local/",
        "http://internal-host/",
    ],
)
def test_internal_browser_url_policy_covers_private_targets(url: str) -> None:
    assert _is_internal_browser_url(url)


def test_landing_redirect_treats_www_as_same_site() -> None:
    assert _looks_like_landing_redirect(
        "https://example.com/world/2026/07/article",
        "https://www.example.com/world",
    )


@pytest.mark.asyncio
async def test_chunked_stream_crossing_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 101))
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr("cognis.tools.executor.web.headers._MAX_RESPONSE_SIZE", 100)
    with patch("cognis.tools.executor.web.headers.httpx.AsyncClient", return_value=client):
        result = await fetch_with_retry("https://example.com", max_retries=1)
    assert isinstance(result, ToolResult)
    assert result.is_error
    assert result.metadata["failure_category"] == "response_too_large"


@pytest.mark.asyncio
async def test_pdf_worker_is_terminated_when_parser_budget_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_queue = MagicMock()
    result_queue.get.side_effect = __import__("queue").Empty
    process = MagicMock()
    process.is_alive.side_effect = [True, False]
    context = MagicMock()
    context.Queue.return_value = result_queue
    context.Process.return_value = process
    monkeypatch.setattr(
        "cognis.tools.executor.web.backends.direct.multiprocessing.get_context",
        lambda _method: context,
    )
    response = httpx.Response(
        200,
        headers={"content-type": "application/pdf"},
        content=b"%PDF-test",
        request=httpx.Request("GET", "https://example.com/paper.pdf"),
    )
    result = await _format_pdf_response_in_process(
        response,
        output_format="markdown",
        requested_url=str(response.url),
        source_url=str(response.url),
        options=None,
        timeout=0.01,
    )
    assert result.is_error
    assert result.metadata["failure_category"] == "pdf_extraction_timeout"
    process.terminate.assert_called_once()
