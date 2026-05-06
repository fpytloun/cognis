"""Unit tests for LSP diagnostic formatting."""

from __future__ import annotations

from cognis.tools.executor.lsp.diagnostics import (
    MAX_OTHER_FILES,
    format_diagnostic_line,
    format_diagnostics_for_llm,
)
from cognis.tools.executor.lsp.types import (
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)


def _diag(
    line: int = 0,
    col: int = 0,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
    message: str = "test error",
    code: str | int | None = None,
    source: str | None = None,
) -> Diagnostic:
    """Helper to create a diagnostic."""
    return Diagnostic(
        range=Range(
            start=Position(line=line, character=col),
            end=Position(line=line, character=col + 1),
        ),
        severity=severity,
        code=code,
        source=source,
        message=message,
    )


class TestFormatDiagnosticLine:
    """Test individual diagnostic line formatting."""

    def test_basic_error(self) -> None:
        diag = _diag(line=9, col=3, message="undefined variable 'x'", code="E001")
        result = format_diagnostic_line(diag, "/src/foo.py")
        assert "/src/foo.py:10:4 error: undefined variable 'x' (E001)" in result

    def test_warning_severity(self) -> None:
        diag = _diag(severity=DiagnosticSeverity.WARNING, message="unused import")
        result = format_diagnostic_line(diag, "/src/foo.py")
        assert "warning:" in result

    def test_no_code(self) -> None:
        diag = _diag(message="something wrong")
        result = format_diagnostic_line(diag, "/src/foo.py")
        assert "something wrong" in result
        assert "()" not in result  # No empty parens for missing code

    def test_relative_path(self) -> None:
        diag = _diag(message="error")
        result = format_diagnostic_line(
            diag, "/home/user/project/src/foo.py", cwd="/home/user/project"
        )
        assert "src/foo.py" in result
        assert "/home/user/project" not in result

    def test_integer_code(self) -> None:
        diag = _diag(message="type error", code=2304)
        result = format_diagnostic_line(diag, "/src/foo.py")
        assert "(2304)" in result


class TestFormatDiagnosticsForLlm:
    """Test the full diagnostics formatting function."""

    def test_empty_diagnostics(self) -> None:
        result = format_diagnostics_for_llm({}, "/src/foo.py")
        assert result == ""

    def test_no_actionable_diagnostics(self) -> None:
        """Hint and info diagnostics should be filtered out."""
        diags = {
            "/src/foo.py": [
                _diag(severity=DiagnosticSeverity.HINT, message="hint"),
                _diag(severity=DiagnosticSeverity.INFORMATION, message="info"),
            ]
        }
        result = format_diagnostics_for_llm(diags, "/src/foo.py")
        assert result == ""

    def test_errors_only(self) -> None:
        diags = {
            "/src/foo.py": [
                _diag(message="error 1"),
                _diag(message="error 2"),
            ]
        }
        result = format_diagnostics_for_llm(diags, "/src/foo.py")
        assert "LSP diagnostics for this file" in result
        assert "2 errors" in result
        assert "fix before proceeding" in result
        assert "error 1" in result
        assert "error 2" in result

    def test_header_uses_review_when_only_warnings(self) -> None:
        diags = {
            "/src/foo.py": [
                _diag(severity=DiagnosticSeverity.WARNING, message="warn 1"),
            ]
        }
        result = format_diagnostics_for_llm(diags, "/src/foo.py")
        assert "1 warning" in result
        assert "review" in result
        # Warning-only output must not push the model to "fix" — the
        # severity rules differ.
        assert "fix before proceeding" not in result

    def test_warnings_included(self) -> None:
        diags = {
            "/src/foo.py": [
                _diag(severity=DiagnosticSeverity.WARNING, message="unused var"),
            ]
        }
        result = format_diagnostics_for_llm(diags, "/src/foo.py")
        assert "warning:" in result

    def test_other_files(self) -> None:
        diags = {
            "/src/foo.py": [_diag(message="error in foo")],
            "/src/bar.py": [_diag(message="error in bar")],
        }
        result = format_diagnostics_for_llm(diags, "/src/foo.py")
        assert "LSP diagnostics for this file" in result
        assert "error in foo" in result
        assert "LSP diagnostics in other files:" in result
        assert "error in bar" in result

    def test_cap_per_file(self) -> None:
        """More than MAX_DIAGNOSTICS_PER_FILE should be capped."""
        many_diags = [_diag(line=i, message=f"error {i}") for i in range(30)]
        diags = {"/src/foo.py": many_diags}
        result = format_diagnostics_for_llm(diags, "/src/foo.py")
        assert "omitted" in result

    def test_cap_other_files(self) -> None:
        """More than MAX_OTHER_FILES should be limited."""
        diags: dict[str, list[Diagnostic]] = {"/src/main.py": [_diag(message="main error")]}
        for i in range(MAX_OTHER_FILES + 3):
            diags[f"/src/file{i}.py"] = [_diag(message=f"error in file{i}")]

        result = format_diagnostics_for_llm(diags, "/src/main.py")
        assert "LSP diagnostics for this file" in result

    def test_errors_sorted_before_warnings(self) -> None:
        """Errors should appear before warnings."""
        diags = {
            "/src/foo.py": [
                _diag(severity=DiagnosticSeverity.WARNING, line=1, message="warning first"),
                _diag(severity=DiagnosticSeverity.ERROR, line=2, message="error second"),
            ]
        }
        result = format_diagnostics_for_llm(diags, "/src/foo.py")
        error_pos = result.index("error second")
        warning_pos = result.index("warning first")
        assert error_pos < warning_pos
