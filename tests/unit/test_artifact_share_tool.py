from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.tool_arguments import validate_tool_arguments
from cognis.runtime_context import RuntimeAccessContext, current_runtime_access_context
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import ArtifactGrantRow, Base
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_user,
    lock_artifact_revision_target,
)
from cognis.tools.builtin.artifact_tools import ARTIFACT_SHARE_TOOL, handle_artifact_tool


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
async def test_library_lineage_sharing_through_tool(tmp_path, kind):
    owner = "sharing@cognis.test"
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/sharing.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "payloads")))

    async def write(previous=None):
        async with factory() as session:
            if kind == "deliverable":
                row = await create_deliverable(
                    session,
                    conversation_id="author-chat",
                    content="# Revision",
                    published_owner_email=owner,
                    supersedes_deliverable_id=previous,
                    artifact_store=store,
                )
                row.status = "delivered"
                ref = row.deliverable_id
            else:
                lineage = (
                    await lock_artifact_revision_target(session, previous) if previous else None
                )
                row = await create_artifact_record(
                    session,
                    artifact_id="art_" + uuid.uuid4().hex,
                    namespace="artifacts",
                    object_id=uuid.uuid4().hex,
                    filename="state.txt",
                    owner_email=owner,
                    owner_agent_id="author",
                    purpose="artifact_write",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=0,
                    status="attached",
                    lineage_id=lineage.lineage_id if lineage else None,
                    revision=lineage.revision if lineage else 1,
                    supersedes_artifact_id=previous,
                )
                ref = row.artifact_id
            await session.commit()
            return ref

    async def call(ref, action, actor="manager", user=owner, delegated=False, **fields):
        arguments = {"content_ref": ref, "action": action, **fields}
        assert (
            validate_tool_arguments(
                "artifact_share", arguments, schema=ARTIFACT_SHARE_TOOL.parameters
            )
            is None
        )
        token = current_runtime_access_context.set(
            RuntimeAccessContext(
                user_email=user,
                agent_id=actor,
                agent_owner_email=user,
                parent_session_id="parent" if delegated else None,
            )
        )
        try:
            return await handle_artifact_tool(
                "artifact_share",
                arguments,
                llm=None,
                artifact_store=store,
                session_factory=factory,
                user_email=user,
                runtime_metadata={"agent_id": actor},
            )
        finally:
            current_runtime_access_context.reset(token)

    try:
        async with factory() as session:
            await create_user(session, owner, name="Owner", password_hash="test")
            await create_user(session, "other@cognis.test", name="Other", password_hash="test")
            for agent in ("author", "manager", "reader"):
                await create_agent(
                    session,
                    agent_id=agent,
                    owner_email=owner,
                    name=agent,
                    status="active",
                )
            await create_agent(
                session,
                agent_id="foreign",
                owner_email="other@cognis.test",
                name="Foreign",
                status="active",
            )
            await create_conversation(
                session,
                conversation_id="author-chat",
                user_email=owner,
                agent_id="author",
                context_type="web",
            )
            await session.commit()
        root = await write()
        head = await write(root)
        granted = await call(root, "grant", agent_id="reader")
        assert not granted.is_error, granted.output
        for action, fields in (("get", {}), ("set", {"agent_ids": ["manager"]})):
            denied = await call(root, action, actor="reader", delegated=True, **fields)
            assert denied.is_error
        future = await write(head)
        async with factory() as session:
            refs = set(
                await session.scalars(
                    select(ArtifactGrantRow.content_ref).where(
                        ArtifactGrantRow.agent_id == "reader",
                        ArtifactGrantRow.source == "explicit",
                    )
                )
            )
            assert refs == {root, head, future}
        listed = await call(head, "get")
        assert not listed.is_error, listed.output
        agents = {agent["agent_id"]: agent for agent in json.loads(listed.output)["agents"]}
        assert agents["reader"]["explicit"]
        assert agents["author"]["inherited"]
        denied = await call(root, "set", agent_ids=["foreign"])
        assert denied.is_error
        denied = await call(root, "get", user="other@cognis.test", actor="foreign")
        assert denied.is_error
        revoked = await call(root, "revoke", agent_id="reader")
        assert not revoked.is_error, revoked.output
        replaced = await call(future, "set", agent_ids=["manager"])
        assert not replaced.is_error, replaced.output
        async with factory() as session:
            grants = list(await session.scalars(select(ArtifactGrantRow)))
            assert {
                (grant.content_ref, grant.agent_id)
                for grant in grants
                if grant.source == "explicit"
            } == {(ref, "manager") for ref in (root, head, future)}
    finally:
        await engine.dispose()
