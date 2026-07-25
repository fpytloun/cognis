"""Cross-platform, best-effort collection of current executor resources."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from cognis.models.executor_resources import (
    AcceleratorResourceSnapshot,
    CPUResourceSnapshot,
    DiskResourceSnapshot,
    ExecutorResourceSnapshot,
    ExecutorRuntimeResourceSnapshot,
    MemoryResourceSnapshot,
    OllamaResourceSnapshot,
)
from cognis.models.local_models import OLLAMA_MANAGED_ENDPOINT

_COMMAND_TIMEOUT_SECONDS = 3.0
_OLLAMA_TIMEOUT_SECONDS = 2.0
_OLLAMA_STORE_TIMEOUT_SECONDS = 1.0
_MAX_RUNNING_MODEL_NAMES = 20


class ExecutorResourceCollector:
    """Collect a bounded current snapshot without retaining sample history."""

    def __init__(self) -> None:
        self._ollama_store_probe: asyncio.Task[DiskResourceSnapshot | None] | None = None

    async def collect(
        self,
        *,
        runtime: ExecutorRuntimeResourceSnapshot | None = None,
        ollama_endpoint: str = OLLAMA_MANAGED_ENDPOINT,
        ollama_model_store_path: str | None = None,
    ) -> ExecutorResourceSnapshot:
        common_task = asyncio.create_task(asyncio.to_thread(_collect_common_resources))
        ollama_task = asyncio.create_task(_collect_ollama(ollama_endpoint))
        ollama_store_task = asyncio.create_task(
            self._collect_ollama_model_store_bounded(ollama_model_store_path)
        )
        common = await common_task
        accelerators, ollama, ollama_store = await asyncio.gather(
            asyncio.to_thread(
                _collect_accelerators,
                common["os"],
                common["memory"].total_bytes if common["memory"] is not None else None,
                common["memory"].unified if common["memory"] is not None else None,
            ),
            ollama_task,
            ollama_store_task,
        )
        return ExecutorResourceSnapshot(
            observed_at=datetime.now(UTC),
            os=common["os"],
            arch=common["arch"],
            cpu=common["cpu"],
            memory=common["memory"],
            accelerators=accelerators,
            ollama_model_store=ollama_store,
            ollama=ollama,
            runtime=runtime,
        )

    async def _collect_ollama_model_store_bounded(
        self,
        model_store_path: str | None,
    ) -> DiskResourceSnapshot | None:
        """Reuse one outstanding filesystem probe after a timeout."""

        if self._ollama_store_probe is None:
            self._ollama_store_probe = asyncio.create_task(
                asyncio.to_thread(_collect_ollama_model_store, model_store_path)
            )
        probe = self._ollama_store_probe
        try:
            result = await asyncio.wait_for(
                asyncio.shield(probe),
                timeout=_OLLAMA_STORE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return None
        except Exception:
            self._ollama_store_probe = None
            return None
        self._ollama_store_probe = None
        return result


def _collect_common_resources() -> dict[str, Any]:
    os_name = platform.system().lower() or None
    arch = platform.machine().lower() or None

    physical_cores = _safe_psutil_value(lambda: psutil.cpu_count(logical=False))
    logical_cores = _safe_psutil_value(lambda: psutil.cpu_count(logical=True))
    utilization = _safe_psutil_value(lambda: psutil.cpu_percent(interval=0.1))
    cpu = CPUResourceSnapshot(
        model=_cpu_model(os_name),
        physical_cores=_positive_int_or_none(physical_cores),
        logical_cores=_positive_int_or_none(logical_cores),
        utilization_percent=_percent_or_none(utilization),
    )

    memory: MemoryResourceSnapshot | None
    try:
        virtual_memory = psutil.virtual_memory()
    except (OSError, RuntimeError, psutil.Error):
        memory = None
    else:
        memory = MemoryResourceSnapshot(
            total_bytes=_nonnegative_int_or_none(virtual_memory.total),
            available_bytes=_nonnegative_int_or_none(virtual_memory.available),
            used_bytes=_used_memory_bytes(
                virtual_memory.total,
                virtual_memory.available,
            ),
            unified=_macos_unified_memory(os_name),
        )

    return {
        "os": os_name,
        "arch": arch,
        "cpu": cpu,
        "memory": memory,
    }


def _cpu_model(os_name: str | None) -> str | None:
    processor = platform.processor().strip()
    if processor:
        return processor
    if os_name == "linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    value = line.split(":", 1)[1].strip()
                    if value:
                        return value
        except OSError:
            return None
    if os_name == "darwin":
        return _run_command(("sysctl", "-n", "machdep.cpu.brand_string"))
    return None


def _macos_unified_memory(os_name: str | None) -> bool | None:
    if os_name != "darwin":
        return None
    arm64 = _run_command(("sysctl", "-n", "hw.optional.arm64"))
    if arm64 == "1":
        return True
    return None


def _collect_accelerators(
    os_name: str | None,
    total_memory_bytes: int | None,
    unified_memory: bool | None,
) -> list[AcceleratorResourceSnapshot] | None:
    nvidia = _collect_nvidia_accelerators()
    if nvidia is not None:
        return nvidia
    if os_name == "darwin":
        return _collect_macos_accelerators(
            total_memory_bytes=total_memory_bytes,
            unified_memory=unified_memory,
        )
    return None


def _collect_nvidia_accelerators() -> list[AcceleratorResourceSnapshot] | None:
    if shutil.which("nvidia-smi") is None:
        return None
    output = _run_command(
        (
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        )
    )
    if output is None:
        return None
    return _parse_nvidia_smi(output)


def _parse_nvidia_smi(output: str) -> list[AcceleratorResourceSnapshot]:
    accelerators: list[AcceleratorResourceSnapshot] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        total_mib = _nonnegative_int_or_none(parts[1])
        used_mib = _nonnegative_int_or_none(parts[2])
        accelerators.append(
            AcceleratorResourceSnapshot(
                backend="nvidia",
                name=parts[0] or None,
                total_memory_bytes=total_mib * 1024 * 1024 if total_mib is not None else None,
                used_memory_bytes=used_mib * 1024 * 1024 if used_mib is not None else None,
                utilization_percent=_percent_or_none(parts[3]),
            )
        )
    return accelerators


def _collect_macos_accelerators(
    *,
    total_memory_bytes: int | None,
    unified_memory: bool | None,
) -> list[AcceleratorResourceSnapshot] | None:
    output = _run_command(("system_profiler", "SPDisplaysDataType", "-json"))
    if output is None:
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    displays = payload.get("SPDisplaysDataType")
    if not isinstance(displays, list):
        return None
    accelerators: list[AcceleratorResourceSnapshot] = []
    for display in displays:
        if not isinstance(display, dict):
            continue
        name = _first_string(
            display,
            ("sppci_model", "_name", "spdisplays_vendor", "spdisplays_chipset-model"),
        )
        if name is None:
            continue
        dedicated_memory = _parse_memory_size(display.get("spdisplays_vram"))
        accelerators.append(
            AcceleratorResourceSnapshot(
                backend="metal",
                name=name,
                total_memory_bytes=(
                    total_memory_bytes if unified_memory is True else dedicated_memory
                ),
                used_memory_bytes=None,
                utilization_percent=None,
            )
        )
    return accelerators


def _parse_memory_size(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"([\d.]+)\s*(GB|MB)", value, flags=re.IGNORECASE)
    if match is None:
        return None
    amount = float(match.group(1))
    multiplier = 1024**3 if match.group(2).lower() == "gb" else 1024**2
    return int(amount * multiplier)


async def _collect_ollama(base_url: str = OLLAMA_MANAGED_ENDPOINT) -> OllamaResourceSnapshot:
    timeout = httpx.Timeout(_OLLAMA_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            version_result, tags_result, running_result = await asyncio.gather(
                _get_ollama_json(client, f"{base_url}/api/version"),
                _get_ollama_json(client, f"{base_url}/api/tags"),
                _get_ollama_json(client, f"{base_url}/api/ps"),
            )
    except Exception:
        return OllamaResourceSnapshot(status="unreachable")

    results = (version_result, tags_result, running_result)
    if not any(reachable for reachable, _ in results):
        return OllamaResourceSnapshot(status="unreachable")

    version_data = version_result[1]
    tags_data = tags_result[1]
    running_data = running_result[1]
    installed = _model_entries(tags_data)
    running = _model_entries(running_data)
    running_names = [
        name
        for item in running or []
        if (name := _first_string(item, ("name", "model"))) is not None
    ][:_MAX_RUNNING_MODEL_NAMES]
    return OllamaResourceSnapshot(
        status="reachable",
        version=_first_string(version_data, ("version",)) if version_data else None,
        installed_model_count=len(installed) if installed is not None else None,
        running_model_count=len(running) if running is not None else None,
        running_models=running_names if running is not None else None,
    )


async def _get_ollama_json(
    client: httpx.AsyncClient,
    url: str,
) -> tuple[bool, dict[str, Any] | None]:
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return False, None
    if not response.is_success:
        return True, None
    try:
        payload = response.json()
    except ValueError:
        return True, None
    return True, payload if isinstance(payload, dict) else None


def _model_entries(payload: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if payload is None:
        return None
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    return [item for item in models if isinstance(item, dict)]


def _collect_ollama_model_store(model_store_path: str | None = None) -> DiskResourceSnapshot | None:
    models_path = Path(
        model_store_path or os.environ.get("OLLAMA_MODELS") or "~/.ollama/models"
    ).expanduser()
    if not models_path.exists():
        return None
    try:
        usage = psutil.disk_usage(str(models_path))
    except (OSError, RuntimeError, psutil.Error):
        return None
    return DiskResourceSnapshot(
        total_bytes=_nonnegative_int_or_none(usage.total),
        free_bytes=_nonnegative_int_or_none(usage.free),
    )


def _run_command(command: tuple[str, ...]) -> str | None:
    """Run one fixed collector command with a hard timeout."""

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _safe_psutil_value(callback: Any) -> Any:
    try:
        return callback()
    except (OSError, RuntimeError, psutil.Error):
        return None


def _positive_int_or_none(value: Any) -> int | None:
    parsed = _nonnegative_int_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _used_memory_bytes(total: Any, available: Any) -> int | None:
    total_bytes = _nonnegative_int_or_none(total)
    available_bytes = _nonnegative_int_or_none(available)
    if total_bytes is None or available_bytes is None or available_bytes > total_bytes:
        return None
    return total_bytes - available_bytes


def _nonnegative_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _percent_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 100 else None


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
