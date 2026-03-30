"""LSP language server definitions.

Each definition describes how to detect, install, and run a language
server.  The initial set covers the most common languages; additional
servers can be added as data-driven entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cognis.tools.executor.lsp.install import (
    InstallStrategy,
    NpmInstall,
    ToolchainInstall,
)

# Note: GitHubBinaryInstall is available but not used in the initial
# server set because SHA-256 digests need to be maintained per-release.
# It will be used for clangd and similar servers once a digest update
# mechanism is in place.


@dataclass(frozen=True)
class LSPServerDefinition:
    """Definition of an LSP language server."""

    server_id: str
    """Unique identifier, e.g. ``pyright``."""

    extensions: frozenset[str]
    """File extensions handled by this server, e.g. ``{".py", ".pyi"}``."""

    command: str
    """Binary/command name, e.g. ``pyright-langserver``."""

    args: tuple[str, ...]
    """Command-line arguments, e.g. ``("--stdio",)``."""

    root_markers: tuple[str, ...]
    """Files that indicate the project root, e.g. ``("pyproject.toml",)``."""

    language_id_map: dict[str, str] = field(default_factory=dict)
    """Mapping of extension to LSP language ID, e.g. ``{".py": "python"}``."""

    install_strategy: InstallStrategy | None = None
    """Optional auto-install strategy.  None = PATH detection only."""

    init_options: dict[str, Any] | None = None
    """Server-specific initialization options."""

    npm_run: bool = False
    """If True, the resolved command is a JS file run via ``node``."""

    def language_id(self, extension: str) -> str:
        """Return the LSP language ID for a file extension."""
        return self.language_id_map.get(extension, self.server_id)


# ---------------------------------------------------------------------------
# Initial server set (7 servers, architect-approved scope)
# ---------------------------------------------------------------------------

PYRIGHT = LSPServerDefinition(
    server_id="pyright",
    extensions=frozenset({".py", ".pyi"}),
    command="pyright-langserver",
    args=("--stdio",),
    root_markers=(
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
        "pyrightconfig.json",
    ),
    language_id_map={".py": "python", ".pyi": "python"},
    install_strategy=NpmInstall(
        package="pyright",
        version="1.1.390",
        entry_point="node_modules/pyright/dist/pyright-langserver.js",
    ),
    npm_run=True,
)

RUFF_LSP = LSPServerDefinition(
    server_id="ruff",
    extensions=frozenset({".py", ".pyi"}),
    command="ruff",
    args=("server",),
    root_markers=(
        "pyproject.toml",
        "ruff.toml",
        ".ruff.toml",
        "setup.py",
        "setup.cfg",
    ),
    language_id_map={".py": "python", ".pyi": "python"},
    # ruff is typically already installed in Python projects;
    # no auto-install — rely on PATH detection.
    install_strategy=None,
)

TYPESCRIPT = LSPServerDefinition(
    server_id="typescript",
    extensions=frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}),
    command="typescript-language-server",
    args=("--stdio",),
    root_markers=(
        "tsconfig.json",
        "jsconfig.json",
        "package.json",
        "package-lock.json",
    ),
    language_id_map={
        ".ts": "typescript",
        ".tsx": "typescriptreact",
        ".js": "javascript",
        ".jsx": "javascriptreact",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".mts": "typescript",
        ".cts": "typescript",
    },
    install_strategy=NpmInstall(
        package="typescript-language-server",
        version="4.3.3",
        entry_point="node_modules/typescript-language-server/lib/cli.mjs",
    ),
    npm_run=True,
)

GOPLS = LSPServerDefinition(
    server_id="gopls",
    extensions=frozenset({".go"}),
    command="gopls",
    args=(),
    root_markers=("go.work", "go.mod", "go.sum"),
    language_id_map={".go": "go"},
    install_strategy=ToolchainInstall(
        command=["go", "install", "golang.org/x/tools/gopls@v0.17.1"],
        binary_name="gopls",
        env_overrides={"GOBIN": "{cache_dir}"},
    ),
)

RUST_ANALYZER = LSPServerDefinition(
    server_id="rust-analyzer",
    extensions=frozenset({".rs"}),
    command="rust-analyzer",
    args=(),
    root_markers=("Cargo.toml", "Cargo.lock"),
    language_id_map={".rs": "rust"},
    # No auto-install — rust-analyzer is typically installed via rustup
    install_strategy=None,
)

CLANGD = LSPServerDefinition(
    server_id="clangd",
    extensions=frozenset({".c", ".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx"}),
    command="clangd",
    args=("--background-index",),
    root_markers=(
        "compile_commands.json",
        "compile_flags.txt",
        ".clangd",
        "CMakeLists.txt",
        "Makefile",
    ),
    language_id_map={
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".c++": "cpp",
        ".hpp": "cpp",
        ".hh": "cpp",
        ".hxx": "cpp",
    },
    # No auto-install initially — clangd binary downloads require
    # per-release SHA-256 digests.  Users should install via their
    # system package manager.
    install_strategy=None,
)

BASH_LS = LSPServerDefinition(
    server_id="bash",
    extensions=frozenset({".sh", ".bash", ".zsh", ".ksh"}),
    command="bash-language-server",
    args=("start",),
    root_markers=(),  # Bash has no standard project root marker
    language_id_map={
        ".sh": "shellscript",
        ".bash": "shellscript",
        ".zsh": "shellscript",
        ".ksh": "shellscript",
    },
    install_strategy=NpmInstall(
        package="bash-language-server",
        version="5.4.2",
        entry_point="node_modules/bash-language-server/out/cli.js",
    ),
    npm_run=True,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: All built-in server definitions.
BUILTIN_SERVERS: list[LSPServerDefinition] = [
    PYRIGHT,
    RUFF_LSP,
    TYPESCRIPT,
    GOPLS,
    RUST_ANALYZER,
    CLANGD,
    BASH_LS,
]

#: Mapping of file extension to server definitions (multiple servers per
#: extension is allowed, e.g. pyright + ruff for ``.py``).
_EXTENSION_MAP: dict[str, list[LSPServerDefinition]] | None = None


def get_servers_for_extension(extension: str) -> list[LSPServerDefinition]:
    """Return server definitions that handle the given file extension."""
    global _EXTENSION_MAP
    if _EXTENSION_MAP is None:
        _EXTENSION_MAP = {}
        for server in BUILTIN_SERVERS:
            for ext in server.extensions:
                _EXTENSION_MAP.setdefault(ext, []).append(server)
    return _EXTENSION_MAP.get(extension, [])


def get_server_by_id(server_id: str) -> LSPServerDefinition | None:
    """Return a server definition by its ID."""
    for server in BUILTIN_SERVERS:
        if server.server_id == server_id:
            return server
    return None
