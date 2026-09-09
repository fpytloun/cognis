"""Typed current resource snapshots reported by executors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

RESOURCE_SNAPSHOT_SCHEMA_VERSION: Literal[1] = 1
RESOURCE_SNAPSHOT_STALE_AFTER_SECONDS = 120
RESOURCE_SNAPSHOT_MAX_ACCELERATORS = 16
RESOURCE_SNAPSHOT_MAX_RUNNING_MODELS = 20
RESOURCE_SNAPSHOT_MAX_BYTES = 2**63 - 1
BoundedModelName = Annotated[str, Field(max_length=256)]


class ResourceSnapshotFreshness(BaseModel):
    """Controller-computed age for a current snapshot."""

    model_config = ConfigDict(extra="ignore")

    age_seconds: int = Field(ge=0)
    stale_after_seconds: int = Field(ge=1)
    stale: bool


class CPUResourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str | None = Field(default=None, max_length=256)
    physical_cores: int | None = Field(default=None, ge=1)
    logical_cores: int | None = Field(default=None, ge=1)
    utilization_percent: float | None = Field(default=None, ge=0, le=100)


class MemoryResourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_bytes: int | None = Field(default=None, ge=0, le=RESOURCE_SNAPSHOT_MAX_BYTES)
    available_bytes: int | None = Field(
        default=None,
        ge=0,
        le=RESOURCE_SNAPSHOT_MAX_BYTES,
    )
    used_bytes: int | None = Field(default=None, ge=0, le=RESOURCE_SNAPSHOT_MAX_BYTES)
    unified: bool | None = None


class AcceleratorResourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    backend: Literal["metal", "nvidia"]
    name: str | None = Field(default=None, max_length=256)
    total_memory_bytes: int | None = Field(
        default=None,
        ge=0,
        le=RESOURCE_SNAPSHOT_MAX_BYTES,
    )
    used_memory_bytes: int | None = Field(
        default=None,
        ge=0,
        le=RESOURCE_SNAPSHOT_MAX_BYTES,
    )
    utilization_percent: float | None = Field(default=None, ge=0, le=100)


class DiskResourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_bytes: int | None = Field(default=None, ge=0, le=RESOURCE_SNAPSHOT_MAX_BYTES)
    free_bytes: int | None = Field(default=None, ge=0, le=RESOURCE_SNAPSHOT_MAX_BYTES)


class OllamaResourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["reachable", "unreachable", "unknown"] = "unknown"
    version: str | None = Field(default=None, max_length=128)
    installed_model_count: int | None = Field(default=None, ge=0)
    running_model_count: int | None = Field(default=None, ge=0)
    running_models: list[BoundedModelName] | None = Field(
        default=None,
        max_length=RESOURCE_SNAPSHOT_MAX_RUNNING_MODELS,
    )


class ExecutorRuntimeResourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uptime_seconds: int | None = Field(default=None, ge=0)
    active_calls: int | None = Field(default=None, ge=0)
    configured: bool | None = None
    state: str | None = Field(default=None, max_length=64)


class ExecutorResourceSnapshot(BaseModel):
    """Latest executor state only; this model is not a historical sample."""

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1] = RESOURCE_SNAPSHOT_SCHEMA_VERSION
    observed_at: datetime
    freshness: ResourceSnapshotFreshness | None = None
    os: str | None = Field(default=None, max_length=64)
    arch: str | None = Field(default=None, max_length=64)
    cpu: CPUResourceSnapshot | None = None
    memory: MemoryResourceSnapshot | None = None
    accelerators: list[AcceleratorResourceSnapshot] | None = Field(
        default=None,
        max_length=RESOURCE_SNAPSHOT_MAX_ACCELERATORS,
    )
    ollama_model_store: DiskResourceSnapshot | None = None
    ollama: OllamaResourceSnapshot | None = None
    runtime: ExecutorRuntimeResourceSnapshot | None = None

    def with_current_freshness(
        self,
        *,
        now: datetime | None = None,
        received_at: datetime | None = None,
        stale_after_seconds: int = RESOURCE_SNAPSHOT_STALE_AFTER_SECONDS,
    ) -> ExecutorResourceSnapshot:
        current = now or datetime.now(UTC)
        freshness_reference = received_at or self.observed_at
        if freshness_reference.tzinfo is None:
            freshness_reference = freshness_reference.replace(tzinfo=UTC)
        age_seconds = max(0, int((current - freshness_reference).total_seconds()))
        return self.model_copy(
            update={
                "freshness": ResourceSnapshotFreshness(
                    age_seconds=age_seconds,
                    stale_after_seconds=stale_after_seconds,
                    stale=age_seconds > stale_after_seconds,
                )
            }
        )


def normalize_executor_resource_snapshot(
    value: Any,
    *,
    now: datetime | None = None,
) -> ExecutorResourceSnapshot | None:
    """Validate and sanitize an executor-supplied current snapshot."""

    if not isinstance(value, dict):
        return None
    try:
        snapshot = ExecutorResourceSnapshot.model_validate(value)
    except ValidationError:
        return None
    current = now or datetime.now(UTC)
    observed = snapshot.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
        snapshot = snapshot.model_copy(update={"observed_at": observed})
    if observed > current + timedelta(minutes=5):
        return None
    return snapshot
