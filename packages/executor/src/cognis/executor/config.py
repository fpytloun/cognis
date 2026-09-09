"""Persistent configuration and precedence rules for the standalone executor."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from platformdirs import user_config_dir, user_data_dir, user_log_dir

CONFIG_SCHEMA_VERSION = 1
CONFIG_FILENAME = "config.json"


class ConfigError(ValueError):
    """Raised when executor configuration cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    """Native per-user locations used by the executor."""

    config_dir: Path
    data_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        """Return the persistent JSON configuration path."""
        return self.config_dir / CONFIG_FILENAME


@dataclass(frozen=True, slots=True)
class ExecutorSettings:
    """Resolved executor settings, with secrets kept in memory only."""

    controller_url: str = ""
    token: str = ""
    workspace: str = ""
    log_level: str = "info"

    def redacted(self) -> dict[str, object]:
        """Return a display-safe representation of these settings."""
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "controller_url": self.controller_url,
            "token": _redact_secret(self.token),
            "workspace": self.workspace,
            "log_level": self.log_level,
        }

    def as_file_data(self) -> dict[str, object]:
        """Return the versioned on-disk representation."""
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "controller_url": self.controller_url,
            "token": self.token,
            "workspace": self.workspace,
            "log_level": self.log_level,
        }


def get_config_paths(*, app_name: str = "cognis-executor") -> ConfigPaths:
    """Return platform-native user paths for the executor."""
    return ConfigPaths(
        config_dir=Path(user_config_dir(app_name)),
        data_dir=Path(user_data_dir(app_name)),
        log_dir=Path(user_log_dir(app_name)),
    )


def _redact_secret(value: str) -> str:
    return "[redacted]" if value else ""


def _posix_private(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)


def _validate_data(data: object, *, source: str) -> ExecutorSettings:
    if not isinstance(data, dict):
        raise ConfigError(f"{source} must contain a JSON object")
    version = data.get("schema_version")
    if version != CONFIG_SCHEMA_VERSION:
        raise ConfigError(
            f"{source} has unsupported schema_version {version!r}; expected {CONFIG_SCHEMA_VERSION}"
        )
    allowed = {"schema_version", "controller_url", "token", "workspace", "log_level"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{source} contains unsupported key(s): {', '.join(unknown)}")
    values: dict[str, str] = {}
    for key in ("controller_url", "token", "workspace", "log_level"):
        value = data.get(key, "")
        if not isinstance(value, str):
            raise ConfigError(f"{source} key {key!r} must be a string")
        values[key] = value
    return ExecutorSettings(**values)


def load_config(path: Path | None = None) -> ExecutorSettings:
    """Load the versioned config file, or defaults when it does not exist."""
    config_path = path or get_config_paths().config_file
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ExecutorSettings()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read executor configuration: {type(exc).__name__}") from exc
    return _validate_data(raw, source=str(config_path))


def save_config(settings: ExecutorSettings, path: Path | None = None) -> Path:
    """Atomically save settings with owner-only POSIX permissions."""
    config_path = path or get_config_paths().config_file
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _posix_private(config_path.parent, stat.S_IRWXU)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        _posix_private(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings.as_file_data(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, config_path)
        _posix_private(config_path, stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary_path.unlink()
        raise
    return config_path


def validate_controller_url(url: str, *, allow_insecure: bool = False) -> None:
    """Validate the same local/insecure WebSocket policy used at startup."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ConfigError("Controller URL is invalid") from exc
    if parsed.scheme not in {"ws", "wss"} or not hostname or parsed.query or parsed.fragment:
        raise ConfigError("Controller URL must be a ws:// or wss:// URL without query or fragment")
    local = hostname.lower() in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if parsed.scheme == "ws" and not local and not allow_insecure:
        raise ConfigError(
            "Remote executor connections require wss:// (TLS); ws:// is allowed only for localhost"
        )


def resolve_settings(
    cli: Mapping[str, str | None] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    file_settings: ExecutorSettings | None = None,
) -> ExecutorSettings:
    """Resolve settings using CLI, environment, file, then defaults precedence."""
    cli_values = cli or {}
    env = environ or os.environ
    file_values = file_settings or ExecutorSettings()

    def choose(name: str, *environment_names: str) -> str:
        explicit = cli_values.get(name)
        if explicit is not None and explicit != "":
            return explicit
        for environment_name in environment_names:
            value = env.get(environment_name, "")
            if value:
                return value
        return cast(str, getattr(file_values, name))

    workspace = choose("workspace", "COGNIS_EXECUTOR_WORKSPACE", "COGNIS_EXECUTOR_WORKDIR")
    return ExecutorSettings(
        controller_url=choose("controller_url", "COGNIS_CONTROLLER_URL"),
        token=choose("token", "COGNIS_EXECUTOR_TOKEN"),
        workspace=workspace or str(Path.home()),
        log_level=choose("log_level", "COGNIS_LOG_LEVEL") or "info",
    )


def normalized_settings(settings: ExecutorSettings) -> ExecutorSettings:
    """Validate and normalize user-facing values before startup or persistence."""
    workspace = Path(os.path.expandvars(os.path.expanduser(settings.workspace))).resolve(
        strict=False
    )
    if not workspace.is_dir():
        raise ConfigError(
            f"Executor working directory does not exist or is not a directory: {settings.workspace}"
        )
    if settings.controller_url:
        validate_controller_url(
            settings.controller_url,
            allow_insecure=_allow_insecure_ws(),
        )
    return replace(settings, workspace=str(workspace), log_level=settings.log_level.lower())


def _allow_insecure_ws() -> bool:
    return os.environ.get("COGNIS_EXECUTOR_ALLOW_INSECURE_WS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
