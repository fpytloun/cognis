from __future__ import annotations

import hashlib
import io
import stat
import tarfile
from pathlib import Path

import pytest

from cognis.channels.adapters import signal_cli_install
from cognis.channels.adapters.signal_cli_install import (
    SIGNAL_CLI_CERTIFIED_VERSION,
    SignalCliRuntimeConfig,
    _cache_command_path,
    _download_to_cache,
    ensure_signal_cli,
    resolve_signal_cli_runtime_config,
)


def test_signal_cli_cache_path_uses_cognis_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("COGNIS_SIGNAL_CLI_COMMAND", raising=False)
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))

    config = resolve_signal_cli_runtime_config({})
    command_path = _cache_command_path(config)

    assert command_path == (
        tmp_path
        / "data"
        / "cache"
        / "signal-cli"
        / SIGNAL_CLI_CERTIFIED_VERSION
        / f"signal-cli-{SIGNAL_CLI_CERTIFIED_VERSION}"
        / "bin"
        / "signal-cli"
    )


def test_signal_cli_runtime_config_supports_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = resolve_signal_cli_runtime_config(
        {
            "signal": {
                "auto_install": False,
                "version": SIGNAL_CLI_CERTIFIED_VERSION,
                "command": "/usr/local/bin/signal-cli",
                "cache_dir": str(tmp_path / "cache"),
            }
        }
    )

    assert config.auto_install is False
    assert config.version == SIGNAL_CLI_CERTIFIED_VERSION
    assert config.command == "/usr/local/bin/signal-cli"
    assert config.cache_dir == tmp_path / "cache"

    monkeypatch.setenv("COGNIS_SIGNAL_CLI_COMMAND", "/env/bin/signal-cli")
    monkeypatch.setenv("COGNIS_SIGNAL_CLI_AUTO_INSTALL", "0")
    monkeypatch.setenv("COGNIS_SIGNAL_CLI_VERSION", "9.9.9")
    monkeypatch.setenv("COGNIS_SIGNAL_CLI_CACHE_DIR", str(tmp_path / "env-cache"))

    env_config = resolve_signal_cli_runtime_config({})

    assert env_config.auto_install is False
    assert env_config.version == "9.9.9"
    assert env_config.command == "/env/bin/signal-cli"
    assert env_config.cache_dir == tmp_path / "env-cache"


@pytest.mark.asyncio
async def test_signal_cli_rejects_uncertified_managed_version() -> None:
    status = await ensure_signal_cli(SignalCliRuntimeConfig(version="9.9.9"))

    assert status.available is False
    assert status.version == "9.9.9"
    assert "not certified" in (status.error or "")


@pytest.mark.asyncio
async def test_signal_cli_explicit_command_bypasses_managed_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = tmp_path / "signal-cli"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(command.stat().st_mode | stat.S_IXUSR)

    async def fake_probe_version(path: Path) -> str | None:
        return "0.14.1" if path == command else None

    async def fail_download(*_: object) -> Path:
        raise AssertionError("explicit command must not install managed signal-cli")

    monkeypatch.setattr(signal_cli_install, "_probe_version", fake_probe_version)
    monkeypatch.setattr(signal_cli_install, "_download_to_cache", fail_download)

    status = await ensure_signal_cli(SignalCliRuntimeConfig(command=str(command)))

    assert status.available is True
    assert status.command == str(command)
    assert status.version == "0.14.1"
    assert status.installed_from == "override"
    assert "certified managed version" in (status.warning or "")


@pytest.mark.asyncio
async def test_signal_cli_uses_verified_cached_managed_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = SignalCliRuntimeConfig(cache_dir=tmp_path)
    command = _cache_command_path(config)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(command.stat().st_mode | stat.S_IXUSR)

    async def fake_probe_version(path: Path) -> str | None:
        return SIGNAL_CLI_CERTIFIED_VERSION if path == command else None

    async def fail_download(*_: object) -> Path:
        raise AssertionError("valid cached signal-cli must not be downloaded")

    monkeypatch.setattr(signal_cli_install, "_probe_version", fake_probe_version)
    monkeypatch.setattr(signal_cli_install, "_download_to_cache", fail_download)

    status = await ensure_signal_cli(config)

    assert status.available is True
    assert status.command == str(command)
    assert status.version == SIGNAL_CLI_CERTIFIED_VERSION
    assert status.installed_from == "cache"


@pytest.mark.asyncio
async def test_signal_cli_download_to_cache_verifies_sha_and_extracts_distribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = _signal_cli_distribution_archive()
    expected_sha = hashlib.sha256(archive).hexdigest()

    async def fake_download(_: str) -> bytes:
        return archive

    monkeypatch.setattr(signal_cli_install, "_download_archive_bytes", fake_download)

    command = await _download_to_cache(
        "https://example.test/signal-cli.tar.gz", expected_sha, tmp_path
    )

    assert command == tmp_path / "bin" / "signal-cli"
    assert command.is_file()
    assert command.stat().st_mode & stat.S_IXUSR


@pytest.mark.asyncio
async def test_signal_cli_download_to_cache_rejects_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_download(_: str) -> bytes:
        return _signal_cli_distribution_archive()

    monkeypatch.setattr(signal_cli_install, "_download_archive_bytes", fake_download)

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        await _download_to_cache("https://example.test/signal-cli.tar.gz", "0" * 64, tmp_path)


def _signal_cli_distribution_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        data = b"#!/bin/sh\necho signal-cli 0.14.5\n"
        info = tarfile.TarInfo(f"signal-cli-{SIGNAL_CLI_CERTIFIED_VERSION}/bin/signal-cli")
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
