"""Focused tests for the standalone executor capability contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.api import executor_ws
from cognis.api.routes import executors as executor_routes
from cognis.executor.runner import ExecutorRunner
from cognis.executor.runtime_capability_probe import collect_runtime_capabilities
from cognis.models.runtime_capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    RuntimeCapabilityReport,
    normalize_runtime_capability_report,
)
from cognis.tools.executor.browser.handlers import build_manager_from_config
from cognis.tools.executor.browser.manager import BrowserManager


def test_report_is_bounded_and_unknown_schema_is_non_fatal() -> None:
    report = RuntimeCapabilityReport.unknown()
    assert report.schema_version == CAPABILITY_SCHEMA_VERSION
    assert normalize_runtime_capability_report(report.model_dump(mode="json")) is not None
    assert normalize_runtime_capability_report({"schema_version": 999}) is None
    assert normalize_runtime_capability_report({"schema_version": 1, "message": "bad"}) is None


def test_image_variant_is_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_EXECUTOR_VARIANT", "development")
    assert collect_runtime_capabilities().image_variant == "development"
    monkeypatch.setenv("COGNIS_EXECUTOR_VARIANT", "secret-image")
    assert collect_runtime_capabilities().image_variant is None


def test_probes_are_allowlisted_bounded_and_non_mutating(monkeypatch) -> None:
    commands: list[str] = []

    def fake_which(command: str) -> str:
        commands.append(command)
        return "/private/path/" + command

    def fake_run(args, **kwargs):
        commands.append(args[0])
        assert args[0] in {"git", "node", "uv", "officecli"}
        assert args[1:] == ("--version",)
        assert kwargs["timeout"] <= 1
        return SimpleNamespace(stdout="tool 1.2.3", stderr="", returncode=0)

    monkeypatch.setattr("cognis.executor.runtime_capability_probe.shutil.which", fake_which)
    monkeypatch.setattr("cognis.executor.runtime_capability_probe.subprocess.run", fake_run)
    report = collect_runtime_capabilities()
    serialized = json.dumps(report.model_dump(mode="json"))
    assert "/private/path" not in serialized
    assert "tool 1.2.3" not in serialized
    assert "rm" not in commands


def test_missing_optional_packages_are_installable_or_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "cognis.executor.runtime_capability_probe.importlib.util.find_spec",
        lambda _name: None,
    )
    report = collect_runtime_capabilities()
    assert report.browser.runtimes["patchright"].state == "installable"
    assert report.components["browser"].state == "installable"
    assert report.mcp_launch.state == "installable"


def test_tool_axes_remain_distinct_and_exclude_mcp_names() -> None:
    configured = SimpleNamespace(name="configured_tool", source=SimpleNamespace(type="executor"))
    remote = SimpleNamespace(name="remote_tool", source=SimpleNamespace(type="local_mcp"))
    report = collect_runtime_capabilities(
        desired_tools=["configured_tool", "not_loaded"],
        observed_tools=[configured, remote],
    )
    assert report.desired_tools == ["configured_tool", "not_loaded"]
    assert report.observed_tools == ["configured_tool"]
    assert "configured_tool" in report.supported_tools
    assert "not_loaded" not in report.supported_tools
    assert "remote_tool" not in report.supported_tools


def test_officecli_capability_uses_resolved_certified_runtime() -> None:
    report = collect_runtime_capabilities(
        runtime_metadata={
            "officecli": {
                "available": True,
                "enabled": True,
                "version": "v1.0.102",
                "command": "/private/cache/officecli",
                "installed_from": "cache",
            }
        }
    )

    assert report.officecli.state == "ready"
    assert report.officecli.version == "v1.0.102"
    assert report.components["officecli"] == report.officecli
    assert "/private/cache" not in json.dumps(report.model_dump(mode="json"))


def test_officecli_capability_reports_resolver_failure_consistently() -> None:
    report = collect_runtime_capabilities(
        runtime_metadata={
            "officecli": {
                "available": False,
                "enabled": True,
                "version": None,
                "error": "install failed for https://user:secret@example.com/private",
            }
        }
    )

    assert report.officecli.state == "unavailable"
    assert report.officecli.reason_code == "runtime_unavailable"
    assert report.components["officecli"] == report.officecli
    assert "secret" not in json.dumps(report.model_dump(mode="json"))


def test_ready_metadata_preserves_absent_report_and_marks_malformed_unknown() -> None:
    report = RuntimeCapabilityReport.unknown()
    row = SimpleNamespace(
        runtime_metadata={"observed_capabilities": report.model_dump(mode="json")}
    )
    received_at = datetime.now(UTC)
    retained = executor_ws._ready_runtime_metadata(
        row,
        environment=None,
        platform=None,
        resource_snapshot=None,
        received_at=received_at,
    )
    assert retained["observed_capabilities"]["schema_version"] == CAPABILITY_SCHEMA_VERSION
    unknown = executor_ws._ready_runtime_metadata(
        row,
        environment=None,
        platform=None,
        resource_snapshot=None,
        observed_capabilities={"schema_version": 999},
        received_at=received_at,
    )
    assert "observed_capabilities" not in unknown
    assert unknown["observed_capabilities_state"] == "unknown"


def test_api_response_has_observed_tools_and_typed_capabilities() -> None:
    report = RuntimeCapabilityReport.unknown()
    row = SimpleNamespace(
        executor_id="executor-1",
        name="Executor",
        executor_type="websocket",
        labels={},
        enabled_tools=[],
        enabled_tool_groups=[],
        config={},
        status="active",
        runtime_state="active",
        desired_config_version=1,
        applied_config_version=1,
        runtime_metadata={"observed_capabilities": report.model_dump(mode="json")},
        observed_tools=[{"name": "read"}],
        last_observed_at=None,
        is_default=False,
        owner_email="user@example.com",
        created_at=None,
        updated_at=None,
    )
    response = executor_routes._executor_to_response(row)
    assert response.observed_tools == [{"name": "read"}]
    assert isinstance(response.observed_capabilities, RuntimeCapabilityReport)


def test_browser_defaults_and_explicit_legacy_values() -> None:
    default = build_manager_from_config({"browser": {}})
    assert (default.runtime, default.engine, default.channel, default.stealth_enabled) == (
        "patchright",
        "chromium",
        "chrome",
        True,
    )
    legacy = build_manager_from_config(
        {
            "browser": {
                "runtime": "playwright",
                "engine": "firefox",
                "channel": "msedge",
                "stealth_enabled": False,
            }
        }
    )
    assert (legacy.runtime, legacy.engine, legacy.channel, legacy.stealth_enabled) == (
        "playwright",
        "firefox",
        "msedge",
        False,
    )


def test_patchright_does_not_create_playwright_stealth() -> None:
    manager = BrowserManager(runtime="patchright", stealth_enabled=True)
    assert manager._build_stealth() is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_runner_configure_and_ready_payloads_include_capabilities() -> None:
    class WebSocket:
        def __init__(self) -> None:
            self.sent = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    from cognis.models.tool import ExecutorConfig

    runner = ExecutorRunner(ExecutorConfig(executor_id="executor-1", controller_token="token"))
    ws = WebSocket()
    await runner._handle_configure(  # noqa: SLF001
        ws,
        "configure-1",
        {"enabled_tools": ["read"], "enabled_tool_groups": [], "config": {}},
    )
    configure_result = ws.sent[-1]["result"]
    assert configure_result["observed_capabilities"]["schema_version"] == CAPABILITY_SCHEMA_VERSION
    assert "observed_capabilities" in configure_result["runtime_metadata"]
    ready = runner._build_ready_params(None)  # noqa: SLF001
    assert ready["observed_capabilities"]["schema_version"] == CAPABILITY_SCHEMA_VERSION
