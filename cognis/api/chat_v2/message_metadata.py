"""Safe message provenance shared by canonical and admission projections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cognis.core.message_envelope import normalize_message_metadata


def project_message_metadata(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Expose known authorship without forwarding arbitrary event metadata."""
    raw = data.get("message_metadata")
    raw = raw if isinstance(raw, dict) else {}
    result = normalize_message_metadata(raw)
    if (
        raw.get("private_controller_instruction") is True
        or raw.get("source") == "controller_instruction"
        or data.get("private_controller_instruction") is True
        or data.get("source") == "controller_instruction"
    ):
        result = {**(result or {}), "private_controller_instruction": True}
    # Only the opaque submission identity is exposed; the UI loads comments through the
    # access-checked feedback API.
    feedback = raw.get("feedback")
    if isinstance(feedback, dict):
        submission_id, manifest_hash = feedback.get("submission_id"), feedback.get("manifest_hash")
        if isinstance(submission_id, str) and isinstance(manifest_hash, str):
            result = {
                **(result or {}),
                "feedback": {"submission_id": submission_id, "manifest_hash": manifest_hash},
            }
    return result
