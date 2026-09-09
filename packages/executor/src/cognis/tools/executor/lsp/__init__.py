"""LSP diagnostics integration for executor file tools.

Provides automatic language server feedback (errors, warnings) after
file edits.  The ``LSPManager`` lazily spawns language servers, routes
file notifications, and collects diagnostics for LLM context injection.

Usage in tool handlers::

    from cognis.tools.executor.lsp import LSP_MANAGER_KEY, LSPManager

    lsp: LSPManager | None = context.runtime_metadata.get(LSP_MANAGER_KEY)
    if lsp is not None:
        await lsp.touch_file(file_path, wait=True)
        diagnostics = lsp.get_diagnostics(file_path)
"""

from __future__ import annotations

from pkgutil import extend_path
from typing import Any

__path__ = extend_path(__path__, __name__)

from cognis.tools.executor.lsp.diagnostics import format_diagnostics_for_llm
from cognis.tools.executor.lsp.manager import LSP_MANAGER_KEY, LSPManager
from cognis.tools.executor.lsp.runtime import LSP_STATUS_CAPABILITY, LSPStatusReport


def __getattr__(name: str) -> Any:
    """Resolve legacy runtime exports without an import-order dependency."""
    if name in {
        "build_lsp_manager",
        "build_lsp_status_report",
        "build_lsp_unavailable_report",
        "cleanup_lsp_manager",
        "resolve_lsp_runtime_config",
    }:
        from cognis.executor import lsp_runtime

        return getattr(lsp_runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LSP_MANAGER_KEY",
    "LSP_STATUS_CAPABILITY",
    "LSPManager",
    "LSPStatusReport",
    "build_lsp_manager",
    "build_lsp_status_report",
    "build_lsp_unavailable_report",
    "cleanup_lsp_manager",
    "format_diagnostics_for_llm",
    "resolve_lsp_runtime_config",
]
