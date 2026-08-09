from __future__ import annotations

from cognis.tools.executor.web.backends.searxng import (
    SearxngBackend,
    _automatic_category,
    _search_diagnostics,
)


def test_searxng_deduplicates_reranks_and_preserves_diagnostics() -> None:
    payload = {
        "results": [
            {
                "title": "Jobs at The Guardian",
                "url": "https://workwithus.theguardian.com/jobs",
                "content": "Employment listings",
                "score": 8,
                "engine": "bing",
            },
            {
                "title": "Current Guardian world article",
                "url": "https://www.theguardian.com/world/2026/jul/21/story",
                "content": "Current world news",
                "score": 7,
                "publishedDate": "2026-07-21",
                "engine": "google",
                "category": "news",
            },
            {
                "title": "Duplicate",
                "url": "https://www.theguardian.com/world/2026/jul/21/story#comments",
                "content": "Duplicate",
                "score": 6,
            },
        ]
    }
    results = SearxngBackend._format_results(
        payload,
        query="current Guardian world article",
        num_results=5,
        options={
            "include_domains": ["theguardian.com"],
            "exclude_domains": ["workwithus.theguardian.com"],
            "result_type": "document",
        },
    )
    assert len(results) == 1
    assert results[0]["title"] == "Current Guardian world article"
    assert results[0]["published_date"] == "2026-07-21"
    assert results[0]["engine"] == "google"
    assert results[0]["category"] == "news"
    assert results[0]["provider_score"] == 7
    assert results[0]["cognis_score"] != results[0]["provider_score"]
    assert results[0]["result_type"] == "article"
    assert results[0]["freshness"] == "known"
    assert results[0]["fetch_recommendation"] in {"low", "medium", "high"}


def test_search_intent_routing_uses_portable_searxng_categories() -> None:
    assert _automatic_category("web", "repository") == "it"
    assert _automatic_category("web", "discussion") == "it"
    assert _automatic_category("web", "paper") == "science"
    assert _automatic_category("videos", None) == "videos"
    assert _automatic_category("images", None) == "images"
    assert _automatic_category("news", None) == "news"
    assert _automatic_category("web", "document") is None


def test_search_diagnostics_report_degraded_engine_failures() -> None:
    diagnostics = _search_diagnostics(
        {
            "results": [{"title": "fallback"}],
            "unresponsive_engines": [
                ["duckduckgo", "CAPTCHA"],
                ["brave", "rate limit"],
            ],
            "suggestions": ["better query"],
        },
        params={"engines": "github", "time_range": "day"},
        results=[
            {
                "engine": "seznam",
                "engines": ["seznam"],
            }
        ],
    )
    assert diagnostics["search_quality"] == "degraded"
    assert diagnostics["engines_requested"] == ["github"]
    assert diagnostics["engines_contributing"] == ["seznam"]
    assert len(diagnostics["engine_failures"]) == 2
    assert diagnostics["suggestions"] == ["better query"]


def test_structured_authority_prefers_original_video_author() -> None:
    payload = {
        "results": [
            {
                "title": "3Blue1Brown - What is a neural network?",
                "url": "https://www.youtube.com/watch?v=repost",
                "author": "Course Mirror",
                "score": 3,
                "engine": "youtube",
            },
            {
                "title": "But what is a neural network?",
                "url": "https://www.youtube.com/watch?v=canonical",
                "author": "3Blue1Brown",
                "score": 1,
                "engine": "youtube",
            },
        ]
    }
    results = SearxngBackend._format_results(
        payload,
        query="3Blue1Brown neural network",
        num_results=2,
        options={},
    )
    assert results[0]["url"] == "https://www.youtube.com/watch?v=canonical"


def test_pubmed_legacy_result_host_matches_canonical_domain_preference() -> None:
    results = SearxngBackend._format_results(
        {
            "results": [
                {
                    "title": "Molegro Virtual Docker for Docking.",
                    "url": "https://www.ncbi.nlm.nih.gov/pubmed/31452104",
                    "engine": "pubmed",
                    "score": 1,
                }
            ]
        },
        query="31452104",
        num_results=1,
        options={
            "include_domains": ["pubmed.ncbi.nlm.nih.gov"],
            "result_type": "paper",
        },
    )
    assert results[0]["url"].endswith("/pubmed/31452104")
    assert results[0]["result_type"] == "paper"


def test_pubmed_domain_alias_rejects_unrelated_ncbi_paths() -> None:
    results = SearxngBackend._format_results(
        {
            "results": [
                {
                    "title": "Unrelated NCBI resource",
                    "url": "https://www.ncbi.nlm.nih.gov/datasets/genome/",
                    "engine": "bing",
                }
            ]
        },
        query="31452104",
        num_results=1,
        options={"include_domains": ["pubmed.ncbi.nlm.nih.gov"]},
    )
    assert results == []
