from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cognis.tools.executor.web.backends.formatting import build_search_tool_result


def test_strict_freshness_with_unknown_dates_is_explicitly_degraded() -> None:
    result = build_search_tool_result(
        answer=None,
        results=[
            {
                "title": "Undated news",
                "url": "https://example.com/news",
                "result_type": "article",
            }
        ],
        metadata={"requested_time_range": "day"},
    )

    assert "Freshness 'day' could not be verified" in result.output
    assert "Content type: article" in result.output
    assert result.metadata["freshness_verified"] is False
    assert result.metadata["unknown_date_result_count"] == 1
    assert result.metadata["search_degraded"] is True


def test_strict_freshness_drops_known_out_of_window_results() -> None:
    old_date = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    current_date = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

    result = build_search_tool_result(
        answer=None,
        results=[
            {"title": "Old", "url": "https://example.com/old", "published_date": old_date},
            {
                "title": "Current",
                "url": "https://example.com/current",
                "published_date": current_date,
            },
        ],
        metadata={"requested_time_range": "day"},
    )

    assert "[1] Current" in result.output
    assert "Old" not in result.output
    assert result.metadata["freshness_verified"] is True
    assert result.metadata["dated_result_count"] == 2
    assert result.metadata["in_window_result_count"] == 1


def test_degraded_warning_names_failed_engines_and_mode_fallback() -> None:
    result = build_search_tool_result(
        answer=None,
        results=[{"title": "Fallback", "url": "https://example.com"}],
        metadata={
            "search_degraded": True,
            "degraded_reason": "Native mode unavailable.",
            "engine_failures": [
                ["vimeo", "Suspended: access denied"],
                ["other", "timeout"],
            ],
            "requested_search_mode": "videos",
            "effective_search_mode": "web",
        },
    )

    assert "vimeo — Suspended: access denied" in result.output
    assert "other — timeout" in result.output
    assert "Search mode changed from videos to web" in result.output


def test_empty_search_preserves_freshness_diagnostics() -> None:
    result = build_search_tool_result(
        answer=None,
        results=[],
        metadata={"requested_time_range": "day"},
    )

    assert result.output == "No search results found."
    assert result.metadata["freshness_requested"] == "day"
