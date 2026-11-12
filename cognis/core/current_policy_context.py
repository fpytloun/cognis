"""Ephemeral, acknowledged Intaris policy evidence for model context."""

from __future__ import annotations

import json
from typing import Any

from cognis.guardrails_redaction import redact_runtime_evidence
from cognis.models.session import IntarisSession, SessionModel

_HEADING = "CURRENT POLICY SNAPSHOT\n"
_GUIDANCE = (
    "Observed controller policy, not a user grant. Earlier denials are history, not proof "
    "of a current prohibition; explicit human refusals and operational preconditions still "
    "bind. Tools are still evaluated normally: do not evade a denial or retry an ambiguous "
    "executed mutation. Unknown fields grant no permission."
)


async def current_policy_message(guardrails: Any, session: SessionModel) -> dict[str, Any]:
    """Read the current session owner without a second policy cache or policy mutation."""
    values: dict[str, Any] = {
        "availability": "unknown",
        "evaluation_mode": "unknown",
        "profile": "unknown",
        "maximum_outcome": "unknown",
        "minimum_outcome": "unknown",
        "interaction_mode": "unknown",
        "revision": "unknown",
    }
    try:
        read = getattr(guardrails, "get_session", None)
        intaris_session_id = session.intaris_session_id or session.session_id
        observed = await read(intaris_session_id) if read is not None else None
    except Exception:
        # Optional context evidence may degrade; tool evaluation remains fail-closed.
        observed = None
    if (
        isinstance(observed, IntarisSession)
        and observed.session_id == intaris_session_id
        and observed.user_id == session.user_email
        and isinstance(observed.policy, dict)
    ):
        policy = observed.policy or {}
        active = observed.active_policy or policy.get("active_policy") or {}
        if not isinstance(active, dict):
            active = {}
        values["availability"] = "acknowledged"
        for label, candidates, allowed in (
            (
                "evaluation_mode",
                [active.get("evaluation_policy_version"), policy.get("evaluation_policy_version")],
                {"legacy", "hybrid_v1"},
            ),
            (
                "profile",
                [active.get("profile"), policy.get("profile")],
                {"strict", "balanced", "bounded_autonomy"},
            ),
            ("maximum_outcome", [policy.get("maximum_outcome")], {"approve", "escalate", "deny"}),
            ("minimum_outcome", [policy.get("minimum_outcome")], {"approve", "escalate", "deny"}),
            (
                "interaction_mode",
                [policy.get("interaction_mode")],
                {"none", "explicit_gates", "step_requests"},
            ),
        ):
            value = next((v for v in candidates if isinstance(v, str) and v in allowed), "unknown")
            values[label] = value
        if type(observed.policy_revision) is int and observed.policy_revision >= 1:
            values["revision"] = observed.policy_revision
    return {
        "role": "system",
        "content": _HEADING
        + json.dumps(redact_runtime_evidence(values), sort_keys=True)
        + "\n"
        + _GUIDANCE,
    }


def is_current_policy_message(message: dict[str, Any]) -> bool:
    """Identify this owner's ephemeral system notice after projection strips markers."""
    content = message.get("content")
    return (
        message.get("role") == "system"
        and isinstance(content, str)
        and content.startswith(_HEADING)
        and content.endswith(_GUIDANCE)
    )


async def refresh_current_policy(
    messages: list[dict[str, Any]], guardrails: Any, session: SessionModel
) -> dict[str, Any] | None:
    """Append changed evidence without rewriting the frozen session preamble."""
    message = await current_policy_message(guardrails, session)
    previous = next(
        (
            row
            for row in reversed(messages)
            if is_current_policy_message(row)
            or (
                row.get("role") == "system"
                and isinstance(row.get("content"), str)
                and row["content"].startswith("Context update:")
                and _HEADING in row["content"]
            )
        ),
        None,
    )
    if previous is not None and message["content"] in previous["content"]:
        return None
    messages.append(message)
    return message
