"""Canonical isolated E2E child environment owner; declared inputs are opt-in."""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, TextIO

from tests.e2e.usage_checkpoint.inputs import WorkerInputs


def isolated_subprocess_env(
    inherited: Mapping[str, str],
    extra: Mapping[str, str],
    *,
    declared: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Preserve ordinary E2E defaults; ambient uv state cannot override usage inputs."""
    result = dict(inherited)
    for key in (
        "COGNIS_DATA_DIR",
        "COGNIS_PORT",
        "COGNIS_HOST",
        "DATABASE_URL",
        "COGNIS_MNEMORY_URL",
        "COGNIS_INTARIS_URL",
        "COGNIS_INITIAL_ADMIN_EMAIL",
        "COGNIS_INITIAL_ADMIN_PASSWORD",
        "COGNIS_LOG_FORMAT",
        "COGNIS_LOG_LEVEL",
        "COGNIS_E2E_MODE",
    ):
        result.pop(key, None)
    if declared is not None:
        for key in tuple(result):
            if (
                (key.startswith("UV_") and key != "UV_PROJECT_ENVIRONMENT")
                or key.startswith(("XDG_", "HF_", "FASTEMBED_"))
                or key
                in {
                    "HOME",
                    "TMPDIR",
                    "TMP",
                    "TEMP",
                    "VIRTUAL_ENV",
                }
            ):
                result.pop(key)
        if "UV_CONSTRAINT" in declared:
            result.pop("UV_PROJECT_ENVIRONMENT", None)
        # Preserve PYTHONPATH's existing source/network fence. The controller's
        # fresh UV_PROJECT_ENVIRONMENT remains the separate bootstrap owner's gate.
    result.update(extra)
    if declared is not None:
        result.update(declared)
    return result


class Closable(Protocol):
    """Resource whose lifetime belongs to this E2E stack."""

    def close(self) -> None: ...


@dataclass
class ServiceProcess:
    """Own the process and log explicitly, without injecting Popen attributes."""

    process: subprocess.Popen
    log: TextIO

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self) -> None:
        """Keep existing termination bounds, then close the owned log."""
        try:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.process.poll() is None:
                raise RuntimeError("E2E child teardown is incomplete")
        finally:
            self.log.close()


@dataclass
class StackResources:
    """Release scratch only after proxy, clients and every child teardown."""

    inputs: WorkerInputs | None = None
    processes: list[tuple[str, ServiceProcess]] = field(default_factory=list)
    clients: list[Closable] = field(default_factory=list)
    append_proxy: Closable | None = None

    def close(self) -> None:
        failures: list[Exception] = []
        for resource in ([self.append_proxy] if self.append_proxy else []) + self.clients:
            try:
                resource.close()
            except Exception as exc:
                failures.append(exc)
        for _, process in reversed(self.processes):
            try:
                process.stop()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("E2E teardown failed; input scratch retained", failures)
        if self.inputs is not None:
            self.inputs.close()
