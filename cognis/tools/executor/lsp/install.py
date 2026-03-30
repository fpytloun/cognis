"""Auto-install strategies for LSP language servers.

Supports three installation methods:
- npm packages (via npm/npx, with ``--ignore-scripts`` for security)
- GitHub release binary downloads (with SHA-256 digest verification)
- Language toolchain commands (e.g. ``go install``, ``gem install``)

All installations target a cache directory (default
``~/.cache/cognis/lsp/``) and are versioned per-server so upgrades
don't require a full cache wipe.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Protocol

import httpx

from cognis.logging import get_logger

logger = get_logger(__name__)

# Default cache directory follows XDG and Cognis data dir conventions.
_DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "COGNIS_LSP_CACHE_DIR",
        os.path.join(
            os.environ.get("COGNIS_DATA_DIR", os.path.expanduser("~/.cognis")),
            "cache",
            "lsp",
        ),
    )
)


def get_cache_dir() -> Path:
    """Return the LSP cache directory, creating it if needed."""
    cache_dir = _DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class InstallStrategy(Protocol):
    """Protocol for LSP server installation strategies."""

    async def install(self, server_id: str, cache_dir: Path) -> Path | None:
        """Install the server and return the binary path, or None on failure."""
        ...

    async def detect(self, server_id: str, cache_dir: Path) -> Path | None:
        """Check if already installed in cache, return path or None."""
        ...


@dataclass(frozen=True)
class NpmInstall:
    """Install an npm package into the cache directory.

    Uses ``npm install --ignore-scripts`` for security (no postinstall
    script execution).  The server is then run via ``node <entry_point>``.
    """

    package: str
    """npm package name, e.g. ``pyright``."""

    version: str
    """Pinned version, e.g. ``1.1.390``."""

    entry_point: str
    """Relative path to the JS entry point within node_modules."""

    async def detect(self, server_id: str, cache_dir: Path) -> Path | None:
        """Check if the npm package is already installed."""
        server_dir = cache_dir / server_id / self.version
        entry = server_dir / self.entry_point
        if entry.is_file():
            return entry
        return None

    async def install(self, server_id: str, cache_dir: Path) -> Path | None:
        """Install the npm package into the server-versioned cache dir."""
        server_dir = cache_dir / server_id / self.version
        server_dir.mkdir(parents=True, exist_ok=True)

        # Find npm or npx
        npm = await asyncio.to_thread(shutil.which, "npm")
        if npm is None:
            npm = await asyncio.to_thread(shutil.which, "bun")
            if npm is None:
                logger.warning(
                    "lsp: npm/bun not found, cannot install",
                    extra={"extra_data": {"server_id": server_id, "package": self.package}},
                )
                return None

        start = perf_counter()
        npm_name = Path(npm).name
        logger.info(
            "lsp: installing npm package",
            extra={
                "extra_data": {
                    "server_id": server_id,
                    "package": self.package,
                    "version": self.version,
                    "runner": npm_name,
                }
            },
        )

        try:
            if npm_name == "bun":
                cmd = [npm, "add", "--ignore-scripts", f"{self.package}@{self.version}"]
            else:
                cmd = [
                    npm,
                    "install",
                    "--prefix",
                    str(server_dir),
                    "--ignore-scripts",
                    f"{self.package}@{self.version}",
                ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(server_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120.0)
            if proc.returncode != 0:
                logger.warning(
                    "lsp: npm install failed",
                    extra={
                        "extra_data": {
                            "server_id": server_id,
                            "exit_code": proc.returncode,
                            "duration_ms": int((perf_counter() - start) * 1000),
                        }
                    },
                )
                return None
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "lsp: npm install error",
                extra={
                    "extra_data": {
                        "server_id": server_id,
                        "error": type(exc).__name__,
                        "duration_ms": int((perf_counter() - start) * 1000),
                    }
                },
            )
            return None

        entry = server_dir / self.entry_point
        if not entry.is_file():
            logger.warning(
                "lsp: npm install succeeded but entry point not found",
                extra={
                    "extra_data": {
                        "server_id": server_id,
                        "entry_point": self.entry_point,
                    }
                },
            )
            return None

        duration_ms = int((perf_counter() - start) * 1000)
        logger.info(
            "lsp: npm install succeeded",
            extra={
                "extra_data": {
                    "server_id": server_id,
                    "duration_ms": duration_ms,
                    "path": str(entry),
                }
            },
        )
        return entry


@dataclass(frozen=True)
class ToolchainInstall:
    """Install via a language toolchain command.

    For example: ``go install golang.org/x/tools/gopls@latest``
    with ``GOBIN`` set to the cache directory.
    """

    command: list[str]
    """Install command, e.g. ``["go", "install", "golang.org/x/tools/gopls@v0.16.2"]``."""

    binary_name: str
    """Expected binary name after install, e.g. ``gopls``."""

    env_overrides: dict[str, str]
    """Environment variable overrides.  ``{cache_dir}`` is substituted."""

    async def detect(self, server_id: str, cache_dir: Path) -> Path | None:
        """Check if the binary exists in the cache."""
        server_dir = cache_dir / server_id
        binary = server_dir / self.binary_name
        if binary.is_file():
            return binary
        return None

    async def install(self, server_id: str, cache_dir: Path) -> Path | None:
        """Run the toolchain install command."""
        server_dir = cache_dir / server_id
        server_dir.mkdir(parents=True, exist_ok=True)

        # Check that the base command exists
        base_cmd = self.command[0]
        base_path = await asyncio.to_thread(shutil.which, base_cmd)
        if base_path is None:
            logger.warning(
                "lsp: toolchain command not found",
                extra={"extra_data": {"server_id": server_id, "command": base_cmd}},
            )
            return None

        start = perf_counter()
        env = {**os.environ}
        for key, value in self.env_overrides.items():
            env[key] = value.replace("{cache_dir}", str(server_dir))

        logger.info(
            "lsp: running toolchain install",
            extra={
                "extra_data": {
                    "server_id": server_id,
                    "command": base_cmd,
                }
            },
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=300.0)
            if proc.returncode != 0:
                logger.warning(
                    "lsp: toolchain install failed",
                    extra={
                        "extra_data": {
                            "server_id": server_id,
                            "exit_code": proc.returncode,
                            "duration_ms": int((perf_counter() - start) * 1000),
                        }
                    },
                )
                return None
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "lsp: toolchain install error",
                extra={
                    "extra_data": {
                        "server_id": server_id,
                        "error": type(exc).__name__,
                    }
                },
            )
            return None

        binary = server_dir / self.binary_name
        if not binary.is_file():
            logger.warning(
                "lsp: toolchain install succeeded but binary not found",
                extra={"extra_data": {"server_id": server_id, "expected": str(binary)}},
            )
            return None

        duration_ms = int((perf_counter() - start) * 1000)
        logger.info(
            "lsp: toolchain install succeeded",
            extra={
                "extra_data": {
                    "server_id": server_id,
                    "duration_ms": duration_ms,
                    "path": str(binary),
                }
            },
        )
        return binary


@dataclass(frozen=True)
class GitHubBinaryInstall:
    """Download a platform-specific binary from GitHub releases.

    Verifies the downloaded file against a SHA-256 digest before use.
    """

    repo: str
    """GitHub repo, e.g. ``clangd/clangd``."""

    tag: str
    """Release tag, e.g. ``18.1.3``."""

    asset_patterns: dict[str, str]
    """Platform-specific asset filenames.

    Keys are ``{system}-{machine}`` (e.g. ``Linux-x86_64``), values are
    the asset filename in the release.
    """

    binary_name: str
    """Name of the binary after extraction."""

    sha256_digests: dict[str, str]
    """SHA-256 hex digests keyed by asset filename."""

    strip_prefix: str = ""
    """Directory prefix to strip when extracting archives."""

    async def detect(self, server_id: str, cache_dir: Path) -> Path | None:
        """Check if the binary already exists in cache."""
        server_dir = cache_dir / server_id / self.tag
        binary = server_dir / self.binary_name
        if binary.is_file():
            return binary
        return None

    async def install(self, server_id: str, cache_dir: Path) -> Path | None:
        """Download and verify the binary from GitHub releases."""
        system = platform.system()
        machine = platform.machine()
        platform_key = f"{system}-{machine}"

        asset_name = self.asset_patterns.get(platform_key)
        if asset_name is None:
            logger.warning(
                "lsp: no binary available for platform",
                extra={
                    "extra_data": {
                        "server_id": server_id,
                        "platform": platform_key,
                    }
                },
            )
            return None

        expected_digest = self.sha256_digests.get(asset_name)
        if expected_digest is None:
            logger.warning(
                "lsp: no SHA-256 digest for asset, refusing to install",
                extra={
                    "extra_data": {
                        "server_id": server_id,
                        "asset": asset_name,
                    }
                },
            )
            return None

        server_dir = cache_dir / server_id / self.tag
        server_dir.mkdir(parents=True, exist_ok=True)

        url = f"https://github.com/{self.repo}/releases/download/{self.tag}/{asset_name}"
        start = perf_counter()

        logger.info(
            "lsp: downloading binary from GitHub",
            extra={
                "extra_data": {
                    "server_id": server_id,
                    "repo": self.repo,
                    "asset": asset_name,
                    "platform": platform_key,
                }
            },
        )

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.content
        except httpx.HTTPError as exc:
            logger.warning(
                "lsp: GitHub download failed",
                extra={
                    "extra_data": {
                        "server_id": server_id,
                        "error": type(exc).__name__,
                        "duration_ms": int((perf_counter() - start) * 1000),
                    }
                },
            )
            return None

        # Verify SHA-256
        actual_digest = hashlib.sha256(data).hexdigest()
        if actual_digest != expected_digest:
            logger.error(
                "lsp: SHA-256 digest mismatch — refusing to install",
                extra={
                    "extra_data": {
                        "server_id": server_id,
                        "asset": asset_name,
                        "expected": expected_digest[:16] + "...",
                        "actual": actual_digest[:16] + "...",
                    }
                },
            )
            return None

        # Extract
        binary_path = await asyncio.to_thread(self._extract, data, asset_name, server_dir)

        if binary_path is None or not binary_path.is_file():
            logger.warning(
                "lsp: extraction failed or binary not found",
                extra={"extra_data": {"server_id": server_id}},
            )
            return None

        # Make executable
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC)

        duration_ms = int((perf_counter() - start) * 1000)
        logger.info(
            "lsp: GitHub download succeeded",
            extra={
                "extra_data": {
                    "server_id": server_id,
                    "duration_ms": duration_ms,
                    "path": str(binary_path),
                }
            },
        )
        return binary_path

    def _extract(self, data: bytes, asset_name: str, target_dir: Path) -> Path | None:
        """Extract an archive and return the binary path."""
        if asset_name.endswith(".zip"):
            return self._extract_zip(data, target_dir)
        if asset_name.endswith((".tar.gz", ".tgz")):
            return self._extract_tar(data, target_dir)
        # Plain binary
        binary = target_dir / self.binary_name
        binary.write_bytes(data)
        return binary

    def _extract_zip(self, data: bytes, target_dir: Path) -> Path | None:
        """Extract binary from a zip archive."""
        with zipfile.ZipFile(BytesIO(data)) as zf:
            for name in zf.namelist():
                basename = Path(name).name
                if basename == self.binary_name:
                    # Extract just this file
                    member_data = zf.read(name)
                    target = target_dir / self.binary_name
                    target.write_bytes(member_data)
                    return target
        return None

    def _extract_tar(self, data: bytes, target_dir: Path) -> Path | None:
        """Extract binary from a tar.gz archive."""
        with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile() and Path(member.name).name == self.binary_name:
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    target = target_dir / self.binary_name
                    target.write_bytes(extracted.read())
                    return target
        return None


async def resolve_command(
    command: str,
    server_id: str,
    install_strategy: InstallStrategy | None,
    *,
    auto_install: bool = True,
    cache_dir: Path | None = None,
) -> str | None:
    """Resolve a server command: check PATH, then cache, then auto-install.

    Returns the resolved command path, or None if unavailable.
    """
    effective_cache = cache_dir or get_cache_dir()

    # 1. System PATH
    system_path = await asyncio.to_thread(shutil.which, command)
    if system_path is not None:
        logger.debug(
            "lsp: server found on PATH",
            extra={"extra_data": {"server_id": server_id, "path": system_path}},
        )
        return system_path

    if install_strategy is None:
        logger.debug(
            "lsp: server not found and no install strategy",
            extra={"extra_data": {"server_id": server_id, "command": command}},
        )
        return None

    # 2. Check cache
    cached = await install_strategy.detect(server_id, effective_cache)
    if cached is not None:
        logger.debug(
            "lsp: server found in cache",
            extra={"extra_data": {"server_id": server_id, "path": str(cached)}},
        )
        return str(cached)

    # 3. Auto-install
    if not auto_install:
        logger.debug(
            "lsp: auto-install disabled",
            extra={"extra_data": {"server_id": server_id}},
        )
        return None

    installed = await install_strategy.install(server_id, effective_cache)
    if installed is not None:
        return str(installed)

    return None
