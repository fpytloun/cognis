from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api import runtime_support
from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.tool_arguments import validate_tool_arguments
from cognis.models.tool import tool_input_schema
from cognis.tools.builtin.orchestration import TASK_OUTCOME_PROPERTIES
from cognis.tools.introspection import audit_tool_descriptors


@pytest.mark.parametrize("knowledgebase_enabled", [False, True])
def test_static_startup_registry_in_fresh_interpreter(knowledgebase_enabled: bool) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cognis.api.runtime_support import build_static_registry; "
            "from cognis.tools.introspection import audit_tool_descriptors; "
            f"registry = build_static_registry(knowledgebase_enabled={knowledgebase_enabled!r}); "
            "assert registry.get('create_task') is not None; "
            "assert registry.get('update_task') is not None; "
            "assert audit_tool_descriptors(registry.list_tools()) == []",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("knowledgebase_enabled", [False, True])
async def test_shared_startup_runtime_keeps_task_contracts(
    monkeypatch: pytest.MonkeyPatch, knowledgebase_enabled: bool
) -> None:
    providers = SimpleNamespace(_session_factory=object(), executor=AsyncMock())
    monkeypatch.setattr(
        runtime_support,
        "load_executor_policy",
        AsyncMock(return_value=ExecutorPolicy(allow_in_process=False)),
    )
    runtime = await runtime_support.build_shared_runtime(
        providers, knowledgebase_enabled=knowledgebase_enabled
    )
    providers.executor.spawn.assert_not_awaited()
    assert audit_tool_descriptors(runtime.tool_registry.list_tools()) == []
    for name, required in (
        ("create_task", ["title", "description"]),
        ("update_task", ["task_id"]),
    ):
        registered = runtime.tool_registry.get(name)
        assert registered is not None
        schema = tool_input_schema(registered.definition)
        assert schema["required"] == required
        for field, expected in TASK_OUTCOME_PROPERTIES.items():
            assert schema["properties"][field] == expected
    await runtime.cleanup()


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("create_task", {"title": "Startup regression", "description": "Check task schemas"}),
        ("update_task", {"task_id": "queued-task"}),
    ],
)
def test_registered_task_outcome_argument_contract(name: str, required: dict[str, str]) -> None:
    registered = runtime_support.build_static_registry().get(name)
    assert registered is not None
    schema = tool_input_schema(registered.definition)
    arguments = {
        **required,
        "delivery_mode": "same_conversation",
        "delivery_target": None,
        "completion_mode_family": "default",
        "allow_silent_completion": False,
        "on_finish_instructions": None,
    }
    assert validate_tool_arguments(name, arguments, schema=schema) is None
    for patch in (
        {"delivery_mode": "unsupported"},
        {"completion_mode_family": "unsupported"},
        {"allow_silent_completion": "false"},
        {"on_finish_instructions": "x" * 8001},
    ):
        assert validate_tool_arguments(name, {**arguments, **patch}, schema=schema) is not None
