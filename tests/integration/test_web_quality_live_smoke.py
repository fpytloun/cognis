"""Opt-in read-only live smoke coverage for native web quality.

Run manually with:
COGNIS_RUN_WEB_LIVE_SMOKE=1 uv run pytest \
  tests/integration/test_web_quality_live_smoke.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from cognis.core.anchored_output import markdown_heading_anchors
from cognis.core.output_anchor_registry import build_anchor_manifest
from cognis.core.tool_output_store import FilesystemToolOutputBackend, ToolOutputStore
from cognis.models.tool import ExecutorHandle
from cognis.tools.executor.web.handlers import handle_web_fetch
from cognis.tools.registry import ToolExecutionContext

pytestmark = pytest.mark.skipif(
    os.getenv("COGNIS_RUN_WEB_LIVE_SMOKE") != "1",
    reason="Set COGNIS_RUN_WEB_LIVE_SMOKE=1 for read-only live web smoke tests.",
)

_URLS = (
    "https://github.com/pallets/flask",
    "https://en.wikipedia.org/wiki/Web_scraping",
    "https://pubmed.ncbi.nlm.nih.gov/31452104/",
    "https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do-in-python",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=aircAruvnKk",
    "https://arxiv.org/pdf/1706.03762",
    "https://arxiv.org/abs/2606.00103",
    "https://en.wikipedia.org/wiki/Artificial_intelligence",
    "https://www.cnn.com/2026/07/02/world/live-news/iran-war-us-talks",
    "https://steamcommunity.com/sharedfiles/filedetails/?id=3739156180",
    "https://game8.co/games/Gothic-1-Remake/archives/604500",
    "https://www.neoseeker.com/gothic-remake/Guides/Weapons",
    "https://github.com/openclaw/openclaw/issues/20221",
)

_PROBLEMATIC_EXPECTATIONS = {
    "steamcommunity.com": ("Frequently Asked Questions", "More Gothic 1 Remake Guides"),
    "game8.co": ("Gothic 1 Remake",),
    "neoseeker.com": ("Weapons",),
    "github.com": ("Untrusted metadata",),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _URLS)
async def test_live_fetch_quality_and_tail_recovery(url: str, tmp_path: Path) -> None:
    runtime_metadata = {"web_fetch_backend": "direct"}
    result = await handle_web_fetch(
        {"url": url, "format": "markdown", "timeout": 60},
        ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="live-smoke", executor_type="local"),
            runtime_metadata=runtime_metadata,
            shared_runtime_metadata=runtime_metadata,
        ),
    )
    fetched = result
    host = url.split("/", 3)[2].removeprefix("www.")
    if host == "neoseeker.com" and result.is_error:
        metadata = result.metadata or {}
        assert metadata.get("browser_fallback_skipped_reason") == "browser_unavailable"
        assert "Cloudflare browser verification" in result.output
        return
    assert not result.is_error, result.output
    assert result.metadata is not None
    stored_output = str(result.metadata["stored_output"])
    drafts = result.metadata.get("output_anchors")
    raw_drafts = list(drafts) if isinstance(drafts, list) else []
    raw_drafts.extend(markdown_heading_anchors(stored_output, existing_anchors=raw_drafts))
    call_id = "call_live_" + str(abs(hash(url)))
    manifest, private = build_anchor_manifest(call_id, "web_fetch", raw_drafts)
    payload = manifest.to_dict()
    payload["anchors"] = private
    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))
    await store.save(call_id, stored_output, anchors=private, anchor_manifest=payload)

    anchors = await store.list_anchors(call_id)
    assert anchors
    full = await store.read(call_id, offset=1, limit=10_000)
    assert full is not None
    tail_offset = max(1, full.total_lines - 8)
    tail = await store.read(call_id, offset=tail_offset, limit=12)
    assert tail is not None and tail.content.strip()
    adapter = (fetched.metadata or {}).get("adapter")
    recovered_transcript_anchor = None
    if adapter == "youtube_transcript":
        transcript_anchors = [
            anchor for anchor in anchors if anchor.anchor.startswith("transcript:")
        ]
        assert transcript_anchors
        recovered_transcript_anchor = transcript_anchors[-1].anchor
        transcript_tail = await store.read_anchor(call_id, recovered_transcript_anchor)
        assert transcript_tail is not None
        assert "[" in transcript_tail.content and "]" in transcript_tail.content

    document = (fetched.metadata or {}).get("extracted_document")
    quality = document.get("semantic_quality") if isinstance(document, dict) else None
    status = (
        str(quality.get("status"))
        if isinstance(quality, dict)
        else str((fetched.metadata or {}).get("adapter_status") or "unavailable")
    )
    expected = _PROBLEMATIC_EXPECTATIONS.get(host)
    if expected:
        assert status in {"complete", "partial"}, {"url": url, "status": status}
        assert len(stored_output) >= 500
        assert any(token.lower() in stored_output.lower() for token in expected)
        assert "Begin Survey" not in stored_output
        assert "site improvement survey" not in stored_output.lower()
    if host == "steamcommunity.com":
        assert isinstance(document, dict)
        assert document.get("extractor") == "dom_structural"
        assert "Store Page" not in stored_output[:1_000]
        assert "Sign In" not in stored_output[:1_000]
    evidence = {
        "url": url,
        "status": status,
        "adapter": adapter,
        "transcript_availability": (fetched.metadata or {}).get("transcript_availability"),
        "transcript_error_type": (fetched.metadata or {}).get("transcript_error_type"),
        "transcript_language": (fetched.metadata or {}).get("transcript_language"),
        "transcript_generated": (fetched.metadata or {}).get("transcript_generated"),
        "recovered_transcript_anchor": recovered_transcript_anchor,
        "producer_truncated": bool(result.metadata.get("producer_truncated")),
        "stored_characters": len(stored_output),
        "anchor_count": len(anchors),
        "first_anchor": anchors[0].anchor,
        "last_anchor": anchors[-1].anchor,
        "tail_offset": tail_offset,
        "tail_recovered": bool(tail.content.strip()),
    }
    print(json.dumps(evidence, sort_keys=True))


@pytest.mark.asyncio
async def test_live_parallel_problematic_fetch_wave_remains_usable(tmp_path: Path) -> None:
    urls = (
        "https://steamcommunity.com/sharedfiles/filedetails/?id=3739156180",
        "https://game8.co/games/Gothic-1-Remake/archives/604500",
        "https://game8.co/games/Gothic-1-Remake/archives/604500?smoke=1",
        "https://game8.co/games/Gothic-1-Remake/archives/604500?smoke=2",
    )
    runtime_metadata = {"web_fetch_backend": "direct"}
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="live-wave", executor_type="local"),
        runtime_metadata=runtime_metadata,
        shared_runtime_metadata=runtime_metadata,
    )
    results = await asyncio.gather(
        *(
            handle_web_fetch(
                {"url": url, "format": "markdown", "timeout": 30},
                context,
            )
            for url in urls
        )
    )

    assert all(not result.is_error for result in results), [
        result.output[:200] for result in results if result.is_error
    ]
    for result in results:
        metadata = result.metadata or {}
        stored_output = str(metadata.get("stored_output") or "")
        assert len(stored_output) >= 500
        assert "Tool execution failed" not in result.output
        assert "executor disconnected" not in result.output.lower()
