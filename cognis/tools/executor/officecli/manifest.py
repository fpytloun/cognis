"""Pinned OfficeCLI compatibility manifest."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Any

OFFICECLI_CERTIFIED_VERSION = "v1.0.102"
OFFICECLI_RELEASE_BASE = (
    "https://github.com/iOfficeAI/OfficeCLI/releases/download/" + OFFICECLI_CERTIFIED_VERSION
)


@dataclass(frozen=True)
class OfficeCliAsset:
    platform_key: str
    filename: str
    sha256: str
    url: str


OFFICECLI_ASSETS: dict[str, OfficeCliAsset] = {
    "linux-x64": OfficeCliAsset(
        platform_key="linux-x64",
        filename="officecli-linux-x64",
        sha256="d58438a2d701ec68685bb04bb043b546696b1620f141e670eaf438dae5898a66",
        url=f"{OFFICECLI_RELEASE_BASE}/officecli-linux-x64",
    ),
    "linux-arm64": OfficeCliAsset(
        platform_key="linux-arm64",
        filename="officecli-linux-arm64",
        sha256="dedca5682cad211df9c75886936b441475cf7840a8bd6974dcbd2278c7f1d1a1",
        url=f"{OFFICECLI_RELEASE_BASE}/officecli-linux-arm64",
    ),
    "darwin-arm64": OfficeCliAsset(
        platform_key="darwin-arm64",
        filename="officecli-mac-arm64",
        sha256="964b23466549681f5283c922cc535d8914d8d089d453bb5d0a100cec6c5fe206",
        url=f"{OFFICECLI_RELEASE_BASE}/officecli-mac-arm64",
    ),
    "darwin-x64": OfficeCliAsset(
        platform_key="darwin-x64",
        filename="officecli-mac-x64",
        sha256="99be36950b43782cc0e02ba6be4afe5db3d6b4788f0fb32853f4d3ead5950b78",
        url=f"{OFFICECLI_RELEASE_BASE}/officecli-mac-x64",
    ),
}

OFFICECLI_CERTIFIED_CAPABILITIES: dict[str, Any] = {
    "version": OFFICECLI_CERTIFIED_VERSION,
    "formats": ["docx", "xlsx", "pptx"],
    "tools": [
        "office_read",
        "office_get",
        "office_query",
        "office_validate",
        "office_render",
        "office_create",
        "office_patch",
    ],
    "read_views": ["outline", "stats", "issues", "text", "annotated", "html", "json"],
    "render_views": ["html", "screenshot", "svg", "pdf", "forms"],
    "verbs": ["create", "view", "get", "query", "validate", "add", "set", "remove"],
}


def normalize_platform(system: str | None = None, machine: str | None = None) -> str | None:
    os_name = (system or platform.system()).lower()
    arch = (machine or platform.machine()).lower()
    if arch in {"x86_64", "amd64"}:
        arch_key = "x64"
    elif arch in {"aarch64", "arm64"}:
        arch_key = "arm64"
    else:
        return None
    if os_name == "linux":
        return f"linux-{arch_key}"
    if os_name == "darwin":
        return f"darwin-{arch_key}"
    return None


def certified_asset_for_platform(platform_key: str | None = None) -> OfficeCliAsset | None:
    return OFFICECLI_ASSETS.get(platform_key or normalize_platform() or "")


def certified_capabilities_for_version(version: str | None) -> dict[str, Any] | None:
    if version == OFFICECLI_CERTIFIED_VERSION:
        return dict(OFFICECLI_CERTIFIED_CAPABILITIES)
    return None


def certified_tool_names(version: str | None = OFFICECLI_CERTIFIED_VERSION) -> set[str]:
    capabilities = certified_capabilities_for_version(version)
    if not capabilities:
        return set()
    return set(capabilities["tools"])
