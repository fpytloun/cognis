"""Executor-local OfficeCLI resolver and pinned binary installer."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cognis.tools.executor.officecli.manifest import (
    OFFICECLI_CERTIFIED_VERSION,
    certified_asset_for_platform,
    certified_capabilities_for_version,
    normalize_platform,
)

OFFICECLI_RUNTIME_METADATA_KEY = "officecli"
_VERSION_RE = re.compile(r"v?\d+\.\d+\.\d+")
_INSTALL_TIMEOUT = 90.0


@dataclass(frozen=True)
class OfficeCliRuntimeConfig:
    enabled: bool = True
    auto_install: bool = True
    version: str | None = None
    binary_path: Path | None = None
    cache_dir: Path | None = None


@dataclass(frozen=True)
class OfficeCliStatus:
    available: bool
    enabled: bool
    auto_install: bool
    version: str | None
    platform_key: str | None
    command: str | None
    capabilities: dict[str, Any] | None = None
    error: str | None = None
    warning: str | None = None
    installed_from: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "auto_install": self.auto_install,
            "version": self.version,
            "platform": self.platform_key,
            "command": self.command,
            "capabilities": self.capabilities or {},
            "error": self.error,
            "warning": self.warning,
            "installed_from": self.installed_from,
        }


def resolve_officecli_runtime_config(config: dict[str, Any] | None) -> OfficeCliRuntimeConfig:
    raw = (config or {}).get("officecli", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    enabled = _bool(raw.get("enabled", os.environ.get("COGNIS_OFFICECLI_ENABLED", "true")))
    auto_install = _bool(
        raw.get("auto_install", os.environ.get("COGNIS_OFFICECLI_AUTO_INSTALL", "true"))
    )
    version = str(raw.get("version") or os.environ.get("COGNIS_OFFICECLI_VERSION") or "").strip()
    binary_raw = raw.get("binary_path") or os.environ.get("COGNIS_OFFICECLI_BINARY_PATH")
    cache_raw = raw.get("cache_dir") or os.environ.get("COGNIS_OFFICECLI_CACHE_DIR")
    return OfficeCliRuntimeConfig(
        enabled=enabled,
        auto_install=auto_install,
        version=version or None,
        binary_path=Path(str(binary_raw)).expanduser() if binary_raw else None,
        cache_dir=Path(str(cache_raw)).expanduser() if cache_raw else None,
    )


async def ensure_officecli(config: OfficeCliRuntimeConfig | None = None) -> OfficeCliStatus:
    config = config or OfficeCliRuntimeConfig()
    platform_key = normalize_platform()
    if not config.enabled:
        return OfficeCliStatus(
            False, False, config.auto_install, None, platform_key, None, error="disabled"
        )
    expected_version = (config.version or OFFICECLI_CERTIFIED_VERSION).strip()
    if expected_version != OFFICECLI_CERTIFIED_VERSION:
        return OfficeCliStatus(
            False,
            True,
            config.auto_install,
            expected_version,
            platform_key,
            None,
            error=(
                f"OfficeCLI version {expected_version!r} is not certified; "
                f"expected {OFFICECLI_CERTIFIED_VERSION}"
            ),
        )
    asset = certified_asset_for_platform(platform_key)
    if asset is None:
        return OfficeCliStatus(
            False,
            True,
            config.auto_install,
            None,
            platform_key,
            None,
            error="unsupported platform",
        )

    if config.binary_path is not None:
        candidate = config.binary_path
        version = await _probe_version(candidate) if candidate.is_file() else None
        if version == OFFICECLI_CERTIFIED_VERSION and await _validated_binary(
            candidate, asset.sha256
        ):
            return OfficeCliStatus(
                True,
                True,
                config.auto_install,
                version,
                asset.platform_key,
                str(candidate),
                certified_capabilities_for_version(version),
                installed_from="configured_path",
            )
        return OfficeCliStatus(
            False,
            True,
            config.auto_install,
            version,
            asset.platform_key,
            None,
            error=(
                "configured OfficeCLI binary is unavailable, has wrong sha256, "
                f"or reports {version!r}; expected {OFFICECLI_CERTIFIED_VERSION}"
            ),
        )

    cache_path = _cache_binary_path(config, asset.platform_key)
    cached = await _validated_binary(cache_path, asset.sha256)
    if cached:
        version = await _probe_version(cached)
        if version == OFFICECLI_CERTIFIED_VERSION:
            return OfficeCliStatus(
                True,
                True,
                config.auto_install,
                version,
                asset.platform_key,
                str(cached),
                certified_capabilities_for_version(version),
                installed_from="cache",
            )

    path_candidate = await asyncio.to_thread(shutil.which, "officecli")
    if path_candidate:
        candidate = Path(path_candidate)
        version = await _probe_version(candidate)
        if version == OFFICECLI_CERTIFIED_VERSION and await _validated_binary(
            candidate, asset.sha256
        ):
            return OfficeCliStatus(
                True,
                True,
                config.auto_install,
                version,
                asset.platform_key,
                str(candidate),
                certified_capabilities_for_version(version),
                installed_from="path",
            )

    if not config.auto_install:
        return OfficeCliStatus(
            False,
            True,
            False,
            None,
            asset.platform_key,
            None,
            error="certified OfficeCLI unavailable and auto-install disabled",
        )

    try:
        installed = await _download_to_cache(asset.url, asset.sha256, cache_path)
    except Exception as exc:
        return OfficeCliStatus(
            False,
            True,
            True,
            None,
            asset.platform_key,
            None,
            error=f"install failed: {type(exc).__name__}: {str(exc)[:200]}",
        )
    version = await _probe_version(installed)
    if version != OFFICECLI_CERTIFIED_VERSION:
        return OfficeCliStatus(
            False,
            True,
            True,
            version,
            asset.platform_key,
            None,
            error=f"installed OfficeCLI reported {version!r}, expected {OFFICECLI_CERTIFIED_VERSION}",
        )
    return OfficeCliStatus(
        True,
        True,
        True,
        version,
        asset.platform_key,
        str(installed),
        certified_capabilities_for_version(version),
        installed_from="download",
    )


def _cache_binary_path(config: OfficeCliRuntimeConfig, platform_key: str) -> Path:
    version = config.version or OFFICECLI_CERTIFIED_VERSION
    base = (
        config.cache_dir
        or Path(
            os.environ.get(
                "COGNIS_DATA_DIR",
                os.path.join(os.path.expanduser("~"), ".cognis"),
            )
        )
        / "cache"
        / "officecli"
    )
    return base / version / platform_key / "officecli"


async def _validated_binary(path: Path, expected_sha256: str) -> Path | None:
    if not path.is_file():
        return None
    digest = await asyncio.to_thread(_sha256_file, path)
    return path if digest == expected_sha256 else None


async def _probe_version(path: Path) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            "--version",
            env=_officecli_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:
        return None
    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    match = _VERSION_RE.search(text)
    if not match:
        return None
    version = match.group(0)
    return version if version.startswith("v") else f"v{version}"


async def _download_to_cache(url: str, expected_sha256: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        async with httpx.AsyncClient(timeout=_INSTALL_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            await asyncio.to_thread(tmp_path.write_bytes, response.content)
        digest = await asyncio.to_thread(_sha256_file, tmp_path)
        if digest != expected_sha256:
            raise RuntimeError(f"sha256 mismatch for {url}")
        mode = tmp_path.stat().st_mode
        tmp_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        await asyncio.to_thread(os.replace, tmp_path, dest)
        return dest
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _officecli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["OFFICECLI_SKIP_UPDATE"] = "1"
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
