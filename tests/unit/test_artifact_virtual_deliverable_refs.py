from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from cognis.core.artifact_access import artifact_authorized_for_conversation
from cognis.core.artifact_inputs import (
    authorize_outbound_artifact_refs_in_session,
    outbound_artifact_grant_is_valid,
)
from cognis.core.content_refs import get_deliverable_ref_unscoped
from cognis.core.tool_router import ToolRouter
from cognis.models.artifact import AttachmentRef
from cognis.store.models import Agent, AuditLog, ManagedConversationLink
from cognis.store.queries import (
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_managed_conversation_link,
    get_artifact_record,
    get_managed_conversation_link_for_target,
)
from cognis.tools.builtin.artifact_tools import handle_artifact_tool

pytest_plugins = ("tests.unit.test_task_continuation_tools",)


def _scoped_metadata(task_id: str) -> dict[str, object]:
    return {
        "conversation_context": {
            "platform_data": {
                "forked_from": "task",
                "task_id": task_id,
            }
        }
    }


def _managed_metadata(conversation_id: str, agent_id: str) -> dict[str, object]:
    return {"conversation_id": conversation_id, "agent_id": agent_id}


async def _seed_managed_deliverables(factory) -> None:
    async with factory() as session:
        session.add_all(
            [
                Agent(agent_id="agent-child", owner_email="owner@example.com", name="Child"),
                Agent(
                    agent_id="agent-grandchild",
                    owner_email="owner@example.com",
                    name="Grandchild",
                ),
                Agent(
                    agent_id="agent-unrelated",
                    owner_email="owner@example.com",
                    name="Unrelated",
                ),
                Agent(
                    agent_id="agent-sibling",
                    owner_email="owner@example.com",
                    name="Sibling",
                ),
                Agent(
                    agent_id="agent-branch-controller",
                    owner_email="owner@example.com",
                    name="Branch controller",
                ),
                Agent(
                    agent_id="agent-branch-child",
                    owner_email="owner@example.com",
                    name="Branch child",
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
        await create_conversation(
            session,
            "owner@example.com",
            "agent-unrelated",
            "agent_direct",
            conversation_id="conv-unrelated",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-sibling",
            "agent_work",
            conversation_id="conv-sibling",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-branch-controller",
            "agent_direct",
            conversation_id="conv-branch-controller",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-branch-child",
            "agent_work",
            conversation_id="conv-branch-child",
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
        await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="agent-owner",
            controller_conversation_id="conv-controller",
            controller_session_id="sess-controller",
            target_agent_id="agent-sibling",
            target_conversation_id="conv-sibling",
            target_session_id="sess-sibling",
            title="Sibling",
        )
        await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="agent-branch-controller",
            controller_conversation_id="conv-branch-controller",
            controller_session_id="sess-branch-controller",
            target_agent_id="agent-branch-child",
            target_conversation_id="conv-branch-child",
            target_session_id="sess-branch-child",
            title="Unrelated branch child",
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_published",
            conversation_id="conv-controller",
            turn_id="turn-published",
            content="Published deliverable",
            title="Published report",
            artifact_store=factory.artifact_store,
            published_owner_email="owner@example.com",
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_child",
            conversation_id="conv-child",
            turn_id="turn-child",
            content="Child deliverable",
            title="Child result",
            artifact_store=factory.artifact_store,
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_grandchild",
            conversation_id="conv-grandchild",
            turn_id="turn-grandchild",
            content="Nested deliverable",
            title="Nested result",
            artifact_store=factory.artifact_store,
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_sibling_child",
            conversation_id="conv-sibling",
            turn_id="turn-sibling",
            content="Sibling deliverable",
            title="Sibling result",
            artifact_store=factory.artifact_store,
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_branch_child",
            conversation_id="conv-branch-child",
            turn_id="turn-branch-child",
            content="Unrelated branch deliverable",
            title="Branch result",
            artifact_store=factory.artifact_store,
        )
        await create_artifact_record(
            session,
            artifact_id="att-child-context",
            namespace="attachments",
            object_id="att-child-context",
            filename="context.txt",
            owner_email="owner@example.com",
            conversation_id="conv-child",
            purpose="chat_input",
            kind="file",
            mime_type="text/plain",
            size_bytes=12,
            status="attached",
        )
        await session.commit()


@pytest.mark.asyncio
async def test_artifact_tools_read_and_metadata_owned_deliverable(
    task_continuation_db,
) -> None:
    metadata = await handle_artifact_tool(
        "artifact_get_metadata",
        {"artifact_id": "dlv_owner"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )
    read = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_owner", "offset": 1, "limit": 2},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )

    assert metadata.is_error is False
    assert metadata.metadata is not None
    assert metadata.metadata["artifact_id"] == "dlv_owner"
    assert metadata.metadata["source"] == "deliverable"
    assert metadata.metadata["virtual"] is True
    assert metadata.metadata["filename"] == "Full-report.md"
    assert metadata.metadata["mime_type"] == "text/markdown"
    assert metadata.metadata["size_bytes"] == len(b"# Full report\n\nComplete deliverable body.")
    assert metadata.metadata["task_id"] == "task-owner"
    assert json.loads(metadata.output)["download_url_tool"] == "artifact_get_url"

    assert read.is_error is False
    assert "1: # Full report" in read.output
    assert "2: " in read.output
    assert read.metadata is not None
    assert read.metadata["source"] == "deliverable"
    assert read.metadata["virtual"] is True


@pytest.mark.asyncio
async def test_artifact_tools_deny_cross_user_deliverable(task_continuation_db) -> None:
    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_owner"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="other@example.com",
    )

    assert result.is_error is True
    assert result.output == "Artifact not found: dlv_owner"


@pytest.mark.asyncio
async def test_unrelated_owner_conversation_can_use_published_chat_deliverable(
    task_continuation_db,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)

    metadata = await handle_artifact_tool(
        "artifact_get_metadata",
        {"artifact_id": "dlv_published"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_managed_metadata("conv-unrelated", "agent-unrelated"),
    )
    search = await handle_artifact_tool(
        "artifact_search",
        {"query": "Published report"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )
    read = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_published"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_managed_metadata("conv-unrelated", "agent-unrelated"),
    )

    assert metadata.is_error is False
    assert metadata.metadata is not None
    assert metadata.metadata["filename"] == "Published-report.md"
    assert search.is_error is False
    assert search.metadata is not None
    assert search.metadata["items"][0]["artifact_id"] == "dlv_published"
    assert read.is_error is False
    assert read.output == "1: Published deliverable"


@pytest.mark.asyncio
async def test_other_owner_cannot_use_published_chat_deliverable(task_continuation_db) -> None:
    await _seed_managed_deliverables(task_continuation_db)

    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_published"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="other@example.com",
    )

    assert result.is_error is True
    assert result.output == "Artifact not found: dlv_published"


@pytest.mark.asyncio
async def test_rich_deliverable_virtual_artifact_serves_fallback_text_metadata(
    task_continuation_db,
) -> None:
    metadata = await handle_artifact_tool(
        "artifact_get_metadata",
        {"artifact_id": "dlv_rich"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )
    read = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_rich"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )

    assert metadata.is_error is False
    assert metadata.metadata is not None
    assert metadata.metadata["filename"] == "Rich-report.md"
    assert metadata.metadata["mime_type"] == "text/markdown"
    assert metadata.metadata["size_bytes"] == len(b"Rich fallback")
    assert metadata.metadata["deliverable_format"] == "rich"
    assert metadata.metadata["rich_payload"]["blocks"][0]["type"] == "card"
    assert metadata.metadata["render_metadata"]["schema"] == "cognis.rich_deliverable.v1"
    assert "copy" in metadata.metadata["export_metadata"]["available"]

    assert read.is_error is False
    assert read.output == "1: Rich fallback"


@pytest.mark.asyncio
async def test_artifact_tools_preserve_task_scope_for_deliverable_refs(
    task_continuation_db,
) -> None:
    metadata = await handle_artifact_tool(
        "artifact_get_metadata",
        {"artifact_id": "dlv_sibling"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_scoped_metadata("task-owner"),
    )
    read = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_sibling"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_scoped_metadata("task-owner"),
    )
    url = await handle_artifact_tool(
        "artifact_get_url",
        {"artifact_id": "dlv_sibling", "ttl_seconds": 60},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_scoped_metadata("task-owner"),
    )

    assert metadata.is_error is True
    assert metadata.output == "Artifact not found: dlv_sibling"
    assert read.is_error is True
    assert read.output == "Artifact not found: dlv_sibling"
    assert url.is_error is True
    assert url.output == "Artifact not found: dlv_sibling"


@pytest.mark.asyncio
async def test_artifact_get_url_returns_virtual_deliverable_url(task_continuation_db) -> None:
    result = await handle_artifact_tool(
        "artifact_get_url",
        {"artifact_id": "dlv_owner", "ttl_seconds": 60},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["source"] == "deliverable"
    assert result.metadata["virtual"] is True
    assert (
        "/api/v1/artifacts/virtual/deliverables/dlv_owner/Full-report.md" in result.metadata["url"]
    )
    assert "sig=" in result.metadata["url"]
    assert result.metadata["mode"] == "download"


@pytest.mark.asyncio
async def test_artifact_get_url_rejects_virtual_deliverable_view_for_non_html(
    task_continuation_db,
) -> None:
    result = await handle_artifact_tool(
        "artifact_get_url",
        {"artifact_id": "dlv_owner", "ttl_seconds": 60, "mode": "view"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )

    assert result.is_error is True
    assert result.output == "Artifact view is only supported for HTML artifacts: dlv_owner"


@pytest.mark.asyncio
async def test_controller_can_read_direct_child_deliverable_and_records_audit(
    task_continuation_db,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)

    result = await handle_artifact_tool(
        "artifact_get_metadata",
        {"artifact_id": "dlv_child"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_managed_metadata("conv-controller", "agent-owner"),
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["conversation_id"] == "conv-child"
    assert result.metadata["creator_agent_id"] == "agent-child"
    assert result.metadata["purpose"] == "conversation_deliverable"
    async with task_continuation_db() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "managed_deliverable_access")
            )
        ).scalar_one()
    assert audit.user_email == "owner@example.com"
    assert audit.agent_id == "agent-owner"
    assert audit.details == {
        "deliverable_id": "dlv_child",
        "creator_agent_id": "agent-child",
        "creator_conversation_id": "conv-child",
        "creator_control_link_id": audit.details["creator_control_link_id"],
        "owner_email": "owner@example.com",
        "accessor_agent_id": "agent-owner",
        "accessor_conversation_id": "conv-controller",
        "control_link_id": audit.details["control_link_id"],
        "managed_descendant_depth": 1,
    }
    assert "Child deliverable" not in json.dumps(audit.details)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation_id", "agent_id"),
    [
        ("conv-child", "agent-child"),
        ("conv-grandchild", "agent-grandchild"),
    ],
)
async def test_managed_descendant_can_access_ancestor_artifact(
    task_continuation_db,
    conversation_id: str,
    agent_id: str,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)

    async with task_continuation_db() as session:
        assert await artifact_authorized_for_conversation(
            session,
            artifact=SimpleNamespace(
                owner_email="owner@example.com",
                conversation_id="conv-controller",
            ),
            owner_email="owner@example.com",
            conversation_id=conversation_id,
            agent_id=agent_id,
        )


@pytest.mark.asyncio
async def test_managed_artifact_access_preserves_ancestor_and_sibling_boundaries(
    task_continuation_db,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)
    child_artifact = SimpleNamespace(
        owner_email="owner@example.com",
        conversation_id="conv-child",
    )

    async with task_continuation_db() as session:
        assert await artifact_authorized_for_conversation(
            session,
            artifact=child_artifact,
            owner_email="owner@example.com",
            conversation_id="conv-controller",
            agent_id="agent-owner",
        )
        assert not await artifact_authorized_for_conversation(
            session,
            artifact=child_artifact,
            owner_email="owner@example.com",
            conversation_id="conv-sibling",
            agent_id="agent-sibling",
        )
        assert not await artifact_authorized_for_conversation(
            session,
            artifact=child_artifact,
            owner_email="other@example.com",
            conversation_id="conv-controller",
            agent_id="agent-owner",
        )


@pytest.mark.asyncio
async def test_nested_ancestor_grant_binds_to_accessor_link(task_continuation_db) -> None:
    await _seed_managed_deliverables(task_continuation_db)

    async with task_continuation_db() as session:
        artifact = await get_artifact_record(session, "att-child-context")
        assert artifact is not None
        authorized = await authorize_outbound_artifact_refs_in_session(
            session,
            [
                AttachmentRef(
                    artifact_id=artifact.artifact_id,
                    kind=artifact.kind,
                    mime_type=artifact.mime_type,
                    filename=artifact.filename,
                    size_bytes=artifact.size_bytes,
                )
            ],
            user_email="owner@example.com",
            conversation_id="conv-grandchild",
            agent_id="agent-grandchild",
        )
        grant = authorized[0]["_delivery_authorization"]
        grandchild_link = await get_managed_conversation_link_for_target(
            session,
            "conv-grandchild",
            user_email="owner@example.com",
        )
        assert grandchild_link is not None
        assert grant["scope"] == "ancestor"
        assert grant["descendant_link_id"] == grandchild_link.link_id
        assert await outbound_artifact_grant_is_valid(
            session,
            attachment=authorized[0],
            artifact=artifact,
            owner_email="owner@example.com",
        )

        grandchild_link.owner_epoch += 1
        await session.commit()
        assert not await outbound_artifact_grant_is_valid(
            session,
            attachment=authorized[0],
            artifact=artifact,
            owner_email="owner@example.com",
        )


@pytest.mark.asyncio
async def test_root_controller_can_read_nested_deliverable_and_generate_url(
    task_continuation_db,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)
    runtime_metadata = _managed_metadata("conv-controller", "agent-owner")

    read = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_grandchild"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=runtime_metadata,
    )
    url = await handle_artifact_tool(
        "artifact_get_url",
        {"artifact_id": "dlv_grandchild", "ttl_seconds": 60},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=runtime_metadata,
    )

    assert read.is_error is False
    assert read.output == "1: Nested deliverable"
    assert url.is_error is False
    assert "/virtual/deliverables/dlv_grandchild/Nested-result.md" in url.metadata["url"]
    async with task_continuation_db() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.event_type == "managed_deliverable_access")
                )
            )
            .scalars()
            .all()
        )
    assert {audit.details["managed_descendant_depth"] for audit in audits} == {2}
    assert all(audit.details["creator_control_link_id"] for audit in audits)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_conversation_id", "field", "value"),
    [
        ("conv-child", "target_conversation_id", "conv-unrelated"),
        ("conv-child", "target_agent_id", "agent-unrelated"),
        ("conv-child", "root_link_id", "__other__"),
        ("conv-child", "depth", 2),
        ("conv-grandchild", "controller_conversation_id", "conv-unrelated"),
        ("conv-grandchild", "controller_agent_id", "agent-unrelated"),
        ("conv-grandchild", "root_link_id", "__other__"),
        ("conv-grandchild", "depth", 1),
    ],
)
async def test_nested_deliverable_denies_malformed_lineage_edge(
    task_continuation_db,
    target_conversation_id,
    field,
    value,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)
    async with task_continuation_db() as session:
        links = (await session.execute(select(ManagedConversationLink))).scalars().all()
        link = next(item for item in links if item.target_conversation_id == target_conversation_id)
        if value == "__other__":
            value = (
                link.link_id
                if target_conversation_id == "conv-grandchild"
                else next(item.link_id for item in links if item.link_id != link.link_id)
            )
        setattr(link, field, value)
        await session.commit()

    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_grandchild"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_managed_metadata("conv-controller", "agent-owner"),
    )

    assert result.is_error is True
    assert result.output == "Artifact not found: dlv_grandchild"


@pytest.mark.asyncio
async def test_executor_content_ref_bridge_materializes_child_deliverable(
    task_continuation_db,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)
    router = ToolRouter(
        guardrails=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
    )

    content, mime_type, filename = await router._load_binary_content_ref(  # noqa: SLF001
        "dlv_child",
        "owner@example.com",
        accessor_conversation_id="conv-controller",
        accessor_agent_id="agent-owner",
    )

    assert content == b"Child deliverable"
    assert mime_type == "text/markdown"
    assert filename == "Child-result.md"


@pytest.mark.asyncio
async def test_signed_download_resolver_loads_direct_deliverable_payload(
    task_continuation_db,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)

    async with task_continuation_db() as session:
        ref = await get_deliverable_ref_unscoped(
            session,
            task_continuation_db.artifact_store,
            "dlv_child",
        )

    assert ref is not None
    assert ref.content_bytes == b"Child deliverable"
    assert ref.owner_email == "owner@example.com"
    assert ref.task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_email", "runtime_metadata"),
    [
        ("owner@example.com", _managed_metadata("conv-unrelated", "agent-unrelated")),
        ("other@example.com", _managed_metadata("conv-controller", "agent-owner")),
        ("owner@example.com", _managed_metadata("conv-controller", "agent-child")),
        ("owner@example.com", None),
    ],
)
async def test_direct_deliverable_denies_unrelated_or_invalid_accessor(
    task_continuation_db,
    user_email,
    runtime_metadata,
) -> None:
    await _seed_managed_deliverables(task_continuation_db)

    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_child"},
        llm=None,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
        user_email=user_email,
        runtime_metadata=runtime_metadata,
    )

    assert result.is_error is True
    assert result.output == "Artifact not found: dlv_child"
