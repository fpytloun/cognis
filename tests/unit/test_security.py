from __future__ import annotations

from cognis.security import (
    LoginRateLimiter,
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
