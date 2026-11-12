from __future__ import annotations

import httpx
import pytest

from cognis.api.models import ProviderUsageResponse
from cognis.providers.llm import elevenlabs_usage as usage


def subscription(**kwargs):
    return {
        "tier": "free",
        "character_count": 1536,
        "character_limit": 10000,
        "can_extend_character_limit": False,
        "max_credit_limit_extension": 0,
        "next_character_count_reset_unix": 1738356858,
        **kwargs,
    }


def test_credit_counts_reset_and_public_contract():
    result = usage.normalize_subscription_usage(
        subscription(private_account_data="must not persist")
    )
    public = ProviderUsageResponse.model_validate({**result, "provider_id": "elevenlabs"})
    window = public.additional_rate_limits[0].primary
    assert public.ok and public.plan_type == "free"
    assert window.used_percent == pytest.approx(15.36)
    assert (window.limit, window.remaining) == (10000, 8464)
    assert window.resets_at == "2025-01-31T20:54:18+00:00"
    assert window.window_duration_mins is None
    assert public.allowed and not public.limit_reached
    assert "must not persist" not in str(result)


@pytest.mark.parametrize(
    ("counts", "allowed"),
    [
        ({"character_count": 10000}, False),
        ({"character_limit": 0, "character_count": 0}, False),
        (
            {
                "character_count": 12000,
                "can_extend_character_limit": True,
                "max_credit_limit_extension": 3000,
            },
            True,
        ),
        (
            {
                "character_count": 13000,
                "can_extend_character_limit": True,
                "max_credit_limit_extension": 3000,
            },
            False,
        ),
        (
            {
                "character_count": 13000,
                "can_extend_character_limit": True,
                "max_credit_limit_extension": "unlimited",
            },
            True,
        ),
        (
            {
                "character_count": 10000,
                "can_extend_character_limit": True,
                "max_credit_limit_extension": None,
            },
            None,
        ),
    ],
)
def test_included_allowance_does_not_hide_or_invent_usage_based_capacity(counts, allowed):
    report = usage.normalize_subscription_usage(subscription(**counts))
    assert report["allowed"] is allowed
    assert report["limit_reached"] is (not allowed if allowed is not None else None)
    for item in report["additional_rate_limits"]:
        assert 0 <= item["primary"]["used_percent"] <= 100
        assert item["primary"]["remaining"] >= 0


@pytest.mark.parametrize("field", ["character_count", "character_limit"])
@pytest.mark.parametrize("value", [None, True, -1, "10000", float("nan")])
def test_missing_or_invalid_counts_are_unknown_not_zero(field, value):
    report = usage.normalize_subscription_usage(subscription(**{field: value}))
    assert report["ok"] is False
    assert "limit_reached" not in report


@pytest.mark.parametrize("status", [200, 401, 403, 404, 429, 503])
@pytest.mark.parametrize("base", ["https://api.elevenlabs.io", "https://api.elevenlabs.io/v1"])
async def test_quota_fetch_is_one_metadata_get_without_exposing_errors(monkeypatch, status, base):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, json=subscription() if status == 200 else {"detail": "Private credentials"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(usage.httpx, "AsyncClient", lambda **kwargs: client)
    result = await usage.fetch_subscription_usage({"api_base": base, "api_key": "test-key"})
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/user/subscription"
    assert requests[0].headers["xi-api-key"] == "test-key"
    assert result["ok"] is (status == 200)
    assert "Private credentials" not in str(result)
    assert "test-key" not in str(result)
