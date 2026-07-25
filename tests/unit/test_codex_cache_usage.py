from __future__ import annotations

from cognis.core.agent_loop import _normalize_token_usage
from cognis.providers.llm.litellm import _record_llm_token_metrics
from cognis.providers.llm.responses_bridge import _extract_usage


def test_responses_usage_exposes_nested_cache_write_tokens() -> None:
    usage = _extract_usage(
        {
            "usage": {
                "input_tokens": 1_200,
                "output_tokens": 80,
                "input_tokens_details": {
                    "cached_tokens": 900,
                    "cache_write_tokens": 240,
                },
            }
        }
    )

    assert usage["cached_tokens"] == 900
    assert usage["cache_write_tokens"] == 240


def test_token_usage_accepts_top_level_and_nested_cache_write_shapes() -> None:
    assert _normalize_token_usage({"cache_write_tokens": 240}) == {"cache_write_tokens": 240}
    assert _normalize_token_usage({"input_tokens_details": {"cache_write_tokens": 241}}) == {
        "cache_write_tokens": 241
    }


def test_cache_write_metric_does_not_double_count_legacy_creation_alias() -> None:
    values = _record_llm_token_metrics(
        {
            "cache_write_tokens": 240,
            "cache_creation_input_tokens": 240,
            "input_tokens_details": {
                "cache_write_tokens": 240,
                "cache_creation_input_tokens": 240,
            },
        },
        provider_id="codex-cache-write-test",
        model="gpt-5.6-sol",
        llm_api="responses",
        location="controller",
    )

    assert values["cache_write"] == 240
    assert values["cache_creation"] == 0
