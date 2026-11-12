"""Trusted traffic projection, including conservative legacy inference."""

from __future__ import annotations

from typing import Any

from cognis.models.usage import UsageAttribution


def traffic_dimensions(attribution: UsageAttribution) -> dict[str, Any]:
    """Project legacy evidence without changing immutable observation digests."""
    identity = attribution.identity
    auth = {
        "jwt": "user_jwt",
        "inference_companion": "service",
        "session": "browser_session",
    }.get(identity.auth_type or "", identity.auth_type)
    if auth not in {"api_key", "user_jwt", "service", "browser_session"}:
        auth = "unknown" if identity.auth_type else None
    origin = identity.traffic_origin
    if origin == "unknown":
        if auth in {"api_key", "user_jwt", "service", "browser_session"}:
            origin = "api"
        elif (
            attribution.resources is not None and attribution.resources.provenance == "controller"
        ) or identity.service_id == "cognis":
            origin = "cognis"
    client = identity.client_id if origin == "api" else None
    key = identity.key_record_id
    if key is None and origin == "api" and auth == "api_key":
        key = client
    return {
        "traffic_origin": origin,
        "traffic_auth_type": auth if origin != "cognis" else None,
        "traffic_client_id": client,
        "traffic_key_id": key if origin == "api" else None,
    }
