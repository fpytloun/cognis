"""Safe runtime metadata exposed on assistant turns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def assistant_message_runtime_metadata(
    agent: object,
    runtime_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return non-secret runtime identity/inference metadata for one assistant turn."""

    info = runtime_info or {}
    metadata: dict[str, Any] = {
        "agent_id": _string_value(getattr(agent, "agent_id", None)),
        "agent_name": _string_value(getattr(agent, "name", None)),
        "agent_display_name": _string_value(getattr(agent, "display_name", None)),
        "requested_agent_profile_id": _string_value(info.get("requested_agent_profile_id")),
        "agent_profile_id": _string_value(info.get("resolved_agent_profile_id")),
        "agent_profile_source": _string_value(info.get("agent_profile_source")),
        "agent_profile_synthetic": _bool_value(info.get("agent_profile_synthetic")),
        "provider_id": _string_value(
            info.get("resolved_provider_id") or info.get("agent_profile_provider_id")
        ),
        "model": _string_value(
            info.get("resolved_model")
            or info.get("current_model")
            or info.get("agent_profile_model")
        ),
        "reasoning_effort": _string_value(
            info.get("reasoning_effort")
            or info.get("current_reasoning_effort")
            or info.get("agent_profile_reasoning_effort")
        ),
    }
    return {key: value for key, value in metadata.items() if value is not None}
