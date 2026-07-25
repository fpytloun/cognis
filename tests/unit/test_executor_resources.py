from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from time import perf_counter, sleep
from types import SimpleNamespace

import httpx
import pytest
import respx

from cognis.executor import resources
from cognis.models.executor_resources import (
    ExecutorResourceSnapshot,
    normalize_executor_resource_snapshot,
)


def test_common_resources_collect_cpu_and_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources.platform, "system", lambda: "Linux")
    monkeypatch.setattr(resources.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(resources.platform, "processor", lambda: "Example CPU")
    monkeypatch.setattr(
        resources.psutil,
        "cpu_count",
        lambda *, logical: 16 if logical else 8,
    )
    monkeypatch.setattr(resources.psutil, "cpu_percent", lambda *, interval: 37.5)
    monkeypatch.setattr(
        resources.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=64, available=24, used=50),
    )

    result = resources._collect_common_resources()

    assert result["os"] == "linux"
    assert result["arch"] == "x86_64"
    assert result["cpu"].model == "Example CPU"
    assert result["cpu"].physical_cores == 8
    assert result["cpu"].logical_cores == 16
    assert result["cpu"].utilization_percent == 37.5
    assert result["memory"].model_dump() == {
        "total_bytes": 64,
        "available_bytes": 24,
        "used_bytes": 40,
        "unified": None,
    }


def test_common_resources_leave_unavailable_values_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unavailable(*_: object, **__: object) -> object:
        raise OSError("not available")

    monkeypatch.setattr(resources.platform, "system", lambda: "")
    monkeypatch.setattr(resources.platform, "machine", lambda: "")
    monkeypatch.setattr(resources.platform, "processor", lambda: "")
    monkeypatch.setattr(resources.psutil, "cpu_count", _unavailable)
    monkeypatch.setattr(resources.psutil, "cpu_percent", _unavailable)
    monkeypatch.setattr(resources.psutil, "virtual_memory", _unavailable)

    result = resources._collect_common_resources()

    assert result["os"] is None
    assert result["arch"] is None
    assert result["cpu"].model is None
    assert result["cpu"].physical_cores is None
    assert result["cpu"].logical_cores is None
    assert result["cpu"].utilization_percent is None
    assert result["memory"] is None


def test_nvidia_parser_keeps_only_reported_values() -> None:
    parsed = resources._parse_nvidia_smi("NVIDIA RTX 4090, 24564, 1024, 42\nmalformed row")

    assert len(parsed) == 1
    assert parsed[0].backend == "nvidia"
    assert parsed[0].name == "NVIDIA RTX 4090"
    assert parsed[0].total_memory_bytes == 24564 * 1024 * 1024
    assert parsed[0].used_memory_bytes == 1024 * 1024 * 1024
    assert parsed[0].utilization_percent == 42


def test_macos_collector_reports_metal_unified_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resources,
        "_run_command",
        lambda _command: (
            '{"SPDisplaysDataType":[{"sppci_model":"Apple M4 Max","spdisplays_metal":"Supported"}]}'
        ),
    )

    parsed = resources._collect_macos_accelerators(
        total_memory_bytes=128 * 1024**3,
        unified_memory=True,
    )

    assert parsed is not None
    assert parsed[0].backend == "metal"
    assert parsed[0].name == "Apple M4 Max"
    assert parsed[0].total_memory_bytes == 128 * 1024**3
    assert parsed[0].used_memory_bytes is None
    assert parsed[0].utilization_percent is None


def test_ollama_model_store_reports_current_filesystem_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models_path = tmp_path / "models"
    models_path.mkdir()
    monkeypatch.setattr(
        resources.psutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, free=250),
    )

    snapshot = resources._collect_ollama_model_store(str(models_path))

    assert snapshot is not None
    assert snapshot.total_bytes == 1000
    assert snapshot.free_bytes == 250


def test_ollama_model_store_uses_environment_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models_path = tmp_path / "environment-models"
    models_path.mkdir()
    observed_paths: list[str] = []
    monkeypatch.setenv("OLLAMA_MODELS", str(models_path))
    monkeypatch.setattr(
        resources.psutil,
        "disk_usage",
        lambda path: observed_paths.append(path) or SimpleNamespace(total=1000, free=250),
    )

    snapshot = resources._collect_ollama_model_store()

    assert snapshot is not None
    assert observed_paths == [str(models_path)]


@pytest.mark.asyncio
async def test_ollama_model_store_probe_times_out_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources, "_OLLAMA_STORE_TIMEOUT_SECONDS", 0.01)
    collector = resources.ExecutorResourceCollector()
    release_probe = Event()
    probe_calls = 0

    def _stalled_probe(_model_store_path: str | None) -> None:
        nonlocal probe_calls
        probe_calls += 1
        release_probe.wait(timeout=0.1)

    monkeypatch.setattr(resources, "_collect_ollama_model_store", _stalled_probe)
    started_at = perf_counter()

    first = await collector._collect_ollama_model_store_bounded(None)
    second = await collector._collect_ollama_model_store_bounded(None)

    assert first is None
    assert second is None
    assert probe_calls == 1
    assert perf_counter() - started_at < 0.08
    release_probe.set()
    sleep(0.01)


@pytest.mark.asyncio
@respx.mock
async def test_ollama_collector_reports_version_and_model_state() -> None:
    endpoint = "http://127.0.0.1:22434"
    respx.get(f"{endpoint}/api/version").mock(
        return_value=httpx.Response(200, json={"version": "0.9.1"})
    )
    respx.get(f"{endpoint}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={"models": [{"name": "qwen3:8b"}, {"name": "gemma3:12b"}]},
        )
    )
    respx.get(f"{endpoint}/api/ps").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
    )

    snapshot = await resources._collect_ollama(endpoint)

    assert snapshot.status == "reachable"
    assert snapshot.version == "0.9.1"
    assert snapshot.installed_model_count == 2
    assert snapshot.running_model_count == 1
    assert snapshot.running_models == ["qwen3:8b"]


@pytest.mark.asyncio
@respx.mock
async def test_ollama_collector_marks_unreachable_without_guessing_counts() -> None:
    respx.get(url__startswith="http://127.0.0.1:11434").mock(
        side_effect=httpx.ConnectError("offline")
    )

    snapshot = await resources._collect_ollama()

    assert snapshot.status == "unreachable"
    assert snapshot.version is None
    assert snapshot.installed_model_count is None
    assert snapshot.running_model_count is None
    assert snapshot.running_models is None


def test_resource_snapshot_schema_ignores_untrusted_extra_fields() -> None:
    snapshot = normalize_executor_resource_snapshot(
        {
            "observed_at": datetime.now(UTC).isoformat(),
            "os": "linux",
            "cpu": {},
            "private_path": "/home/alice/models",
        }
    )

    assert snapshot is not None
    assert snapshot.cpu is not None
    assert snapshot.cpu.model is None
    assert "private_path" not in snapshot.model_dump()


def test_resource_snapshot_schema_rejects_unbounded_cardinality() -> None:
    snapshot = normalize_executor_resource_snapshot(
        {
            "observed_at": datetime.now(UTC).isoformat(),
            "accelerators": [{"backend": "nvidia", "name": f"gpu-{index}"} for index in range(17)],
        }
    )

    assert snapshot is None


def test_resource_snapshot_freshness_is_computed_from_observed_at() -> None:
    observed_at = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    snapshot = ExecutorResourceSnapshot(observed_at=observed_at)

    current = snapshot.with_current_freshness(now=datetime(2026, 7, 13, 10, 3, tzinfo=UTC))

    assert current.freshness is not None
    assert current.freshness.age_seconds == 180
    assert current.freshness.stale is True


def test_resource_snapshot_freshness_prefers_controller_receipt_time() -> None:
    snapshot = ExecutorResourceSnapshot(observed_at=datetime(2026, 7, 13, 9, 50, tzinfo=UTC))

    current = snapshot.with_current_freshness(
        now=datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
        received_at=datetime(2026, 7, 13, 9, 59, 50, tzinfo=UTC),
    )

    assert current.freshness is not None
    assert current.freshness.age_seconds == 10
    assert current.freshness.stale is False
