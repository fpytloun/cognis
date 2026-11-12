"""Live PTC backend acceptance through the production agent/router path.

Run with:
    COGNIS_E2E_INTARIS_POLICY_MODEL=ptc-policy-model \
      uv run pytest tests/e2e/test_ptc_live_qualification.py -m e2e -q

The named policy model is deterministic test input to the real local Intaris
evaluator. Cognis agent loop, ToolRouter, in-process executor, IPython kernel,
nested memory and mutating-memory routes, Intaris policy, and Chat v2 APIs
remain live.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.e2e.conftest import E2EStack, capture_ws_events, clear_active_scenario, inject_scenario

pytestmark = pytest.mark.e2e

POLICY_MODEL = "ptc-policy-model"
SCENARIO_ID = "ptc-live-qualification"
FIRST_MESSAGE = (
    "Please run the first PTC qualification cell in the isolated local test services. "
    "Set the local sentinel and invoke one read-only memory_search for the qualification "
    "marker; do not use any external or production service. "
    "scenario:ptc-live-qualification"
)
SECOND_MESSAGE = (
    "In this same conversation, run the second PTC qualification cell and verify that "
    "the first cell's sentinel remains in the same Python kernel."
)


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))


def _items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return snapshot["timeline"]["items"]


def _program_result(item: dict[str, Any]) -> dict[str, Any]:
    preview = item.get("result_preview")
    assert isinstance(preview, str), "Missing program result preview"
    result = json.loads(preview)
    assert result["protocol_version"] == "cognis.program.v1"
    return result


def _mock_request_metadata(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep qualification evidence to request metadata, never prompt contents."""
    return [
        {
            "scenario_id": item.get("scenario_id"),
            "model": item.get("request", {}).get("model"),
            "turn_index": item.get("request", {}).get("turn_index"),
            "messages_count": item.get("request", {}).get("messages_count"),
            "stream": item.get("request", {}).get("stream"),
            "policy_model": item.get("request", {}).get("policy_model", False),
            "exposed_tool_names": item.get("request", {}).get("exposed_tool_names", []),
        }
        for item in history
    ]


def _capture_agent_tool_inventory(
    stack: E2EStack,
    executor_id: str,
    agent_id: str,
    artifact_dir: Path,
) -> dict[str, Any]:
    routes = {
        "executor": f"/api/v1/executors/{executor_id}",
        "agent": f"/api/v1/agents/{agent_id}",
        "effective_tools": f"/api/v1/agents/{agent_id}/effective-tools",
        "executor_native_tools": "/api/v1/tools/executor",
    }
    evidence_path = artifact_dir / "agent-executor-effective-tools.json"
    evidence: dict[str, Any] = {}
    for name, route in routes.items():
        response = stack.get(route)
        assert response.status_code == 200, f"{route}: {response.text}"
        evidence[name] = response.json()
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))

    effective = evidence["effective_tools"]
    evidence["tool_names"] = {
        "configured": [tool["name"] for tool in effective["configured_state"]["tools"]],
        "live": [tool["name"] for tool in effective["live_state"]["tools"]],
        "agent_builtin": list(evidence["agent"]["tools"]["builtin_tools"]),
        "executor_native": [tool["name"] for tool in evidence["executor_native_tools"]],
    }
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return evidence


def _create_qualification_conversation(
    stack: E2EStack,
    *,
    include_memory_add: bool = False,
    force_escalation: bool = False,
) -> tuple[str, str, str]:
    suffix = uuid4().hex[:12]
    executor_id = f"ptc-qualification-executor-{suffix}"
    agent_id = f"ptc-qualification-agent-{suffix}"
    builtin_tools = [
        "memory_search",
        "python_execute",
        "search_tools",
        "describe_tool",
        "call_tool",
    ]
    tool_permissions = {
        "python_execute": "evaluate",
        "memory_search": "evaluate",
        "search_tools": "evaluate",
        "describe_tool": "evaluate",
        "call_tool": "evaluate",
    }
    if include_memory_add:
        builtin_tools.append("memory_add")
        tool_permissions["memory_add"] = "evaluate"
    capabilities = {
        "memory_backend": "mnemory",
        "guardrails_backend": "intaris",
    }
    if force_escalation:
        capabilities["minimum_outcome"] = "escalate"
    executor = stack.post(
        "/api/v1/executors",
        json={
            "executor_id": executor_id,
            "name": "PTC live qualification executor",
            "executor_type": "in_process",
            "labels": {"role": "e2e-ptc-qualification"},
            "enabled_tool_groups": [],
            "enabled_tools": ["python_execute"],
            "is_default": False,
            "config": {"program_runtime": {"host_enabled": True}},
        },
    )
    assert executor.status_code == 201, executor.text

    agent = stack.post(
        "/api/v1/agents",
        json={
            "agent_id": agent_id,
            "name": "PTC live qualification agent",
            "display_name": "PTC live qualification agent",
            "description": "Isolated PTC test agent using normal Intaris guardrails.",
            "system_prompt": (
                "You are a deterministic local qualification agent. Follow the user's "
                "scoped test request and use only the requested Python cells."
            ),
            "capabilities": capabilities,
            "tools": {
                "builtin_tools": [
                    *builtin_tools,
                ],
                "tool_groups": [],
            },
            "permissions": {
                "tool_permissions": {
                    **tool_permissions,
                },
                "can_delegate": False,
            },
            "llm_config": {
                "provider_id": "e2e-mock-llm",
                "model": "mock-model",
            },
            "execution": {"executor_id": executor_id},
            "agent_type": "primary",
            "status": "draft",
        },
    )
    assert agent.status_code == 200, agent.text
    activation = stack.post(f"/api/v1/agents/{agent_id}/activate")
    assert activation.status_code == 200, activation.text
    agent_read = stack.get(f"/api/v1/agents/{agent_id}")
    assert agent_read.status_code == 200, agent_read.text
    assert agent_read.json()["capabilities"]["guardrails_backend"] == "intaris"

    conversation = stack.post(
        "/api/v1/conversations",
        json={
            "agent_id": agent_id,
            "title": "PTC live qualification",
            "context": {
                "type": "web",
                "ref": None,
                "platform_data": {},
                "memory_labels": {},
            },
        },
    )
    assert conversation.status_code == 200, conversation.text
    return executor_id, agent_id, conversation.json()["conversation_id"]


def _assert_cell_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_parent_count: int,
    expected_child_count: int,
    first_generation: str | int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    timeline = _items(snapshot)
    ids = [item["id"] for item in timeline]
    assert len(ids) == len(set(ids)), "Canonical terminal snapshot contains duplicate IDs"
    parents = [
        item
        for item in timeline
        if item.get("kind") == "tool_call" and item.get("tool_name") == "python_execute"
    ]
    children = [
        item for item in timeline if item.get("kind") == "tool_call" and item.get("parent_call_id")
    ]
    assert len(parents) == expected_parent_count, "Unexpected Python parent-call count"
    assert len(children) == expected_child_count, "Unexpected nested-tool-call count"
    assert all(parent["status"] == "complete" for parent in parents), (
        "Incomplete Python parent call"
    )
    assert all(child["status"] == "complete" for child in children), "Incomplete nested tool call"

    program_results = {parent["call_id"]: _program_result(parent) for parent in parents}
    for parent in parents:
        program = program_results[parent["call_id"]]
        assert program["program_id"] == parent["call_id"]
        assert program["status"] == "completed"
        assert program["kernel_generation"] is not None
        assert parent.get("evaluation", {}).get("decision") == "approve", (
            "Intaris did not approve the scoped test action"
        )
    if children:
        child = children[0]
        parent = next(parent for parent in parents if parent["call_id"] == child["parent_call_id"])
        program = program_results[parent["call_id"]]
        assert child["tool_name"] == "memory_search"
        assert child["program_id"] == program["program_id"]
        assert child["kernel_generation"] == program["kernel_generation"]
        assert timeline.index(parent) < timeline.index(child)
    if first_generation is not None:
        assert program_results[parents[0]["call_id"]]["kernel_generation"] == first_generation
        assert program_results[parents[1]["call_id"]]["kernel_generation"] == first_generation
    return parents[-1], children


def test_live_ptc_backend_uses_intaris_and_reconciles_two_cells(
    e2e_stack: E2EStack,
) -> None:
    if os.environ.get("COGNIS_E2E_INTARIS_POLICY_MODEL") != POLICY_MODEL:
        pytest.skip(
            "opt in with COGNIS_E2E_INTARIS_POLICY_MODEL=ptc-policy-model; "
            "the existing E2E guardrails default remains unchanged"
        )

    stack = e2e_stack
    run_id = uuid4().hex[:12]
    artifact_dir = (
        Path(os.environ.get("COGNIS_PTC_ARTIFACT_DIR", ".local/ptc-live-qualification")) / run_id
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    executor_id, agent_id, conversation_id = _create_qualification_conversation(stack)
    try:
        inventory = _capture_agent_tool_inventory(stack, executor_id, agent_id, artifact_dir)
        # call_tool is the controller's discovery bridge, not a configured
        # native tool; it must appear in the live model request after search.
        expected_tools = {"python_execute", "search_tools", "describe_tool"}
        assert expected_tools <= set(inventory["tool_names"]["configured"]), inventory["tool_names"]
        if os.environ.get("COGNIS_PTC_PREFLIGHT_ONLY") == "1":
            pytest.skip("effective agent/executor tool inventory retained before cell submission")
        inject_scenario(stack.mock_llm_url, SCENARIO_ID)
        approval_tools = {
            "python_execute",
            "memory_search",
            "search_tools",
            "describe_tool",
            "call_tool",
        }
        first_resolutions: list[dict[str, str]] = []

        def resolve_first_approvals() -> None:
            first_resolutions.extend(
                _resolve_pending_live_ptc_approvals(
                    stack, conversation_id, allowed_tools=approval_tools
                )
            )

        first_events = capture_ws_events(
            stack,
            conversation_id,
            FIRST_MESSAGE,
            approval_poller=resolve_first_approvals,
        )
        _write_jsonl(artifact_dir / "backend-cell-1-chat-v2.jsonl", first_events)
        (artifact_dir / "backend-cell-1-approval-resolutions.json").write_text(
            json.dumps(first_resolutions, ensure_ascii=False, indent=2)
        )
        first_response = stack.get(f"/api/v1/chat/v2/conversations/{conversation_id}/snapshot")
        assert first_response.status_code == 200, first_response.text
        first_snapshot = first_response.json()
        first_parent, _ = _assert_cell_snapshot(
            first_snapshot,
            expected_parent_count=1,
            expected_child_count=1,
        )
        first_generation = _program_result(first_parent)["kernel_generation"]
        first_parent = next(
            item
            for item in _items(first_snapshot)
            if item.get("kind") == "tool_call" and item.get("tool_name") == "python_execute"
        )

        second_resolutions: list[dict[str, str]] = []

        def resolve_second_approvals() -> None:
            second_resolutions.extend(
                _resolve_pending_live_ptc_approvals(
                    stack, conversation_id, allowed_tools=approval_tools
                )
            )

        second_events = capture_ws_events(
            stack,
            conversation_id,
            SECOND_MESSAGE,
            approval_poller=resolve_second_approvals,
        )
        _write_jsonl(artifact_dir / "backend-cell-2-chat-v2.jsonl", second_events)
        (artifact_dir / "backend-cell-2-approval-resolutions.json").write_text(
            json.dumps(second_resolutions, ensure_ascii=False, indent=2)
        )
        final_response = stack.get(f"/api/v1/chat/v2/conversations/{conversation_id}/snapshot")
        assert final_response.status_code == 200, final_response.text
        final_snapshot = final_response.json()
        second_parent, children = _assert_cell_snapshot(
            final_snapshot,
            expected_parent_count=2,
            expected_child_count=1,
            first_generation=first_generation,
        )
        assert second_parent["call_id"] != first_parent["call_id"]
        second_program = _program_result(second_parent)
        assert second_program["result"]["persisted_sentinel"] == "ptc-live-qualification-v1"

        # capture_ws_events exercises a fresh WebSocket subscription, native
        # snapshot, server-owned generation reset, sync reconciliation, and
        # recovery snapshot after each terminal turn.
        terminal_snapshots = [
            event
            for event in second_events
            if event.get("type") == "snapshot" and event.get("timeline", {}).get("items")
        ]
        assert terminal_snapshots, "No recovered terminal Chat v2 snapshot captured"
        recovered = terminal_snapshots[-1]
        _assert_cell_snapshot(
            recovered,
            expected_parent_count=2,
            expected_child_count=1,
            first_generation=first_generation,
        )

        history_response = stack.http.get(f"{stack.mock_llm_url}/__mock/history?limit=50")
        assert history_response.status_code == 200, history_response.text
        model_history = history_response.json()["history"]
        scenario_requests = [
            item["request"] for item in model_history if item.get("scenario_id") == SCENARIO_ID
        ]
        assert len(scenario_requests) >= 6, "Mock LLM scenario did not complete all expected turns"
        assert "search_tools" in scenario_requests[0]["exposed_tool_names"]
        assert "describe_tool" in scenario_requests[1]["exposed_tool_names"]
        assert "call_tool" in scenario_requests[2]["exposed_tool_names"]
        policy_requests = [
            item["request"]
            for item in model_history
            if item["request"].get("model") == POLICY_MODEL
        ]
        assert len(policy_requests) >= 2, "Intaris did not invoke the test policy model"
        (artifact_dir / "mock-llm-history.json").write_text(
            json.dumps(_mock_request_metadata(model_history), ensure_ascii=False, indent=2)
        )
        (artifact_dir / "terminal-chat-v2-snapshot.json").write_text(
            json.dumps(final_snapshot, ensure_ascii=False, indent=2)
        )
        (artifact_dir / "qualification-summary.json").write_text(
            json.dumps(
                {
                    "scenario": SCENARIO_ID,
                    "policy_model": POLICY_MODEL,
                    "guardrails_backend": "intaris",
                    "executor_type": "in_process",
                    "python_runtime_opt_in": True,
                    "effective_tool_names": inventory["tool_names"],
                    "parent_call_ids": [
                        parent["call_id"]
                        for parent in _items(final_snapshot)
                        if parent.get("tool_name") == "python_execute"
                    ],
                    "nested_call_ids": [child["call_id"] for child in children],
                    "kernel_generation": first_generation,
                    "policy_model_calls": len(policy_requests),
                    "approval_resolutions": first_resolutions + second_resolutions,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        clear_active_scenario(stack.mock_llm_url)
        for resource_path in (
            f"/api/v1/conversations/{conversation_id}",
            f"/api/v1/agents/{agent_id}",
            f"/api/v1/executors/{executor_id}",
        ):
            stack.http.delete(
                f"{stack.cognis_url}{resource_path}",
                headers=stack.admin_headers(),
            )
        if stack.log_dir is not None:
            retained_logs = artifact_dir / "service-logs"
            retained_logs.mkdir(exist_ok=True)
            for name in ("cognis", "intaris", "mnemory", "mock-llm"):
                source = stack.log_dir / f"{name}.log"
                if source.exists():
                    shutil.copyfile(source, retained_logs / source.name)


def _resolve_pending_live_ptc_approvals(
    stack: E2EStack,
    conversation_id: str,
    *,
    allowed_tools: set[str],
) -> list[dict[str, str]]:
    """Approve only pending notifications for this isolated PTC test."""
    resolutions: list[dict[str, str]] = []
    notifications_response = stack.get(
        "/api/v1/notifications", params={"conversation_id": conversation_id}
    )
    assert notifications_response.status_code == 200, notifications_response.text
    for notification in notifications_response.json():
        if notification.get("status") != "pending":
            continue
        payload = notification.get("payload", {})
        tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
        if tool_name not in allowed_tools:
            raise AssertionError(f"Unexpected approval in isolated PTC test: {tool_name!r}")
        notification_id = notification.get("notification_id")
        assert isinstance(notification_id, str) and notification_id
        resolved = stack.post(
            f"/api/v1/notifications/{notification_id}/resolve",
            json={
                "decision": "approve",
                "note": "Approved the isolated ephemeral PTC qualification test.",
            },
        )
        assert resolved.status_code == 200, resolved.text
        resolution = resolved.json()
        assert resolution == {
            "ok": True,
            "notification_id": notification_id,
            "decision": "approve",
        }
        notification_type = notification.get("notification_type")
        assert isinstance(notification_type, str) and notification_type
        resolutions.append(
            {
                "notification_id": notification_id,
                "tool_name": tool_name,
                "decision": "approve",
                "notification_type": notification_type,
            }
        )
    return resolutions


def _resolve_live_ptc_approvals(
    stack: E2EStack,
    conversation_id: str,
    *,
    allowed_tools: set[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Resolve pending approvals until this isolated PTC turn is terminal."""
    deadline = time.monotonic() + 90
    resolutions: list[dict[str, str]] = []
    snapshot_url = f"/api/v1/chat/v2/conversations/{conversation_id}/snapshot"
    while time.monotonic() < deadline:
        resolutions.extend(
            _resolve_pending_live_ptc_approvals(stack, conversation_id, allowed_tools=allowed_tools)
        )

        snapshot_response = stack.get(snapshot_url)
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        active = snapshot.get("runtime", {}).get("has_active_turn", False) or snapshot.get(
            "state", {}
        ).get("active_turn", {}).get("has_active_turn", False)
        if not active:
            return snapshot, resolutions
        time.sleep(0.1)
    raise AssertionError("PTC mutation turn did not finish after live approval resolution")


def _matching_content_rows(value: Any, expected: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("memory") == expected:
            matches.append(value)
        for child in value.values():
            matches.extend(_matching_content_rows(child, expected))
    elif isinstance(value, list):
        for child in value:
            matches.extend(_matching_content_rows(child, expected))
    return matches


def test_live_ptc_nested_memory_mutation_uses_normal_governance_and_persists_once(
    e2e_stack: E2EStack,
) -> None:
    if os.environ.get("COGNIS_E2E_INTARIS_POLICY_MODEL") != POLICY_MODEL:
        pytest.skip(
            "opt in with COGNIS_E2E_INTARIS_POLICY_MODEL=ptc-policy-model; "
            "the existing E2E guardrails default remains unchanged"
        )
    stack = e2e_stack
    run_id = uuid4().hex[:12]
    artifact_dir = (
        Path(os.environ.get("COGNIS_PTC_ARTIFACT_DIR", ".local/ptc-live-qualification"))
        / f"mutation-{run_id}"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    executor_id, agent_id, conversation_id = _create_qualification_conversation(
        stack, include_memory_add=True, force_escalation=True
    )
    try:
        inventory = _capture_agent_tool_inventory(stack, executor_id, agent_id, artifact_dir)
        assert "memory_add" in inventory["tool_names"]["configured"]
        if os.environ.get("COGNIS_PTC_PREFLIGHT_ONLY") == "1":
            pytest.skip("effective mutation tool inventory retained before cell submission")
        inject_scenario(stack.mock_llm_url, "ptc-live-mutation")
        events = capture_ws_events(
            stack,
            conversation_id,
            (
                "Run PTC-MUTATION-01 with python_execute in the isolated local test services. "
                "Explicitly add exactly one persistent memory with infer=False through the "
                "nested memory_add tool, then verify it once with memory_search. "
                "Do not use external or production services. "
                "scenario:ptc-live-mutation"
            ),
            timeout=8,
            capture_reconnect_snapshot=False,
        )
        _write_jsonl(artifact_dir / "mutation-chat-v2.jsonl", events)
        snapshot, resolutions = _resolve_live_ptc_approvals(
            stack,
            conversation_id,
            allowed_tools={
                "python_execute",
                "memory_add",
                "memory_search",
                "search_tools",
                "describe_tool",
                "call_tool",
            },
        )
        (artifact_dir / "mutation-terminal-snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2)
        )
        (artifact_dir / "mutation-approval-resolutions.json").write_text(
            json.dumps(resolutions, ensure_ascii=False, indent=2)
        )
        timeline = _items(snapshot)
        ids = [item["id"] for item in timeline]
        assert len(ids) == len(set(ids)), "Canonical terminal snapshot contains duplicate IDs"
        parents = [
            item
            for item in timeline
            if item.get("kind") == "tool_call" and item.get("tool_name") == "python_execute"
        ]
        children = [
            item
            for item in timeline
            if item.get("kind") == "tool_call" and item.get("parent_call_id")
        ]
        assert len(parents) == 1
        assert len(children) == 2
        parent = parents[0]
        add_child, search_child = children
        assert parent["status"] == add_child["status"] == search_child["status"] == "complete"
        assert [add_child["tool_name"], search_child["tool_name"]] == [
            "memory_add",
            "memory_search",
        ]
        assert add_child["parent_call_id"] == search_child["parent_call_id"] == parent["call_id"]
        approved_tools = {
            resolution["tool_name"]
            for resolution in resolutions
            if resolution["decision"] == "approve"
        }
        assert {"python_execute", "memory_add", "memory_search"} <= approved_tools
        notification_ids = [resolution["notification_id"] for resolution in resolutions]
        assert len(notification_ids) == len(set(notification_ids))
        program = _program_result(parent)
        assert program["status"] == "completed"
        assert program["program_id"] == parent["call_id"]
        assert add_child["program_id"] == search_child["program_id"] == program["program_id"]
        assert add_child["call_id"] != search_child["call_id"]
        expected_content = "PTC-MUTATION-01 ephemeral nested memory"
        assert program["result"]["content"] == expected_content
        matches = _matching_content_rows(program["result"]["verified"], expected_content)
        assert len(matches) == 1, f"Mnemory persisted {len(matches)} copies instead of one"
        (artifact_dir / "mutation-summary.json").write_text(
            json.dumps(
                {
                    "parent_call_id": parent["call_id"],
                    "child_call_ids": [add_child["call_id"], search_child["call_id"]],
                    "memory_content": expected_content,
                    "persisted_match_count": len(matches),
                    "separate_approval_tool_names": sorted(
                        {"python_execute", "memory_add", "memory_search"}
                    ),
                    "approval_resolutions": resolutions,
                    "guardrails_backend": "intaris",
                    "executor_type": "in_process",
                    "memory_backend": "mnemory",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        clear_active_scenario(stack.mock_llm_url)
        for resource_path in (
            f"/api/v1/conversations/{conversation_id}",
            f"/api/v1/agents/{agent_id}",
            f"/api/v1/executors/{executor_id}",
        ):
            stack.http.delete(
                f"{stack.cognis_url}{resource_path}",
                headers=stack.admin_headers(),
            )
        if stack.log_dir is not None:
            retained_logs = artifact_dir / "service-logs"
            retained_logs.mkdir(exist_ok=True)
            for name in ("cognis", "intaris", "mnemory", "mock-llm"):
                source = stack.log_dir / f"{name}.log"
                if source.exists():
                    shutil.copyfile(source, retained_logs / source.name)
