from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cognis.api.runtime_support import select_static_tools
from cognis.core.agent_management import TOOL_GROUP_DEFINITIONS
from cognis.core.executor_resolution import is_tool_enabled
from cognis.models.agent import AgentDefinition
from cognis.models.tool import ExecutorHandle, ToolCapability
from cognis.tools.executor.definitions import (
    OFFICE_EXECUTOR_TOOLS,
    executor_tool_definitions,
    executor_tool_handlers,
    office_executor_tool_definitions,
)
from cognis.tools.executor.officecli.install import OfficeCliRuntimeConfig, _cache_binary_path
from cognis.tools.executor.officecli.manifest import (
    OFFICECLI_ASSETS,
    OFFICECLI_CERTIFIED_VERSION,
    certified_capabilities_for_version,
    certified_tool_names,
    normalize_platform,
)


def test_office_tool_definitions_are_curated_and_registered() -> None:
    tools = {tool.name: tool for tool in OFFICE_EXECUTOR_TOOLS}
    handlers = executor_tool_handlers()
    expected = {
        "office_read",
        "office_get",
        "office_query",
        "office_validate",
        "office_render",
        "office_create",
        "office_patch",
    }

    assert expected == set(tools)
    assert expected <= set(handlers)
    for name in expected:
        assert tools[name].category == "office"
        assert tools[name].profile_group == "office"
        assert "command" not in tools[name].parameters.get("properties", {})

    static_names = {tool.name for tool in executor_tool_definitions()}
    assert expected.isdisjoint(static_names)


def test_office_mutating_tools_are_non_bypassable() -> None:
    tools = {tool.name: tool for tool in OFFICE_EXECUTOR_TOOLS}

    assert tools["office_create"].non_bypassable is True
    assert tools["office_patch"].non_bypassable is True
    assert ToolCapability.WRITE in tools["office_patch"].capabilities
    assert tools["office_read"].read_only is True


def test_office_tool_schemas_are_direct_codex_compatible() -> None:
    forbidden_top_level = {"oneOf", "anyOf", "allOf", "enum", "not"}

    for tool in OFFICE_EXECUTOR_TOOLS:
        assert tool.parameters.get("type") == "object"
        assert forbidden_top_level.isdisjoint(tool.parameters)


def test_office_query_schema_does_not_advertise_unsupported_limit() -> None:
    tool = next(tool for tool in OFFICE_EXECUTOR_TOOLS if tool.name == "office_query")

    assert "limit" not in tool.parameters["properties"]


def test_office_manifest_is_pinned_and_unknown_versions_are_not_supported() -> None:
    assert OFFICECLI_CERTIFIED_VERSION == "v1.0.102"
    assert OFFICECLI_ASSETS["linux-x64"].sha256 == (
        "d58438a2d701ec68685bb04bb043b546696b1620f141e670eaf438dae5898a66"
    )
    assert OFFICECLI_ASSETS["linux-arm64"].sha256 == (
        "dedca5682cad211df9c75886936b441475cf7840a8bd6974dcbd2278c7f1d1a1"
    )
    assert certified_capabilities_for_version("v0.0.0") is None
    assert certified_tool_names("v0.0.0") == set()
    assert "office_patch" in certified_tool_names(OFFICECLI_CERTIFIED_VERSION)


def test_office_platform_normalization_and_versioned_cache(tmp_path: Path) -> None:
    assert normalize_platform("Linux", "x86_64") == "linux-x64"
    assert normalize_platform("Linux", "aarch64") == "linux-arm64"
    assert normalize_platform("Darwin", "arm64") == "darwin-arm64"
    assert normalize_platform("Windows", "amd64") is None

    config = OfficeCliRuntimeConfig(cache_dir=tmp_path)
    cache_path = _cache_binary_path(config, "linux-x64")
    assert OFFICECLI_CERTIFIED_VERSION in cache_path.parts
    assert cache_path.name == "officecli"


def test_officecli_runtime_config_supports_executor_and_env_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.tools.executor.officecli.install import resolve_officecli_runtime_config

    config = resolve_officecli_runtime_config(
        {
            "officecli": {
                "enabled": False,
                "auto_install": False,
                "version": "v1.0.102",
                "binary_path": str(tmp_path / "officecli"),
                "cache_dir": str(tmp_path / "cache"),
            }
        }
    )

    assert config.enabled is False
    assert config.auto_install is False
    assert config.version == "v1.0.102"
    assert config.binary_path == tmp_path / "officecli"
    assert config.cache_dir == tmp_path / "cache"

    monkeypatch.setenv("COGNIS_OFFICECLI_ENABLED", "0")
    monkeypatch.setenv("COGNIS_OFFICECLI_AUTO_INSTALL", "0")
    monkeypatch.setenv("COGNIS_OFFICECLI_VERSION", "v9.9.9")
    monkeypatch.setenv("COGNIS_OFFICECLI_BINARY_PATH", str(tmp_path / "env-officecli"))
    monkeypatch.setenv("COGNIS_OFFICECLI_CACHE_DIR", str(tmp_path / "env-cache"))

    env_config = resolve_officecli_runtime_config({})

    assert env_config.enabled is False
    assert env_config.auto_install is False
    assert env_config.version == "v9.9.9"
    assert env_config.binary_path == tmp_path / "env-officecli"
    assert env_config.cache_dir == tmp_path / "env-cache"


@pytest.mark.asyncio
async def test_officecli_rejects_uncertified_requested_version() -> None:
    from cognis.tools.executor.officecli.install import ensure_officecli

    status = await ensure_officecli(OfficeCliRuntimeConfig(version="v9.9.9"))

    assert status.available is False
    assert status.version == "v9.9.9"
    assert "not certified" in (status.error or "")


@pytest.mark.asyncio
async def test_officecli_configured_binary_path_is_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.tools.executor.officecli import install as office_install
    from cognis.tools.executor.officecli.install import ensure_officecli

    binary = tmp_path / "officecli"
    binary.write_bytes(b"not the certified binary")

    async def fake_probe_version(path: Path) -> str | None:
        return OFFICECLI_CERTIFIED_VERSION if path == binary else None

    async def fake_validated_binary(path: Path, expected_sha256: str) -> Path | None:
        return None

    monkeypatch.setattr(office_install, "_probe_version", fake_probe_version)
    monkeypatch.setattr(office_install, "_validated_binary", fake_validated_binary)

    status = await ensure_officecli(OfficeCliRuntimeConfig(binary_path=binary))

    assert status.available is False
    assert status.version == OFFICECLI_CERTIFIED_VERSION
    assert "configured OfficeCLI binary" in (status.error or "")


def test_office_profile_group_enablement_alias() -> None:
    tool = next(
        tool
        for tool in office_executor_tool_definitions(
            {"officecli": {"available": True, "version": OFFICECLI_CERTIFIED_VERSION}}
        )
        if tool.name == "office_read"
    )
    assert is_tool_enabled(tool, [], ["office"]) is True
    assert is_tool_enabled(tool, [], ["document"]) is False


def test_office_tool_group_is_available_for_agent_assignment() -> None:
    groups = {group.group_id: group for group in TOOL_GROUP_DEFINITIONS}

    assert "office" in groups
    assert groups["office"].requires_executor is True
    assert groups["office"].mutating is True
    assert set(groups["office"].tool_ids) == {
        "builtin:office_read",
        "builtin:office_get",
        "builtin:office_query",
        "builtin:office_validate",
        "builtin:office_render",
        "builtin:office_create",
        "builtin:office_patch",
    }


def test_office_tool_group_selects_static_office_tools() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"builtin_tools": [], "tool_groups": ["office"]},
    )

    selected = {tool.name for tool in select_static_tools(agent) if tool.category == "office"}

    assert selected == {
        "office_read",
        "office_get",
        "office_query",
        "office_validate",
        "office_render",
        "office_create",
        "office_patch",
    }


def test_office_tools_require_certified_runtime_metadata() -> None:
    assert office_executor_tool_definitions({}) == []
    assert office_executor_tool_definitions({"officecli": {"available": False}}) == []
    assert (
        office_executor_tool_definitions({"officecli": {"available": True, "version": "v9.9.9"}})
        == []
    )
    tools = office_executor_tool_definitions(
        {"officecli": {"available": True, "version": OFFICECLI_CERTIFIED_VERSION}}
    )
    assert {tool.name for tool in tools} == certified_tool_names(OFFICECLI_CERTIFIED_VERSION)


@pytest.mark.asyncio
async def test_office_handler_uses_runtime_gating() -> None:
    from cognis.tools.executor.officecli.handlers import handle_office_read
    from cognis.tools.registry import ToolExecutionContext

    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={"officecli": {"available": False, "error": "disabled"}},
    )
    result = await handle_office_read({"source_path": __file__, "view": "text"}, context)

    assert result.is_error is True
    assert "OfficeCLI unavailable" in result.output


@pytest.mark.asyncio
async def test_office_read_maps_json_to_text_and_omits_zero_value_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.officecli.handlers import handle_office_read
    from cognis.tools.executor.officecli.runner import OfficeCliCommandResult
    from cognis.tools.registry import ToolExecutionContext

    calls: list[tuple[str, list[str], dict[str, object]]] = []

    async def fake_run(command: str, args: list[str], **kwargs: object) -> OfficeCliCommandResult:
        calls.append((command, args, kwargs))
        return OfficeCliCommandResult(
            argv=[],
            exit_code=0,
            stdout='{"success":true}',
            stderr="",
            json_data={"success": True},
        )

    monkeypatch.setattr("cognis.tools.executor.officecli.handlers.run_officecli", fake_run)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "officecli": {
                "available": True,
                "version": OFFICECLI_CERTIFIED_VERSION,
                "command": "/bin/officecli",
            }
        },
    )

    result = await handle_office_read(
        {
            "source_path": __file__,
            "view": "json",
            "start": 0,
            "end": 0,
            "limit": 0,
            "page": 0,
            "max_lines": 300,
        },
        context,
    )

    assert result.is_error is False
    assert calls == [
        (
            "view",
            [__file__, "text", "--max-lines", "300", "--json"],
            {
                "officecli_path": "/bin/officecli",
                "timeout_seconds": 60.0,
                "parse_json": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_office_read_requests_json_for_stats_and_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.officecli.handlers import handle_office_read
    from cognis.tools.executor.officecli.runner import OfficeCliCommandResult
    from cognis.tools.registry import ToolExecutionContext

    calls: list[list[str]] = []

    async def fake_run(command: str, args: list[str], **kwargs: object) -> OfficeCliCommandResult:
        assert command == "view"
        assert kwargs["parse_json"] is True
        calls.append(args)
        return OfficeCliCommandResult(
            argv=[],
            exit_code=0,
            stdout='{"success":true}',
            stderr="",
            json_data={"success": True},
        )

    monkeypatch.setattr("cognis.tools.executor.officecli.handlers.run_officecli", fake_run)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "officecli": {
                "available": True,
                "version": OFFICECLI_CERTIFIED_VERSION,
                "command": "/bin/officecli",
            }
        },
    )

    for view in ("stats", "issues"):
        result = await handle_office_read({"source_path": __file__, "view": view}, context)
        assert result.is_error is False

    assert calls == [[__file__, "stats", "--json"], [__file__, "issues", "--json"]]


@pytest.mark.asyncio
async def test_office_query_ignores_stale_unsupported_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.officecli.handlers import handle_office_query
    from cognis.tools.executor.officecli.runner import OfficeCliCommandResult
    from cognis.tools.registry import ToolExecutionContext

    calls: list[list[str]] = []

    async def fake_run(command: str, args: list[str], **kwargs: object) -> OfficeCliCommandResult:
        assert command == "query"
        calls.append(args)
        return OfficeCliCommandResult(
            argv=[],
            exit_code=0,
            stdout='{"success":true}',
            stderr="",
            json_data={"success": True},
        )

    monkeypatch.setattr("cognis.tools.executor.officecli.handlers.run_officecli", fake_run)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "officecli": {
                "available": True,
                "version": OFFICECLI_CERTIFIED_VERSION,
                "command": "/bin/officecli",
            }
        },
    )

    result = await handle_office_query(
        {"source_path": __file__, "selector": "*", "limit": 1000},
        context,
    )

    assert result.is_error is False
    assert calls == [[__file__, "*", "--json"]]


@pytest.mark.asyncio
async def test_office_create_publish_false_defaults_to_persistent_workspace_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.tools.executor.officecli.handlers import handle_office_create
    from cognis.tools.executor.officecli.runner import OfficeCliCommandResult
    from cognis.tools.registry import ToolExecutionContext

    async def fake_run(*args: object, **kwargs: object) -> OfficeCliCommandResult:
        output = Path(args[1][0])  # type: ignore[index]
        output.write_bytes(b"doc")
        return OfficeCliCommandResult(
            argv=[],
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("cognis.tools.executor.officecli.handlers.run_officecli", fake_run)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "working_directory": str(tmp_path),
            "officecli": {
                "available": True,
                "version": OFFICECLI_CERTIFIED_VERSION,
                "command": "/bin/officecli",
            },
        },
    )

    result = await handle_office_create(
        {"format": "docx", "output_filename": "created.docx", "publish_artifact": False},
        context,
    )

    assert result.is_error is False
    assert (tmp_path / "created.docx").read_bytes() == b"doc"


@pytest.mark.asyncio
async def test_office_create_format_controls_extension_when_filename_has_no_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.tools.executor.officecli.handlers import handle_office_create
    from cognis.tools.executor.officecli.runner import OfficeCliCommandResult
    from cognis.tools.registry import ToolExecutionContext

    seen_outputs: list[Path] = []

    async def fake_run(*args: object, **kwargs: object) -> OfficeCliCommandResult:
        output = Path(args[1][0])  # type: ignore[index]
        seen_outputs.append(output)
        output.write_bytes(b"xlsx")
        return OfficeCliCommandResult(
            argv=[],
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("cognis.tools.executor.officecli.handlers.run_officecli", fake_run)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "working_directory": str(tmp_path),
            "officecli": {
                "available": True,
                "version": OFFICECLI_CERTIFIED_VERSION,
                "command": "/bin/officecli",
            },
        },
    )

    result = await handle_office_create(
        {"format": "xlsx", "output_filename": "created", "publish_artifact": False},
        context,
    )

    assert result.is_error is False
    assert seen_outputs == [tmp_path / "created.xlsx"]
    assert (tmp_path / "created.xlsx").read_bytes() == b"xlsx"


@pytest.mark.asyncio
async def test_office_create_format_controls_extension_for_explicit_output_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.tools.executor.officecli.handlers import handle_office_create
    from cognis.tools.executor.officecli.runner import OfficeCliCommandResult
    from cognis.tools.registry import ToolExecutionContext

    seen_outputs: list[Path] = []

    async def fake_run(*args: object, **kwargs: object) -> OfficeCliCommandResult:
        output = Path(args[1][0])  # type: ignore[index]
        seen_outputs.append(output)
        output.write_bytes(b"pptx")
        return OfficeCliCommandResult(
            argv=[],
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("cognis.tools.executor.officecli.handlers.run_officecli", fake_run)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "working_directory": str(tmp_path),
            "officecli": {
                "available": True,
                "version": OFFICECLI_CERTIFIED_VERSION,
                "command": "/bin/officecli",
            },
        },
    )

    result = await handle_office_create(
        {"format": "pptx", "output_path": "deck", "publish_artifact": False},
        context,
    )

    assert result.is_error is False
    assert seen_outputs == [tmp_path / "deck.pptx"]
    assert (tmp_path / "deck.pptx").read_bytes() == b"pptx"


@pytest.mark.asyncio
async def test_executor_configure_exposes_office_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorConfig
    from cognis.tools.executor.officecli.install import OfficeCliStatus

    async def fake_ensure(config: Any) -> OfficeCliStatus:
        return OfficeCliStatus(
            available=False,
            enabled=config.enabled,
            auto_install=config.auto_install,
            version=config.version,
            platform_key="linux-x64",
            command=None,
            error="disabled",
        )

    monkeypatch.setattr("cognis.executor.runner.ensure_officecli", fake_ensure)
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send(self, raw: str) -> None:
            import json

            self.sent.append(json.loads(raw))

    ws = DummyWebSocket()
    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": ["office_read"],
            "enabled_tool_groups": [],
            "config": {
                "officecli": {
                    "enabled": False,
                    "auto_install": False,
                    "version": OFFICECLI_CERTIFIED_VERSION,
                }
            },
        },
    )

    metadata = ws.sent[-1]["result"]["runtime_metadata"]
    assert metadata["officecli_enabled"] is False
    assert metadata["officecli_auto_install"] is False
    assert metadata["officecli_error"] == "disabled"
