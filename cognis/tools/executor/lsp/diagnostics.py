"""Format LSP diagnostics for LLM context injection.

Produces human-readable diagnostic summaries that are appended to tool
results after file edits.  Only errors and warnings are included;
informational hints are filtered out.  Output is capped to avoid
flooding the LLM context window.
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath

from cognis.tools.executor.lsp.types import Diagnostic, DiagnosticSeverity

MAX_DIAGNOSTICS_PER_FILE = 20
MAX_OTHER_FILES = 5
MAX_TOTAL_CHARS = 3000

_SEVERITY_LABELS: dict[DiagnosticSeverity, str] = {
    DiagnosticSeverity.ERROR: "error",
    DiagnosticSeverity.WARNING: "warning",
}


def format_diagnostics_for_llm(
    diagnostics: dict[str, list[Diagnostic]],
    edited_file: str,
    *,
    cwd: str | None = None,
) -> str:
    """Format LSP diagnostics as text appended to tool results.

    Returns empty string if no actionable diagnostics (errors/warnings) found.

    Args:
        diagnostics: Mapping of absolute file path to diagnostic list.
        edited_file: The file that was just edited (shown first).
        cwd: Working directory for making paths relative.
    """
    if not diagnostics:
        return ""

    parts: list[str] = []
    total_chars = 0

    # Diagnostics for the edited file
    edited_diags = _filter_actionable(diagnostics.get(edited_file, []))
    if edited_diags:
        section = _format_file_section(
            edited_file, edited_diags[:MAX_DIAGNOSTICS_PER_FILE], cwd=cwd
        )
        overflow = len(edited_diags) - MAX_DIAGNOSTICS_PER_FILE
        if overflow > 0:
            section += f"\n  ({overflow} more diagnostic(s) omitted)"
        parts.append(f"LSP diagnostics for this file:\n{section}")
        total_chars += len(parts[-1])

    # Diagnostics for other files
    other_file_count = 0
    shown_other_paths: set[str] = set()
    for file_path, file_diags in sorted(diagnostics.items()):
        if file_path == edited_file:
            continue
        actionable = _filter_actionable(file_diags)
        if not actionable:
            continue
        if other_file_count >= MAX_OTHER_FILES:
            remaining = sum(
                1
                for fp, fd in diagnostics.items()
                if fp != edited_file and fp not in shown_other_paths and _filter_actionable(fd)
            )
            if remaining > 0:
                parts.append(f"  ({remaining} more file(s) with diagnostics)")
            break
        section = _format_file_section(file_path, actionable[:MAX_DIAGNOSTICS_PER_FILE], cwd=cwd)
        overflow = len(actionable) - MAX_DIAGNOSTICS_PER_FILE
        if overflow > 0:
            section += f"\n  ({overflow} more diagnostic(s) omitted)"
        if other_file_count == 0:
            parts.append(f"LSP diagnostics in other files:\n{section}")
        else:
            parts.append(section)
        total_chars += len(section)
        other_file_count += 1
        shown_other_paths.add(file_path)

        if total_chars >= MAX_TOTAL_CHARS:
            parts.append("  (output truncated)")
            break

    return "\n\n".join(parts)


def format_diagnostic_line(diag: Diagnostic, file_path: str, *, cwd: str | None = None) -> str:
    """Format a single diagnostic as a one-line string.

    Format: ``relative/path.py:10:4 error: message (code)``
    """
    display_path = _make_relative(file_path, cwd)
    line = diag.range.start.line + 1  # LSP is 0-indexed, display as 1-indexed
    col = diag.range.start.character + 1
    severity_label = (
        _SEVERITY_LABELS.get(diag.severity, "info") if diag.severity is not None else "diagnostic"
    )
    code_suffix = f" ({diag.code})" if diag.code is not None else ""
    return f"  {display_path}:{line}:{col} {severity_label}: {diag.message}{code_suffix}"


def _filter_actionable(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Keep only errors and warnings, sorted errors-first."""
    actionable = [
        d
        for d in diagnostics
        if d.severity is not None
        and d.severity in (DiagnosticSeverity.ERROR, DiagnosticSeverity.WARNING)
    ]
    return sorted(actionable, key=lambda d: (d.severity or 99, d.range.start.line))


def _format_file_section(
    file_path: str,
    diagnostics: list[Diagnostic],
    *,
    cwd: str | None = None,
) -> str:
    """Format diagnostics for a single file."""
    lines = [format_diagnostic_line(d, file_path, cwd=cwd) for d in diagnostics]
    return "\n".join(lines)


def _make_relative(file_path: str, cwd: str | None) -> str:
    """Make a file path relative to cwd if possible."""
    if cwd is None:
        return file_path
    try:
        return str(PurePosixPath(os.path.relpath(file_path, cwd)))
    except ValueError:
        return file_path
