"""Safe projection of committed common/ORM contracts into public usage DTOs."""

from __future__ import annotations

from typing import Any, cast, get_args

from pydantic import TypeAdapter, ValidationError

from cognis.core.usage.dimensions import (
    usage_cost_basis_to_api,
    usage_milliseconds_to_seconds,
    usage_outcome_to_api,
)
from cognis.core.usage.query_metadata import utc
from cognis.core.usage.query_models import (
    CostSubtotal,
    ErrorCategory,
    UsageAttemptDetail,
    UsageClientTiming,
    UsageLogicalTiming,
    UsageParameterSnapshot,
    UsagePrivateAttemptDetail,
    UsageRequestParameters,
    UsageServerTiming,
)
from cognis.models.usage import (
    UsageAttemptContext,
    UsageCostBasis,
    UsageModelParameters,
    UsageOutcome,
    UsageParameterName,
    UsageRequestStats,
)
from cognis.models.usage_normalization import normalize_usage_tokens
from cognis.models.usage_traffic import traffic_dimensions
from cognis.store.usage.codec import terminal_event

BASIS = {value: usage_cost_basis_to_api(value) for value in get_args(UsageCostBasis)}
OUTCOME = {value: usage_outcome_to_api(value) for value in get_args(UsageOutcome)}


def seconds(value: Any) -> float | None:
    """Convert a measured same-domain millisecond duration, not timestamps."""
    result = usage_milliseconds_to_seconds(value)
    return float(result) if result is not None else None


def parameters(payload: dict[str, Any]) -> UsageParameterSnapshot:
    """Translate the immutable common scalar envelope, never forward dictionaries."""
    safe = UsageModelParameters.model_validate(payload)
    origins = {
        "request": "explicit",
        "model_config": "configured_default",
        "route": "configured_default",
        "profile": "configured_default",
        "adapter_override": "adapter_normalized",
        "provider_default": "provider_default",
        "unknown": "observed_unspecified",
    }
    values: dict[str, Any] = {}
    for name in UsageParameterSnapshot.model_fields:
        source_name = {"reasoning_budget_tokens": "reasoning_budget"}.get(name, name)
        if source_name not in get_args(UsageParameterName):
            continue
        value = getattr(safe, source_name)
        if value is None:
            continue
        candidate = {
            "value": value,
            "provenance": origins[
                safe.provenance_source(cast(UsageParameterName, source_name)) or "unknown"
            ],
        }
        annotation = UsageParameterSnapshot.model_fields[name].annotation
        try:
            TypeAdapter(annotation).validate_python(candidate)
            values[name] = candidate
        except ValidationError:
            # Unsupported vendor values remain unavailable, not arbitrary labels.
            values[name] = {"provenance": "unsupported"}
    return UsageParameterSnapshot.model_validate(values)


def server_event(stats: Any) -> UsageRequestStats | None:
    """A logical completion needs the trusted correlated server envelope."""
    if stats is None or not stats.server_payload:
        return None
    try:
        event = UsageRequestStats.model_validate(
            {"attribution": stats.attribution, **stats.server_payload}
        )
    except ValidationError:
        return None
    if (
        event.stats_scope != "server"
        or event.request_id != stats.request_id
        or event.observation_id != stats.server_observation_id
        or event.attribution.identity.usage_subject_key != stats.usage_subject_key
    ):
        return None
    return event


def logical_outcome(stats: Any) -> str | None:
    """Do not infer request success/failure from arbitrary last attempt/typed column."""
    event = server_event(stats)
    return OUTCOME.get(event.logical_outcome) if event is not None else None


def logical_timing(stats: Any) -> UsageLogicalTiming | None:
    """Only authenticated, bound canonical common envelopes can supply client E2E."""
    if stats is None:
        return None
    server = None
    client = None
    if stats.server_payload:
        event = server_event(stats)
        if (
            event is not None
            and event.request_id == stats.request_id
            and event.stats_scope == "server"
            and event.server is not None
            and event.server.clock_domain == "controller"
        ):
            server = UsageServerTiming(
                elapsed_seconds=seconds(event.server.elapsed_ms),
                send_duration_seconds=seconds(event.server.send_elapsed_ms),
            )
    if stats.client_payload:
        try:
            event = UsageRequestStats.model_validate(
                {"attribution": stats.attribution, **stats.client_payload}
            )
        except ValidationError:
            event = None
        if (
            event is not None
            and event.request_id == stats.request_id
            and event.stats_scope == "client"
            and event.client is not None
            and event.client.source == "authenticated_report"
            and event.attribution.identity.usage_subject_key == stats.usage_subject_key
            and event.observation_id == stats.client_observation_id
        ):
            client = UsageClientTiming(
                source="authenticated_client_report",
                correlation_verified=True,
                first_chunk_seconds=seconds(event.client.ttft_ms),
                last_chunk_seconds=seconds(event.client.elapsed_ms),
            )
    return UsageLogicalTiming(server=server, client=client) if server or client else None


def attempt_state(row: Any) -> str:
    """Retained observation state; never a guessed logical request outcome."""
    return (
        (OUTCOME.get(row.outcome) or "unknown")
        if row.finalized_at
        else (
            "inflight"
            if row.dispatch_status in {"dispatch_observed", "provider_accepted"}
            else "observed_start"
        )
    )


def attempt_detail(
    row: Any,
    valuation: Any,
    stats: Any,
    *,
    private: bool,
    basis: str,
    price_revision: Any = None,
    selection_ref: str | None = None,
) -> Any:
    """Content-free details; team output is constructed from a reduced model."""
    context = UsageAttemptContext.model_validate(row.context_payload)
    traffic = traffic_dimensions(context.attribution)
    terminal = terminal_event(row) if row.terminal_payload is not None else None
    normalized = normalize_usage_tokens(terminal.usage) if terminal is not None else None
    state = attempt_state(row)
    costs = []
    amount = getattr(valuation, f"{basis}_cost") if valuation is not None else None
    status = getattr(valuation, f"{basis}_status") if valuation is not None else "unknown"
    currency = (
        valuation.currency
        if valuation is not None and valuation.basis == "provider_reported"
        else price_revision.currency
        if price_revision is not None
        else (valuation.currency if valuation is not None else None)
    )
    cost_basis = (
        "provider_reported"
        if valuation is not None and valuation.basis == "provider_reported"
        else price_revision.basis
        if price_revision is not None
        else (valuation.basis if valuation is not None else None)
    )
    if (
        amount is not None
        and status in ("complete", "partial")
        and currency
        and cost_basis in BASIS
        and (basis == "historical" or price_revision is not None)
    ):
        costs.append(
            CostSubtotal(
                known_amount=format(amount, "f"),
                currency=currency,
                basis=BASIS[cost_basis],
                quality=status,
                priced_attempts=int(status == "complete"),
                unpriced_attempts=int(status != "complete"),
                partially_priced_attempts=int(status == "partial"),
            )
        )
    category = {
        "context_limit": "invalid_request",
        "provider_unavailable": "provider",
        "transport": "connection",
        "permission": "authentication",
    }.get(row.error_category, row.error_category)
    allowed_categories = set(TypeAdapter(ErrorCategory).json_schema()["enum"])
    if category not in allowed_categories:
        category = "unknown" if row.outcome == "error" else None
    generation = seconds(row.generation_elapsed_ms)
    # Stored source is provider/native-sdk/adapter, not a clock-domain claim.
    ttft = seconds(row.ttft_ms) if row.streaming else None
    token_counts = {
        "input": normalized.input if normalized else None,
        "output": normalized.output if normalized else None,
        "cache_read": normalized.cache_read if normalized else None,
        "cache_write": normalized.cache_write if normalized else None,
        "reasoning": normalized.reasoning if normalized else None,
    }
    output = normalized.output if normalized else None
    normalized_complete = (
        normalized is not None
        and normalized.input.status == "reported"
        and normalized.output.status == "reported"
        and not any(
            value.status == "partial" for value in token_counts.values() if value is not None
        )
    )
    quality = (
        "complete"
        if row.usage_completeness == "reported"
        and (normalized_complete or terminal is not None and bool(terminal.usage.extras))
        else "partial"
        if any(value is not None and value.value is not None for value in token_counts.values())
        else "unknown"
    )
    values = dict(
        media_usage=list(terminal.usage.extras) if terminal is not None else [],
        inference_operation=context.selection.operation,
        traffic_origin=traffic["traffic_origin"],
        auth_type=traffic["traffic_auth_type"],
        attempt_id=row.attempt_id if private else None,
        subject_email=row.usage_subject_id if row.usage_subject_kind == "user" else None,
        subject_kind=row.usage_subject_kind,
        agent_id=row.agent_id,
        logical_request_id=row.request_id if private else None,
        selection_ref=selection_ref,
        project_id=row.project_id,
        conversation_id=row.conversation_id,
        session_id=row.session_id,
        task_id=row.task_id,
        step_run_id=row.step_run_id,
        turn_id=row.turn_id if private else None,
        cycle_id=row.cycle_id if private else None,
        provider_id=row.provider_id,
        requested_model=row.requested_model,
        resolved_model=row.effective_model,
        reported_model=row.provider_reported_model,
        effort=row.effective_effort,
        purpose=row.purpose,
        hook_id=row.hook_id,
        hook_name=row.hook_name,
        hook_type=row.hook_type,
        hook_invocation_id=row.hook_invocation_id,
        service_id=row.service_id,
        operation=row.service_operation,
        started_at=utc(row.started_at_observed),
        finished_at=utc(row.ended_at) if row.ended_at else None,
        state=state,
        streaming=row.streaming,
        retry=row.retry_of_attempt_id is not None,
        fallback=row.fallback_from_attempt_id is not None,
        status_code=row.http_status,
        error_category=category,
        exception_class=row.exception_class,
        provider_error_code=row.provider_error_code,
        ttft_seconds=ttft,
        latency_seconds=seconds(row.provider_elapsed_ms),
        generation_seconds=generation,
        generation_tokens_per_second=(
            output.value / generation
            if generation
            and output is not None
            and output.status == "reported"
            and output.value is not None
            else None
        ),
        provider_timing_source="unknown",
        logical_timing=logical_timing(stats),
        parameters=UsageRequestParameters(
            service_tier_applicability=context.selection.service_tier_applicability,
            requested=parameters(context.selection.requested_parameters.model_dump(mode="json")),
            effective=parameters(context.selection.effective_parameters.model_dump(mode="json")),
        ),
        tokens={
            name: value.value if value is not None else None for name, value in token_counts.items()
        },
        costs=costs,
        usage_quality=quality,
    )
    if private:
        values.update(
            subject_id=row.usage_subject_id,
            key_id=row.key_record_id,
            client_id=row.client_id,
            provider_request_id=row.provider_request_id,
        )
    model = UsagePrivateAttemptDetail if private else UsageAttemptDetail
    return model.model_validate(values)
