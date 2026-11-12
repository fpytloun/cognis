from __future__ import annotations

from cognis.core.program_config import (
    PROGRAM_RUNTIME_PROFILE_LABEL,
    program_runtime_executor_enabled,
)
from cognis.core.program_plan import build_effective_program_plan
from cognis.tools.builtin.artifact_tools import ARTIFACT_WRITE_TOOL
from cognis.tools.executor.definitions import (
    BASH_TOOL,
    PYTHON_EXECUTE_TOOL,
    WEB_RESEARCH_TOOL,
    WEB_SEARCH_TOOL,
)
from cognis.tools.registry import RegisteredTool, ToolRegistry


def test_program_runtime_is_default_eligible_on_pods_and_explicit_on_hosts() -> None:
    assert program_runtime_executor_enabled(
        {
            "executor_id": "local-compose-executor",
            "executor_type": "websocket",
            "labels": {PROGRAM_RUNTIME_PROFILE_LABEL: "pod"},
            "config": {},
        }
    )
    assert not program_runtime_executor_enabled({"executor_type": "websocket", "config": {}})
    assert not program_runtime_executor_enabled(
        {
            "executor_id": "local-compose-executor",
            "executor_type": "websocket",
            "labels": {PROGRAM_RUNTIME_PROFILE_LABEL: "pod"},
            "config": {"program_runtime": {"enabled": False}},
        }
    )
    assert program_runtime_executor_enabled(
        {"executor_type": "websocket", "config": {"program_runtime": {"host_enabled": True}}}
    )
    assert not program_runtime_executor_enabled({"executor_type": "in_process", "config": {}})
    assert program_runtime_executor_enabled(
        {"executor_type": "subprocess", "config": {"program_runtime": {"host_enabled": True}}}
    )
    assert not program_runtime_executor_enabled(
        {"executor_type": "subprocess", "config": {"program_runtime": {"host_enabled": False}}}
    )


def test_program_plan_allows_governed_mutation_and_preserves_read_only_policy() -> None:
    registry = ToolRegistry()
    for definition in (
        WEB_SEARCH_TOOL,
        WEB_RESEARCH_TOOL,
        BASH_TOOL,
        PYTHON_EXECUTE_TOOL,
        ARTIFACT_WRITE_TOOL,
    ):
        registry.register(RegisteredTool(definition=definition))

    plan = build_effective_program_plan(registry)
    names = {tool.name for tool in plan.tools}

    assert names == {
        BASH_TOOL.name,
        WEB_SEARCH_TOOL.name,
        WEB_RESEARCH_TOOL.name,
        ARTIFACT_WRITE_TOOL.name,
    }
    assert (
        next(tool for tool in plan.tools if tool.name == ARTIFACT_WRITE_TOOL.name).read_only
        is False
    )
    assert PYTHON_EXECUTE_TOOL.name not in names
    assert BASH_TOOL.name in names

    read_only_plan = build_effective_program_plan(registry, read_only_required=True)
    assert ARTIFACT_WRITE_TOOL.name not in {tool.name for tool in read_only_plan.tools}
    assert BASH_TOOL.name not in {tool.name for tool in read_only_plan.tools}

    workflow_plan = build_effective_program_plan(
        registry,
        read_only_required=True,
        exclude_model_invoking=True,
    )
    workflow_names = {tool.name for tool in workflow_plan.tools}
    assert WEB_SEARCH_TOOL.name in workflow_names
    assert WEB_RESEARCH_TOOL.name not in workflow_names

    allowlisted_plan = build_effective_program_plan(
        registry,
        exclude_model_invoking=True,
        allowed_tool_ids=[WEB_SEARCH_TOOL.name],
    )
    assert [tool.name for tool in allowlisted_plan.tools] == [WEB_SEARCH_TOOL.name]
    assert allowlisted_plan.allowed_tool_ids == [WEB_SEARCH_TOOL.name]

    empty_plan = build_effective_program_plan(
        registry,
        exclude_model_invoking=True,
        allowed_tool_ids=[],
    )
    assert empty_plan.tools == []
