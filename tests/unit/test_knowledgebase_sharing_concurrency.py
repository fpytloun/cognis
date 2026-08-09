from __future__ import annotations

import asyncio

import pytest

from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, KnowledgebaseGrantRow, KnowledgebaseRow, User
from cognis.store.queries import (
    get_active_knowledgebase_grant,
    revoke_knowledgebase_grant,
    update_knowledgebase,
)


async def _setup(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/kb-share-race.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all(
            [
                User(email="owner@example.com", name="Owner", role="user"),
                User(email="grantee@example.com", name="Grantee", role="user"),
            ]
        )
        await session.flush()
        session.add(
            KnowledgebaseRow(
                knowledgebase_id="kb-1",
                owner_email="owner@example.com",
                name="Knowledge",
            )
        )
        await session.flush()
        session.add(
            KnowledgebaseGrantRow(
                grant_id="kbgrant-1",
                knowledgebase_id="kb-1",
                grantee_user_email="grantee@example.com",
                permission="view",
                granted_by="owner@example.com",
            )
        )
        await session.commit()
    return engine, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_first", [True, False])
async def test_sqlite_archive_and_revoke_serialize_on_active_knowledgebase(
    tmp_path: object, archive_first: bool
) -> None:
    engine, factory = await _setup(tmp_path)
    try:
        if archive_first:
            async with factory() as archive_session:
                archived = await update_knowledgebase(
                    archive_session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    updates={"status": "archived"},
                )
                assert archived is not None

                async def revoke() -> bool:
                    async with factory() as session:
                        result = await revoke_knowledgebase_grant(
                            session,
                            knowledgebase_id="kb-1",
                            grantee_user_email="grantee@example.com",
                        )
                        await session.commit()
                        return result

                racing_revoke = asyncio.create_task(revoke())
                await asyncio.sleep(0.05)
                assert not racing_revoke.done()
                await archive_session.commit()
            assert await racing_revoke is False
        else:
            async with factory() as revoke_session:
                assert await revoke_knowledgebase_grant(
                    revoke_session,
                    knowledgebase_id="kb-1",
                    grantee_user_email="grantee@example.com",
                )

                async def archive() -> None:
                    async with factory() as session:
                        row = await update_knowledgebase(
                            session,
                            owner_email="owner@example.com",
                            knowledgebase_id="kb-1",
                            updates={"status": "archived"},
                        )
                        assert row is not None
                        await session.commit()

                racing_archive = asyncio.create_task(archive())
                await asyncio.sleep(0.05)
                assert not racing_archive.done()
                await revoke_session.commit()
            await racing_archive

        async with factory() as session:
            knowledgebase = await session.get(KnowledgebaseRow, "kb-1")
            grant = await get_active_knowledgebase_grant(session, "kb-1", "grantee@example.com")
            assert knowledgebase is not None and knowledgebase.status == "archived"
            assert (grant is not None) is archive_first
    finally:
        await engine.dispose()
