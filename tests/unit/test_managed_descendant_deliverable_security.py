from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from cognis.core.tool_router import ToolRouter
from cognis.models.tool import (
    NativeToolDefinition as ToolDefinition,
)
from cognis.models.tool import Permission, ToolCall, ToolResult, ToolSource
from cognis.store.models import Agent, ManagedConversationLink
from cognis.store.queries import create_conversation, create_deliverable
from cognis.tools.builtin.artifact_tools import handle_artifact_tool
from cognis.tools.builtin.task_continuation import build_task_continuation_tool_handlers
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_artifact_virtual_deliverable_refs import (
    _managed_metadata,
    _seed_managed_deliverables,
)
from tests.unit.test_task_continuation_tools import _context
from tests.unit.test_tool_router import _agent, _Guardrails, _RemoteExecutor, _session

pytest_plugins = ("tests.unit.test_task_continuation_tools",)

_READ_SURFACES = (
    "read_task_deliverable",
    "artifact_read",
    "artifact_get_metadata",
    "artifact_get_url",
)
_EXECUTOR_SURFACES = ("artifact_save", "document_generate")
_ALL_SURFACES = _READ_SURFACES + _EXECUTOR_SURFACES


@dataclass(frozen=True)
class _AccessCase:
    name: str
    deliverable_id: str
    user_email: str
    conversation_id: str
    agent_id: str
    lineage_mutation: str | None = None


_DENIED_CASES = (
    _AccessCase(
        "sibling_controller",
        "dlv_sibling_child",
        "owner@example.com",
        "conv-child",
        "agent-child",
    ),
    _AccessCase(
        "sibling_child",
        "dlv_child",
        "owner@example.com",
        "conv-sibling",
        "agent-sibling",
    ),
    _AccessCase(
        "unrelated_branch",
        "dlv_child",
        "owner@example.com",
        "conv-branch-controller",
        "agent-branch-controller",
    ),
    _AccessCase(
        "malformed_lineage",
        "dlv_grandchild",
        "owner@example.com",
        "conv-controller",
        "agent-owner",
        "malformed",
    ),
    _AccessCase(
        "cyclic_lineage",
        "dlv_grandchild",
        "owner@example.com",
        "conv-controller",
        "agent-owner",
        "cycle",
    ),
    _AccessCase(
        "over_depth_lineage",
        "dlv_great_grandchild",
        "owner@example.com",
        "conv-controller",
        "agent-owner",
        "over_depth",
    ),
    _AccessCase(
        "mismatched_controller_agent",
        "dlv_child",
        "owner@example.com",
        "conv-controller",
        "agent-child",
    ),
    _AccessCase(
        "mismatched_controller_conversation",
        "dlv_child",
        "owner@example.com",
        "conv-child",
        "agent-owner",
    ),
    _AccessCase(
        "mismatched_user",
        "dlv_child",
        "other@example.com",
        "conv-controller",
        "agent-owner",
    ),
)


async def _prepare_case(factory, case: _AccessCase) -> None:
    await _seed_managed_deliverables(factory)
    if case.lineage_mutation is None:
        return
    async with factory() as session:
        grandchild = (
            await session.execute(
                select(ManagedConversationLink).where(
                    ManagedConversationLink.target_conversation_id == "conv-grandchild"
                )
            )
        ).scalar_one()
        if case.lineage_mutation == "malformed":
            grandchild.controller_conversation_id = "conv-unrelated"
        elif case.lineage_mutation == "cycle":
            grandchild.parent_link_id = grandchild.link_id
        else:
            session.add(
                Agent(
                    agent_id="agent-great-grandchild",
                    owner_email="owner@example.com",
                    name="Great grandchild",
                )
            )
            await session.flush()
            await create_conversation(
                session,
                "owner@example.com",
                "agent-great-grandchild",
                "agent_work",
                conversation_id="conv-great-grandchild",
            )
            session.add(
                ManagedConversationLink(
                    link_id="mconv_over_depth",
                    user_email="owner@example.com",
                    controller_agent_id="agent-grandchild",
                    controller_conversation_id="conv-grandchild",
                    controller_session_id="sess-grandchild",
                    target_agent_id="agent-great-grandchild",
                    target_conversation_id="conv-great-grandchild",
                    target_session_id="sess-great-grandchild",
                    title="Great grandchild",
                    parent_link_id=grandchild.link_id,
                    root_link_id=grandchild.root_link_id,
                    depth=3,
                )
            )
            await session.flush()
            await create_deliverable(
                session,
                deliverable_id="dlv_great_grandchild",
                conversation_id="conv-great-grandchild",
                turn_id="turn-great-grandchild",
                content="Over-depth deliverable",
                title="Over-depth result",
                artifact_store=factory.artifact_store,
            )
        await session.commit()


def _executor_tool(surface: str) -> tuple[ToolRegistry, ToolCall]:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=surface,
                description=surface,
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )
    arguments = {"source_artifact_id": "unused"}
    if surface == "artifact_save":
        arguments["file_path"] = "/tmp/managed-deliverable.md"
    return registry, ToolCall(call_id=f"security-{surface}", name=surface, arguments=arguments)


async def _invoke_surface(factory, surface: str, case: _AccessCase):
    if surface == "read_task_deliverable":
        handlers = build_task_continuation_tool_handlers(factory)
        return (
            await handlers[surface](
                {"deliverable_id": case.deliverable_id},
                _context(
                    case.user_email,
                    artifact_store=factory.artifact_store,
                    conversation_id=case.conversation_id,
                    agent_id=case.agent_id,
                    use_runtime_access=True,
                ),
            ),
            None,
        )
    if surface in {"artifact_read", "artifact_get_metadata", "artifact_get_url"}:
        arguments: dict[str, object] = {"artifact_id": case.deliverable_id}
        if surface == "artifact_get_url":
            arguments["ttl_seconds"] = 60
        return (
            await handle_artifact_tool(
                surface,
                arguments,
                llm=None,
                artifact_store=factory.artifact_store,
                session_factory=factory,
                user_email=case.user_email,
                runtime_metadata=_managed_metadata(case.conversation_id, case.agent_id),
            ),
            None,
        )

    registry, tool_call = _executor_tool(surface)
    tool_call.arguments["source_artifact_id"] = case.deliverable_id
    executor = _RemoteExecutor(ToolResult(output="executed"))
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=factory.artifact_store,
        session_factory=factory,
    )
    result = await router.execute(
        tool_call,
        _session().model_copy(
            update={
                "user_email": case.user_email,
                "conversation_id": case.conversation_id,
                "agent_id": case.agent_id,
            }
        ),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )
    return result, executor


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _ALL_SURFACES)
async def test_direct_parent_accesses_child_on_every_deliverable_surface(
    task_continuation_db,
    surface: str,
) -> None:
    case = _AccessCase(
        "direct_parent",
        "dlv_child",
        "owner@example.com",
        "conv-controller",
        "agent-owner",
    )
    await _prepare_case(task_continuation_db, case)

    result, executor = await _invoke_surface(task_continuation_db, surface, case)

    if surface == "read_task_deliverable":
        assert result["ok"] is True
        assert result["content"] == "Child deliverable"
    else:
        assert result.is_error is False
    if executor is not None:
        assert executor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _ALL_SURFACES)
@pytest.mark.parametrize("case", _DENIED_CASES, ids=lambda case: case.name)
async def test_invalid_lineage_or_accessor_is_denied_on_every_deliverable_surface(
    task_continuation_db,
    surface: str,
    case: _AccessCase,
) -> None:
    await _prepare_case(task_continuation_db, case)

    result, executor = await _invoke_surface(task_continuation_db, surface, case)

    if surface == "read_task_deliverable":
        assert result == {"ok": False, "error": "not_found"}
    else:
        assert result.is_error is True
        assert f"Artifact not found: {case.deliverable_id}" in result.output
    if executor is not None:
        assert executor.calls == 0
