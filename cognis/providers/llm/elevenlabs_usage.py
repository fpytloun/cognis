"""Read-only ElevenLabs subscription credits in the shared provider quota shape."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.providers.llm.elevenlabs import base, headers
from cognis.providers.llm.hosted import api_endpoint

SOURCE = "elevenlabs_subscription_usage"


def unavailable_usage(reason: str) -> dict[str, Any]:
    """Report an unknown quota without inventing exhaustion or exposing errors."""
    return {
        "ok": False,
        "source": SOURCE,
        "fetched_at": datetime.now(UTC).isoformat(),
        "unavailable_reason": reason,
        "additional_rate_limits": [],
    }


async def fetch_subscription_usage(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Fetch metadata once; restricted billing access is not an inference outage."""
    if not request_kwargs.get("api_key"):
        return unavailable_usage("Subscription quota requires an API key")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                api_endpoint(base(request_kwargs), "user/subscription"),
                headers=headers(request_kwargs),
            )
    except httpx.RequestError:
        return unavailable_usage("Subscription quota endpoint is unavailable")
    if response.status_code in {401, 403}:
        return unavailable_usage("Subscription quota access is denied for this API key")
    if response.status_code == 429:
        return unavailable_usage("Subscription quota endpoint is rate limited")
    if not response.is_success:
        return unavailable_usage("Subscription quota endpoint is unavailable")
    try:
        payload = response.json()
    except ValueError:
        return unavailable_usage("Subscription quota response is invalid")
    if not isinstance(payload, Mapping):
        return unavailable_usage("Subscription quota response is invalid")
    return normalize_subscription_usage(payload)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_subscription_usage(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep credit counts and actual reset dates; never assume a rolling month."""
    used = _count(payload.get("character_count"))
    limit = _count(payload.get("character_limit"))
    if used is None or limit is None:
        return unavailable_usage("Subscription quota response has no valid credit counts")
    now = datetime.now(UTC)
    reset_at = None
    timestamp = _count(payload.get("next_character_count_reset_unix"))
    if timestamp:
        with suppress(OverflowError, OSError, ValueError):
            reset_at = datetime.fromtimestamp(timestamp, UTC)

    def window(consumed: int, maximum: int) -> dict[str, Any]:
        return {
            "used_percent": min(consumed, maximum) / maximum * 100 if maximum else 100.0,
            "limit": maximum,
            "remaining": max(0, maximum - consumed),
            "resets_at": reset_at.isoformat() if reset_at else None,
            "reset_after_seconds": max(0, int((reset_at - now).total_seconds()))
            if reset_at
            else None,
        }

    extension = payload.get("max_credit_limit_extension")
    extension_count = _count(extension)
    entitled = payload.get("can_extend_character_limit")
    unlimited = entitled is True and extension == "unlimited"
    extra = extension_count if entitled is True else None
    additional = [
        {
            "limit_id": "subscription_credits",
            "limit_name": "Included credits · uncapped overage"
            if unlimited
            else "Subscription credits",
            "primary": window(used, limit),
        }
    ]
    if extra:
        additional.append(
            {
                "limit_id": "usage_based_credits",
                "limit_name": "Usage-based credits",
                "primary": window(max(0, used - limit), extra),
            }
        )
    # Entitlement alone does not enable overage: the workspace cap must permit it.
    allowed: bool | None
    if used < limit or unlimited:
        allowed = True
    elif entitled is False or extension_count == 0:
        allowed = False
    elif extra is not None:
        allowed = used < limit + extra
    else:
        allowed = None
    return {
        "ok": True,
        "source": SOURCE,
        "fetched_at": now.isoformat(),
        "plan_type": payload.get("tier") if isinstance(payload.get("tier"), str) else None,
        "allowed": allowed,
        "limit_reached": not allowed if allowed is not None else None,
        "additional_rate_limits": additional,
    }
