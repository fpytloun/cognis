"""Shared notification resolution helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from cognis.core.notifications import Notification


def _parse_credential_response(kind: str, response: str) -> dict[str, str]:
    """Parse a free-text credential response into a structured payload."""

    text = response.strip()
    if not text:
        raise ValueError("Credential response is empty")
    normalized_kind = kind.strip().lower()
    if normalized_kind == "token":
        keyed = re.match(r"^token\s*:\s*(.+)$", text, re.IGNORECASE | re.DOTALL)
        return {"token": keyed.group(1).strip()} if keyed else {"token": text}
    if normalized_kind != "username_password":
        raise ValueError(f"Natural response parsing not implemented for credential kind: {kind}")

    keyed_values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^(username|email|password)\s*:\s*(.+)$", line.strip(), re.IGNORECASE)
        if match:
            field = "username" if match.group(1).lower() in {"username", "email"} else "password"
            keyed_values[field] = match.group(2).strip()
    if {"username", "password"}.issubset(keyed_values):
        return {"username": keyed_values["username"], "password": keyed_values["password"]}

    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(non_empty_lines) == 2:
        return {"username": non_empty_lines[0], "password": non_empty_lines[1]}

    if ":" in text:
        username, password = text.split(":", 1)
        if username.strip() and password.strip():
            return {"username": username.strip(), "password": password.strip()}

    raise ValueError(
        "Provide credentials as username:password, two lines, or username/password keyed lines"
    )


async def build_credential_request_resolution_data(
    *,
    notification: Notification,
    decision: str,
    user_email: str,
    credentials_provider: Any,
    response: str | None = None,
    response_payload: dict[str, object] | None = None,
    credential: Any | None = None,
) -> dict[str, object]:
    """Store a requested credential and return safe metadata for the agent loop."""

    if decision in {"deny", "cancel"}:
        return {}
    if decision != "approve":
        raise ValueError("Invalid credential request decision")

    requested = notification.payload if isinstance(notification.payload, dict) else {}
    credential_payload = getattr(credential, "payload", None) if credential is not None else None
    if credential_payload is None and response_payload is not None:
        credential_payload = response_payload
    if credential_payload is None:
        credential_payload = _parse_credential_response(
            str(requested.get("kind") or getattr(credential, "kind", "") or ""),
            str(response or ""),
        )
    if not isinstance(credential_payload, dict):
        raise ValueError("Credential payload must be an object")

    required_fields = requested.get("required_fields") if isinstance(requested, dict) else []
    if isinstance(required_fields, list):
        missing = [
            str(field)
            for field in required_fields
            if isinstance(field, str)
            and (
                field not in credential_payload
                or credential_payload[field] is None
                or (
                    isinstance(credential_payload[field], str)
                    and not credential_payload[field].strip()
                )
            )
        ]
        if missing:
            raise ValueError(
                f"Credential approval is missing required fields: {', '.join(missing)}"
            )

    requested_metadata = (
        requested.get("metadata") if isinstance(requested.get("metadata"), dict) else {}
    )
    credential_metadata = getattr(credential, "metadata", None) if credential is not None else None
    metadata = {
        **requested_metadata,
        **(credential_metadata if isinstance(credential_metadata, dict) else {}),
    }
    created = await credentials_provider.upsert_credential(
        credential_id=str(
            requested.get("credential_id")
            or (getattr(credential, "credential_id", "") if credential is not None else "")
        ),
        user_email=user_email,
        kind=str(requested.get("kind") or (getattr(credential, "kind", "") if credential else "")),
        label=str(
            requested.get("label")
            or (getattr(credential, "label", "") if credential is not None else "")
        ),
        payload=credential_payload,
        metadata=metadata,
        scope=str(requested.get("scope") or "user"),
        agent_id=str(requested.get("agent_id")) if requested.get("agent_id") is not None else None,
        description=getattr(credential, "description", None) if credential is not None else None,
        expires_at=getattr(credential, "expires_at", None) if credential is not None else None,
    )
    return {
        "credential_id": created.credential_id,
        "credential_label": created.label,
        "credential_kind": created.kind,
    }


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
    required_field_names = (
        [str(field) for field in required_fields if isinstance(field, str)]
        if isinstance(required_fields, list)
        else []
    )
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
