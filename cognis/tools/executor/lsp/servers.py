"""LSP language server definitions.

Each definition describes how to detect, install, and run a language
server.  The initial set covers the most common languages; additional
servers can be added as data-driven entries.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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

    workspace_configuration: dict[str, Any] | None = None
    """Server-specific default workspace/configuration response."""

    initialization_settings: dict[str, Any] | None = None
    """Server-specific default settings sent in initializationOptions."""

    project_config_files: tuple[str, ...] = ()
    """Project config files that take precedence over default workspace config."""

    pyproject_config_sections: tuple[str, ...] = ()
    """Dotted pyproject.toml sections that take precedence over defaults."""

    npm_run: bool = False
    """If True, the resolved command is a JS file run via ``node``."""

    def language_id(self, extension: str) -> str:
        """Return the LSP language ID for a file extension."""
        return self.language_id_map.get(extension, self.server_id)

    def workspace_configuration_for(self, root_path: str) -> dict[str, Any] | None:
        """Return default workspace config unless native project config exists."""
        if self._has_native_project_config(root_path):
            return None

        return self.workspace_configuration

    def initialization_options_for(self, root_path: str) -> dict[str, Any] | None:
        """Return init options merged with defaults unless native config exists."""
        options = dict(self.init_options or {})
        if self.initialization_settings and not self._has_native_project_config(root_path):
            options["settings"] = self.initialization_settings
        return options or None

    def _has_native_project_config(self, root_path: str) -> bool:
        """Return whether native project config should override Cognis defaults."""
        if self.workspace_configuration is None and self.initialization_settings is None:
            return False

        root = Path(root_path)
        if any((root / name).exists() for name in self.project_config_files):
            return True

        return bool(self.pyproject_config_sections) and _pyproject_has_any_section(
            root / "pyproject.toml", self.pyproject_config_sections
        )


def _pyproject_has_any_section(path: Path, sections: tuple[str, ...]) -> bool:
    """Return whether pyproject.toml contains any dotted section path."""
    if not path.exists():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False

    for section in sections:
        current: Any = data
        for part in section.split("."):
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            return True
    return False


_PYRIGHT_ANALYSIS_DEFAULTS: dict[str, Any] = {
    # Keep agent edit-time diagnostics bounded and avoid indexing dependency,
    # generated, cache, and nested-worktree trees in arbitrary projects.
    "diagnosticMode": "openFilesOnly",
    "exclude": [
        "**/.git",
        "**/.hg",
        "**/.svn",
        "**/.venv",
        "**/venv",
        "**/.tox",
        "**/.nox",
        "**/.mypy_cache",
        "**/.pytest_cache",
        "**/.ruff_cache",
        "**/__pycache__",
        "**/node_modules",
        "**/.worktrees",
        "**/dist",
        "**/build",
    ],
}

_GOPLS_DEFAULTS: dict[str, Any] = {
    "directoryFilters": [
        "-.git",
        "-.hg",
        "-.svn",
        "-.worktrees",
        "-node_modules",
        "-.venv",
        "-venv",
        "-dist",
        "-build",
    ]
}

_RUST_ANALYZER_DEFAULTS: dict[str, Any] = {
    "files": {
        "excludeDirs": [
            ".git",
            ".hg",
            ".svn",
            ".worktrees",
            "node_modules",
            ".venv",
            "venv",
            "dist",
            "build",
        ]
    }
}


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
    workspace_configuration={
        "python": {"analysis": _PYRIGHT_ANALYSIS_DEFAULTS},
        "python.analysis": _PYRIGHT_ANALYSIS_DEFAULTS,
    },
    initialization_settings={"python": {"analysis": _PYRIGHT_ANALYSIS_DEFAULTS}},
    project_config_files=("pyrightconfig.json",),
    pyproject_config_sections=("tool.pyright",),
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
        extra_packages=("typescript",),
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
    workspace_configuration={"gopls": _GOPLS_DEFAULTS},
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
    workspace_configuration={"rust-analyzer": _RUST_ANALYZER_DEFAULTS},
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
# Additional servers
# ---------------------------------------------------------------------------

YAML_LS = LSPServerDefinition(
    server_id="yaml",
    extensions=frozenset({".yaml", ".yml"}),
    command="yaml-language-server",
    args=("--stdio",),
    root_markers=(),
    language_id_map={".yaml": "yaml", ".yml": "yaml"},
    install_strategy=NpmInstall(
        package="yaml-language-server",
        version="1.15.0",
        entry_point="node_modules/yaml-language-server/out/server/src/server.js",
    ),
    npm_run=True,
)

JSON_LS = LSPServerDefinition(
    server_id="json",
    extensions=frozenset({".json", ".jsonc"}),
    command="vscode-json-language-server",
    args=("--stdio",),
    root_markers=(),
    language_id_map={".json": "json", ".jsonc": "jsonc"},
    install_strategy=NpmInstall(
        package="vscode-langservers-extracted",
        version="4.10.0",
        entry_point="node_modules/vscode-langservers-extracted/bin/vscode-json-language-server",
    ),
    npm_run=True,
)

CSS_LS = LSPServerDefinition(
    server_id="css",
    extensions=frozenset({".css", ".scss", ".less"}),
    command="vscode-css-language-server",
    args=("--stdio",),
    root_markers=("package.json",),
    language_id_map={".css": "css", ".scss": "scss", ".less": "less"},
    install_strategy=NpmInstall(
        package="vscode-langservers-extracted",
        version="4.10.0",
        entry_point="node_modules/vscode-langservers-extracted/bin/vscode-css-language-server",
    ),
    npm_run=True,
)

HTML_LS = LSPServerDefinition(
    server_id="html",
    extensions=frozenset({".html", ".htm"}),
    command="vscode-html-language-server",
    args=("--stdio",),
    root_markers=("package.json",),
    language_id_map={".html": "html", ".htm": "html"},
    install_strategy=NpmInstall(
        package="vscode-langservers-extracted",
        version="4.10.0",
        entry_point="node_modules/vscode-langservers-extracted/bin/vscode-html-language-server",
    ),
    npm_run=True,
)

SVELTE = LSPServerDefinition(
    server_id="svelte",
    extensions=frozenset({".svelte"}),
    command="svelteserver",
    args=("--stdio",),
    root_markers=("svelte.config.js", "svelte.config.ts", "package.json"),
    language_id_map={".svelte": "svelte"},
    install_strategy=NpmInstall(
        package="svelte-language-server",
        version="0.17.8",
        entry_point="node_modules/svelte-language-server/bin/server.js",
    ),
    npm_run=True,
)

VUE = LSPServerDefinition(
    server_id="vue",
    extensions=frozenset({".vue"}),
    command="vue-language-server",
    args=("--stdio",),
    root_markers=("vue.config.js", "nuxt.config.ts", "package.json"),
    language_id_map={".vue": "vue"},
    install_strategy=NpmInstall(
        package="@vue/language-server",
        version="2.2.8",
        entry_point="node_modules/@vue/language-server/bin/vue-language-server.js",
    ),
    npm_run=True,
)

DOCKERFILE = LSPServerDefinition(
    server_id="dockerfile",
    extensions=frozenset({".dockerfile"}),
    command="docker-langserver",
    args=("--stdio",),
    root_markers=("Dockerfile", "docker-compose.yml", "docker-compose.yaml"),
    language_id_map={".dockerfile": "dockerfile"},
    install_strategy=NpmInstall(
        package="dockerfile-language-server-nodejs",
        version="0.13.0",
        entry_point="node_modules/dockerfile-language-server-nodejs/lib/server.js",
    ),
    npm_run=True,
)

TERRAFORM = LSPServerDefinition(
    server_id="terraform",
    extensions=frozenset({".tf", ".tfvars"}),
    command="terraform-ls",
    args=("serve",),
    root_markers=(".terraform", "main.tf", "terraform.tfvars"),
    language_id_map={".tf": "terraform", ".tfvars": "terraform-vars"},
    install_strategy=None,  # Install via system package manager
)

TOML_LS = LSPServerDefinition(
    server_id="toml",
    extensions=frozenset({".toml"}),
    command="taplo",
    args=("lsp", "stdio"),
    root_markers=(),
    language_id_map={".toml": "toml"},
    install_strategy=None,  # Install via cargo or system package manager
)

LUA_LS = LSPServerDefinition(
    server_id="lua",
    extensions=frozenset({".lua"}),
    command="lua-language-server",
    args=(),
    root_markers=(".luarc.json", ".luarc.jsonc", ".luacheckrc"),
    language_id_map={".lua": "lua"},
    install_strategy=None,  # Install via system package manager
)

NIXD = LSPServerDefinition(
    server_id="nixd",
    extensions=frozenset({".nix"}),
    command="nixd",
    args=(),
    root_markers=("flake.nix", "default.nix", "shell.nix"),
    language_id_map={".nix": "nix"},
    install_strategy=None,  # Install via nix
)

ZLS = LSPServerDefinition(
    server_id="zls",
    extensions=frozenset({".zig", ".zon"}),
    command="zls",
    args=(),
    root_markers=("build.zig", "build.zig.zon"),
    language_id_map={".zig": "zig", ".zon": "zig"},
    install_strategy=None,  # Install via system package manager
)

DART = LSPServerDefinition(
    server_id="dart",
    extensions=frozenset({".dart"}),
    command="dart",
    args=("language-server", "--protocol=lsp"),
    root_markers=("pubspec.yaml", "pubspec.lock"),
    language_id_map={".dart": "dart"},
    install_strategy=None,  # Comes with Dart/Flutter SDK
)

GLEAM = LSPServerDefinition(
    server_id="gleam",
    extensions=frozenset({".gleam"}),
    command="gleam",
    args=("lsp",),
    root_markers=("gleam.toml",),
    language_id_map={".gleam": "gleam"},
    install_strategy=None,  # Comes with gleam toolchain
)

SOURCEKIT_LSP = LSPServerDefinition(
    server_id="sourcekit-lsp",
    extensions=frozenset({".swift"}),
    command="sourcekit-lsp",
    args=(),
    root_markers=("Package.swift", ".swift-version"),
    language_id_map={".swift": "swift"},
    install_strategy=None,  # Comes with Xcode / Swift toolchain
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
    YAML_LS,
    JSON_LS,
    CSS_LS,
    HTML_LS,
    SVELTE,
    VUE,
    DOCKERFILE,
    TERRAFORM,
    TOML_LS,
    LUA_LS,
    NIXD,
    ZLS,
    DART,
    GLEAM,
    SOURCEKIT_LSP,
]

#: Mapping of file extension to server definitions (multiple servers per
#: extension is allowed, e.g. pyright + ruff for ``.py``).
_EXTENSION_MAP: dict[str, list[LSPServerDefinition]] | None = None


def get_servers_for_extension(
    extension: str,
    *,
    purpose: Literal["diagnostics", "semantic"] = "semantic",
) -> list[LSPServerDefinition]:
    """Return server definitions that handle the given file extension.

    Python edit-time diagnostics intentionally prefer Ruff only.  Pyright is
    still available for explicit semantic LSP queries where project-wide type
    analysis is useful and the caller opted into a semantic operation.
    """
    global _EXTENSION_MAP
    if _EXTENSION_MAP is None:
        _EXTENSION_MAP = {}
        for server in BUILTIN_SERVERS:
            for ext in server.extensions:
                _EXTENSION_MAP.setdefault(ext, []).append(server)
    servers = _EXTENSION_MAP.get(extension, [])
    if purpose == "diagnostics" and extension in {".py", ".pyi"}:
        return [server for server in servers if server.server_id == "ruff"]
    return servers


def get_server_by_id(server_id: str) -> LSPServerDefinition | None:
    """Return a server definition by its ID."""
    for server in BUILTIN_SERVERS:
        if server.server_id == server_id:
            return server
    return None
