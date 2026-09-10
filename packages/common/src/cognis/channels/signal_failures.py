"""Sanitized structured failures from the Signal send boundary."""

from __future__ import annotations

import math
from typing import Any

_SIGNAL_RESULT_TYPES = {
    "RATE_LIMIT_FAILURE",
    "UNREGISTERED_FAILURE",
    "IDENTITY_FAILURE",
    "NETWORK_FAILURE",
}
_SIGNAL_CLASSIFICATIONS = {"rate_limit", "challenge", "send_failure", "unknown"}


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return None
    return converted if math.isfinite(converted) and converted >= 0 else None


def signal_failure_next_step(*, classification: str, challenge: bool) -> str:
    """Return canonical guidance without trusting provider or executor text."""
    if challenge:
        return (
            "Complete the Signal challenge or account action out of band, then reconcile "
            "delivery before any manual resend."
        )
    return {
        "rate_limit": (
            "Do not resend automatically. Reconcile Signal delivery externally before "
            "any manual resend."
        ),
        "challenge": (
            "Complete the Signal challenge or account action out of band, then reconcile "
            "delivery before any manual resend."
        ),
        "send_failure": "Reconcile the external delivery before any manual resend.",
        "unknown": "Reconcile the external delivery before any manual resend.",
    }.get(classification, "Reconcile the external delivery before any manual resend.")


def sanitize_signal_failure(
    *,
    rpc_code: int | None = None,
    error_data: Any = None,
) -> dict[str, Any]:
    """Return only allowlisted, PII-free Signal send diagnostics."""
    results: list[dict[str, Any]] = []
    response = error_data.get("response") if isinstance(error_data, dict) else None
    raw_results = response.get("results") if isinstance(response, dict) else None
    if raw_results is None and isinstance(error_data, dict):
        raw_results = error_data.get("results")
    if isinstance(raw_results, list):
        results = [item for item in raw_results if isinstance(item, dict)]

    result_types = {
        str(item.get("type")).upper()
        for item in results
        if isinstance(item.get("type"), str)
        and str(item.get("type")).upper() in _SIGNAL_RESULT_TYPES
    }
    delays = [
        delay
        for item in results
        for delay in (
            _finite_nonnegative(item.get("retryAfterSeconds")),
            _finite_nonnegative(item.get("retry_after_seconds")),
        )
        if delay is not None
    ]
    retry_after = max(delays) if delays else None
    challenge = any(
        item.get("challenge") is True
        or (isinstance(item.get("token"), str) and bool(item["token"].strip()))
        for item in results
    )
    if isinstance(error_data, dict) and error_data.get("challenge") is True:
        challenge = True

    if "RATE_LIMIT_FAILURE" in result_types or rpc_code == -5:
        classification = "rate_limit"
    elif challenge:
        classification = "challenge"
    elif result_types:
        classification = "send_failure"
    else:
        classification = "unknown"

    return {
        "provider": "signal-cli",
        "classification": classification,
        "provider_code": rpc_code if isinstance(rpc_code, int) else None,
        "retry_after_seconds": retry_after,
        "challenge": challenge,
        "next_step": signal_failure_next_step(
            classification=classification,
            challenge=challenge,
        ),
        "retry_scheduled": False,
        "side_effect_certainty": "uncertain",
        "result_count": len(results),
        "success_count": sum(item.get("type") == "SUCCESS" for item in results),
    }


class SignalDeliveryFailure(RuntimeError):
    """A sanitized Signal send failure safe to cross the executor boundary."""

    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = {
            "provider": "signal-cli",
            "classification": (
                str(metadata.get("classification"))
                if metadata.get("classification") in _SIGNAL_CLASSIFICATIONS
                else "unknown"
            ),
            "provider_code": (
                metadata.get("provider_code")
                if isinstance(metadata.get("provider_code"), int)
                else None
            ),
            "retry_after_seconds": _finite_nonnegative(metadata.get("retry_after_seconds")),
            "challenge": metadata.get("challenge") is True,
            "next_step": signal_failure_next_step(
                classification=(
                    str(metadata.get("classification"))
                    if metadata.get("classification") in _SIGNAL_CLASSIFICATIONS
                    else "unknown"
                ),
                challenge=metadata.get("challenge") is True,
            ),
            "retry_scheduled": False,
            "side_effect_certainty": "uncertain",
            "result_count": (
                metadata["result_count"]
                if type(metadata.get("result_count")) is int and metadata["result_count"] >= 0
                else 0
            ),
            "success_count": (
                metadata["success_count"]
                if type(metadata.get("success_count")) is int and metadata["success_count"] >= 0
                else 0
            ),
        }
        super().__init__(self.message)

    @property
    def message(self) -> str:
        classification = self.metadata["classification"]
        delay = self.metadata["retry_after_seconds"]
        suffix = f" (retry-after={delay:g}s)" if isinstance(delay, float) else ""
        challenge = " challenge" if self.metadata["challenge"] else ""
        return f"Signal send {classification}{challenge} failed; outcome is uncertain{suffix}"

    def safe_metadata(self) -> dict[str, Any]:
        """Return a copy suitable for JSON-RPC and durable diagnostics."""
        return dict(self.metadata)
