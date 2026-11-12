"""One canonical context plus a nonduplicated terminal body and query scalars."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cognis.models.usage import UsageAttemptContext, UsageAttemptFinalized
from cognis.models.usage_traffic import traffic_dimensions
from cognis.store.usage.models import UsageAttemptRow


def context_values(context: UsageAttemptContext, now: datetime) -> dict[str, Any]:
    """Flatten only supported query fields; retain context exactly once."""
    table = UsageAttemptRow.__table__
    values: dict[str, Any] = {column.name: None for column in table.columns if column.nullable}
    identity = context.attribution.identity
    trace = context.trace
    selection = context.selection
    values.update(identity.model_dump(exclude={"usage_subject_kind", "usage_subject_id"}))
    values.update(trace.model_dump(exclude={"execution_id"}))
    values.update(
        owner_key=identity.resource_owner or identity.usage_subject_key,
        usage_subject_key=identity.usage_subject_key,
        usage_subject_kind=identity.usage_subject_kind,
        usage_subject_id=identity.usage_subject_id,
        controller_owner_id=context.controller_owner_id,
        execution_id=trace.execution_id,
        purpose=context.attribution.purpose,
        requested_parameters={},
        effective_parameters={},
        coverage_epoch_id=context.coverage_epoch_id,
        started_at_observed=context.started_at,
        bucket_day=context.started_at.date(),
        registered_at=now,
        context_digest=context.observation_digest(),
        context_payload=context.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        start_observed=False,
        fact_version=0,
        projected_version=0,
        usage_completeness="unknown",
        input_status="unknown",
        output_status="unknown",
        cache_read_status="unknown",
        cache_write_status="unknown",
        reasoning_status="unknown",
    )
    values.update(selection.model_dump(exclude={"requested_parameters", "effective_parameters"}))
    # The portable principal carries these names; the ORM uses the exact same keys.
    if context.attribution.resources is not None:
        binding = context.attribution.resources
        values.update(binding.model_dump(exclude={"provenance"}))
        values["binding_provenance"] = binding.provenance
    if context.attribution.hook is not None:
        hook = context.attribution.hook
        values.update(hook.model_dump(exclude={"invocation_id", "occurrence"}))
        values["hook_invocation_id"] = hook.invocation_id
    if context.executor is not None:
        values["executor_id"] = context.executor.executor_id
        values["executor_request_id"] = context.executor.inference_request_id
    values["client_session_id"] = (
        context.attribution.client_labels.session_id
        if context.attribution.client_labels is not None
        else None
    )
    values.update(traffic_dimensions(context.attribution))
    return {key: value for key, value in values.items() if key in table.columns}


def terminal_values(event: UsageAttemptFinalized, now: datetime) -> dict[str, Any]:
    """Duplicate replay must preserve the first trusted persistence timestamp."""
    usage = event.usage
    values: dict[str, Any] = {
        "terminal_digest": event.observation_digest(),
        "terminal_payload": event.model_dump(
            mode="json", exclude={"context"}, exclude_none=True, exclude_defaults=True
        ),
        "observation_id": event.observation_id,
        "finalized_at": now,
        "outcome": event.outcome,
        "dispatch_status": event.dispatch_status,
        "provider_request_id": event.provider_request_id,
        "provider_reported_model": event.provider_reported_model,
        "usage_completeness": usage.completeness,
        "normalization_version": usage.normalization_version,
        "semantic_profile": usage.semantic_profile,
        "semantic_version": usage.semantic_version,
        "cache_read_semantics": usage.cache_read_semantics,
        "cache_write_semantics": usage.cache_write_semantics,
        "reasoning_semantics": usage.reasoning_semantics,
        "cache_eligible": usage.cache_eligible,
        "gateway_response_cache_hit": usage.gateway_response_cache_hit,
        "performance_source": event.performance.source,
    }
    for name in (
        "input_tokens",
        "output_tokens",
        "provider_total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "reasoning_tokens",
    ):
        values[name] = getattr(usage, name).value
    for name in ("input", "output", "cache_read", "cache_write", "reasoning"):
        values[f"{name}_status"] = getattr(usage, f"{name}_tokens").status
    values.update(event.performance.model_dump(exclude={"source", "clock_domain"}))
    for name in (
        "error_category",
        "error_summary_code",
        "exception_class",
        "provider_error_code",
        "http_status",
    ):
        values[name] = None
    if event.error is not None:
        values.update(event.error.model_dump(exclude={"category", "summary_code"}))
        values["error_category"] = event.error.category
        values["error_summary_code"] = event.error.summary_code
    return values


def terminal_event(row: UsageAttemptRow) -> UsageAttemptFinalized:
    """Reconstruct the frozen envelope without storing a second full context."""
    if row.terminal_payload is None:
        raise ValueError("Attempt has no terminal observation")
    return UsageAttemptFinalized.model_validate(
        {**row.terminal_payload, "context": row.context_payload}
    )
