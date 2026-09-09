"""Compatibility exports for portable runtime capability DTOs."""

from cognis.models.runtime_capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    BrowserCapabilityReport,
    CapabilityState,
    CapabilityStatus,
    RuntimeCapabilityReport,
    normalize_runtime_capability_report,
)

__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "BrowserCapabilityReport",
    "CapabilityState",
    "CapabilityStatus",
    "RuntimeCapabilityReport",
    "normalize_runtime_capability_report",
]
