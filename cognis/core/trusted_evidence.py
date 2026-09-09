"""Controller-owned trusted-evidence lifecycle policy.

This module owns the marker, eligibility, event binding, queue identity, and
low-cardinality observability rules.  It deliberately does not read Intaris or
send network requests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from prometheus_client import Counter, Gauge, Histogram
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from cognis.models.session import SessionEvent

TRUSTED_EVIDENCE_PROTOCOL: Literal["mnemory.trusted-evidence.v1"] = "mnemory.trusted-evidence.v1"
TRUSTED_EVIDENCE_VERSION = 1
TRUSTED_EVIDENCE_POLICY_VERSION: Literal["trusted-evidence-policy-v1"] = (
    "trusted-evidence-policy-v1"
)
TRUSTED_EVIDENCE_MARKER_KEY = "trusted_evidence"
TRUSTED_EVIDENCE_ADMISSION_KEY = "trusted_evidence_admission"
EVIDENCE_QUEUE_KIND = "trusted_evidence"
ORDINARY_USER_QUEUE_KIND = "ordinary_user"
ORDINARY_ASSISTANT_QUEUE_KIND = "ordinary_assistant"
TERMINAL_EVIDENCE_OUTCOMES = frozenset(
    {
        "accepted",
        "replayed",
        "recovered",
        "skipped",
        "rejected",
        "conflict",
        "abandoned",
        "unavailable",
    }
)
SUCCESS_EVIDENCE_OUTCOMES = frozenset({"accepted", "replayed", "recovered", "skipped"})

EVIDENCE_OUTCOMES_TOTAL = Counter(
    "cognis_trusted_evidence_total",
    "Trusted evidence queue outcomes.",
    ["outcome"],
)
EVIDENCE_QUEUE_AGE_SECONDS = Gauge(
    "cognis_trusted_evidence_queue_age_seconds",
    "Age in seconds of the oldest pending trusted evidence item.",
)
EVIDENCE_QUEUE_AGE_HISTOGRAM = Histogram(
    "cognis_trusted_evidence_queue_age_observed_seconds",
    "Observed age in seconds of trusted evidence queue items.",
    buckets=(1, 5, 10, 30, 60, 300, 900, 1800, 3600, 7200, 86400),
)
EVIDENCE_POLICY_MISMATCH = Gauge(
    "cognis_trusted_evidence_policy_mismatch_active",
    "Whether this controller has active trusted evidence admitted under another policy.",
)
EVIDENCE_POLICY_MISMATCH_TOTAL = Counter(
    "cognis_trusted_evidence_policy_mismatch_total",
    "Trusted evidence rows observed under a policy other than this controller's policy.",
)

EvidenceOutcome = Literal[
    "accepted",
    "replayed",
    "recovered",
    "skipped",
    "rejected",
    "conflict",
    "abandoned",
    "unavailable",
]


@dataclass(frozen=True, slots=True)
class EvidenceOrigin:
    """Immutable admission provenance carried into the durable event."""

    authenticated: bool
    source: str
    role: str
    prompt_visibility: str
    prompt_provenance: str
    memory_eligible: bool
    private_controller_instruction: bool = False
    system_initiated: bool = False
    retry: bool = False
    delegated: bool = False
    workflow: bool = False
    managed: bool = False
    forked: bool = False
    channel_controller: bool = False


@dataclass(frozen=True, slots=True)
class TrustedEvidenceAdmission:
    """Frozen admission authority for one durable user event."""

    version: int
    admitted: bool
    policy_version: Literal["trusted-evidence-policy-v1"]
    policy_fingerprint: str
    max_attempts: int
    max_age_seconds: int
    owner_id: str | None
    admitted_at: str
    event_binding: dict[str, Any]
    admission_mac: str


class EvidenceOriginSource(StrEnum):
    USER_INPUT = "user_input"
    DELEGATION_INPUT = "delegation_input"
    CONTROLLER_INSTRUCTION = "controller_instruction"
    UNKNOWN = "unknown"


class EvidenceOriginVisibility(StrEnum):
    USER_VISIBLE = "user_visible"
    MODEL_ONLY = "model_only"
    UNKNOWN = "unknown"


class EvidenceOriginRole(StrEnum):
    USER = "user"
    UNKNOWN = "unknown"


class EvidenceOriginProvenance(StrEnum):
    USER_AUTHORED = "user_authored"
    INTERNAL_WORKFLOW_PROMPT = "internal_workflow_prompt"
    DELEGATION_INPUT = "delegation_input"
    UNKNOWN = "unknown"


class _DurableEvidenceOrigin(BaseModel):
    """Strict, bounded wire representation of ``EvidenceOrigin``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    authenticated: StrictBool
    source: EvidenceOriginSource
    role: EvidenceOriginRole
    prompt_visibility: EvidenceOriginVisibility
    prompt_provenance: EvidenceOriginProvenance
    memory_eligible: StrictBool
    private_controller_instruction: StrictBool
    system_initiated: StrictBool
    retry: StrictBool
    delegated: StrictBool
    workflow: StrictBool
    managed: StrictBool
    forked: StrictBool
    channel_controller: StrictBool


class _DurableTrustedEvidenceEventBinding(BaseModel):
    """Canonical immutable identity and content provenance for one event."""

    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["mnemory.trusted-evidence.v1"] = TRUSTED_EVIDENCE_PROTOCOL
    event_id: str = Field(min_length=64, max_length=64)
    event_hash: str = Field(min_length=64, max_length=64)
    intaris_session_id: str = Field(min_length=1)
    cognis_session_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    role: str = Field(min_length=1)
    prompt_visibility: str = Field(min_length=1)
    prompt_provenance: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    attachment_refs_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_hashes(self) -> _DurableTrustedEvidenceEventBinding:
        for name in ("event_id", "event_hash", "content_hash", "attachment_refs_hash"):
            value = getattr(self, name)
            if any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be lowercase hexadecimal")
        return self


class _DurableTrustedEvidenceAdmission(BaseModel):
    """Strict wire representation of one frozen admission decision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    admitted: StrictBool
    policy_version: Literal["trusted-evidence-policy-v1"] = TRUSTED_EVIDENCE_POLICY_VERSION
    policy_fingerprint: str = Field(min_length=0, max_length=64)
    max_attempts: int = Field(ge=1, le=100)
    max_age_seconds: int = Field(ge=60, le=604800)
    owner_id: str | None = None
    admitted_at: str = Field(min_length=1)
    event_binding: _DurableTrustedEvidenceEventBinding
    admission_mac: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_decision(self) -> _DurableTrustedEvidenceAdmission:
        if self.admitted:
            if not self.owner_id or self.owner_id != self.owner_id.strip().lower():
                raise ValueError("admitted evidence requires one normalized owner")
            if len(self.policy_fingerprint) != 16 or any(
                character not in "0123456789abcdef" for character in self.policy_fingerprint
            ):
                raise ValueError("admitted evidence requires a valid policy fingerprint")
        elif self.owner_id is not None:
            raise ValueError("negative evidence admission cannot select an owner")
        if self.admitted and self.event_binding.owner_id != self.owner_id:
            raise ValueError("admission owner must match the event binding")
        try:
            admitted_at = datetime.fromisoformat(self.admitted_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("admission timestamp must be ISO 8601") from exc
        if admitted_at.tzinfo is None:
            raise ValueError("admission timestamp must include a timezone")
        if any(character not in "0123456789abcdef" for character in self.admission_mac):
            raise ValueError("evidence admission MAC must be lowercase hexadecimal")
        return self


def _evidence_admission_mac(
    *,
    key: bytes,
    version: int,
    admitted: bool,
    policy_version: str,
    policy_fingerprint: str,
    max_attempts: int,
    max_age_seconds: int,
    owner_id: str | None,
    admitted_at: str,
    event_binding: Mapping[str, Any],
) -> str:
    payload = json.dumps(
        {
            "version": version,
            "admitted": admitted,
            "policy_version": policy_version,
            "policy_fingerprint": policy_fingerprint,
            "max_attempts": max_attempts,
            "max_age_seconds": max_age_seconds,
            "owner_id": owner_id,
            "admitted_at": admitted_at,
            "event_binding": dict(event_binding),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(
        key,
        b"cognis.trusted-evidence.admission.v1\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def build_evidence_event_binding(
    *,
    intaris_session_id: str,
    cognis_session_id: str,
    conversation_id: str,
    turn_id: str,
    user_id: str,
    owner_id: str,
    source: str,
    role: str,
    prompt_visibility: str,
    prompt_provenance: Mapping[str, Any],
    content_hash: str,
    attachment_refs_value: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one canonical immutable event identity and content hash."""
    identity = {
        "protocol": TRUSTED_EVIDENCE_PROTOCOL,
        "intaris_session_id": intaris_session_id,
        "cognis_session_id": cognis_session_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "user_id": user_id,
        "owner_id": owner_id,
    }
    content_binding = {
        **identity,
        "source": source,
        "role": role,
        "prompt_visibility": prompt_visibility,
        "prompt_provenance": prompt_provenance.get("kind"),
        "content_hash": content_hash,
        "attachment_refs_hash": sha256_hex(attachment_refs_value),
    }
    return _DurableTrustedEvidenceEventBinding.model_validate(
        {
            **content_binding,
            "event_id": sha256_hex(identity),
            "event_hash": sha256_hex(content_binding),
        }
    ).model_dump(mode="json")


def build_evidence_admission(
    *,
    key: bytes,
    admitted: bool,
    owner_id: str | None,
    policy_fingerprint: str,
    event_binding: Mapping[str, Any],
    admitted_at: str | None = None,
    max_attempts: int = 8,
    max_age_seconds: int = 3600,
) -> TrustedEvidenceAdmission:
    """Build one strict admission decision from server-owned policy inputs."""
    if admitted and not key:
        raise ValueError("admitted evidence requires configured key material")
    normalized_owner = owner_id.strip().lower() if admitted and owner_id else None
    normalized_binding = _DurableTrustedEvidenceEventBinding.model_validate(
        event_binding
    ).model_dump(mode="json")
    normalized_admitted_at = admitted_at or datetime.now(UTC).isoformat()
    admission_mac = _evidence_admission_mac(
        key=key,
        version=1,
        admitted=admitted,
        policy_version=TRUSTED_EVIDENCE_POLICY_VERSION,
        policy_fingerprint=policy_fingerprint,
        max_attempts=max_attempts,
        max_age_seconds=max_age_seconds,
        owner_id=normalized_owner,
        admitted_at=normalized_admitted_at,
        event_binding=normalized_binding,
    )
    parsed = _DurableTrustedEvidenceAdmission.model_validate(
        {
            "version": 1,
            "admitted": admitted,
            "policy_version": TRUSTED_EVIDENCE_POLICY_VERSION,
            "policy_fingerprint": policy_fingerprint,
            "max_attempts": max_attempts,
            "max_age_seconds": max_age_seconds,
            "owner_id": normalized_owner,
            "admitted_at": normalized_admitted_at,
            "event_binding": normalized_binding,
            "admission_mac": admission_mac,
        }
    )
    return TrustedEvidenceAdmission(**parsed.model_dump())


def serialize_evidence_admission(admission: TrustedEvidenceAdmission) -> dict[str, Any]:
    """Serialize only the strict fields that authorize later evidence dispatch."""
    return _DurableTrustedEvidenceAdmission.model_validate(
        {
            "version": admission.version,
            "admitted": admission.admitted,
            "policy_version": admission.policy_version,
            "policy_fingerprint": admission.policy_fingerprint,
            "max_attempts": admission.max_attempts,
            "max_age_seconds": admission.max_age_seconds,
            "owner_id": admission.owner_id,
            "admitted_at": admission.admitted_at,
            "event_binding": admission.event_binding,
            "admission_mac": admission.admission_mac,
        }
    ).model_dump(mode="json")


def deserialize_evidence_admission(value: Any) -> TrustedEvidenceAdmission | None:
    """Parse a frozen admission decision without coercion or inference."""
    if not isinstance(value, dict):
        return None
    try:
        parsed = _DurableTrustedEvidenceAdmission.model_validate(value)
    except (TypeError, ValueError):
        return None
    return TrustedEvidenceAdmission(**parsed.model_dump())


def evidence_admission_authorizes(
    value: TrustedEvidenceAdmission | Mapping[str, Any] | None,
    *,
    key: bytes,
    owner_id: str | None,
) -> bool:
    """Return whether a strict positive decision selects exactly this owner."""
    admission = (
        value
        if isinstance(value, TrustedEvidenceAdmission)
        else deserialize_evidence_admission(value)
    )
    normalized_owner = owner_id.strip().lower() if owner_id else None
    expected_mac = (
        _evidence_admission_mac(
            key=key,
            version=admission.version,
            admitted=admission.admitted,
            policy_version=admission.policy_version,
            policy_fingerprint=admission.policy_fingerprint,
            max_attempts=admission.max_attempts,
            max_age_seconds=admission.max_age_seconds,
            owner_id=admission.owner_id,
            admitted_at=admission.admitted_at,
            event_binding=admission.event_binding,
        )
        if admission is not None
        else ""
    )
    return bool(
        admission is not None
        and hmac.compare_digest(admission.admission_mac, expected_mac)
        and admission.admitted
        and normalized_owner
        and admission.owner_id == normalized_owner
    )


def evidence_admission_matches_event(
    value: TrustedEvidenceAdmission | Mapping[str, Any] | None,
    *,
    key: bytes,
    event_binding: Mapping[str, Any],
) -> bool:
    """Verify one admission and its exact canonical event binding."""
    admission = (
        value
        if isinstance(value, TrustedEvidenceAdmission)
        else deserialize_evidence_admission(value)
    )
    if admission is None:
        return False
    try:
        normalized = _DurableTrustedEvidenceEventBinding.model_validate(event_binding).model_dump(
            mode="json"
        )
    except (TypeError, ValueError):
        return False
    return bool(
        evidence_admission_authorizes(
            admission,
            key=key,
            owner_id=normalized["owner_id"],
        )
        and admission.event_binding == normalized
    )


def serialize_evidence_origin(origin: EvidenceOrigin) -> dict[str, Any]:
    """Serialize only the strict origin fields used by evidence policy."""
    try:
        source = EvidenceOriginSource(origin.source)
    except ValueError:
        source = EvidenceOriginSource.UNKNOWN
    try:
        role = EvidenceOriginRole(origin.role)
    except ValueError:
        role = EvidenceOriginRole.UNKNOWN
    try:
        visibility = EvidenceOriginVisibility(origin.prompt_visibility)
    except ValueError:
        visibility = EvidenceOriginVisibility.UNKNOWN
    try:
        provenance = EvidenceOriginProvenance(origin.prompt_provenance)
    except ValueError:
        provenance = EvidenceOriginProvenance.UNKNOWN
    return _DurableEvidenceOrigin.model_validate(
        {
            "authenticated": origin.authenticated,
            "source": source,
            "role": role,
            "prompt_visibility": visibility,
            "prompt_provenance": provenance,
            "memory_eligible": origin.memory_eligible,
            "private_controller_instruction": origin.private_controller_instruction,
            "system_initiated": origin.system_initiated,
            "retry": origin.retry,
            "delegated": origin.delegated,
            "workflow": origin.workflow,
            "managed": origin.managed,
            "forked": origin.forked,
            "channel_controller": origin.channel_controller,
        }
    ).model_dump(mode="json")


def deserialize_evidence_origin(value: Any) -> EvidenceOrigin | None:
    """Reconstruct origin only from the exact durable schema; never infer it."""
    if not isinstance(value, dict):
        return None
    bool_fields = (
        "authenticated",
        "memory_eligible",
        "private_controller_instruction",
        "system_initiated",
        "retry",
        "delegated",
        "workflow",
        "managed",
        "forked",
        "channel_controller",
    )
    if any(type(value.get(field)) is not bool for field in bool_fields):
        return None
    enum_fields = {
        "source": EvidenceOriginSource,
        "role": EvidenceOriginRole,
        "prompt_visibility": EvidenceOriginVisibility,
        "prompt_provenance": EvidenceOriginProvenance,
    }
    if any(type(value.get(field)) is not str for field in enum_fields):
        return None
    if any(
        value[field] not in enum_type._value2member_map_ for field, enum_type in enum_fields.items()
    ):
        return None
    try:
        strict_value = dict(value)
        strict_value.update(
            {field: enum_type(value[field]) for field, enum_type in enum_fields.items()}
        )
        parsed = _DurableEvidenceOrigin.model_validate(strict_value)
    except (TypeError, ValueError):
        return None
    return EvidenceOrigin(**parsed.model_dump())


def fail_closed_evidence_origin() -> EvidenceOrigin:
    """Return an origin that cannot satisfy evidence eligibility."""
    return EvidenceOrigin(
        authenticated=False,
        source=EvidenceOriginSource.UNKNOWN,
        role="unknown",
        prompt_visibility=EvidenceOriginVisibility.UNKNOWN,
        prompt_provenance=EvidenceOriginProvenance.UNKNOWN,
        memory_eligible=False,
        private_controller_instruction=True,
        system_initiated=True,
        retry=True,
        delegated=True,
        workflow=True,
        managed=True,
        forked=True,
        channel_controller=True,
    )


def classify_origin(
    metadata: Mapping[str, Any] | None,
    *,
    system_initiated: bool,
    is_retry: bool,
    delegated: bool,
    workflow: bool,
    managed: bool,
    forked: bool,
    channel_controller: bool,
) -> EvidenceOrigin:
    """Classify authenticated admission without inferring provenance from text."""
    raw = metadata or {}
    source = str(raw.get("source") or "user_input")
    prompt_visibility = str(raw.get("prompt_visibility") or "user_visible")
    provenance = str(raw.get("prompt_provenance") or "user_authored")
    authenticated = raw.get("authenticated_user_input") is True
    return EvidenceOrigin(
        authenticated=authenticated,
        source=source,
        role=str(raw.get("role") or "user"),
        prompt_visibility=prompt_visibility,
        prompt_provenance=provenance,
        memory_eligible=raw.get("memory_eligible", True) is not False,
        private_controller_instruction=raw.get("private_controller_instruction") is True,
        system_initiated=system_initiated,
        retry=is_retry,
        delegated=delegated,
        workflow=workflow,
        managed=managed,
        forked=forked,
        channel_controller=channel_controller,
    )


def authenticated_direct_user_origin(
    metadata: Mapping[str, Any] | None = None,
) -> EvidenceOrigin:
    """Return the explicit provenance granted by authenticated user ingress."""
    raw = dict(metadata or {})
    raw["authenticated_user_input"] = True
    return classify_origin(
        raw,
        system_initiated=False,
        is_retry=False,
        delegated=False,
        workflow=False,
        managed=False,
        forked=False,
        channel_controller=False,
    )


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """Hash one canonical JSON value without retaining the value."""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def trusted_evidence_policy_fingerprint(
    *,
    key: bytes,
    enabled: bool,
    max_attempts: int,
    max_age_seconds: int,
    owner_allowlist: Collection[str],
) -> str:
    """Return a keyed, low-cardinality policy generation fingerprint."""
    normalized_allowlist = sorted({owner.strip().lower() for owner in owner_allowlist})
    payload = json.dumps(
        {
            "version": TRUSTED_EVIDENCE_POLICY_VERSION,
            "enabled": enabled,
            "max_attempts": max_attempts,
            "max_age_seconds": max_age_seconds,
            "owner_allowlist": normalized_allowlist,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        key,
        b"cognis/trusted-evidence-policy-fingerprint/v1\0" + payload,
        hashlib.sha256,
    ).hexdigest()[:16]


def attachment_refs(value: Any) -> list[dict[str, Any]]:
    """Return only stable attachment references, never attachment-derived text."""
    if not isinstance(value, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        ref = {
            key: item[key]
            for key in (
                "attachment_id",
                "artifact_id",
                "filename",
                "mime_type",
                "size_bytes",
                "sha256",
            )
            if key in item
        }
        if ref:
            refs.append(ref)
    return refs


def is_eligible_user_event(
    *,
    content: str | None,
    user_visible_content: str | None,
    source: str,
    role: str,
    prompt_visibility: str,
    prompt_provenance: Mapping[str, Any] | None,
    system_initiated: bool,
    is_retry: bool,
    internal_workflow_prompt: bool,
    delegated: bool,
    profile_switch_reentry: bool,
    origin: EvidenceOrigin | None = None,
) -> bool:
    """Return whether an append may carry the automatic evidence marker."""
    if origin is None:
        return False
    return bool(
        content
        and content.strip()
        and user_visible_content
        and user_visible_content.strip()
        and origin.authenticated
        and origin.memory_eligible
        and not origin.private_controller_instruction
        and origin.source == "user_input"
        and origin.role == "user"
        and origin.prompt_visibility == "user_visible"
        and origin.prompt_provenance == "user_authored"
        and not any(
            (
                origin.system_initiated,
                origin.retry,
                origin.delegated,
                origin.workflow,
                origin.managed,
                origin.forked,
                origin.channel_controller,
                system_initiated,
                is_retry,
                internal_workflow_prompt,
                delegated,
                profile_switch_reentry,
            )
        )
    )


def is_evidence_enabled_for_owner(
    *,
    enabled: bool,
    owner_id: str | None,
    owner_allowlist: Collection[str],
) -> bool:
    """Return whether trusted evidence is enabled for the effective owner."""
    return bool(enabled and owner_id and owner_id.strip().lower() in owner_allowlist)


def build_marker(
    *,
    content: str,
    source: str,
    role: str,
    prompt_visibility: str,
    prompt_provenance: Mapping[str, Any],
    user_id: str,
    owner_id: str,
    intaris_session_id: str,
    cognis_session_id: str,
    conversation_id: str,
    turn_id: str,
    attachment_refs_value: list[dict[str, Any]],
    admission: TrustedEvidenceAdmission,
    admission_key: bytes,
    event_hash_value: str | None = None,
) -> dict[str, Any]:
    """Build the versioned event marker without storing message content."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    event_binding = build_evidence_event_binding(
        intaris_session_id=intaris_session_id,
        cognis_session_id=cognis_session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id=user_id,
        owner_id=owner_id,
        source=source,
        role=role,
        prompt_visibility=prompt_visibility,
        prompt_provenance=prompt_provenance,
        content_hash=content_hash,
        attachment_refs_value=attachment_refs_value,
    )
    if not evidence_admission_matches_event(
        admission,
        key=admission_key,
        event_binding=event_binding,
    ):
        raise ValueError("trusted evidence marker requires positive frozen admission")
    marker_without_hash = {
        "protocol": TRUSTED_EVIDENCE_PROTOCOL,
        "version": TRUSTED_EVIDENCE_VERSION,
        "source": source,
        "role": role,
        "prompt_visibility": prompt_visibility,
        "prompt_provenance": dict(prompt_provenance),
        "user_id": user_id,
        "owner_id": owner_id,
        "intaris_session_id": intaris_session_id,
        "cognis_session_id": cognis_session_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "attachment_refs": attachment_refs_value,
        TRUSTED_EVIDENCE_ADMISSION_KEY: serialize_evidence_admission(admission),
        "content_hash": content_hash,
        "admitted_at": admission.admitted_at,
    }
    if event_hash_value is not None:
        marker_without_hash["event_hash"] = event_hash_value
    marker = dict(marker_without_hash)
    marker["marker_hash"] = sha256_hex({**marker_without_hash, "content": content})
    return marker


def marker_is_valid(marker: Mapping[str, Any], *, admission_key: bytes | None = None) -> bool:
    """Validate marker shape and fixed protocol values without content."""
    required = {
        "protocol",
        "version",
        "source",
        "role",
        "prompt_visibility",
        "prompt_provenance",
        "user_id",
        "owner_id",
        "intaris_session_id",
        "cognis_session_id",
        "conversation_id",
        "turn_id",
        "attachment_refs",
        "content_hash",
        "marker_hash",
        "admitted_at",
        TRUSTED_EVIDENCE_ADMISSION_KEY,
    }
    admission = deserialize_evidence_admission(marker.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
    if admission is None or marker.get("admitted_at") != admission.admitted_at:
        return False
    try:
        admitted_at = datetime.fromisoformat(admission.admitted_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if admitted_at > datetime.now(UTC) + timedelta(minutes=5):
        return False
    try:
        marker_binding = build_evidence_event_binding(
            intaris_session_id=str(marker.get("intaris_session_id") or ""),
            cognis_session_id=str(marker.get("cognis_session_id") or ""),
            conversation_id=str(marker.get("conversation_id") or ""),
            turn_id=str(marker.get("turn_id") or ""),
            user_id=str(marker.get("user_id") or ""),
            owner_id=str(marker.get("owner_id") or ""),
            source=str(marker.get("source") or ""),
            role=str(marker.get("role") or ""),
            prompt_visibility=str(marker.get("prompt_visibility") or ""),
            prompt_provenance=(
                dict(marker["prompt_provenance"])
                if isinstance(marker.get("prompt_provenance"), Mapping)
                else {}
            ),
            content_hash=str(marker.get("content_hash") or ""),
            attachment_refs_value=list(marker.get("attachment_refs") or []),
        )
    except (TypeError, ValueError):
        return False
    return (
        required.issubset(marker)
        and marker.get("protocol") == TRUSTED_EVIDENCE_PROTOCOL
        and marker.get("version") == TRUSTED_EVIDENCE_VERSION
        and marker.get("source") == "user_input"
        and marker.get("role") == "user"
        and marker.get("prompt_visibility") == "user_visible"
        and isinstance(marker.get("prompt_provenance"), Mapping)
        and marker["prompt_provenance"].get("kind") == "user_authored"
        and all(
            isinstance(marker.get(key), str) and marker[key]
            for key in (
                "user_id",
                "owner_id",
                "intaris_session_id",
                "cognis_session_id",
                "conversation_id",
                "turn_id",
                "content_hash",
                "marker_hash",
            )
        )
        and isinstance(marker.get("attachment_refs"), list)
        and (
            admission is not None
            if admission_key is None
            else evidence_admission_matches_event(
                admission,
                key=admission_key,
                event_binding=marker_binding,
            )
        )
    )


def marker_matches_event(
    marker: Mapping[str, Any],
    *,
    admission_key: bytes,
    intaris_session_id: str,
    event_data: Mapping[str, Any],
) -> bool:
    """Verify that a signed marker belongs to this exact persisted event body."""
    content = event_data.get("user_visible_content")
    attachments = event_data.get("attachments", [])
    provenance = event_data.get("prompt_provenance")
    if (
        not isinstance(content, str)
        or not isinstance(attachments, list)
        or not isinstance(provenance, Mapping)
    ):
        return False
    admission = deserialize_evidence_admission(marker.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
    try:
        event_binding = build_evidence_event_binding(
            intaris_session_id=intaris_session_id,
            cognis_session_id=str(marker.get("cognis_session_id") or ""),
            conversation_id=str(marker.get("conversation_id") or ""),
            turn_id=str(event_data.get("turn_id") or ""),
            user_id=str(marker.get("user_id") or ""),
            owner_id=str(marker.get("owner_id") or ""),
            source=str(event_data.get("source") or ""),
            role=str(event_data.get("role") or ""),
            prompt_visibility=str(event_data.get("prompt_visibility") or ""),
            prompt_provenance=provenance,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            attachment_refs_value=attachments,
        )
    except (TypeError, ValueError):
        return False
    return bool(
        marker_is_valid(marker, admission_key=admission_key)
        and evidence_admission_matches_event(
            admission,
            key=admission_key,
            event_binding=event_binding,
        )
    )


def _event_payload(event: SessionEvent | Mapping[str, Any]) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        dumped = event.model_dump(mode="json")
        return {"type": dumped.get("type"), "data": dumped.get("data", {})}
    if isinstance(event, Mapping):
        return {"type": event.get("type"), "data": event.get("data", {})}
    return {
        "type": getattr(event, "type", None),
        "data": getattr(event, "data", None),
    }


def event_hash(stream_id: str, seq: int, event: SessionEvent | Mapping[str, Any]) -> str:
    """Bind the exact persisted event to its authoritative Intaris stream/seq."""
    return sha256_hex({"stream_id": stream_id, "seq": seq, "event": _event_payload(event)})


def marker_for_event(event: SessionEvent | Mapping[str, Any]) -> dict[str, Any] | None:
    payload = _event_payload(event)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    marker = data.get(TRUSTED_EVIDENCE_MARKER_KEY)
    return dict(marker) if isinstance(marker, Mapping) else None


def canonical_last_assistant_event(
    events: list[SessionEvent | Mapping[str, Any]],
    turn_id: str,
) -> SessionEvent | Mapping[str, Any] | None:
    """Select the highest-sequence assistant event for a turn."""
    candidates: list[tuple[int, SessionEvent | Mapping[str, Any]]] = []
    for event in events:
        payload = _event_payload(event)
        data = payload.get("data")
        if payload.get("type") != "assistant_message" or not isinstance(data, Mapping):
            continue
        if data.get("turn_id") != turn_id:
            continue
        raw_seq = event.get("seq") if isinstance(event, Mapping) else getattr(event, "seq", None)
        if isinstance(raw_seq, int):
            candidates.append((raw_seq, event))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def deterministic_queue_id(
    kind: str,
    event_hash_value: str,
    secondary_hash: str | None = None,
) -> str:
    """Return a stable queue id for one event/kind pair."""
    identity = f"{kind}:{event_hash_value}"
    if secondary_hash is not None:
        identity = f"{identity}:{secondary_hash}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"rq_evidence_{digest}"


def deterministic_dependency_id(kind: str, event_hash_value: str) -> str:
    """Return the deterministic ordinary row id for one evidence event."""
    return deterministic_queue_id(kind, event_hash_value)


def observe_outcome(outcome: str) -> None:
    """Record a fixed-cardinality outcome metric."""
    label = "rejected" if outcome == "conflict" else outcome
    if label in {
        "accepted",
        "replayed",
        "recovered",
        "rejected",
        "skipped",
        "abandoned",
        "unavailable",
    }:
        EVIDENCE_OUTCOMES_TOTAL.labels(outcome=label).inc()


def observe_queue_age(created_at: datetime, *, now: datetime | None = None) -> float:
    """Record queue age without labels or identifiers."""
    current = now or datetime.now(UTC)
    normalized = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    age = max(0.0, (current - normalized).total_seconds())
    EVIDENCE_QUEUE_AGE_SECONDS.set(age)
    EVIDENCE_QUEUE_AGE_HISTOGRAM.observe(age)
    return age


__all__ = [
    "EVIDENCE_QUEUE_KIND",
    "EVIDENCE_QUEUE_AGE_HISTOGRAM",
    "EVIDENCE_QUEUE_AGE_SECONDS",
    "EVIDENCE_OUTCOMES_TOTAL",
    "EVIDENCE_POLICY_MISMATCH",
    "EVIDENCE_POLICY_MISMATCH_TOTAL",
    "ORDINARY_ASSISTANT_QUEUE_KIND",
    "ORDINARY_USER_QUEUE_KIND",
    "SUCCESS_EVIDENCE_OUTCOMES",
    "TERMINAL_EVIDENCE_OUTCOMES",
    "TRUSTED_EVIDENCE_MARKER_KEY",
    "TRUSTED_EVIDENCE_ADMISSION_KEY",
    "TRUSTED_EVIDENCE_PROTOCOL",
    "TRUSTED_EVIDENCE_POLICY_VERSION",
    "TRUSTED_EVIDENCE_VERSION",
    "deserialize_evidence_origin",
    "deserialize_evidence_admission",
    "build_marker",
    "build_evidence_admission",
    "build_evidence_event_binding",
    "authenticated_direct_user_origin",
    "canonical_last_assistant_event",
    "classify_origin",
    "deterministic_dependency_id",
    "deterministic_queue_id",
    "event_hash",
    "EvidenceOrigin",
    "TrustedEvidenceAdmission",
    "evidence_admission_authorizes",
    "evidence_admission_matches_event",
    "serialize_evidence_origin",
    "serialize_evidence_admission",
    "is_eligible_user_event",
    "is_evidence_enabled_for_owner",
    "marker_for_event",
    "marker_is_valid",
    "marker_matches_event",
    "observe_outcome",
    "observe_queue_age",
    "sha256_hex",
    "trusted_evidence_policy_fingerprint",
]
