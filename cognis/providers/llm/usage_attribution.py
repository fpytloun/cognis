"""Trusted controller attribution construction; client labels are not bindings."""

from __future__ import annotations

from typing import Any

from cognis.models.usage import (
    UsageAttribution,
    UsageIdentity,
    UsageModelParameters,
    UsageResourceBinding,
)
from cognis.providers.llm.usage_capture import UsageCapture, UsageRequest, optional_id
from cognis.providers.llm.usage_parameters import project_parameters
from cognis.providers.llm.usage_scope import current_usage_execution

ATTRIBUTION_KEY = "_cognis_usage_attribution"


def controller_request(
    capture: UsageCapture | None,
    *,
    actor: str | None,
    purpose: str,
    model: str | None,
    kwargs: dict[str, Any],
) -> UsageRequest | None:
    """Create one request before routing can replace actor with provider owner."""
    supplied = kwargs.pop(ATTRIBUTION_KEY, None)
    if capture is None:
        return None
    if isinstance(supplied, UsageAttribution):
        attribution = supplied
        if attribution.identity.traffic_origin == "unknown":
            attribution = attribution.model_copy(
                update={
                    "identity": attribution.identity.model_copy(update={"traffic_origin": "cognis"})
                }
            )
    else:
        try:
            identity = UsageIdentity(
                traffic_origin="cognis",
                principal_type="user" if actor else "service",
                principal_subject=actor or "cognis",
                usage_subject_kind="user" if actor else "service",
                usage_subject_id=actor or "cognis",
                initiating_actor=actor,
                service_id=None if actor else "cognis",
            )
        except Exception:
            capture.failed_events += 1
            identity = UsageIdentity(
                principal_type="unattributed", usage_subject_kind="unattributed"
            )
        values = {
            field: value
            for field, key in (
                ("session_id", "cognis_session_id"),
                ("conversation_id", "cognis_conversation_id"),
                ("agent_id", "cognis_agent_id"),
                ("agent_profile_id", "cognis_agent_profile_id"),
                ("project_id", "cognis_project_id"),
                ("task_id", "cognis_task_id"),
                ("step_run_id", "cognis_step_run_id"),
                ("turn_id", "cognis_turn_id"),
            )
            if (value := optional_id(kwargs.get(key))) is not None
        }
        execution = current_usage_execution.get()
        if (
            execution is not None
            and actor == execution.actor
            and values.get("conversation_id", execution.conversation_id)
            == execution.conversation_id
        ):
            # Explicit physical identities win, including after session rotation.
            values = execution.request_values() | values
        attribution = UsageAttribution(
            identity=identity,
            purpose=optional_id(purpose) or "inference",
            resources=UsageResourceBinding(provenance="controller", **values) if values else None,
        )
    try:
        parameters = project_parameters(kwargs)
    except Exception:
        capture.failed_events += 1
        parameters = UsageModelParameters()
    return capture.request(
        attribution=attribution,
        request_id=optional_id(kwargs.get("cognis_llm_request_id")),
        cycle_id=optional_id(kwargs.get("cognis_cycle_id")),
        requested_model=model,
        requested_parameters=parameters,
    )
