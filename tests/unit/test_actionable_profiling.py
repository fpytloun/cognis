from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import tracemalloc
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from cognis.api.authentication import AuthenticatedUser
from cognis.api.routes.profiling import router
from cognis.core.profiling_capture import CaptureOptions, LocalCapture
from cognis.core.profiling_owners import ProfilingOwners
from cognis.core.session_cache import CachedSessionState, SessionCache
from cognis.core.turn_scheduler import TurnScheduler
from cognis.providers.llm.litellm import LiteLLMProvider


def cli():
    spec = importlib.util.spec_from_file_location(
        "controller_profiling_cli",
        Path(__file__).parents[2] / "scripts" / "controller-profiling.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_real_owner_composition_through_cpu_capture_and_stop(monkeypatch):
    cache = SessionCache(Mock())
    cache._entries["private-session"] = CachedSessionState(
        session_id="private-session", intaris_session_id="private-intaris"
    )
    scheduler = TurnScheduler(
        **{
            name: Mock()
            for name in (
                "session_factory",
                "workflow_engine",
                "decision_engine",
                "task_queue",
                "session_manager",
                "session_cache",
                "compaction_strategy",
                "agent_loop",
                "pause_waiter",
                "notification_service",
                "providers",
                "artifact_store",
                "workflow_registry",
                "event_bus",
            )
        }
    )
    llm = LiteLLMProvider(Mock())
    llm._count_memo[("private-provider", "private-model", "private-digest")] = 12
    owners = ProfilingOwners(
        cache.resource_owner_accounting,
        {
            "turn_scheduler": scheduler.resource_owner_counts,
            "llm": llm.resource_owner_counts,
        },
    )
    monkeypatch.setenv("COGNIS_BUILD_SHA", "72eb4097433304635d9a64a51d985a89a3e99257")
    capture = LocalCapture(str(uuid4()), CaptureOptions(level="cpu"), owners)
    tracing_before = tracemalloc.is_tracing()
    capture.start()
    await asyncio.sleep(0.2)
    await capture.stop()
    assert capture.state == "stopped"
    assert tracemalloc.is_tracing() == tracing_before
    resources = capture.report["resources"]
    assert resources["errors"] == []
    assert resources["runtime"]["build_source"] == "environment"
    memory = resources["memory"][0]
    assert memory["runtime"]["gc_delta"][0]["collections"] >= 0
    assert memory["timestamp"] > 0
    assert memory["owners"]["owner_counts"]["llm"]["counts"]["token_count_memos"] == 1
    assert memory["owners"]["owner_counts"]["turn_scheduler"]["counts"]["active_turns"] == 0
    cpu = resources["cpu"]
    assert cpu["thread_scope"] == "python_threads_only"
    assert cpu["sampler_thread_cpu_seconds"] >= 0
    assert cpu["unattributed_process_cpu_seconds"] >= 0
    loop = resources["event_loop"]
    assert loop["samples"] >= 1
    assert sum(loop["bucket_counts"]) == loop["samples"]
    assert not any(t.get_name() == "resource-event-loop-delay" for t in asyncio.all_tasks())
    assert "private" not in json.dumps(capture.report)
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.user = AuthenticatedUser("admin@example.test", "admin", auth_type="api_key")
        return await call_next(request)

    app.state.profiling_control = Mock(
        read=AsyncMock(
            return_value={
                "meta": {},
                "desired": "stop",
                "controllers": {},
                "reports": {"controller-a:incarnation-a": capture.report},
            }
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/system/profiling/captures/{capture.capture_id}/report"
        )
    assert response.status_code == 200
    assert response.json()["reports"]["controller-a:incarnation-a"]["resources"] == json.loads(
        json.dumps(resources)
    )


@pytest.mark.asyncio
async def test_optional_and_failed_owner_counts_are_explicit_and_sanitized():
    owners = ProfilingOwners(
        lambda: {"sessions": 0},
        {
            "missing": None,
            "failed": Mock(side_effect=RuntimeError("private-error-content")),
            "invalid": lambda: {"count": "private-content"},
        },
    )
    result = await owners()
    assert all(not owner["available"] for owner in result["owner_counts"].values())
    assert "private" not in json.dumps(result)


def report(timestamp=1.0, rss=100, complete=True):
    owner = "controller-a:incarnation-a"
    return {
        "meta": {"options": {"level": "cpu", "sample_interval_ms": 100}},
        "reports": {
            owner: {
                "resources": {
                    "schema_version": 2,
                    "runtime": {
                        "build": "72eb4097433304635d9a64a51d985a89a3e99257",
                        "build_source": "environment",
                    },
                    "memory": [
                        {
                            "timestamp": timestamp,
                            "process": {"VmRSS": rss},
                            "owners": {
                                "sessions": 1,
                                "accounting": {
                                    "schema_version": 2,
                                    "coverage_complete": complete,
                                    "fields": {"events": {"estimated_bytes": 20}},
                                },
                            },
                        }
                    ],
                    "cpu": {
                        "elapsed_seconds": 10,
                        "process_cpu_seconds": 2,
                        "sampler_thread_cpu_seconds": 0.1,
                    },
                },
            }
        },
    }


def test_compare_reports_uses_process_deltas_not_rss_ownership_subtraction():
    module = cli()
    result = module.compare_reports(report(), report(timestamp=10, rss=140))
    row = result["comparisons"]["controller-a:incarnation-a"]
    assert row["process_counter_deltas"] == {"VmRSS": 40}
    assert row["owner_bytes_comparable"]
    assert row["non_additive_field_byte_deltas"] == {"events": 0}
    assert row["cpu_windows"][0]["average_cores"] == 0.2
    assert "workload_not_controlled" in result["warnings"]
    assert "unaccounted" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    ["restart", "build", "coverage", "time", "truncated", "options", "allocation_interval"],
)
def test_comparison_flags_incompatible_evidence(change):
    before, after = report(), report(timestamp=10, rss=140)
    owner = "controller-a:incarnation-a"
    resources = after["reports"][owner]["resources"]
    if change == "restart":
        after["reports"]["controller-a:incarnation-b"] = after["reports"].pop(owner)
    elif change == "build":
        resources["runtime"]["build"] = "3357fa49cb2bc15f773b6f5b3dd4283fb094eb73"
    elif change == "coverage":
        resources["memory"][0]["owners"]["accounting"]["coverage_complete"] = False
    elif change == "time":
        resources["memory"][0]["timestamp"] = 0.5
    elif change == "truncated":
        after["reports"][owner]["truncated"] = True
    elif change == "options":
        after["meta"]["options"]["level"] = "full"
    else:
        before["meta"]["options"].update(level="full", allocation_interval_seconds=60)
        after["meta"]["options"].update(level="full", allocation_interval_seconds=30)
    result = cli().compare_reports(before, after)
    if change == "restart":
        assert not result["comparisons"]
    else:
        row = result["comparisons"][owner]
        assert row["warnings"]
        if change == "coverage":
            assert row["process_comparable"] and not row["owner_bytes_comparable"]
        elif change in ("options", "allocation_interval"):
            assert not row["cpu_options_comparable"]
            assert "allocation_tracing_observer_overhead" in row["warnings"]
        else:
            assert not row["process_comparable"]


def test_compare_is_offline_and_bounded(tmp_path, monkeypatch, capsys):
    module = cli()
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(report()))
    after.write_text(json.dumps(report(timestamp=10)))
    monkeypatch.delenv("COGNIS_URL", raising=False)
    monkeypatch.delenv("COGNIS_ADMIN_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controller-profiling.py",
            "compare",
            "--before",
            str(before),
            "--after",
            str(after),
        ],
    )
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["comparisons"]
    after.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="report_file_too_large"):
        module.read_report(str(after))


def test_legacy_reports_do_not_get_false_comparability():
    before = report()
    after = copy.deepcopy(before)
    after["reports"]["controller-a:incarnation-a"]["resources"].pop("runtime")
    result = cli().compare_reports(before, after)
    assert not result["comparisons"]["controller-a:incarnation-a"]["process_comparable"]


@pytest.mark.asyncio
async def test_report_size_reduction_keeps_first_last_samples_and_omission_counts():
    from cognis.core.profiling_control import ProfilingControl

    control = ProfilingControl(Mock(), Mock(), "controller-a:incarnation-a", lambda: {})
    control._eval = AsyncMock()
    capture = LocalCapture(str(uuid4()), CaptureOptions(level="light"), lambda: {})
    capture.state = "completed"
    capture.report["resources"] = {
        "memory": [{"timestamp": i, "counters": [0] * 5000} for i in range(21)],
        "cpu": {"top_endpoint_stacks": [[["module.py:1:hot"], 10, 1.0]]},
    }
    await control._publish(capture)
    encoded = control._eval.call_args.args[2][2]
    result = json.loads(encoded)
    assert len(encoded) <= 131072
    assert result["truncated"]
    assert result["omitted"]["memory_samples"] > 0
    assert result["resources"]["memory"][0]["timestamp"] == 0
    assert result["resources"]["memory"][-1]["timestamp"] == 20
    assert result["resources"]["cpu"]["top_endpoint_stacks"]
    assert result["omitted"]["cpu_stack_rows"] == 0
