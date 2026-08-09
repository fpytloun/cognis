"""Opt-in live transport classification for known broken public domains."""

import os

import pytest

from cognis.tools.executor.web.backends.direct import DirectBackend

pytestmark = pytest.mark.skipif(
    os.getenv("COGNIS_RUN_WEB_LIVE_SMOKE") != "1",
    reason="set COGNIS_RUN_WEB_LIVE_SMOKE=1",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "category"),
    [
        ("https://historypark.cz/", "dns_resolution_failed"),
        ("https://www.majalandpraha.cz/", "tls_certificate_invalid"),
    ],
)
async def test_live_terminal_transport_failure_is_precise(
    url: str,
    category: str,
) -> None:
    result = await DirectBackend().fetch(
        url,
        output_format="markdown",
        timeout=30,
        options={"include_media": "none", "media_limit": 0},
    )

    assert result.is_error
    assert result.metadata is not None
    assert result.metadata["failure_category"] == category
    assert result.metadata["browser_fallback_recommended"] is False
    assert "Web fetch request failed" not in result.output
