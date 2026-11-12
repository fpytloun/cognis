"""Source-resolution checks; unavailable archive fidelity never triggers raw scans."""

from __future__ import annotations

from cognis.core.usage.query_models import UsageAvailability, UsageQuery


class UsageResolutionError(ValueError):
    """The requested shape is unsupported by the authorized available source."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_query_availability(query: UsageQuery, available: UsageAvailability) -> None:
    """Check source compatibility after authorization and before analytical SQL.

    This is not an access grant. Retention metadata must describe only the current
    authorized scope; even an expired/unavailable response needs fresh authority.
    """
    if available.resolution == "unavailable":
        raise UsageResolutionError("usage_source_unavailable", "Usage source is unavailable")
    archived = available.resolution in {"aggregate_only", "detail_expired", "mixed"}
    if archived:
        if query.bucket == "hour" or any(
            at.hour or at.minute or at.second or at.microsecond for at in (query.start, query.end)
        ):
            raise UsageResolutionError(
                "unsupported_historic_resolution",
                "Archived accounting supports UTC-day-aligned queries, not subday detail",
            )
        if query.filters.turn_ids or query.filters.cycle_ids:
            raise UsageResolutionError(
                "unsupported_historic_filter", "Request/turn/cycle filters expire with detail"
            )
        requested_dimensions: set[str] = {query.group_by} if query.group_by else set()
        if query.view == "companions":
            requested_dimensions.update({"service", "operation"})
        if query.view == "cost_breakdowns":
            requested_dimensions.update({"subject", "agent", "provider", "model"})
        filter_dimensions = {
            "traffic_origins": "traffic_origin",
            "auth_types": "auth_type",
            "subject_emails": "subject",
            "agent_ids": "agent",
            "agent_scope": "agent",
            "provider_ids": "provider",
            "model_ids": "model",
            "profile_ids": "profile",
            "efforts": "effort",
            "purposes": "purpose",
            "hook_ids": "hook",
            "service_ids": "service",
            "operations": "operation",
            "project_ids": "project",
            "conversation_ids": "conversation",
            "session_ids": "session",
            "task_ids": "task",
            "schedule_ids": "schedule",
            "task_origin": "task",
        }
        for name, value in query.filters.model_dump(exclude_none=True).items():
            if not value:
                continue
            dimension = filter_dimensions.get(name)
            if dimension is None:
                raise UsageResolutionError(
                    "unsupported_historic_filter", "Filter requires expired request fidelity"
                )
            requested_dimensions.add(dimension)
        if not requested_dimensions.issubset(available.supported_dimensions):
            raise UsageResolutionError(
                "unsupported_historic_filter", "Source does not establish the requested dimension"
            )
        if query.pricing_basis == "current" and available.historical_repricing != "supported":
            raise UsageResolutionError(
                "unsupported_historic_repricing",
                "Retained billing strata cannot establish the requested current valuation",
            )
    if "profile" in available.unsupported_fields and (
        query.filters.profile_ids or query.group_by == "profile"
    ):
        raise UsageResolutionError("unsupported_historic_filter", "Profile fidelity is unavailable")
