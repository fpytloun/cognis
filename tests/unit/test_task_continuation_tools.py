"""Tests for task deliverable/step-run continuation tools."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from cognis.models.tool import ExecutorHandle
from cognis.store.models import Agent, AuditLog
from cognis.store.queries import (
    create_conversation,
    create_deliverable,
    create_managed_conversation_link,
)
from cognis.tools.builtin.task_continuation import build_task_continuation_tool_handlers
from cognis.tools.registry import ToolExecutionContext


def _context(
    user_email: str,
    *,
    scope_task_id: str | None = None,
    artifact_store: object | None = None,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    use_runtime_access: bool = False,
) -> ToolExecutionContext:
    runtime_metadata: dict[str, object] = {"user_email": user_email}
    if artifact_store is not None:
        runtime_metadata["artifact_store"] = artifact_store
    if scope_task_id is not None:
        runtime_metadata["conversation_context"] = {
            "platform_data": {
                "forked_from": "task",
                "task_id": scope_task_id,
            }
        }
    if conversation_id is not None and agent_id is not None:
        if use_runtime_access:
            runtime_metadata["runtime_access"] = {
                "conversation_id": conversation_id,
                "agent_id": agent_id,
            }
        else:
            runtime_metadata["conversation_id"] = conversation_id
            runtime_metadata["agent_id"] = agent_id
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata=runtime_metadata,
    )


async def _seed_managed_rich_deliverable(factory) -> None:
    async with factory() as session:
        session.add_all(
            [
                Agent(agent_id="agent-child", owner_email="owner@example.com", name="Child"),
                Agent(
                    agent_id="agent-grandchild",
                    owner_email="owner@example.com",
                    name="Grandchild",
                ),
            ]
        )
        await session.flush()
        await create_conversation(
            session,
            "owner@example.com",
            "agent-owner",
            "agent_direct",
            conversation_id="conv-controller",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-child",
            "agent_work",
            conversation_id="conv-child",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-grandchild",
            "agent_work",
            conversation_id="conv-grandchild",
        )
        parent = await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="agent-owner",
            controller_conversation_id="conv-controller",
            controller_session_id="sess-controller",
            target_agent_id="agent-child",
            target_conversation_id="conv-child",
            target_session_id="sess-child",
            title="Child",
        )
        await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="agent-child",
            controller_conversation_id="conv-child",
            controller_session_id="sess-child",
            target_agent_id="agent-grandchild",
            target_conversation_id="conv-grandchild",
            target_session_id="sess-grandchild",
            title="Grandchild",
            parent_link_id=parent.link_id,
            root_link_id=parent.link_id,
            depth=2,
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_managed_rich",
            conversation_id="conv-grandchild",
            turn_id="turn-grandchild",
            content="Managed rich fallback",
            format="rich",
            title="Managed rich report",
            outputs={"kind": "managed_report"},
            rich={"blocks": [{"type": "card", "title": "Finding", "content": "Nested"}]},
            artifact_store=factory.artifact_store,
        )
        await session.commit()


@pytest.mark.asyncio
async def test_read_task_deliverable_allows_owned_deliverable_from_main_chat(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_owner"},
        _context("owner@example.com", artifact_store=task_continuation_db.artifact_store),
    )

    assert result["ok"] is True
    assert result["task_id"] == "task-owner"
    assert result["deliverable_id"] == "dlv_owner"
    assert result["step_run_id"] == "sr-owner"
    assert result["content"] == "# Full report\n\nComplete deliverable body."
    assert result["outputs"] == {"kind": "report"}


@pytest.mark.asyncio
async def test_list_task_step_runs_allows_owned_task_from_main_chat(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    result = await handlers["list_task_step_runs"](
        {"task_id": "task-owner"},
        _context("owner@example.com"),
    )

    assert result["ok"] is True
    assert result["task_id"] == "task-owner"
    assert result["step_runs"] == [
        {
            "step_run_id": "sr-owner",
            "step_name": "execute",
            "step_type": "direct",
            "status": "approved",
            "attempt": 1,
            "agent_id": "agent-owner",
            "conversation_id": "conv-owner",
            "session_id": None,
            "intaris_session_id": None,
            "deliverable_id": "dlv_owner",
            "runtime_info": {"source": "test"},
        }
    ]


@pytest.mark.asyncio
async def test_read_task_deliverable_returns_rich_metadata(task_continuation_db) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_rich"},
        _context("owner@example.com", artifact_store=task_continuation_db.artifact_store),
    )

    assert result["ok"] is True
    assert result["task_id"] == "task-rich"
    assert result["step_run_id"] == "sr-rich"
    assert result["format"] == "rich"
    assert result["content"] == "Rich fallback"
    assert result["rich_payload"] == {
        "blocks": [{"type": "card", "title": "Finding", "content": "Body"}],
        "assets": [],
        "sources": [],
        "datasets": [],
        "exports": [],
        "metadata": {},
    }
    assert result["render_metadata"]["schema"] == "cognis.rich_deliverable.v1"
    assert "copy" in result["export_metadata"]["available"]


@pytest.mark.asyncio
async def test_task_continuation_tools_keep_cross_user_access_denied(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    deliverable_result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_owner"},
        _context("other@example.com"),
    )
    step_runs_result = await handlers["list_task_step_runs"](
        {"task_id": "task-owner"},
        _context("other@example.com"),
    )

    assert deliverable_result == {"ok": False, "error": "not_found"}
    assert step_runs_result == {"ok": False, "error": "not_found"}


@pytest.mark.asyncio
async def test_task_continuation_tools_keep_fork_scope_restriction(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    deliverable_result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_owner"},
        _context("owner@example.com", scope_task_id="task-other"),
    )
    step_runs_result = await handlers["list_task_step_runs"](
        {"task_id": "task-owner"},
        _context("owner@example.com", scope_task_id="task-other"),
    )

    assert deliverable_result == {"ok": False, "error": "not_found"}
    assert step_runs_result == {"ok": False, "error": "outside_continuation_scope"}


@pytest.mark.asyncio
async def test_read_task_deliverable_returns_managed_descendant_payload_and_audits_accessor(
    task_continuation_db,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_managed_rich"},
        _context(
            "owner@example.com",
            artifact_store=task_continuation_db.artifact_store,
            conversation_id="conv-controller",
            agent_id="agent-owner",
            use_runtime_access=True,
        ),
    )

    assert result["ok"] is True
    assert result["task_id"] is None
    assert result["conversation_id"] == "conv-grandchild"
    assert result["session_id"] is None
    assert result["turn_id"] == "turn-grandchild"
    assert result["creator_agent_id"] == "agent-grandchild"
    assert result["source"] == "managed_conversation_deliverable"
    assert result["content"] == "Managed rich fallback"
    assert result["outputs"] == {"kind": "managed_report"}
    assert result["rich_payload"]["blocks"] == [
        {"type": "card", "title": "Finding", "content": "Nested"}
    ]
    assert result["render_metadata"]["schema"] == "cognis.rich_deliverable.v1"
    assert "copy" in result["export_metadata"]["available"]

    async with task_continuation_db() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "managed_deliverable_access")
            )
        ).scalar_one()
    assert audit.user_email == "owner@example.com"
    assert audit.agent_id == "agent-owner"
    assert audit.details["creator_agent_id"] == "agent-grandchild"
    assert audit.details["creator_conversation_id"] == "conv-grandchild"
    assert audit.details["accessor_agent_id"] == "agent-owner"
    assert audit.details["accessor_conversation_id"] == "conv-controller"
    assert audit.details["managed_descendant_depth"] == 2
    assert audit.details["creator_control_link_id"]
    assert "Managed rich fallback" not in str(audit.details)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_email", "conversation_id", "agent_id"),
    [
        ("owner@example.com", "conv-controller", "agent-child"),
        ("owner@example.com", "conv-child", "agent-owner"),
        ("other@example.com", "conv-controller", "agent-owner"),
    ],
)
async def test_read_task_deliverable_denies_invalid_managed_accessor(
    task_continuation_db,
    user_email,
    conversation_id,
    agent_id,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_managed_rich"},
        _context(
            user_email,
            artifact_store=task_continuation_db.artifact_store,
            conversation_id=conversation_id,
            agent_id=agent_id,
        ),
    )

    assert result == {"ok": False, "error": "not_found"}
