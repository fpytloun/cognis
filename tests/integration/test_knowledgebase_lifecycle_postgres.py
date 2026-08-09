from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cognis.store.database import create_session_factory
from cognis.store.models import (
    ArtifactRecordRow,
    Base,
    KnowledgebaseArtifactRow,
    KnowledgebaseGrantRow,
    KnowledgebaseRow,
    User,
)
from cognis.store.queries import (
    attach_artifact_to_knowledgebase,
    delete_artifact_record,
    mark_artifact_deleted,
    revoke_knowledgebase_grant,
    update_knowledgebase,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://")
        else url
    )


@pytest.mark.parametrize(
    ("delete_operation", "delete_first"),
    [
        (mark_artifact_deleted, False),
        (delete_artifact_record, False),
        (mark_artifact_deleted, True),
        (delete_artifact_record, True),
    ],
)
@pytest.mark.asyncio
async def test_postgres_canonical_delete_wins_concurrent_replacement(
    delete_operation: Callable[[AsyncSession, str], Awaitable[bool]],
    delete_first: bool,
) -> None:
    url = _url()
    schema_name = f"cognis_kb_lifecycle_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            session.add(
                KnowledgebaseRow(
                    knowledgebase_id="kb-1",
                    owner_email="owner@example.com",
                    name="Knowledge",
                )
            )
            session.add_all(
                [
                    ArtifactRecordRow(
                        artifact_id="artifact-old",
                        namespace="owner",
                        object_id="old",
                        filename="guide.txt",
                        owner_email="owner@example.com",
                        mime_type="text/plain",
                        size_bytes=3,
                        status="attached",
                    ),
                    ArtifactRecordRow(
                        artifact_id="artifact-new",
                        namespace="owner",
                        object_id="new",
                        filename="guide.txt",
                        owner_email="owner@example.com",
                        mime_type="text/plain",
                        size_bytes=3,
                        status="temporary",
                    ),
                ]
            )
            session.add(
                KnowledgebaseArtifactRow(
                    kb_artifact_id="kba-1",
                    knowledgebase_id="kb-1",
                    source_path="docs/guide.txt",
                    artifact_id="artifact-old",
                    active_generation=1,
                    desired_generation=1,
                    status="indexed",
                )
            )
            await session.commit()

        async def replace() -> KnowledgebaseArtifactRow | None:
            async with factory() as session:
                replacement = await attach_artifact_to_knowledgebase(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-new",
                    source_path="docs/guide.txt",
                )
                await session.commit()
                return replacement

        async def delete_pending() -> bool:
            async with factory() as session:
                deleted = await delete_operation(session, "artifact-new")
                await session.commit()
                return bool(deleted)

        if delete_first:
            async with factory() as delete_session:
                assert await delete_operation(delete_session, "artifact-new")
                replacement = asyncio.create_task(replace())
                await asyncio.sleep(0.05)
                assert not replacement.done()
                await delete_session.commit()
            assert await replacement is None
        else:
            async with factory() as replacement_session:
                replacement = await attach_artifact_to_knowledgebase(
                    replacement_session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-new",
                    source_path="docs/guide.txt",
                )
                assert replacement is not None
                deletion = asyncio.create_task(delete_pending())
                await asyncio.sleep(0.05)
                assert not deletion.done()
                await replacement_session.commit()
            assert await deletion

        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            artifact = await session.get(ArtifactRecordRow, "artifact-new")
            assert attachment is not None
            assert attachment.artifact_id == "artifact-old"
            assert attachment.pending_artifact_id is None
            if delete_operation is delete_artifact_record:
                assert artifact is None
            else:
                assert artifact is not None
                assert artifact.status == "deleted"
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.parametrize("archive_first", [True, False])
@pytest.mark.asyncio
async def test_postgres_archive_and_share_revoke_serialize(
    archive_first: bool,
) -> None:
    url = _url()
    schema_name = f"cognis_kb_share_race_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
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

        if archive_first:
            async with factory() as archive_session:
                assert await update_knowledgebase(
                    archive_session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    updates={"status": "archived"},
                )

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
                        assert await update_knowledgebase(
                            session,
                            owner_email="owner@example.com",
                            knowledgebase_id="kb-1",
                            updates={"status": "archived"},
                        )
                        await session.commit()

                racing_archive = asyncio.create_task(archive())
                await asyncio.sleep(0.05)
                assert not racing_archive.done()
                await revoke_session.commit()
            await racing_archive

        async with factory() as session:
            knowledgebase = await session.get(KnowledgebaseRow, "kb-1")
            grant = await session.get(KnowledgebaseGrantRow, "kbgrant-1")
            assert knowledgebase is not None and knowledgebase.status == "archived"
            assert grant is not None
            assert (grant.revoked_at is None) is archive_first
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(
                sa_schema.DropSchema(schema_name, cascade=True, if_exists=True)
            )
        await admin.dispose()
