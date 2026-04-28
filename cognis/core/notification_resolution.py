"""Shared notification resolution helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cognis.core.notifications import Notification


async def build_auth_challenge_resolution_data(
    *,
    notification: Notification,
    decision: str,
    user_email: str,
    credentials_provider: Any,
    response: str | None = None,
    response_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build safe resolution data for an auth challenge notification."""

    required_fields = (
        notification.payload.get("required_fields")
        if isinstance(notification.payload, dict)
        else []
    )
    required_field_names = [str(field) for field in required_fields if isinstance(field, str)] if isinstance(required_fields, list) else []
    response_value: str | None = None
    if response is not None:
        response_value = response
    elif response_payload is not None:
        for candidate in [*required_field_names, "code", "response"]:
            if response_payload.get(candidate) is not None:
                response_value = str(response_payload[candidate])
                break

    if decision in {"deny", "cancel"}:
        return {"challenge_completed": False}
    if required_field_names and not response_value:
        raise ValueError(
            "Auth challenge requires response fields: " + ", ".join(required_field_names)
        )
    if response_value:
        created = await credentials_provider.upsert_credential(
            credential_id=f"challenge_{notification.notification_id}",
            user_email=user_email,
            kind="text",
            label=f"Challenge response {notification.notification_id}",
            payload={"value": response_value},
            metadata={"notification_id": notification.notification_id, "ephemeral": True},
            description="Ephemeral auth challenge response",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return {
            "response_ref": f"$credential:{created.credential_id}.value",
            "challenge_completed": True,
        }
    if decision in {"approve", "continue", "completed"}:
        return {"challenge_completed": True}
    return {}
