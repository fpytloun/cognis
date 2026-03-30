"""Minimal LSP type definitions for diagnostics integration.

Only the types needed for consuming ``textDocument/publishDiagnostics``
notifications.  This avoids pulling in the full ``lsprotocol`` package.
"""

from __future__ import annotations

from enum import IntEnum

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
