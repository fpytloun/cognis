"""Platform-neutral lifecycle boundary for installed user services.

This module deliberately delegates process supervision to Homebrew/launchd or
an existing Linux user-systemd unit.  It never creates a supervisor or starts
the foreground executor recursively.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SERVICE_NAME = "cognis-executor"
FOREGROUND_COMMAND = "cognis-executor"
STDOUT_LOG_NAME = "cognis-executor.log"
STDERR_LOG_NAME = "cognis-executor.error.log"


class ServiceState(StrEnum):
    """Observable service states."""

    NOT_INSTALLED = "not installed"
    STOPPED = "stopped"
    RUNNING = "running"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """Result from a service-manager operation."""

    ok: bool
    state: ServiceState
    message: str
    foreground_command: str = FOREGROUND_COMMAND


@dataclass(frozen=True, slots=True)
class LogResult:
    """Result from a service log operation."""

    returncode: int
    message: str = ""


class ServiceManager:
    """Small boundary implemented by platform-specific service managers."""

    def start(self) -> ServiceResult:
        raise NotImplementedError

    def stop(self) -> ServiceResult:
        raise NotImplementedError

    def restart(self) -> ServiceResult:
        raise NotImplementedError

    def status(self) -> ServiceResult:
        raise NotImplementedError

    def logs(self, *, lines: int, follow: bool) -> LogResult:
        raise NotImplementedError


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _failure(action: str, completed: subprocess.CompletedProcess[str]) -> ServiceResult:
    detail = (completed.stderr or completed.stdout).strip().splitlines()
    message = detail[0][:300] if detail else f"{action} failed"
    return ServiceResult(False, ServiceState.UNKNOWN, message)


def _run_stream(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode


def _tail_file(path: Path, *, lines: int) -> list[str] | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return list(deque(stream, maxlen=lines))
    except FileNotFoundError:
        return None


class MacOSServiceManager(ServiceManager):
    """Delegate lifecycle operations to Homebrew services (launchd)."""

    def __init__(self, *, brew: str | None = None) -> None:
        self.brew = brew or shutil.which("brew")

    def _missing(self) -> ServiceResult | None:
        if self.brew is None:
            return ServiceResult(
                False,
                ServiceState.NOT_INSTALLED,
                "Homebrew is not installed; no Cognis launchd service is available",
                "cognis-executor",
            )
        return None

    def _operation(self, action: str) -> ServiceResult:
        missing = self._missing()
        if missing:
            return missing
        assert self.brew is not None
        try:
            completed = _run((self.brew, "services", action, SERVICE_NAME))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ServiceResult(
                False, ServiceState.UNKNOWN, f"Homebrew service command failed: {exc}"
            )
        if completed.returncode:
            return _failure(action, completed)
        return ServiceResult(
            True,
            ServiceState.RUNNING if action != "stop" else ServiceState.STOPPED,
            f"Service {action} requested",
        )

    def start(self) -> ServiceResult:
        return self._operation("start")

    def stop(self) -> ServiceResult:
        return self._operation("stop")

    def restart(self) -> ServiceResult:
        return self._operation("restart")

    def status(self) -> ServiceResult:
        missing = self._missing()
        if missing:
            return missing
        assert self.brew is not None
        try:
            completed = _run((self.brew, "services", "list", "--json"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ServiceResult(
                False, ServiceState.UNKNOWN, f"Homebrew service status failed: {exc}"
            )
        if completed.returncode:
            return _failure("status", completed)
        try:
            import json

            records = json.loads(completed.stdout)
        except (ValueError, TypeError):
            return ServiceResult(
                False, ServiceState.UNKNOWN, "Homebrew returned invalid service status"
            )
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            return ServiceResult(
                False, ServiceState.UNKNOWN, "Homebrew returned invalid service status"
            )
        record = next((item for item in records if item.get("name") == SERVICE_NAME), None)
        if record is None:
            return ServiceResult(
                False, ServiceState.NOT_INSTALLED, "Cognis service is not installed"
            )
        status = str(record.get("status", "")).lower()
        if status in {"started", "running"}:
            return ServiceResult(True, ServiceState.RUNNING, "Service is running")
        if status in {"none", "stopped"}:
            return ServiceResult(True, ServiceState.STOPPED, "Service is stopped")
        return ServiceResult(
            False,
            ServiceState.UNKNOWN,
            f"Homebrew reported service status {status or 'unknown'}",
        )

    def _log_paths(self) -> tuple[Path, Path] | LogResult:
        missing = self._missing()
        if missing:
            return LogResult(1, missing.message)
        assert self.brew is not None
        try:
            completed = _run((self.brew, "--prefix"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return LogResult(1, f"Cannot determine the Homebrew prefix: {exc}")
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            return LogResult(1, detail or "Cannot determine the Homebrew prefix")
        prefix = Path(completed.stdout.strip())
        if not prefix.is_absolute():
            return LogResult(1, "Homebrew returned an invalid prefix")
        log_dir = prefix / "var" / "log"
        return log_dir / STDERR_LOG_NAME, log_dir / STDOUT_LOG_NAME

    def _installed(self) -> bool:
        assert self.brew is not None
        try:
            return _run((self.brew, "list", "--versions", SERVICE_NAME)).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def logs(self, *, lines: int, follow: bool) -> LogResult:
        paths = self._log_paths()
        if isinstance(paths, LogResult):
            return paths
        stderr_path, stdout_path = paths
        if not self._installed():
            return LogResult(
                1,
                "Cognis executor is not installed. "
                f"Service logs would be {stderr_path} and {stdout_path}.",
            )
        if follow:
            tail = shutil.which("tail") or "/usr/bin/tail"
            try:
                return LogResult(
                    _run_stream(
                        (
                            tail,
                            "-n",
                            str(lines),
                            "-F",
                            str(stderr_path),
                            str(stdout_path),
                        )
                    )
                )
            except KeyboardInterrupt:
                return LogResult(130)
            except OSError as exc:
                return LogResult(1, f"Cannot follow service logs: {exc}")

        for label, path in (("stderr", stderr_path), ("stdout", stdout_path)):
            sys.stdout.write(f"==> {label}: {path} <==\n")
            try:
                content = _tail_file(path, lines=lines)
            except OSError as exc:
                return LogResult(1, f"Cannot read {label} service log: {exc}")
            if content is None:
                sys.stdout.write("[missing]\n")
            elif not content:
                sys.stdout.write("[empty]\n")
            else:
                sys.stdout.writelines(content)
                if not content[-1].endswith("\n"):
                    sys.stdout.write("\n")
        return LogResult(0)


class LinuxSystemdServiceManager(ServiceManager):
    """Delegate lifecycle operations to an already-installed user unit."""

    def __init__(self, *, systemctl: str | None = None) -> None:
        self.systemctl = systemctl or shutil.which("systemctl")

    def _unit_state(self) -> ServiceState:
        if self.systemctl is None:
            return ServiceState.NOT_INSTALLED
        try:
            unit = _run((self.systemctl, "--user", "cat", f"{SERVICE_NAME}.service"))
        except (OSError, subprocess.TimeoutExpired):
            return ServiceState.NOT_INSTALLED
        return ServiceState.STOPPED if unit.returncode else ServiceState.UNKNOWN

    def _operation(self, action: str) -> ServiceResult:
        if self.systemctl is None:
            return ServiceResult(False, ServiceState.NOT_INSTALLED, "systemctl is not installed")
        if self._unit_state() == ServiceState.NOT_INSTALLED:
            return ServiceResult(
                False,
                ServiceState.NOT_INSTALLED,
                "No installed Cognis user-systemd unit was found",
            )
        try:
            completed = _run((self.systemctl, "--user", action, f"{SERVICE_NAME}.service"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ServiceResult(
                False, ServiceState.UNKNOWN, f"systemd service command failed: {exc}"
            )
        if completed.returncode:
            return _failure(action, completed)
        return ServiceResult(
            True,
            ServiceState.RUNNING if action != "stop" else ServiceState.STOPPED,
            f"Service {action} requested",
        )

    def start(self) -> ServiceResult:
        return self._operation("start")

    def stop(self) -> ServiceResult:
        return self._operation("stop")

    def restart(self) -> ServiceResult:
        return self._operation("restart")

    def status(self) -> ServiceResult:
        if self.systemctl is None:
            return ServiceResult(False, ServiceState.NOT_INSTALLED, "systemctl is not installed")
        if self._unit_state() == ServiceState.NOT_INSTALLED:
            return ServiceResult(
                False, ServiceState.NOT_INSTALLED, "No installed Cognis user-systemd unit was found"
            )
        try:
            completed = _run((self.systemctl, "--user", "is-active", f"{SERVICE_NAME}.service"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ServiceResult(False, ServiceState.UNKNOWN, f"systemd status failed: {exc}")
        if completed.returncode == 0 and completed.stdout.strip() == "active":
            return ServiceResult(True, ServiceState.RUNNING, "Service is running")
        state = completed.stdout.strip()
        if state in {"inactive", "deactivating"}:
            return ServiceResult(True, ServiceState.STOPPED, "Service is stopped")
        return ServiceResult(
            False,
            ServiceState.UNKNOWN,
            f"systemd reported service status {state or 'unknown'}",
        )

    def logs(self, *, lines: int, follow: bool) -> LogResult:
        if self.systemctl is None:
            return LogResult(
                1,
                "systemctl is not installed. "
                "If systemd is available, run "
                "`journalctl --user -u cognis-executor.service`.",
            )
        if self._unit_state() == ServiceState.NOT_INSTALLED:
            return LogResult(
                1,
                "No installed Cognis user-systemd unit was found. "
                "Manual command: `journalctl --user -u cognis-executor.service`.",
            )
        journalctl = shutil.which("journalctl")
        if journalctl is None:
            return LogResult(
                1,
                "journalctl is not installed. "
                "Manual command: `journalctl --user -u cognis-executor.service`.",
            )
        command = [
            journalctl,
            "--user",
            "-u",
            f"{SERVICE_NAME}.service",
            "-n",
            str(lines),
        ]
        if follow:
            command.append("-f")
        try:
            return LogResult(_run_stream(command))
        except KeyboardInterrupt:
            return LogResult(130)
        except OSError as exc:
            return LogResult(1, f"Cannot read systemd service logs: {exc}")


class UnsupportedServiceManager(ServiceManager):
    """Explain lifecycle limitations on unsupported operating systems."""

    def _result(self) -> ServiceResult:
        return ServiceResult(
            False,
            ServiceState.UNSUPPORTED,
            f"Service lifecycle is not supported on {platform.system()}; "
            f"run `{FOREGROUND_COMMAND}` in the foreground",
        )

    def start(self) -> ServiceResult:
        return self._result()

    def stop(self) -> ServiceResult:
        return self._result()

    def restart(self) -> ServiceResult:
        return self._result()

    def status(self) -> ServiceResult:
        return self._result()

    def logs(self, *, lines: int, follow: bool) -> LogResult:
        del lines, follow
        return LogResult(
            1,
            f"Service logs are not supported on {platform.system()}. "
            "Run `cognis-executor` in the foreground to view logs.",
        )


def get_service_manager(*, system: str | None = None) -> ServiceManager:
    """Select a manager without probing or mutating a live service."""
    selected = (system or platform.system()).lower()
    if selected in {"darwin", "macos"}:
        return MacOSServiceManager()
    if selected == "linux":
        return LinuxSystemdServiceManager()
    return UnsupportedServiceManager()
