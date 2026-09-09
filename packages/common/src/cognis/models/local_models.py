"""Typed contracts for declarative local-model desired state."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

LOCAL_MODEL_ASSESSMENT_MAX = 2**53 - 1
LOCAL_MODEL_BYTE_COUNT_MAX = 2**63 - 1
OLLAMA_DEFAULT_PORT = 11434
OLLAMA_MANAGED_ENDPOINT: Literal["http://127.0.0.1:11434"] = "http://127.0.0.1:11434"


def ollama_loopback_endpoint(port: int) -> str:
    """Build the only endpoint shape Cognis permits for managed Ollama."""

    return f"http://127.0.0.1:{port}"


def _ollama_loopback_port(value: str) -> int:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Ollama endpoint must use a valid loopback port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != ollama_loopback_endpoint(port)
    ):
        raise ValueError("Ollama endpoint must be an exact 127.0.0.1 HTTP origin")
    return port


def _normalize_advertised_ollama_endpoint(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    endpoint = normalized.get("endpoint")
    endpoint_port = _ollama_loopback_port(endpoint) if isinstance(endpoint, str) else None
    configured_port = normalized.get("port", endpoint_port or OLLAMA_DEFAULT_PORT)
    if endpoint_port is not None and configured_port != endpoint_port:
        raise ValueError("advertised Ollama endpoint and port conflict")
    normalized["port"] = configured_port
    normalized["endpoint"] = ollama_loopback_endpoint(configured_port)
    return normalized


class LocalModelRuntimeType(StrEnum):
    """Supported local-model runtimes."""

    OLLAMA = "ollama"


class LocalModelSource(StrEnum):
    """Supported model reference sources."""

    OLLAMA = "ollama"
    HUGGINGFACE = "huggingface"


class LocalModelDesiredState(StrEnum):
    """Desired presence of a model on selected executors."""

    PRESENT = "present"
    ABSENT = "absent"


class LocalModelUpdatePolicy(StrEnum):
    """When a reconciler should refresh an already-present model."""

    IF_CHANGED = "if_changed"
    ALWAYS = "always"
    MANUAL = "manual"


class LocalModelPrunePolicy(StrEnum):
    """What a future reconciler may do with no-longer-desired model data."""

    RETAIN = "retain"
    DELETE = "delete"


class LocalModelTargetState(StrEnum):
    """Observed target reconciliation state."""

    PENDING = "pending"
    RECONCILING = "reconciling"
    READY = "ready"
    ABSENT = "absent"
    BLOCKED = "blocked"
    ERROR = "error"


class LocalModelOperationAction(StrEnum):
    """Future executor operation kind."""

    PULL = "pull"
    DELETE = "delete"


class LocalModelOperationState(StrEnum):
    """Durable operation state."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class OllamaRuntimeConfig(BaseModel):
    """Executor-local configuration for the managed Ollama control surface."""

    model_config = ConfigDict(extra="forbid")

    port: StrictInt = Field(default=OLLAMA_DEFAULT_PORT, ge=1, le=65535)
    endpoint: str = OLLAMA_MANAGED_ENDPOINT
    management_enabled: StrictBool = True
    max_concurrent_pulls: int = Field(default=1, ge=1, le=8)
    disk_headroom_bytes: int = Field(default=5 * 1024**3, ge=0, le=1024**5)
    request_timeout_seconds: float = Field(default=1800.0, ge=30.0, le=86400.0)
    model_store_path: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_endpoint(cls, value: Any) -> Any:
        """Accept only the historical fixed endpoint and normalize it to a port."""

        if not isinstance(value, dict) or "endpoint" not in value:
            return value
        endpoint = value.get("endpoint")
        if endpoint != OLLAMA_MANAGED_ENDPOINT:
            raise ValueError(
                "ollama_runtime.endpoint is legacy-only; configure integer port instead"
            )
        normalized = dict(value)
        configured_port = normalized.get("port", OLLAMA_DEFAULT_PORT)
        if configured_port != OLLAMA_DEFAULT_PORT:
            raise ValueError("ollama_runtime endpoint and port conflict")
        normalized["port"] = OLLAMA_DEFAULT_PORT
        normalized.pop("endpoint", None)
        return normalized

    @model_validator(mode="after")
    def derive_endpoint(self) -> OllamaRuntimeConfig:
        """Never retain a caller-supplied endpoint."""

        self.endpoint = ollama_loopback_endpoint(self.port)
        return self

    @field_validator("model_store_path")
    @classmethod
    def validate_model_store_path(cls, value: str | None) -> str | None:
        """Require an executor-local absolute model-store path when configured."""

        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("model_store_path contains an invalid character")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("model_store_path must be absolute")
        return str(path)


class OllamaRuntimeCapability(BaseModel):
    """Capability advertised by an executor after runtime configuration."""

    runtime_type: Literal["ollama"] = "ollama"
    port: StrictInt = Field(default=OLLAMA_DEFAULT_PORT, ge=1, le=65535)
    endpoint: str = OLLAMA_MANAGED_ENDPOINT
    management_enabled: bool
    max_concurrent_pulls: int = Field(ge=1, le=8)
    disk_headroom_bytes: int = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def normalize_endpoint(cls, value: Any) -> Any:
        return _normalize_advertised_ollama_endpoint(value)

    @model_validator(mode="after")
    def require_derived_endpoint(self) -> OllamaRuntimeCapability:
        if self.endpoint != ollama_loopback_endpoint(self.port):
            raise ValueError("advertised Ollama endpoint must match its loopback port")
        return self


class OllamaRuntimeStartRequest(BaseModel):
    """Typed controller-to-executor managed operation request."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=255)
    action: LocalModelOperationAction
    runtime_name: str = Field(min_length=1, max_length=255)
    request_hash: str = Field(min_length=1, max_length=255)
    force: bool = False

    @field_validator("runtime_name")
    @classmethod
    def require_canonical_reference(cls, value: str) -> str:
        """Managed pulls and deletes accept only WS2A canonical references."""

        from cognis.core.local_models import parse_local_model_reference

        parsed = parse_local_model_reference(value)
        if parsed.runtime_name != value:
            raise ValueError("runtime_name must be a canonical local-model reference")
        return value


class OllamaRuntimeModelRequest(BaseModel):
    """Typed read-only request for one canonical Ollama model."""

    model_config = ConfigDict(extra="forbid")

    runtime_name: str = Field(min_length=1, max_length=255)

    @field_validator("runtime_name")
    @classmethod
    def require_canonical_reference(cls, value: str) -> str:
        return OllamaRuntimeStartRequest.require_canonical_reference(value)


class OllamaRuntimeOperationStatus(BaseModel):
    """Bounded executor-local view of one managed operation."""

    operation_id: str
    action: LocalModelOperationAction
    runtime_name: str
    request_hash: str
    state: Literal["running", "succeeded", "failed", "cancelled"]
    progress_seq: int = Field(default=0, ge=0)
    progress_bytes: StrictInt = Field(default=0, ge=0, le=LOCAL_MODEL_BYTE_COUNT_MAX)
    phase: str | None = Field(default=None, max_length=120)
    error: str | None = Field(default=None, max_length=1000)


class OllamaRuntimeStatus(BaseModel):
    """Bounded read-only runtime observation returned by an executor."""

    runtime_type: Literal["ollama"] = "ollama"
    port: StrictInt = Field(default=OLLAMA_DEFAULT_PORT, ge=1, le=65535)
    endpoint: str = OLLAMA_MANAGED_ENDPOINT
    management_enabled: bool
    reachable: bool
    version: str | None = Field(default=None, max_length=120)
    installed: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    running: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    operations: list[OllamaRuntimeOperationStatus] = Field(default_factory=list, max_length=256)
    error: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def normalize_endpoint(cls, value: Any) -> Any:
        return _normalize_advertised_ollama_endpoint(value)

    @model_validator(mode="after")
    def require_derived_endpoint(self) -> OllamaRuntimeStatus:
        if self.endpoint != ollama_loopback_endpoint(self.port):
            raise ValueError("Ollama status endpoint must match its loopback port")
        return self


class LocalModelSelector(BaseModel):
    """Declarative selector resolved to concrete authorized executor rows."""

    executor_ids: list[str] = Field(default_factory=list, max_length=100)
    match_labels: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("executor_ids")
    @classmethod
    def validate_executor_ids(cls, values: list[str]) -> list[str]:
        """Normalize executor IDs while retaining caller order."""

        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("executor_ids must contain only non-empty values")
            if item not in seen:
                normalized.append(item)
                seen.add(item)
        return normalized

    @field_validator("match_labels")
    @classmethod
    def validate_match_labels(cls, values: dict[str, str]) -> dict[str, str]:
        """Require exact non-empty string label declarations."""

        normalized: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = raw_key.strip()
            value = raw_value.strip()
            if not key or not value:
                raise ValueError("match_labels must contain only non-empty string pairs")
            normalized[key] = value
        return normalized

    @model_validator(mode="after")
    def require_selector_term(self) -> LocalModelSelector:
        """Require at least one exact ID or label matcher."""

        if not self.executor_ids and not self.match_labels:
            raise ValueError("selector must specify executor_ids or match_labels")
        return self


class ParsedLocalModelReference(BaseModel):
    """Validated canonical representation of a local-model reference."""

    requested_ref: str
    canonical_name: str
    runtime_name: str
    source: LocalModelSource
    revision: str | None = None


class LocalModelDeploymentCreateRequest(BaseModel):
    """Create one declarative local-model deployment."""

    model_config = ConfigDict(extra="forbid")

    runtime_type: LocalModelRuntimeType = LocalModelRuntimeType.OLLAMA
    requested_ref: str = Field(min_length=1, max_length=255)
    digest: str | None = Field(default=None, max_length=255)
    selector: LocalModelSelector
    desired_state: LocalModelDesiredState = LocalModelDesiredState.PRESENT
    update_policy: LocalModelUpdatePolicy = LocalModelUpdatePolicy.IF_CHANGED
    prune_policy: LocalModelPrunePolicy = LocalModelPrunePolicy.RETAIN
    max_parallel: int = Field(default=1, ge=1, le=100)
    provider_id: str = Field(min_length=1, max_length=255)
    capacity_override_acknowledged: bool = False
    capacity_assessment_generation: int | None = Field(
        default=None,
        ge=0,
        le=LOCAL_MODEL_ASSESSMENT_MAX,
    )
    shared: bool = False

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        """Accept only a bounded opaque digest token, never a URL or path."""

        if value is None:
            return None
        if value != value.strip() or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9:+._-]{0,254}",
            value,
        ):
            raise ValueError("digest must be a bounded opaque token")
        return value


class LocalModelDeploymentUpdateRequest(BaseModel):
    """Patch mutable local-model desired state."""

    model_config = ConfigDict(extra="forbid")

    requested_ref: str | None = Field(default=None, min_length=1, max_length=255)
    digest: str | None = Field(default=None, max_length=255)
    selector: LocalModelSelector | None = None
    desired_state: LocalModelDesiredState | None = None
    update_policy: LocalModelUpdatePolicy | None = None
    prune_policy: LocalModelPrunePolicy | None = None
    max_parallel: int | None = Field(default=None, ge=1, le=100)
    provider_id: str | None = Field(default=None, max_length=255)
    capacity_override_acknowledged: bool | None = None
    capacity_assessment_generation: int | None = Field(
        default=None,
        ge=0,
        le=LOCAL_MODEL_ASSESSMENT_MAX,
    )

    @field_validator(
        "requested_ref",
        "selector",
        "desired_state",
        "update_policy",
        "prune_policy",
        "max_parallel",
        "capacity_override_acknowledged",
        mode="before",
    )
    @classmethod
    def reject_null_for_non_nullable_fields(cls, value: Any) -> Any:
        """Reject explicit null while still allowing fields to be omitted."""

        if value is None:
            raise PydanticCustomError(
                "null_not_allowed",
                "field must not be null",
            )
        return value

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        """Accept only a bounded opaque digest token, never a URL or path."""

        return LocalModelDeploymentCreateRequest.validate_digest(value)


class LocalModelDeploymentResponse(BaseModel):
    """Persisted local-model deployment."""

    deployment_id: str
    owner_email: str
    shared: bool
    runtime_type: LocalModelRuntimeType
    requested_ref: str
    canonical_name: str
    runtime_name: str
    source: LocalModelSource
    digest: str | None = None
    revision: str | None = None
    selector: LocalModelSelector
    desired_state: LocalModelDesiredState
    update_policy: LocalModelUpdatePolicy
    prune_policy: LocalModelPrunePolicy
    max_parallel: int
    generation: int
    provider_id: str | None = None
    lifecycle_state: Literal["managed", "needs_provider"]
    capacity_override_acknowledged: bool
    capacity_assessment_generation: int | None = Field(
        default=None,
        ge=0,
        le=LOCAL_MODEL_ASSESSMENT_MAX,
    )
    reconcile_requested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LocalModelTargetStatusResponse(BaseModel):
    """Persisted observed status for a concrete target."""

    target_id: str
    deployment_id: str
    executor_id: str
    generation: int
    observed_generation: int
    state: LocalModelTargetState
    observed_digest: str | None = None
    observed_size_bytes: StrictInt | None = Field(
        default=None,
        ge=0,
        le=LOCAL_MODEL_BYTE_COUNT_MAX,
    )
    current_operation_id: str | None = None
    last_error: str | None = None
    reconcile_requested_at: datetime | None = None
    reconcile_started_at: datetime | None = None
    reconciled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LocalModelOperationResponse(BaseModel):
    """Persisted future executor operation."""

    operation_id: str
    deployment_id: str
    executor_id: str
    generation: int
    action: LocalModelOperationAction
    state: LocalModelOperationState
    progress_seq: int
    progress_bytes: StrictInt = Field(ge=0, le=LOCAL_MODEL_BYTE_COUNT_MAX)
    phase: str | None = None
    idempotency_key: str
    request_hash: str
    post_pull_provider_upsert: bool = False
    sanitized_error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    finished_at: datetime | None = None


class ProviderLocalModelUpsertRequest(BaseModel):
    """Atomically add or merge a local model into an Ollama provider config."""

    model_config = ConfigDict(populate_by_name=True)

    requested_ref: str = Field(min_length=1, max_length=255)
    model_options: dict[str, Any] = Field(default_factory=dict, alias="model_config")
    set_default: bool = False

    @field_validator("model_options")
    @classmethod
    def reject_model_id_override(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Keep the validated reference authoritative for the model ID."""

        if "model_id" in value:
            raise ValueError("model_config must not override model_id")
        return value


class LocalModelRuntimeOperationCreateRequest(BaseModel):
    """Create one exact-target operation without accepting transport details."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(min_length=1, max_length=255)
    action: LocalModelOperationAction
    idempotency_key: str = Field(min_length=1, max_length=255)


class LocalModelProviderRecommendationRequest(BaseModel):
    """Find compatible Ollama providers for one model and optional host subset."""

    model_config = ConfigDict(extra="forbid")

    requested_ref: str = Field(min_length=1, max_length=255)
    selector: LocalModelSelector | None = None
    shared: bool = False


class LocalModelProviderCandidate(BaseModel):
    """Deterministically ranked provider eligible to own a deployment."""

    provider_id: str
    display_name: str
    owner_email: str
    executor_ids: list[str]
    contains_model: bool
    managed_local: bool
    healthy_host_count: int
    reason_codes: list[str]


class LocalModelProviderRecommendationResponse(BaseModel):
    """Stable recommendation result; the first candidate is preferred."""

    requested_ref: str
    runtime_name: str
    recommended_provider_id: str | None = None
    candidates: list[LocalModelProviderCandidate]


class LocalModelProviderFindOrCreateRequest(LocalModelProviderRecommendationRequest):
    """Resolve a reusable provider or create one for the exact host selector."""

    force_create: bool = False


class LocalModelProviderFindOrCreateResponse(BaseModel):
    """Idempotent provider resolution result."""

    provider_id: str
    created: bool
    reason_code: str


class LocalModelManagedDeploymentCreateRequest(BaseModel):
    """Atomically resolve/create a provider and create its first deployment."""

    model_config = ConfigDict(extra="forbid")

    requested_ref: str = Field(min_length=1, max_length=255)
    digest: str | None = Field(default=None, max_length=255)
    selector: LocalModelSelector
    desired_state: LocalModelDesiredState = LocalModelDesiredState.PRESENT
    update_policy: LocalModelUpdatePolicy = LocalModelUpdatePolicy.IF_CHANGED
    prune_policy: LocalModelPrunePolicy = LocalModelPrunePolicy.RETAIN
    max_parallel: int = Field(default=1, ge=1, le=100)
    capacity_override_acknowledged: bool = False
    capacity_assessment_generation: int | None = Field(
        default=None,
        ge=0,
        le=LOCAL_MODEL_ASSESSMENT_MAX,
    )
    shared: bool = False
    force_create_provider: bool = False

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        return LocalModelDeploymentCreateRequest.validate_digest(value)


class LocalModelManagedDeploymentCreateResponse(BaseModel):
    """Atomic provider resolution and deployment creation result."""

    deployment: LocalModelDeploymentResponse
    provider_id: str
    provider_created: bool
    provider_reason_code: str


class LocalModelManagedProviderAttachRequest(BaseModel):
    """Atomically create/reuse and attach a provider to a legacy deployment."""

    model_config = ConfigDict(extra="forbid")

    force_create_provider: bool = False


class LocalModelManagedProviderAttachResponse(LocalModelManagedDeploymentCreateResponse):
    """Atomic provider attachment result."""


class LocalModelDeploymentStatusResponse(BaseModel):
    """Rollout summary for one visible deployment."""

    deployment_id: str
    generation: int
    desired_state: LocalModelDesiredState
    total_targets: int
    state_counts: dict[str, int]
    ready: bool
    targets: list[LocalModelTargetStatusResponse]


class LocalModelCatalogSource(StrEnum):
    """User-facing catalog sources."""

    INSTALLED = "installed"
    OLLAMA = "ollama"
    HUGGINGFACE = "huggingface"


class LocalModelCatalogCapability(StrEnum):
    """Normalized capabilities advertised by catalog metadata."""

    CHAT = "chat"
    TOOLS = "tools"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    REASONING = "reasoning"


class LocalModelQuantization(BaseModel):
    """One selectable, immutable model artifact."""

    name: str = Field(min_length=1, max_length=64)
    requested_ref: str = Field(min_length=1, max_length=255)
    file_name: str | None = Field(default=None, max_length=512)
    size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    bits_per_weight: float | None = Field(default=None, gt=0, le=32)


class LocalModelCatalogItem(BaseModel):
    """Normalized model metadata independent of its upstream catalog."""

    catalog_id: str = Field(min_length=1, max_length=255)
    source: LocalModelCatalogSource
    requested_ref: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=255)
    publisher: str | None = Field(default=None, max_length=255)
    repository_url: str | None = Field(default=None, max_length=512)
    model_card_url: str | None = Field(default=None, max_length=512)
    revision_sha: str | None = Field(default=None, min_length=7, max_length=64)
    license: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    downloads: int | None = Field(default=None, ge=0, le=2**63 - 1)
    likes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    last_modified: datetime | None = None
    pipeline_tag: str | None = Field(default=None, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=100)
    base_models: list[str] = Field(default_factory=list, max_length=20)
    capabilities: list[LocalModelCatalogCapability] = Field(default_factory=list)
    parameter_count: int | None = Field(default=None, ge=1, le=2**63 - 1)
    quantizations: list[LocalModelQuantization] = Field(default_factory=list, max_length=100)
    file_size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    advertised_max_context: int | None = Field(default=None, ge=1, le=2**31 - 1)
    architecture: dict[str, int] = Field(default_factory=dict)
    architecture_name: str | None = Field(default=None, max_length=128)
    metadata_status: Literal["basic", "complete", "error"] = "basic"
    metadata_confidence: Literal["low", "medium", "high"] = "medium"
    metadata_diagnostics: list[str] = Field(default_factory=list, max_length=10)
    reference_integrity: Literal["pinned", "floating", "unknown"] = "floating"
    warnings: list[str] = Field(default_factory=list, max_length=20)


class LocalModelCatalogSourceStatus(BaseModel):
    """Availability of one catalog adapter."""

    source: LocalModelCatalogSource
    available: bool
    detail: str | None = Field(default=None, max_length=500)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86400)


class LocalModelCatalogResponse(BaseModel):
    """One bounded page of normalized catalog results."""

    items: list[LocalModelCatalogItem]
    next_cursor: str | None = Field(default=None, max_length=512)
    sources: list[LocalModelCatalogSourceStatus]
    cached: bool = False
    pagination_note: str | None = Field(default=None, max_length=500)


class LocalModelFitStatus(StrEnum):
    """Advisory model-fit outcome for one executor."""

    FIT = "FIT"
    FIT_WITH_OFFLOAD = "FIT_WITH_OFFLOAD"
    NO_FIT = "NO_FIT"
    UNKNOWN = "UNKNOWN"


class LocalModelFitConfidence(StrEnum):
    """Confidence in an advisory estimate."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LocalModelFitMetadata(BaseModel):
    """Catalog, GGUF, or Ollama metadata used by the estimator."""

    requested_ref: str = Field(min_length=1, max_length=255)
    weights_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    file_size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    parameter_count: int | None = Field(default=None, ge=1, le=2**63 - 1)
    quantization: str | None = Field(default=None, max_length=64)
    bits_per_weight: float | None = Field(default=None, gt=0, le=32)
    layer_count: int | None = Field(default=None, ge=1, le=1000)
    kv_head_count: int | None = Field(default=None, ge=1, le=1000)
    head_dimension: int | None = Field(default=None, ge=1, le=65536)
    kv_bytes_per_element_min: int = Field(default=1, ge=1, le=8)
    kv_bytes_per_element_max: int = Field(default=2, ge=1, le=8)
    advertised_max_context: int | None = Field(default=None, ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def validate_kv_range(self) -> LocalModelFitMetadata:
        if self.kv_bytes_per_element_min > self.kv_bytes_per_element_max:
            raise ValueError("KV minimum bytes per element cannot exceed maximum")
        return self


class LocalModelFitPlanRequest(BaseModel):
    """Plan one exact model artifact and context across selected executors."""

    model: LocalModelFitMetadata
    selector: LocalModelSelector
    provider_id: str | None = Field(default=None, min_length=1, max_length=255)
    context_tokens: int = Field(gt=0)


class LocalModelFitBreakdown(BaseModel):
    """Memory components for one executor estimate."""

    weights_bytes: int | None = None
    kv_cache_min_bytes: int | None = None
    kv_cache_max_bytes: int | None = None
    runtime_buffer_bytes: int | None = None
    reserved_headroom_bytes: int | None = None
    required_min_bytes: int | None = None
    required_max_bytes: int | None = None


class LocalModelFitAssessment(BaseModel):
    """Static or current-admission assessment."""

    status: LocalModelFitStatus
    confidence: LocalModelFitConfidence
    available_bytes: int | None = None
    accelerator_available_bytes: int | None = None
    host_available_bytes: int | None = None
    reason_codes: list[str]


class LocalModelExecutorFitResult(BaseModel):
    """Independent estimate for one executor; results are never averaged."""

    executor_id: str
    executor_name: str
    context_tokens: int
    static: LocalModelFitAssessment
    admission: LocalModelFitAssessment
    breakdown: LocalModelFitBreakdown
    unified_memory: bool | None = None
    snapshot_age_seconds: int | None = None
    advertised_max_exceeded: bool = False
    assumptions: list[str] = Field(default_factory=list)


class LocalModelContextOption(BaseModel):
    """One standard context preset and its group-wide safety zone."""

    context_tokens: int
    zone: Literal["green", "yellow", "red", "unknown"]
    limiting_executor_ids: list[str] = Field(default_factory=list)


class LocalModelFitPlanResponse(BaseModel):
    """Advisory capacity plan for an exact selector and context."""

    assessment_generation: int = Field(ge=0, le=LOCAL_MODEL_ASSESSMENT_MAX)
    advisory_only: Literal[True] = True
    requested_context_tokens: int
    advertised_max_context: int | None = None
    advertised_max_exceeded: bool
    recommended_context_tokens: int | None = None
    context_options: list[LocalModelContextOption]
    executors: list[LocalModelExecutorFitResult]
