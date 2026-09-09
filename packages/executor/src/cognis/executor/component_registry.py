"""Static registry for optional executor components.

This is deliberately an allowlist, not a plugin mechanism. Component modules
are imported only when their declared Python dependencies are available.
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    """Import and dependency metadata for one built-in component."""

    packages: tuple[str, ...]
    modules: tuple[str, ...]


COMPONENTS: dict[str, ComponentSpec] = {
    "browser": ComponentSpec(
        packages=("patchright", "playwright", "playwright_stealth"),
        modules=(
            "cognis.tools.executor.browser.definitions",
            "cognis.tools.executor.browser.handlers",
        ),
    ),
    "mcp": ComponentSpec(packages=("mcp",), modules=("cognis.mcp_runtime",)),
    "web": ComponentSpec(
        packages=(
            "ddgs",
            "trafilatura",
            "aiolimiter",
            "extruct",
            "w3lib",
            "bs4",
            "readability",
            "youtube_transcript_api",
            "markdown",
            "markdownify",
        ),
        modules=("cognis.tools.executor.web.definitions", "cognis.tools.executor.web.handlers"),
    ),
    "documents": ComponentSpec(
        packages=("pypdf", "PIL", "weasyprint", "markdown", "jinja2", "markdownify"),
        modules=("cognis.tools.executor.document",),
    ),
    "inference": ComponentSpec(
        packages=("litellm", "tiktoken"),
        modules=("cognis.executor.inference",),
    ),
    "channels": ComponentSpec(
        packages=("bs4", "google.auth", "markdown"),
        modules=("cognis.executor.channel_handler",),
    ),
    "lsp": ComponentSpec(
        packages=(), modules=("cognis.tools.executor.lsp", "cognis.tools.executor.lsp.tool")
    ),
    "officecli": ComponentSpec(
        packages=(),
        modules=(
            "cognis.tools.executor.officecli",
            "cognis.tools.executor.officecli.definitions",
            "cognis.tools.executor.officecli.manifest",
            "cognis.tools.executor.officecli.handlers",
        ),
    ),
}


def component_available(name: str) -> bool:
    """Return whether all Python packages required by a known component exist."""
    spec = COMPONENTS[name]
    try:
        return all(importlib.util.find_spec(package) is not None for package in spec.packages)
    except ModuleNotFoundError:
        return False


def load_component(name: str) -> tuple[ModuleType, ...]:
    """Import one allowlisted component, or return no modules when unavailable."""
    if not component_available(name):
        return ()
    try:
        return tuple(importlib.import_module(module) for module in COMPONENTS[name].modules)
    except (ImportError, ModuleNotFoundError):
        return ()
