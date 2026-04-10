"""Domain models for agent-facing credentials."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

CredentialKind = Literal[
    "text",
    "token",
    "username_password",
    "totp_seed",
    "recovery_codes",
    "browser_storage_state",
]

CredentialStatus = Literal["active", "expired", "revoked", "invalid"]


class CredentialRecord(BaseModel):
    """Structured credential record stored by Cognis."""

    credential_id: str
    user_email: str
    scope: str = "user"
    agent_id: str | None = None
    kind: CredentialKind
    label: str
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    status: CredentialStatus = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_verified_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class CredentialResolution(BaseModel):
    """Transient resolved credential field used for execution."""

    credential_id: str
    field: str | None = None
    value: Any
