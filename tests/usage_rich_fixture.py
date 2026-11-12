"""Synthetic dense metadata with fixed-width IDs and no payload or client secrets."""

from datetime import UTC, datetime, timedelta

from cognis.models.usage import UsageAttemptFinalized
from tests.usage_fixtures import finalized_attempt


def dense_usage_event() -> UsageAttemptFinalized:
    data = finalized_attempt().model_dump()
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    context = data["context"]
    context["trace"] = {
        name: f"{name}_00000000000000000000000000000002"
        for name in (
            "request_id",
            "cycle_id",
            "attempt_id",
            "retry_of_attempt_id",
            "fallback_from_attempt_id",
            "parent_operation_id",
            "execution_id",
        )
    } | {"attempt_ordinal": 2}
    identity = context["attribution"]["identity"]
    identity.update(
        on_behalf_user="usage+test@cognis.test",
        initiating_actor="operator@cognis.test",
        resource_owner="usage+test@cognis.test",
        provider_payer="payer@cognis.test",
        routing_owner="payer@cognis.test",
        auth_type="service_jwt",
        service_id="intaris",
        service_operation="policy_compile",
        client_id="client_0000000000000001",
        key_record_id="key_0000000000000002",
        token_record_id="token_0000000000000003",
    )
    context["attribution"]["resources"] = {
        "provenance": "controller",
        **{
            name: f"{name}_00000000000000000000000000000002"
            for name in (
                "project_id",
                "conversation_id",
                "session_id",
                "activity_scope_id",
                "task_id",
                "step_run_id",
                "turn_id",
                "agent_id",
                "agent_profile_id",
            )
        },
    }
    context["attribution"]["hook"] = {
        "hook_id": "hook_0000000000000007",
        "hook_revision": 3,
        "hook_name": "Průběžné shrnutí 🛠️",
        "hook_type": "after_turn",
        "invocation_id": "hrun_0000000000000008",
        "occurrence": "occurrence_01",
    }
    context["attribution"]["client_labels"] = {
        "operation": "policy_compile",
        "operation_id": "operation_0000000000000002",
        "client_request_id": "request_0000000000000003",
        "client_version": "1.2.3",
        "project_id": "project_unverified",
        "session_id": "session_unverified",
        "task_id": "task_unverified",
    }
    params = {
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 20,
        "max_input_tokens": 128000,
        "max_output_tokens": 8192,
        "max_total_tokens": 136192,
        "seed": 42,
        "frequency_penalty": 0.1,
        "presence_penalty": 0.2,
        "repetition_penalty": 1.05,
        "reasoning_effort": "high",
        "reasoning_budget": 4096,
        "reasoning_enabled": True,
        "service_tier": "priority",
        "response_format": "json_schema",
        "tool_choice": "required",
    }
    params["provenance"] = [{"parameter": name, "source": "adapter_override"} for name in params]
    context["selection"].update(
        requested_model="provider/deployment/" + "m" * 160,
        effective_model="provider/deployment/" + "m" * 160,
        requested_route="route_deep",
        requested_effort="high",
        effective_effort="high",
        reasoning_budget=4096,
        service_tier="priority",
        cache_ttl="5m",
        captured_price_revision_id="upr_0000000000000002",
        resolver_snapshot_id="resolver_01",
        requested_parameters=params,
        effective_parameters=params,
    )
    context["executor"] = {
        "executor_id": "executor_0000000000000002",
        "controller_owner_id": "controller_a",
        "inference_request_id": "inference_0000000000000003",
        "connection_epoch": 3,
    }
    data["usage"].update(
        completeness="reported",
        cache_eligible=True,
        gateway_response_cache_hit=False,
        extras=[
            {"name": name, "value": 1500, "unit": unit, "source": "native_sdk"}
            for name, unit in (
                ("input_audio_tokens", "tokens"),
                ("output_audio_tokens", "tokens"),
                ("input_image_tokens", "tokens"),
                ("input_audio_duration_ms", "milliseconds"),
                ("output_audio_duration_ms", "milliseconds"),
                ("input_video_duration_ms", "milliseconds"),
                ("output_video_duration_ms", "milliseconds"),
                ("input_image_count", "images"),
                ("output_image_count", "images"),
                ("input_characters", "characters"),
                ("output_characters", "characters"),
                ("billed_characters", "characters"),
            )
        ],
    )
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
        data["usage"][name] = {"value": 100, "status": "reported", "source": "provider"}
    data["performance"].update(
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        total_elapsed_ms=2000,
        dispatch_elapsed_ms=1900,
        provider_elapsed_ms=1850,
        ttft_ms=100,
        first_generated_at=now + timedelta(milliseconds=100),
        last_generated_at=now + timedelta(milliseconds=1900),
        generation_elapsed_ms=1800,
    )
    data.update(
        provider_request_id="req_00000000000000000000000000000002",
        provider_reported_model="provider/deployment/" + "m" * 160,
        error={
            "category": "rate_limit",
            "summary_code": "provider_rate_limited",
            "exception_class": "RateLimitError",
            "provider_error_code": "rate_limit_exceeded",
            "http_status": 429,
        },
    )
    # Diagnostic subclass preserves every field and validates every nested model;
    # it is used only to measure a rejected envelope, never to bypass production.
    from pydantic import model_validator

    class MeasuredEnvelope(UsageAttemptFinalized):
        @model_validator(mode="after")
        def validate_size(self) -> "MeasuredEnvelope":
            size = len(self.model_dump_json(exclude_none=True, exclude_defaults=True).encode())
            context_size = len(
                self.context.model_dump_json(exclude_none=True, exclude_defaults=True).encode()
            )
            print(f"usage_rich_measured_bytes={size}; context_bytes={context_size}")
            return self

    MeasuredEnvelope.model_validate(data)
    return UsageAttemptFinalized.model_validate(data)
