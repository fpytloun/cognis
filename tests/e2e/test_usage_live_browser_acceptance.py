"""Real auth/app/router/SQL checkpoint acceptance with content-free seeded facts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from tests.e2e.conftest import E2EStack
from tests.e2e.usage_checkpoint.build import verify_build
from tests.e2e.usage_checkpoint.expansion import (
    QUOTA_PROVIDER,
    assert_breakdowns,
    assert_quota,
    assert_summary,
    expansion_query,
    quota_request,
    seed_expansion,
    summary_requests,
)
from tests.e2e.usage_checkpoint.phases import assert_no_analytics_inference, retain_history
from tests.e2e.usage_checkpoint.requests import grouped_query
from tests.e2e.usage_checkpoint.seed import seed

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("COGNIS_USAGE_CHECKPOINT") != "1",
        reason="Explicit isolated usage checkpoint opt-in is required",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def usage_checkpoint_environment() -> Iterator[None]:
    """Fail before e2e_stack can connect to inherited databases or credentials."""
    if os.environ.get("COGNIS_USAGE_CHECKPOINT") != "1":
        yield
        return
    output = Path(os.environ["COGNIS_USAGE_RUN_DIR"]).resolve()
    assert output.is_dir() and output != ROOT
    assert Path(os.environ["COGNIS_E2E_CONTROLLER_PROJECT"]).resolve() == ROOT
    assert os.environ.get("UV_NO_SYNC") == os.environ.get("UV_OFFLINE") == "1"
    assert os.environ.get("COGNIS_SERVE_UI") == "true"
    for name in (
        "DATABASE_URL",
        "COGNIS_E2E_TEST_DATABASE_URL",
        "COGNIS_DATA_DIR",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_PROFILE",
        "COGNIS_E2E_INTARIS_POLICY_MODEL",
    ):
        assert name not in os.environ, f"Remove inherited {name}"
    assert "usage_checkpoint/sitecustomize.py" in str(
        Path(os.environ["PYTHONPATH"].split(os.pathsep)[0]) / "sitecustomize.py"
    )
    verify_build(ROOT)
    yield
    facts = (
        json.loads((output / "runtime.json").read_text())
        if (output / "runtime.json").exists()
        else {}
    )
    alive = []
    for proof in output.glob("python-*.json"):
        pid = json.loads(proof.read_text())["pid"]
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        alive.append(pid)
    listening = False
    if facts.get("url"):
        endpoint = urlsplit(facts["url"])
        with socket.socket() as client:
            listening = client.connect_ex((endpoint.hostname, endpoint.port)) == 0
    (output / "teardown.json").write_text(
        json.dumps(
            {
                "remaining_controller_pids": alive,
                "controller_port_listening": listening,
            },
            indent=2,
        )
    )
    assert alive == [] and not listening, "Task-owned app must be stopped by fixture teardown"


def _retain_logs(stack: E2EStack, output: Path) -> None:
    assert stack.log_dir is not None
    target = output / "service-logs"
    target.mkdir(exist_ok=True)
    for source in stack.log_dir.glob("*.log"):
        shutil.copyfile(source, target / source.name)


def _source_proof(output: Path, database: Path) -> dict[str, Any]:
    candidates = [json.loads(path.read_text()) for path in output.glob("python-*.json")]
    assert len(candidates) == 1, "Require attestation from the actual controller process"
    proof = candidates[0]
    expected = {
        "cognis.api.app": ROOT / "cognis/api/app.py",
        "cognis.api.routes.usage": ROOT / "cognis/api/routes/usage.py",
        "cognis.core.usage.query_service": ROOT / "cognis/core/usage/query_service.py",
        "cognis.store.usage.models": ROOT / "cognis/store/usage/models.py",
        "cognis.models.usage": ROOT / "packages/common/src/cognis/models/usage.py",
    }
    assert proof["modules"] == {name: str(path.resolve()) for name, path in expected.items()}
    assert proof["database_url"] == f"sqlite+aiosqlite:///{database}"
    assert proof["ui_build"] == str(ROOT / "ui/build")
    assert proof["auth_override"] is False and proof["loopback_only"] is True
    return proof


def test_live_usage_checkpoint_uses_real_app(e2e_stack: E2EStack) -> None:
    """Preserve awaited fixture inference, then require zero analytics-phase delta."""
    output = Path(os.environ["COGNIS_USAGE_RUN_DIR"]).resolve()
    baseline = retain_history(
        output,
        "fixture",
        e2e_stack.http.get(f"{e2e_stack.mock_llm_url}/__mock/history?limit=100").json(),
    )
    try:
        _exercise_checkpoint(e2e_stack, output, baseline)
    finally:
        _retain_logs(e2e_stack, output)
        current = retain_history(
            output,
            "history",
            e2e_stack.http.get(f"{e2e_stack.mock_llm_url}/__mock/history?limit=100").json(),
        )
        (output / "inference-phases.json").write_text(
            json.dumps(
                {
                    "barrier": "awaited fixture create/activate identity synchronization",
                    "fixture_count": len(baseline),
                    "final_count": len(current),
                    "full_history_unchanged": current == baseline,
                    "analytics_delta": len(current) - len(baseline),
                },
                indent=2,
            )
        )
        assert_no_analytics_inference(baseline, current)


def _exercise_checkpoint(e2e_stack: E2EStack, output: Path, baseline: list[Any]) -> None:
    """Seed only this fixture DB, then drive the real served app through Playwright."""
    assert e2e_stack.log_dir is not None
    database = (e2e_stack.log_dir.parent / "cognis/cognis.db").resolve()
    assert database.is_relative_to(output) and database.is_file()
    facts = {
        "database": str(database),
        "actor": e2e_stack.admin_email,
        "fixture_password": e2e_stack.admin_password,
        "agent": e2e_stack.e2e_agent_id,
        "url": e2e_stack.cognis_url,
        "intaris_url": e2e_stack.intaris_upstream_url,
    }
    (output / "runtime.json").write_text(json.dumps(facts, indent=2))
    proof = _source_proof(output, database)
    manifest = asyncio.run(seed())
    start = f"{manifest['start']}T00:00:00Z"
    from datetime import date, timedelta

    end = f"{date.fromisoformat(manifest['end']) + timedelta(days=1)}T00:00:00Z"
    responses = {}
    for mode, scope in (
        ("personal", {"mode": "personal"}),
        ("global", {"mode": "admin"}),
        ("project", {"mode": "project", "project_id": manifest["project"]}),
    ):
        response = e2e_stack.post(
            "/api/v1/usage/query",
            json=grouped_query(scope, start, end),
        )
        responses[mode] = {"status": response.status_code, "body": response.json()}
    (output / "api-preflight.json").write_text(json.dumps(responses, indent=2))
    assert all(value["status"] == 200 for value in responses.values())
    unauthenticated = httpx.post(
        f"{e2e_stack.cognis_url}/api/v1/usage/query",
        json={"scope": {"mode": "admin"}, "start": start, "end": end},
    )
    assert unauthenticated.status_code == 401
    index = e2e_stack.http.get(e2e_stack.cognis_url)
    assert index.status_code == 200
    assert index.content == (ROOT / "ui/build/index.html").read_bytes()
    source_hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "ui/src/lib/usage").glob("*"))
        if path.is_file()
    }
    (output / "provenance.json").write_text(
        json.dumps(
            {
                "checkpoint": os.environ["COGNIS_USAGE_BASE_REVISION"],
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "python": sys.executable,
                "controller": proof,
                "ui_node_modules": str((ROOT / "ui/node_modules").resolve()),
                "ui_source_hashes": source_hashes,
                "served_index_sha256": hashlib.sha256(index.content).hexdigest(),
                "verified_build": verify_build(ROOT),
                "api_backed": True,
                "synthetic_data": True,
                "real_app_auth": True,
                "final_capture_qualification": False,
            },
            indent=2,
        )
    )
    environment = {
        **os.environ,
        "COGNIS_E2E_URL": e2e_stack.cognis_url,
        "COGNIS_E2E_ADMIN_EMAIL": e2e_stack.admin_email,
        "COGNIS_E2E_ADMIN_PASSWORD": e2e_stack.admin_password,
        "COGNIS_USAGE_PYTHON": sys.executable,
    }
    before_browser = retain_history(
        output,
        "before-browser",
        e2e_stack.http.get(f"{e2e_stack.mock_llm_url}/__mock/history?limit=100").json(),
    )
    assert_no_analytics_inference(baseline, before_browser)
    browser_phase_started = time.monotonic()
    result = subprocess.run(
        [
            str(ROOT / "ui/node_modules/.bin/playwright"),
            "test",
            "e2e/usage-live-qualification.spec.ts",
            "--reporter=line",
            "--retries=0",
            f"--output={output / 'playwright'}",
        ],
        cwd=ROOT / "ui",
        env=environment,
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    (output / "playwright.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, f"Inspect {output / 'playwright.log'}"
    # Only a complete successful original five-case process admits the additive
    # phase. Its revoked grant and 12-fact assertions are never rewritten.
    expansion = asyncio.run(seed_expansion())
    expansion_preflight = {}
    for name, combined in (("filtered", False), ("combined", True)):
        request = expansion_query(expansion, combined=combined)
        response = e2e_stack.post("/api/v1/usage/query", json=request)
        expansion_preflight[name] = {
            "request": request,
            "status": response.status_code,
            "body": response.json(),
        }
    for name, request in summary_requests(expansion["execution"]).items():
        response = e2e_stack.post("/api/v1/usage/session-summaries", json=request)
        expansion_preflight[name] = {
            "request": request,
            "status": response.status_code,
            "body": response.json(),
        }
    request = quota_request(expansion)
    response = e2e_stack.get(
        f"/api/v1/usage/providers/{QUOTA_PROVIDER}/history",
        params={
            key: json.dumps(value) if key == "scope" else value
            for key, value in request.items()
            if value is not None
        },
    )
    expansion_preflight["quota"] = {
        "request": request,
        "status": response.status_code,
        "body": response.json(),
    }
    (output / "expansion-api-preflight.json").write_text(json.dumps(expansion_preflight, indent=2))
    assert all(item["status"] == 200 for item in expansion_preflight.values())
    assert_quota(expansion_preflight["quota"]["body"], expansion)
    for name, combined in (("filtered", False), ("combined", True)):
        assert_breakdowns(expansion_preflight[name]["body"], expansion, combined=combined)
    for name, expected in expansion["summaries"].items():
        assert_summary(expansion_preflight[name]["body"], expected)
    before_expansion = retain_history(
        output,
        "before-expansion-browser",
        e2e_stack.http.get(f"{e2e_stack.mock_llm_url}/__mock/history?limit=100").json(),
    )
    assert_no_analytics_inference(baseline, before_expansion)
    # One shared 360-second browser-phase budget; the new phase is not permission
    # to extend the outer attempt or independently enforced PID1 lease.
    remaining = 360 - (time.monotonic() - browser_phase_started)
    assert remaining > 0, "Expansion was not started: shared browser-phase budget exhausted"
    result = subprocess.run(
        [
            str(ROOT / "ui/node_modules/.bin/playwright"),
            "test",
            "e2e/usage-expansion-live-qualification.spec.ts",
            "--reporter=line",
            "--retries=0",
            "--max-failures=4",
            f"--output={output / 'playwright-expansion'}",
        ],
        cwd=ROOT / "ui",
        env={**environment, "COGNIS_USAGE_EXPANSION": "1"},
        capture_output=True,
        text=True,
        timeout=remaining,
        check=False,
    )
    (output / "playwright-expansion.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, f"Inspect {output / 'playwright-expansion.log'}"
