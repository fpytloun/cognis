"""Portable runtime capability report contracts.

Runtime probing belongs to the executor.  This module contains only the
versioned DTOs and tolerant normalization used by both distributions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CAPABILITY_SCHEMA_VERSION = 1
CapabilityState = Literal["ready", "installable", "unavailable", "unknown"]
_MAX_ITEMS = 256
_MAX_NAME_LENGTH = 100
_MAX_TEXT_LENGTH = 180
_KNOWN_VARIANTS = frozenset({"minimal", "general", "development"})


class CapabilityStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: CapabilityState
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    version: str | None = Field(default=None, max_length=64)


class BrowserCapabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtimes: dict[str, CapabilityStatus] = Field(default_factory=dict, max_length=4)
    engines: dict[str, CapabilityStatus] = Field(default_factory=dict, max_length=4)
    channels: dict[str, CapabilityStatus] = Field(default_factory=dict, max_length=12)


class RuntimeCapabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=CAPABILITY_SCHEMA_VERSION, ge=1, le=1)
    observed_at: datetime
    executor_version: str = Field(min_length=1, max_length=64)
    image_variant: str | None = Field(default=None, max_length=32)
    desired_tools: list[str] = Field(default_factory=list, max_length=_MAX_ITEMS)
    observed_tools: list[str] = Field(default_factory=list, max_length=_MAX_ITEMS)
    supported_tools: list[str] = Field(default_factory=list, max_length=_MAX_ITEMS)
    supported_components: list[str] = Field(default_factory=list, max_length=32)
    components: dict[str, CapabilityStatus] = Field(default_factory=dict, max_length=32)
    browser: BrowserCapabilityReport = Field(default_factory=BrowserCapabilityReport)
    officecli: CapabilityStatus
    mcp_launch: CapabilityStatus
    git: CapabilityStatus
    node: CapabilityStatus
    uv: CapabilityStatus
    lsp: CapabilityStatus

    @field_validator("desired_tools", "observed_tools", "supported_tools", "supported_components")
    @classmethod
    def _bounded_names(cls, values: list[str]) -> list[str]:
        return [
            value
            for value in values
            if isinstance(value, str) and 0 < len(value) <= _MAX_NAME_LENGTH
        ][:_MAX_ITEMS]

    @field_validator("image_variant")
    @classmethod
    def _allowlisted_variant(cls, value: str | None) -> str | None:
        return value if value in _KNOWN_VARIANTS else None

    @classmethod
    def unknown(cls, *, observed_at: datetime | None = None) -> RuntimeCapabilityReport:
        status = CapabilityStatus(
            state="unknown",
            reason_code="report_unknown",
            message="Runtime capability report is unavailable or uses an unknown schema.",
        )
        return cls(
            observed_at=observed_at or datetime.now(UTC),
            executor_version="unknown",
            officecli=status,
            mcp_launch=status,
            git=status,
            node=status,
            uv=status,
            lsp=status,
        )


def normalize_runtime_capability_report(value: Any) -> RuntimeCapabilityReport | None:
    """Validate a report without allowing malformed telemetry to reject RPCs."""
    if not isinstance(value, dict) or value.get("schema_version") != CAPABILITY_SCHEMA_VERSION:
        return None
    try:
        return RuntimeCapabilityReport.model_validate(value)
    except Exception:
        return None
