from __future__ import annotations

import pytest

from cognis.knowledgebase.access import (
    KnowledgebaseAccessContext,
    list_available_knowledgebases,
    resolve_knowledgebase_access,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, KnowledgebaseRow, User
from cognis.store.queries import (
    assign_knowledgebase_to_agent,
    create_agent_grant,
    revoke_agent_grant,
)


async def _setup(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/kb-access.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all(
            [
                User(email="owner@example.com", name="Owner", role="user"),
                User(email="grantee@example.com", name="Grantee", role="user"),
                User(email="other@example.com", name="Other", role="user"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                KnowledgebaseRow(
                    knowledgebase_id="kb_owner",
                    owner_email="owner@example.com",
                    name="Owner KB",
                ),
                KnowledgebaseRow(
                    knowledgebase_id="kb_other",
                    owner_email="other@example.com",
                    name="Other KB",
                ),
                Agent(
                    agent_id="agent_owner",
                    owner_email="owner@example.com",
                    name="Owner Agent",
                    status="active",
                ),
            ]
        )
        await session.commit()
    return engine, factory


@pytest.mark.asyncio
async def test_owner_can_manage_and_directly_use_own_kb(tmp_path: object) -> None:
    engine, factory = await _setup(tmp_path)
    try:
        async with factory() as session:
            context = KnowledgebaseAccessContext(actor_email="owner@example.com")
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id="kb_owner", context=context, mode="manage"
                )
                is not None
            )
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id="kb_owner", context=context, mode="use"
                )
                is not None
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_active_agent_context_requires_assignment_even_for_owner(tmp_path: object) -> None:
    engine, factory = await _setup(tmp_path)
    try:
        async with factory() as session:
            context = KnowledgebaseAccessContext(
                actor_email="owner@example.com",
                agent_id="agent_owner",
                agent_owner_email="owner@example.com",
            )
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id="kb_owner", context=context, mode="use"
                )
                is None
            )
            assert await assign_knowledgebase_to_agent(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb_owner",
                agent_id="agent_owner",
            )
            await session.commit()
            resolved = await resolve_knowledgebase_access(
                session, knowledgebase_id="kb_owner", context=context, mode="use"
            )
            assert resolved is not None
            assert resolved.owner_email == "owner@example.com"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_agent_grantee_can_use_assigned_kb_but_not_manage(
    tmp_path: object,
) -> None:
    engine, factory = await _setup(tmp_path)
    try:
        async with factory() as session:
            await assign_knowledgebase_to_agent(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb_owner",
                agent_id="agent_owner",
            )
            grant = await create_agent_grant(
                session,
                agent_id="agent_owner",
                grantee_user_email="grantee@example.com",
                executor_scope="shared_pool",
                granted_by="owner@example.com",
            )
            await session.commit()

            context = KnowledgebaseAccessContext(
                actor_email="grantee@example.com",
                agent_id="agent_owner",
                agent_owner_email="owner@example.com",
            )
            resolved = await resolve_knowledgebase_access(
                session, knowledgebase_id="kb_owner", context=context, mode="use"
            )
            assert resolved is not None
            assert resolved.is_agent_grantee is True
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id="kb_owner", context=context, mode="manage"
                )
                is None
            )

            assert [
                row.knowledgebase_id
                for row in await list_available_knowledgebases(session, context=context)
            ] == ["kb_owner"]

            await revoke_agent_grant(session, grant.grant_id)
            await session.commit()
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id="kb_owner", context=context, mode="use"
                )
                is None
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_assignment_does_not_cross_kb_owner_boundary(tmp_path: object) -> None:
    engine, factory = await _setup(tmp_path)
    try:
        async with factory() as session:
            assert not await assign_knowledgebase_to_agent(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb_other",
                agent_id="agent_owner",
            )
    finally:
        await engine.dispose()
