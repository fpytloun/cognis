"""Minimal LSP type definitions for diagnostics integration.

Only the types needed for consuming ``textDocument/publishDiagnostics``
notifications.  This avoids pulling in the full ``lsprotocol`` package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from pydantic import BaseModel


class DiagnosticSeverity(IntEnum):
    """LSP diagnostic severity levels."""

    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4


class Position(BaseModel):
    """Zero-indexed line/character position in a text document."""

    line: int
    character: int


class Range(BaseModel):
    """A range in a text document (start inclusive, end exclusive)."""

    start: Position
    end: Position


class Diagnostic(BaseModel):
    """A diagnostic (error, warning, etc.) reported by a language server."""

    range: Range
    severity: DiagnosticSeverity | None = None
    code: str | int | None = None
    source: str | None = None
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == DiagnosticSeverity.ERROR

    @property
    def is_warning(self) -> bool:
        return self.severity == DiagnosticSeverity.WARNING


class DiagnosticFreshness(StrEnum):
    """Freshness/provenance state for diagnostics returned by a language server."""

    FRESH = "fresh"
    FRESH_UNVERSIONED = "fresh_unversioned"
    STALE = "stale"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class DiagnosticSnapshot:
    """Diagnostics plus the version/provenance metadata needed to trust them."""

    server_id: str
    uri: str
    document_version: int | None
    diagnostic_version: int | None
    received_sequence: int
    received_at_monotonic: float
    diagnostics: list[Diagnostic]
    freshness: DiagnosticFreshness
    reason: str | None = None

    @property
    def is_fresh(self) -> bool:
        return self.freshness in (
            DiagnosticFreshness.FRESH,
            DiagnosticFreshness.FRESH_UNVERSIONED,
        )


@dataclass(slots=True, frozen=True)
class DiagnosticWaitResult:
    """Outcome of waiting for fresh diagnostics for one server/file pair."""

    server_id: str
    uri: str
    target_version: int | None
    status: DiagnosticFreshness
    duration_ms: int
    snapshot: DiagnosticSnapshot | None = None
    message: str | None = None
    error_count: int = 0
    warning_count: int = 0


@dataclass(slots=True)
class DiagnosticCollection:
    """Aggregated diagnostics and wait outcomes for an edit operation."""

    waits: list[DiagnosticWaitResult] = field(default_factory=list)
    snapshots_by_path: dict[str, list[DiagnosticSnapshot]] = field(default_factory=dict)
