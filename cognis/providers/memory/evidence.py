"""Pure Cognis trusted-evidence v1 contract helpers."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

EVIDENCE_PROTOCOL = "mnemory.trusted-evidence.v1"
EVIDENCE_PATH = "/api/evidence/remember/v1"
USER_EVENT_PATH = "/api/user-events/remember/v1"

type JsonObject = dict[str, Any]
type EvidenceOutcome = Literal[
    "accepted",
    "replayed",
    "recovered",
    "skipped",
    "conflict",
    "rejected",
    "unavailable",
]


class EvidenceActor(BaseModel):
    """User and owner identity bound to an evidence event."""

    model_config = {"extra": "forbid"}

    user_id: str = Field(min_length=1, max_length=256)
    owner_id: str = Field(min_length=1, max_length=256)


class EvidenceEvent(BaseModel):
    """Stable event identity supplied by Cognis."""

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=1, max_length=256)
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cognis_session_id: str = Field(min_length=1, max_length=256)
    conversation_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)


class EvidenceMessage(BaseModel):
    """The single user message attached to an evidence event."""

    model_config = {"extra": "forbid"}

    role: Literal["user"]
    content: str = Field(min_length=1, max_length=400_000)


class UserEventMessage(EvidenceMessage):
    """The signed source message, with the same limit as the evidence route."""


class EvidenceRememberRequest(BaseModel):
    """Strict body contract for the trusted evidence endpoint."""

    model_config = {"extra": "forbid"}

    version: Literal[1]
    actor: EvidenceActor
    event: EvidenceEvent
    messages: Sequence[EvidenceMessage] = Field(min_length=1, max_length=1)

    @field_validator("messages")
    @classmethod
    def validate_message(cls, value: Sequence[EvidenceMessage]) -> Sequence[EvidenceMessage]:
        """Reject a structurally present but empty user message."""
        if not value or not value[0].content.strip():
            raise ValueError("Evidence message content must be non-empty")
        return value


class UserEventRememberRequest(EvidenceRememberRequest):
    """Strict body contract for shared trusted user-event ingestion."""

    messages: list[UserEventMessage] = Field(min_length=1, max_length=1)


type EvidenceBody = EvidenceRememberRequest | UserEventRememberRequest | Mapping[str, Any]


class TrustedEventRejection(BaseModel):
    """Only the server's durable, effect-free budget rejection contract."""

    model_config = {"extra": "forbid", "strict": True}
    status: Literal["rejected"]
    outcome: Literal["rejected_before_write"]
    reason: Literal[
        "input_budget_exceeded",
        "extraction_fact_budget_exceeded",
        "action_limit_exceeded",
        "plan_budget_exceeded",
    ]
    terminal: Literal[True]
    retryable: Literal[False]
    fallback_allowed: Literal[False]
    semantic_effects: Literal["none"]
    source_retention: Literal["caller_queue"]
    operation_id: str = Field(min_length=1, max_length=256)

    @field_validator("terminal", "retryable", "fallback_allowed", mode="before")
    @classmethod
    def require_boolean(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("Rejection safety flags must be booleans")
        return value


class TrustedEventRejectedError(Exception):
    """A verified request was durably rejected without semantic effects."""

    def __init__(self, rejection: TrustedEventRejection, body: EvidenceBody):
        super().__init__(f"Trusted memory rejected before write: {rejection.reason}")
        self.rejection = rejection
        self.source = evidence_body_dict(body)


def parse_trusted_rejection(payload: Any) -> TrustedEventRejection | None:
    """Do not classify unrelated validation errors as effect-free rejections."""
    try:
        return TrustedEventRejection.model_validate(payload)
    except ValidationError:
        return None


def _normalize(value: Any) -> Any:
    """Normalize strings to NFC and object keys to sorted order recursively."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {key: _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def evidence_body_dict(body: EvidenceBody) -> JsonObject:
    """Validate and return the JSON representation used by Mnemory."""
    if isinstance(body, EvidenceRememberRequest):
        return body.model_dump(mode="json")
    return EvidenceRememberRequest.model_validate(body).model_dump(mode="json")


def _canonical_request_bytes(body: EvidenceBody, path: str) -> bytes:
    """Return Mnemory's canonical POST route, method, and body bytes."""
    envelope = {
        "body": _normalize(evidence_body_dict(body)),
        "method": "POST",
        "route": path,
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_request_bytes(body: EvidenceBody) -> bytes:
    """Return Mnemory's canonical evidence request bytes."""
    return _canonical_request_bytes(body, EVIDENCE_PATH)


def canonical_request_hash(body: EvidenceBody) -> str:
    """Return the SHA-256 hash of the canonical evidence request."""
    return hashlib.sha256(canonical_request_bytes(body)).hexdigest()


def user_event_request_hash(body: EvidenceBody) -> str:
    """Return the hash of the canonical trusted user-ingestion request."""
    return hashlib.sha256(_canonical_request_bytes(body, USER_EVENT_PATH)).hexdigest()


def derive_evidence_root(body: EvidenceBody) -> str:
    """Return Mnemory's stable root from protocol, actor, and event identity."""
    value = evidence_body_dict(body)
    actor = value["actor"]
    event = value["event"]
    identity = [
        EVIDENCE_PROTOCOL,
        actor["user_id"],
        actor["owner_id"],
        event["id"],
        event["event_hash"],
        event["cognis_session_id"],
        event["conversation_id"],
        event["turn_id"],
    ]
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "EvidenceActor",
    "EvidenceBody",
    "EvidenceEvent",
    "EvidenceMessage",
    "EvidenceOutcome",
    "EvidenceRememberRequest",
    "EVIDENCE_PATH",
    "EVIDENCE_PROTOCOL",
    "USER_EVENT_PATH",
    "UserEventRememberRequest",
    "TrustedEventRejection",
    "TrustedEventRejectedError",
    "parse_trusted_rejection",
    "canonical_request_bytes",
    "canonical_request_hash",
    "derive_evidence_root",
    "evidence_body_dict",
    "user_event_request_hash",
]
