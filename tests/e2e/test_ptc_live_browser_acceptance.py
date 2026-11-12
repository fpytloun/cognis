"""Run the real Playwright PTC scenario against this checkout's isolated E2E stack."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from tests.e2e.conftest import E2EStack

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("COGNIS_E2E_INTARIS_POLICY_MODEL") != "ptc-policy-model",
        reason="opt in with COGNIS_E2E_INTARIS_POLICY_MODEL=ptc-policy-model",
    ),
]


def _probe(stack: E2EStack, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        response = stack.http.get(url, headers=headers, timeout=5)
        return {"url": url, "status_code": response.status_code}
    except Exception as exc:
        return {
            "url": url,
            "error_type": type(exc).__name__,
        }


def _service_preflight(stack: E2EStack) -> dict[str, Any]:
    assert stack.append_proxy is not None
    cognis_health = _probe(stack, f"{stack.cognis_url}/api/health", stack.admin_headers())
    diagnostics = _probe(
        stack,
        f"{stack.cognis_url}/api/v1/system/diagnostics",
        stack.admin_headers(),
    )
    if diagnostics.get("status_code") == 200:
        try:
            response = stack.get("/api/v1/system/diagnostics")
            configured_url = response.json().get("config", {}).get("intaris_url")
            if isinstance(configured_url, str):
                configured = urlsplit(configured_url)
                expected = urlsplit(stack.append_proxy.url)
                diagnostics["intaris_target"] = {
                    "scheme": configured.scheme,
                    "host": configured.hostname,
                    "port": configured.port,
                }
                diagnostics["intaris_target_matches_fixture_proxy"] = (
                    configured.scheme,
                    configured.hostname,
                    configured.port,
                ) == (expected.scheme, expected.hostname, expected.port)
        except Exception as exc:
            diagnostics["intaris_config_error_type"] = type(exc).__name__
    llm_health = _probe(stack, f"{stack.mock_llm_url}/health")
    mnemory_health = _probe(
        stack,
        f"{stack.mnemory_url}/health",
        {"Authorization": f"Bearer {stack.mnemory_health_token}"},
    )
    intaris_upstream = _probe(stack, f"{stack.intaris_upstream_url}/health")
    intaris_configured_proxy = _probe(stack, f"{stack.append_proxy.url}/health")
    return {
        "cognis_api": cognis_health,
        "system_diagnostics": diagnostics,
        "mock_llm": llm_health,
        "mnemory": mnemory_health,
        "intaris_upstream": intaris_upstream,
        "intaris_configured_proxy": intaris_configured_proxy,
        "intaris_process": {
            "pid": stack.intaris_process.pid,
            "command": stack.intaris_process.args,
            "returncode": stack.intaris_process.poll(),
        },
        "intaris_configured_endpoint": stack.append_proxy.url,
        "python_runtime": {
            "executable": sys.executable,
            "version": sys.version,
            "uv_python": os.environ.get("UV_PYTHON"),
            "uv_no_sync": os.environ.get("UV_NO_SYNC"),
        },
    }


def _retain_service_logs(stack: E2EStack, output_dir: Path) -> None:
    if stack.append_proxy is not None:
        (output_dir / "intaris-request-metadata.json").write_text(
            json.dumps(stack.append_proxy.request_metadata(), indent=2)
        )
    if stack.log_dir is None:
        return
    log_output = output_dir / "service-logs"
    log_output.mkdir(exist_ok=True)
    for label in ("cognis", "intaris", "mnemory", "mock-llm"):
        source = stack.log_dir / f"{label}.log"
        if source.exists():
            shutil.copyfile(source, log_output / source.name)


def test_live_ptc_browser_acceptance_uses_isolated_backend(e2e_stack: E2EStack) -> None:
    repository_root = Path(__file__).parents[2]
    ui_dir = repository_root / "ui"
    playwright = ui_dir / "node_modules" / ".bin" / "playwright"
    assert playwright.is_file(), "Install the UI lockfile dependencies with npm ci first"

    output_dir = (
        Path(
            os.environ.get(
                "COGNIS_PTC_ARTIFACT_DIR",
                str(repository_root / ".local" / "ptc-live-qualification"),
            )
        ).resolve()
        / f"browser-{uuid4().hex[:12]}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    preflight = _service_preflight(e2e_stack)
    (output_dir / "service-process-endpoints.json").write_text(
        json.dumps({"before_browser": preflight}, indent=2)
    )
    _retain_service_logs(e2e_stack, output_dir)
    required_services = (
        "cognis_api",
        "system_diagnostics",
        "mock_llm",
        "mnemory",
        "intaris_upstream",
        "intaris_configured_proxy",
    )
    failed_services: dict[str, Any] = {}
    for service in required_services:
        service_result = preflight[service]
        if not isinstance(service_result, dict) or service_result.get("status_code") != 200:
            failed_services[service] = service_result
    diagnostics = preflight["system_diagnostics"]
    if (
        isinstance(diagnostics, dict)
        and diagnostics.get("intaris_target_matches_fixture_proxy") is not True
    ):
        failed_services["cognis_intaris_configuration"] = diagnostics
    process_before = preflight["intaris_process"]
    assert isinstance(process_before, dict)
    assert not failed_services and process_before["returncode"] is None, (
        "Isolated service preflight failed before browser submission; "
        f"see {output_dir / 'service-process-endpoints.json'}: "
        f"services={failed_services}, intaris={process_before}"
    )
    environment = {
        **os.environ,
        "COGNIS_E2E_URL": e2e_stack.cognis_url,
        "MOCK_LLM_URL": e2e_stack.mock_llm_url,
        "COGNIS_PTC_INTARIS_UPSTREAM_URL": e2e_stack.intaris_upstream_url,
        "COGNIS_PTC_INTARIS_PROXY_URL": e2e_stack.append_proxy.url,
        "COGNIS_E2E_ADMIN_EMAIL": e2e_stack.admin_email,
        "COGNIS_E2E_ADMIN_PASSWORD": e2e_stack.admin_password,
    }
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = subprocess.run(
            [
                str(playwright),
                "test",
                "e2e/ptc-live-qualification.spec.ts",
                "--reporter=line",
                "--retries=0",
                f"--output={output_dir}",
            ],
            cwd=ui_dir,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=360,
        )
    finally:
        postflight = _service_preflight(e2e_stack)
        (output_dir / "service-process-endpoints.json").write_text(
            json.dumps({"before_browser": preflight, "after_browser": postflight}, indent=2)
        )
        _retain_service_logs(e2e_stack, output_dir)
    assert result is not None
    assert result.returncode == 0, (
        f"Playwright PTC acceptance failed; inspect safe artifacts under {output_dir}; "
        f"returncode={result.returncode}"
    )
