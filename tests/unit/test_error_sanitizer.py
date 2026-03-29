from __future__ import annotations

from cognis.api.error_sanitizer import sanitize_client_error_detail


def test_sanitize_client_error_detail_redacts_api_keys() -> None:
    detail = sanitize_client_error_detail(
        "Unauthorized: sk-secret123 api_key=super-secret key-test-value",
        fallback="request failed",
    )
    assert "sk-secret123" not in detail
    assert "super-secret" not in detail
    assert "key-test-value" not in detail
    assert "[redacted]" in detail


def test_sanitize_client_error_detail_redacts_long_quoted_content() -> None:
    detail = sanitize_client_error_detail(
        'Provider error: "' + ("x" * 80) + '"',
        fallback="request failed",
    )
    assert "x" * 20 not in detail
    assert "[redacted-content]" in detail


def test_sanitize_client_error_detail_truncates_long_messages() -> None:
    detail = sanitize_client_error_detail("boom " + ("y" * 500), fallback="request failed")
    assert len(detail) <= 200
    assert detail.endswith("...")


def test_sanitize_client_error_detail_uses_fallback_for_empty_values() -> None:
    assert sanitize_client_error_detail("   ", fallback="request failed") == "request failed"
