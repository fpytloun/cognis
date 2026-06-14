"""Executor-managed signal-cli resolver and certified distribution installer."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

SIGNAL_CLI_CERTIFIED_VERSION = "0.14.5"
SIGNAL_CLI_RELEASE_URL = (
    "https://github.com/AsamK/signal-cli/releases/download/"
    f"v{SIGNAL_CLI_CERTIFIED_VERSION}/signal-cli-{SIGNAL_CLI_CERTIFIED_VERSION}.tar.gz"
)
SIGNAL_CLI_RELEASE_SHA256 = "62d38ebfef3988d78f437e7328183b75ee549d111382e66c1af70d3ebd3cd7a7"
SIGNAL_CLI_RUNTIME_METADATA_KEY = "signal_cli"

_INSTALL_TIMEOUT = 120.0
_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
_INSTALL_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class SignalCliRuntimeConfig:
    """Executor-level signal-cli runtime settings.

    ``command`` is an explicit advanced override. When it is unset, Cognis
    materializes and uses the certified managed signal-cli distribution.
    """

    auto_install: bool = True
    version: str | None = None
    command: str | None = None
    cache_dir: Path | None = None
    command_source: str | None = None


@dataclass(frozen=True)
class SignalCliStatus:
    available: bool
    auto_install: bool
    version: str | None
    command: str | None
    error: str | None = None
    warning: str | None = None
    installed_from: str | None = None
    install_dir: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "auto_install": self.auto_install,
            "version": self.version,
            "command": self.command,
            "error": self.error,
            "warning": self.warning,
            "installed_from": self.installed_from,
            "install_dir": self.install_dir,
        }


def resolve_signal_cli_runtime_config(config: dict[str, Any] | None) -> SignalCliRuntimeConfig:
    raw = (config or {}).get("signal", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    command_raw = raw.get("command")
    command_source = "configured_command" if command_raw else None
    if not command_raw:
        command_raw = os.environ.get("COGNIS_SIGNAL_CLI_COMMAND")
        command_source = "env_command" if command_raw else None

    command = str(command_raw).strip() if command_raw else None
    cache_raw = raw.get("cache_dir") or os.environ.get("COGNIS_SIGNAL_CLI_CACHE_DIR")
    version = str(raw.get("version") or os.environ.get("COGNIS_SIGNAL_CLI_VERSION") or "").strip()

    return SignalCliRuntimeConfig(
        auto_install=_bool(
            raw.get("auto_install", os.environ.get("COGNIS_SIGNAL_CLI_AUTO_INSTALL", "true"))
        ),
        version=version or None,
        command=command or None,
        cache_dir=Path(str(cache_raw)).expanduser() if cache_raw else None,
        command_source=command_source,
    )


async def ensure_signal_cli(config: SignalCliRuntimeConfig | None = None) -> SignalCliStatus:
    config = config or SignalCliRuntimeConfig()
    expected_version = (config.version or SIGNAL_CLI_CERTIFIED_VERSION).strip()

    if config.command:
        resolved = await asyncio.to_thread(shutil.which, config.command)
        source = config.command_source or "override"
        if not resolved:
            return SignalCliStatus(
                False,
                config.auto_install,
                None,
                None,
                error=f"configured signal-cli command not found: {config.command}",
                installed_from=source,
            )
        version = await _probe_version(Path(resolved))
        warning = None
        if version and version != SIGNAL_CLI_CERTIFIED_VERSION:
            warning = (
                f"explicit signal-cli override reports {version}; "
                f"certified managed version is {SIGNAL_CLI_CERTIFIED_VERSION}"
            )
        return SignalCliStatus(
            True,
            config.auto_install,
            version,
            resolved,
            warning=warning,
            installed_from=source,
        )

    if expected_version != SIGNAL_CLI_CERTIFIED_VERSION:
        return SignalCliStatus(
            False,
            config.auto_install,
            expected_version,
            None,
            error=(
                f"signal-cli version {expected_version!r} is not certified; "
                f"expected {SIGNAL_CLI_CERTIFIED_VERSION}"
            ),
        )

    command_path = _cache_command_path(config)
    cached_version = await _probe_version(command_path) if command_path.is_file() else None
    if cached_version == SIGNAL_CLI_CERTIFIED_VERSION:
        return SignalCliStatus(
            True,
            config.auto_install,
            cached_version,
            str(command_path),
            installed_from="cache",
            install_dir=str(_cache_install_dir(config)),
        )

    if not config.auto_install:
        return SignalCliStatus(
            False,
            False,
            cached_version,
            None,
            error="certified signal-cli unavailable and auto-install disabled",
            install_dir=str(_cache_install_dir(config)),
        )

    async with _INSTALL_LOCK:
        cached_version = await _probe_version(command_path) if command_path.is_file() else None
        if cached_version == SIGNAL_CLI_CERTIFIED_VERSION:
            return SignalCliStatus(
                True,
                config.auto_install,
                cached_version,
                str(command_path),
                installed_from="cache",
                install_dir=str(_cache_install_dir(config)),
            )
        try:
            installed = await _download_to_cache(
                SIGNAL_CLI_RELEASE_URL,
                SIGNAL_CLI_RELEASE_SHA256,
                _cache_install_dir(config),
            )
        except Exception as exc:
            return SignalCliStatus(
                False,
                True,
                cached_version,
                None,
                error=f"install failed: {type(exc).__name__}: {str(exc)[:200]}",
                install_dir=str(_cache_install_dir(config)),
            )

    version = await _probe_version(installed)
    if version != SIGNAL_CLI_CERTIFIED_VERSION:
        return SignalCliStatus(
            False,
            True,
            version,
            None,
            error=f"installed signal-cli reported {version!r}, expected {SIGNAL_CLI_CERTIFIED_VERSION}",
            install_dir=str(_cache_install_dir(config)),
        )
    return SignalCliStatus(
        True,
        True,
        version,
        str(installed),
        installed_from="download",
        install_dir=str(_cache_install_dir(config)),
    )


def _cache_install_dir(config: SignalCliRuntimeConfig) -> Path:
    version = config.version or SIGNAL_CLI_CERTIFIED_VERSION
    base = (
        config.cache_dir
        or Path(
            os.environ.get(
                "COGNIS_DATA_DIR",
                os.path.join(os.path.expanduser("~"), ".cognis"),
            )
        )
        / "cache"
        / "signal-cli"
    )
    return base / version / f"signal-cli-{version}"


def _cache_command_path(config: SignalCliRuntimeConfig) -> Path:
    return _cache_install_dir(config) / "bin" / "signal-cli"


async def _download_to_cache(url: str, expected_sha256: str, install_dir: Path) -> Path:
    archive = await _download_archive_bytes(url)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError(f"sha256 mismatch for {url}")

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=install_dir.parent) as tmp:
        tmp_path = Path(tmp)
        extracted_root = await asyncio.to_thread(_extract_distribution, archive, tmp_path)
        command = extracted_root / "bin" / "signal-cli"
        if not command.is_file():
            raise RuntimeError("signal-cli distribution does not contain bin/signal-cli")
        mode = command.stat().st_mode
        command.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with contextlib.suppress(FileNotFoundError):
            if install_dir.is_symlink() or install_dir.is_file():
                install_dir.unlink()
            elif install_dir.is_dir():
                shutil.rmtree(install_dir)
        await asyncio.to_thread(os.replace, extracted_root, install_dir)
    return install_dir / "bin" / "signal-cli"


async def _download_archive_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=_INSTALL_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _extract_distribution(archive: bytes, dest: Path) -> Path:
    dest_resolved = dest.resolve()
    with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as tf:
        members = tf.getmembers()
        roots = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
        if len(roots) != 1:
            raise RuntimeError(
                "signal-cli distribution archive must contain one top-level directory"
            )
        root = next(iter(roots))
        for member in members:
            target = (dest / member.name).resolve()
            if not target.is_relative_to(dest_resolved):
                raise RuntimeError("unsafe path in signal-cli distribution archive")
            if member.issym() or member.islnk():
                raise RuntimeError("signal-cli distribution archive contains unsupported links")
        tf.extractall(dest)
    return dest / root


async def _probe_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            "--version",
            env=_signal_cli_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:
        return None
    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    match = _VERSION_RE.search(text)
    return match.group(0) if match else None


def _signal_cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
