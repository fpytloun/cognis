"""Operation-level repeatability at the same-turn management boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import (
    _record_tool_outcome_in_same_turn_ledger,
    _same_turn_duplicate_tool_call_indexes,
)
from cognis.core.harness_guards import SameTurnToolCallLedger
from cognis.models.tool import ToolCall, ToolResult
from cognis.tools.builtin.mcp_management import mcp_management_tools
from cognis.tools.builtin.system_management import system_management_tools
from cognis.tools.registry import RegisteredTool, ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    for definition in [*mcp_management_tools(), *system_management_tools()]:
        registry.register(RegisteredTool(definition=definition))
    return registry


@pytest.mark.parametrize(
    ("name", "action", "repeatable"),
    [
        ("manage_executors", "get", True),
        ("manage_executors", "list", True),
        ("manage_executors", "provision_token", False),
        ("manage_mcp", "assignments_get", True),
        ("manage_mcp", "servers_get", True),
        ("manage_mcp", "servers_create", False),
        ("manage_mcp", "assignments_add", False),
        ("manage_executors", "unknown_action", False),
        ("manage_executors", None, False),
    ],
)
@pytest.mark.parametrize("uncertain", [False, True])
def test_management_guard_uses_selected_operation(
    registry: ToolRegistry, name: str, action: str | None, repeatable: bool, uncertain: bool
) -> None:
    arguments = {"executor_id": "fixture-executor"}
    if action is not None:
        arguments["action"] = action
    ledger = SameTurnToolCallLedger()
    if uncertain:
        ledger.record_uncertain(name, arguments)
    else:
        ledger.record(name, arguments)
    call = ToolCall(call_id="management-readback", name=name, arguments=arguments)

    expected = (set(), set()) if repeatable else ((set(), {0}) if uncertain else ({0}, set()))
    assert _same_turn_duplicate_tool_call_indexes(ledger, [call], registry) == expected


@pytest.mark.parametrize("action", ["get", "provision_token"])
def test_management_ledger_records_only_mutating_operations(
    registry: ToolRegistry, action: str
) -> None:
    ledger = SameTurnToolCallLedger()
    context = SimpleNamespace(tool_registry=registry, same_turn_tool_call_ledger=ledger)
    call = ToolCall(
        call_id="management-result",
        name="manage_executors",
        arguments={"action": action, "executor_id": "fixture-executor"},
    )
    _record_tool_outcome_in_same_turn_ledger(context, call, ToolResult(output="complete"))
    assert ledger.already_executed(call.name, call.arguments) is (action == "provision_token")
