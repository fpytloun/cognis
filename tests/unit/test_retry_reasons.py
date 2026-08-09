from cognis.models.retry import (
    RetryReason,
    normalize_retry_reason,
    retry_notice_text,
    retry_reason_from_interruption,
)
from cognis.store.direct_turns import DirectTurnPayloadV1


def test_retry_reason_contract_is_closed_and_user_safe() -> None:
    assert {reason.value for reason in RetryReason} == {
        "manual_retry",
        "controller_restart",
        "executor_reconnect",
        "transient_runtime",
    }
    assert retry_notice_text(RetryReason.CONTROLLER_RESTART) == (
        "Retrying turn after controller restart…"
    )
    assert retry_notice_text(RetryReason.EXECUTOR_RECONNECT) == (
        "Retrying turn after executor reconnect…"
    )
    assert "temporary runtime interruption" in retry_notice_text(RetryReason.TRANSIENT_RUNTIME)
    assert retry_notice_text(RetryReason.MANUAL_RETRY) == "Retrying turn on request…"
    assert normalize_retry_reason("legacy_internal_exception") is RetryReason.TRANSIENT_RUNTIME
    assert retry_reason_from_interruption("controller_restart") is RetryReason.CONTROLLER_RESTART
    assert retry_reason_from_interruption("executor_reconnect") is RetryReason.EXECUTOR_RECONNECT


def test_old_and_unknown_durable_payloads_remain_compatible() -> None:
    old = DirectTurnPayloadV1.model_validate(
        {"schema_version": 1, "content": "hello", "attachments": [], "metadata": {}}
    )
    unknown = DirectTurnPayloadV1.model_validate(
        {
            "schema_version": 1,
            "content": "hello",
            "attachments": [],
            "metadata": {},
            "retry_reason": "legacy_reason",
        }
    )

    assert old.retry_reason is None
    assert unknown.retry_reason is RetryReason.TRANSIENT_RUNTIME
