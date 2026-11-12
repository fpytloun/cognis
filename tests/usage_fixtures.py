"""Small content-free fixtures for accounting contract and schema qualification."""

from datetime import UTC, datetime

from cognis.models.usage import (
    NormalizedUsage,
    UsageAttemptContext,
    UsageAttemptFinalized,
    UsageAttribution,
    UsageIdentity,
    UsageModelSelection,
    UsagePerformance,
    UsageTrace,
)

OBSERVED_AT = datetime(2026, 10, 29, 19, 0, tzinfo=UTC)


def attempt_context() -> UsageAttemptContext:
    return UsageAttemptContext(
        trace=UsageTrace(
            request_id="req_usage_contract",
            cycle_id="cycle_usage_contract",
            attempt_id="attempt_usage_contract",
            attempt_ordinal=0,
        ),
        attribution=UsageAttribution(
            identity=UsageIdentity(
                principal_subject="usage+test@cognis.test",
                principal_type="user",
                usage_subject_kind="user",
                usage_subject_id="usage+test@cognis.test",
            ),
            purpose="evaluation",
        ),
        selection=UsageModelSelection(
            provider_id="provider_usage_contract",
            effective_model="chat-model",
            streaming=True,
            source="provider",
            operation="chat",
        ),
        started_at=OBSERVED_AT,
        controller_owner_id="controller_usage_contract",
        coverage_epoch_id="coverage_usage_contract",
    )


def finalized_attempt() -> UsageAttemptFinalized:
    return UsageAttemptFinalized(
        context=attempt_context(),
        observation_id="observation_usage_final",
        outcome="success",
        dispatch_status="provider_accepted",
        finalized_at=OBSERVED_AT,
        performance=UsagePerformance(source="provider", total_elapsed_ms=120),
        usage=NormalizedUsage(
            semantic_profile="openai",
            semantic_version=1,
            cache_read_semantics="included_in_input",
            cache_write_semantics="unknown",
            reasoning_semantics="included_in_output",
        ),
    )
