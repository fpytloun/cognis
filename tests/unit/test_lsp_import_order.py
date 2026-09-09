"""Cold-import coverage for executor LSP compatibility exports."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "first_module",
    ["cognis.executor.lsp_runtime", "cognis.tools.executor.lsp"],
)
def test_lsp_runtime_exports_are_independent_of_import_order(first_module: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import {first_module}; "
                "from cognis.executor import lsp_runtime; "
                "from cognis.tools.executor import lsp; "
                "assert lsp.build_lsp_manager is lsp_runtime.build_lsp_manager; "
                "assert lsp.LSPStatusReport is lsp_runtime.LSPStatusReport; "
                "assert lsp.build_lsp_manager({'lsp_enabled': False}) is None"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
