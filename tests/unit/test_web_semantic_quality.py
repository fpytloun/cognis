from __future__ import annotations

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.extraction import extract_document
from cognis.tools.executor.web.handlers import (
    _annotate_fallback_comparison,
    _result_is_browser_fallback_candidate,
    _result_quality_key,
)
from cognis.tools.executor.web.semantic_quality import (
    assess_semantic_quality,
    compare_candidates,
    url_provenance,
)


def test_semantic_quality_distinguishes_usable_and_blocked_content() -> None:
    usable = assess_semantic_quality(
        "A substantive paragraph with enough information for a reader.\n\n"
        "A second paragraph adds context and evidence."
    )
    blocked = assess_semantic_quality("Please wait for verification. Verify you are human.")

    assert usable.status == "partial"
    assert usable.words > 10
    assert blocked.label == "blocked"
    assert blocked.blocked


def test_semantic_quality_exposes_all_bounded_statuses() -> None:
    complete = (
        "This substantive article body contains enough words and context to qualify as "
        "complete evidence for readers. It includes multiple sentences, meaningful detail, "
        "and a second paragraph with additional supporting information for the topic."
    )
    cases = {
        "complete": complete,
        "partial": "A short body fragment with a few useful words.",
        "empty": "",
        "title_only": "Only the page title",
        "navigation_only": "Home Menu Search Contact Privacy Terms",
        "login_required": "Please sign in to continue.",
        "paywall": "Subscription required to read this article.",
        "interstitial": "Please wait while loading.",
        "blocked": "Access denied. Verify you are human.",
    }
    for expected, content in cases.items():
        title = "Only the page title" if expected == "title_only" else None
        assert assess_semantic_quality(content, title=title).status == expected


def test_candidate_comparison_returns_auditable_winner() -> None:
    winner, comparison = compare_candidates(
        [
            {"source": "thin", "content": "Subscribe now."},
            {
                "source": "article",
                "content": (
                    "A useful article paragraph with durable information.\n\n"
                    "Another paragraph explains the result in detail."
                ),
            },
        ]
    )

    assert winner is not None
    assert winner["source"] == "article"
    assert [row["source"] for row in comparison] == ["article", "thin"]
    assert all("score" in row and "quality" in row for row in comparison)


def test_url_provenance_preserves_requested_fetched_and_canonical_urls() -> None:
    provenance = url_provenance(
        "https://example.test/start",
        fetched_url="https://example.test/final",
        canonical_url="https://example.test/article",
    )

    assert provenance["requested_url"].endswith("/start")
    assert provenance["fetched_url"].endswith("/final")
    assert provenance["canonicalized"] is True
    assert provenance["requested_domain"] == "example.test"


def test_extracted_document_contains_semantic_quality_and_url_provenance() -> None:
    document = extract_document(
        "<html><head><title>Example</title>"
        '<link rel="canonical" href="/article"></head>'
        "<body><article><p>A useful paragraph with enough content.</p>"
        "<p>A second paragraph makes the extraction meaningful.</p>"
        "</article></body></html>",
        url="https://example.test/final",
        options={"requested_url": "https://example.test/start"},
    )

    data = document.as_dict()
    assert data["url_provenance"]["requested_url"] == "https://example.test/start"
    assert data["url_provenance"]["fetched_url"] == "https://example.test/final"
    assert data["url_provenance"]["canonical_url"] == "https://example.test/article"
    assert data["semantic_quality"]["status"] == "partial"
    assert data["candidate_comparison"]


def test_html_output_quality_uses_visible_text_not_markup_volume() -> None:
    html = (
        "<html><head><title>Navigation</title></head><body><nav>"
        + "".join(f'<a href="/{index}">Menu {index}</a>' for index in range(80))
        + "</nav></body></html>"
    )
    document = extract_document(
        html,
        url="https://example.com/navigation",
        output_format="html",
        options={"include_media": "none"},
    )
    assert document.semantic_quality["status"] != "complete"


def _quality_result(status: str, score: float, content: str) -> ToolResult:
    return ToolResult(
        output=content,
        metadata={
            "primary_backend": "direct",
            "extracted_document": {"semantic_quality": {"status": status, "score": score}},
        },
    )


def test_successful_partial_result_triggers_browser_fallback() -> None:
    assert _result_is_browser_fallback_candidate(
        _quality_result("partial", 35.0, "useful but incomplete")
    )


def test_candidate_comparison_selects_better_browser_and_preserves_better_primary() -> None:
    partial = _quality_result("partial", 35.0, "partial")
    complete = _quality_result("complete", 90.0, "complete")
    selected = max(
        (partial, complete),
        key=lambda item: _result_quality_key(item, 0 if item is partial else 1),
    )
    annotated = _annotate_fallback_comparison(
        selected,
        partial,
        complete,
        mode="headless",
        attempted=["headless"],
        selected_backend="browser",
    )
    assert annotated is complete
    assert annotated.metadata["browser_fallback_selection"] == "browser_selected"
    assert annotated.metadata["browser_fallback_success"] is True

    selected = max(
        (complete, partial),
        key=lambda item: _result_quality_key(item, 0 if item is complete else 1),
    )
    annotated = _annotate_fallback_comparison(
        selected,
        complete,
        partial,
        mode="headless",
        attempted=["headless"],
        selected_backend="direct",
    )
    assert annotated is complete
    assert annotated.metadata["browser_fallback_selection"] == "primary_preserved"
    assert annotated.metadata["browser_fallback_success"] is False
