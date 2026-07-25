from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from cognis.security import (
    LoginRateLimiter,
    RequestRateLimiter,
    create_password_hasher,
    generate_api_key_material,
    parse_api_key,
    verify_api_key,
)


def test_api_key_generation_and_parse() -> None:
    key_id, plaintext = generate_api_key_material()
    parsed = parse_api_key(plaintext)

    assert key_id.startswith("ck")
    assert parsed is not None
    assert parsed[0] == key_id


def test_api_key_hash_verification() -> None:
    hasher = create_password_hasher()
    _, plaintext = generate_api_key_material()
    hashed = hasher.hash(plaintext)

    assert verify_api_key(hasher, plaintext, hashed) is True
    assert verify_api_key(hasher, plaintext + "x", hashed) is False


def test_login_rate_limiter_blocks_after_limit() -> None:
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("user@example.com")
    assert limiter.is_limited("user@example.com") is False
    limiter.record_failure("user@example.com")
    assert limiter.is_limited("user@example.com") is True
    limiter.clear("user@example.com")
    assert limiter.is_limited("user@example.com") is False


def test_request_rate_limiter_rejects_unseen_keys_without_evicting_active_buckets() -> None:
    bounded = RequestRateLimiter(
        read_requests_per_minute=2,
        window_seconds=60,
        max_state_entries=3,
    )

    assert asyncio.run(bounded.allow(user_key="first", path="/first", method="GET")) is True
    assert asyncio.run(bounded.allow(user_key="second", path="/second", method="GET")) is True
    assert asyncio.run(bounded.allow(user_key="third", path="/third", method="GET")) is True

    for index in range(1_000):
        assert (
            asyncio.run(
                bounded.allow(
                    user_key=f"unseen-{index}",
                    path="/distributed",
                    method="GET",
                )
            )
            is False
        )

    assert list(bounded._state) == ["first:read", "second:read", "third:read"]
    assert asyncio.run(bounded.allow(user_key="first", path="/first", method="GET")) is True
    assert asyncio.run(bounded.allow(user_key="first", path="/first", method="GET")) is False
    assert len(bounded._state["first:read"].requests) == 2


def test_request_rate_limiter_recovers_expired_capacity_without_active_eviction() -> None:
    current = datetime(2026, 7, 13, tzinfo=UTC)

    def clock() -> datetime:
        return current

    limiter = RequestRateLimiter(
        read_requests_per_minute=1,
        window_seconds=60,
        max_state_entries=2,
        clock=clock,
    )
    assert asyncio.run(limiter.allow(user_key="first", path="/first", method="GET")) is True
    current += timedelta(seconds=10)
    assert asyncio.run(limiter.allow(user_key="second", path="/second", method="GET")) is True
    assert asyncio.run(limiter.allow(user_key="unseen", path="/unseen", method="GET")) is False

    current += timedelta(seconds=51)
    assert asyncio.run(limiter.allow(user_key="replacement", path="/new", method="GET")) is True
    assert list(limiter._state) == ["second:read", "replacement:read"]
    assert asyncio.run(limiter.allow(user_key="second", path="/second", method="GET")) is False


def test_request_rate_limiter_prunes_accessed_bucket_with_nonzero_window() -> None:
    current = datetime(2026, 7, 13, tzinfo=UTC)

    def clock() -> datetime:
        return current

    limiter = RequestRateLimiter(
        read_requests_per_minute=1,
        window_seconds=60,
        clock=clock,
    )

    assert asyncio.run(limiter.allow(user_key="user", path="/first", method="GET")) is True
    current += timedelta(seconds=30)
    assert asyncio.run(limiter.allow(user_key="user", path="/second", method="GET")) is False
    current += timedelta(seconds=31)
    assert asyncio.run(limiter.allow(user_key="user", path="/third", method="GET")) is True
