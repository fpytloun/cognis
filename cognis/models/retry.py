"""User-safe retry reason contract."""

from enum import StrEnum


class RetryReason(StrEnum):
    MANUAL_RETRY = "manual_retry"
    CONTROLLER_RESTART = "controller_restart"
    EXECUTOR_RECONNECT = "executor_reconnect"
    TRANSIENT_RUNTIME = "transient_runtime"


def normalize_retry_reason(value: RetryReason | str | None) -> RetryReason:
    if isinstance(value, RetryReason):
        return value
    try:
        return RetryReason(value)
    except (TypeError, ValueError):
        return RetryReason.TRANSIENT_RUNTIME


def retry_notice_text(value: RetryReason | str | None) -> str:
    return {
        RetryReason.MANUAL_RETRY: "Retrying turn on request…",
        RetryReason.CONTROLLER_RESTART: "Retrying turn after controller restart…",
        RetryReason.EXECUTOR_RECONNECT: "Retrying turn after executor reconnect…",
        RetryReason.TRANSIENT_RUNTIME: "Retrying turn after a temporary runtime interruption…",
    }[normalize_retry_reason(value)]


def retry_reason_from_interruption(value: str | None) -> RetryReason:
    if value == RetryReason.CONTROLLER_RESTART.value:
        return RetryReason.CONTROLLER_RESTART
    if value == RetryReason.EXECUTOR_RECONNECT.value:
        return RetryReason.EXECUTOR_RECONNECT
    return RetryReason.TRANSIENT_RUNTIME
