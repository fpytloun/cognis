"""Native data-plane behavior through authorization, HTTP, storage and publication."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import BigInteger, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.api.routes.artifacts import router as artifacts_router
from cognis.api.routes.transfers import router as transfers_router
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.artifact_revisions import ContentActor, write_artifact_document
from cognis.core.binary_transfers import BinaryTransferService, TransferRejected
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, BinaryTransferRow
from cognis.store.queries import create_agent, create_conversation, create_session, create_user
from cognis.transfers.client import BinaryTransferClient
from cognis.transfers.models import DownloadDescriptor, UploadRequest

OWNER = "binary-transfer-owner@example.com"
AGENT = "binary-transfer-agent"
CONVERSATION = "binary-transfer-conversation"
SESSION = "binary-transfer-session"
CALL = "binary-transfer-call"


@pytest.fixture
async def env(tmp_path: Path):
    postgres = os.environ.get("COGNIS_BINARY_TEST_POSTGRES_URL")
    admin = None
    schema = "binary_" + uuid.uuid4().hex
    if postgres:
        admin = create_engine(postgres)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            postgres, connect_args={"server_settings": {"search_path": schema}}
        )
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/transfers.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        await create_user(db, email=OWNER, name="Transfer owner", password_hash="test-hash")
        await create_agent(
            db, agent_id=AGENT, owner_email=OWNER, name="Transfer agent", status="active"
        )
        await create_conversation(
            db,
            user_email=OWNER,
            agent_id=AGENT,
            context_type="web",
            conversation_id=CONVERSATION,
        )
        await create_session(db, CONVERSATION, OWNER, AGENT, session_id=SESSION)
        await db.commit()
    store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "objects"),
            base_url="http://transfer.test",
            signing_secret="binary-transfer-test-signing-secret",
        )
    )
    service = BinaryTransferService(factory, store)
    app = FastAPI()
    app.state.session_factory = factory
    app.state.artifact_store = store
    app.state.binary_transfers = service
    app.include_router(transfers_router)
    app.include_router(artifacts_router)
    scope_id, grant = await service.issue(
        owner_email=OWNER,
        agent_id=AGENT,
        session_id=SESSION,
        conversation_id=CONVERSATION,
        call_id=CALL,
        tool_name="artifact_publish",
        executor_id="binary-transfer-executor",
    )
    client = BinaryTransferClient(grant, transport=httpx.ASGITransport(app=app))
    try:
        yield factory, store, service, app, client, scope_id
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.mark.parametrize("size", [0, 17, 2 * 1024 * 1024 + 11])
async def test_native_round_trip_and_revision_adoption(env, tmp_path: Path, size: int) -> None:
    factory, store, service, app, client, scope_id = env
    content = os.urandom(size)
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    receipt = await client.upload_file(source)
    assert receipt.size_bytes == size
    assert receipt.sha256 == hashlib.sha256(content).hexdigest()
    await service.close_scope(scope_id)
    staged = await service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
    actor = ContentActor(
        user_email=OWNER,
        agent_id=AGENT,
        conversation_id=CONVERSATION,
        session_id=SESSION,
    )
    row, _ = await write_artifact_document(
        session_factory=factory,
        artifact_store=store,
        actor=actor,
        staged=staged,
        filename=staged.filename,
        mime_type=staged.mime_type,
        kind="file",
        purpose="artifact_publish",
    )
    url = await store.async_get_public_url(row.namespace, row.object_id, row.filename)
    destination = tmp_path / "downloaded.bin"
    await client.download_file(
        DownloadDescriptor(
            url=url,
            filename=row.filename,
            size_bytes=size,
            sha256=row.content_hash,
        ),
        destination,
    )
    assert destination.read_bytes() == content
    async with factory() as db:
        uploaded = await db.get(BinaryTransferRow, receipt.transfer_id)
        assert uploaded.state == "consumed"
    # Recovered duplicate results adopt the existing artifact rather than another revision.
    again = await service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
    replay, _ = await write_artifact_document(
        session_factory=factory,
        artifact_store=store,
        actor=actor,
        staged=again,
        filename=again.filename,
        mime_type=again.mime_type,
        kind="file",
        purpose="artifact_publish",
    )
    assert replay.artifact_id == row.artifact_id


async def test_scope_and_receipt_cannot_cross_calls_or_users(env, tmp_path: Path) -> None:
    _, _, service, app, client, scope_id = env
    source = tmp_path / "source.bin"
    source.write_bytes(b"native binary")
    receipt = await client.upload_file(source)
    for owner, session_id, call in (
        ("other-owner@example.com", SESSION, CALL),
        (OWNER, "other-session", CALL),
        (OWNER, SESSION, "other-call"),
    ):
        with pytest.raises(TransferRejected):
            await service.claim(receipt, owner_email=owner, session_id=session_id, call_id=call)
    with pytest.raises(TransferRejected):
        await service.claim(
            receipt.model_copy(update={"sha256": "0" * 64}),
            owner_email=OWNER,
            session_id=SESSION,
            call_id=CALL,
        )
    await service.close_scope(scope_id, cancelled=True)
    with pytest.raises(TransferRejected):
        await service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.post(
            "http://transfer.test/api/v1/transfers",
            json=UploadRequest(slot=1, filename="f.bin", size_bytes=1).model_dump(),
        )
        assert response.status_code == 401


@pytest.mark.parametrize("actual", [b"x", b"xxx"])
async def test_truncated_or_oversized_body_is_not_completed(env, actual: bytes) -> None:
    factory, store, service, _, client, _ = env
    descriptor = await service.reserve(
        client.grant.token, UploadRequest(slot=0, filename="f.bin", size_bytes=2)
    )

    async def body():
        yield actual

    with pytest.raises(ValueError):
        await service.receive(client.grant.token, descriptor.transfer_id, body())
    async with factory() as db:
        row = await db.get(BinaryTransferRow, descriptor.transfer_id)
        assert row.state == "failed"
        assert not await store.async_exists(row.namespace, row.artifact_id, row.filename)


async def test_cancelled_receive_joins_cleanup_and_revokes_late_publication(env) -> None:
    factory, store, service, _, client, scope_id = env
    descriptor = await service.reserve(
        client.grant.token, UploadRequest(slot=0, filename="f.bin", size_bytes=2)
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def body():
        yield b"x"
        started.set()
        await release.wait()
        yield b"x"

    upload = asyncio.create_task(
        service.receive(client.grant.token, descriptor.transfer_id, body())
    )
    await started.wait()
    await service.close_scope(scope_id, cancelled=True)
    release.set()
    with pytest.raises(TransferRejected):
        await upload
    async with factory() as db:
        row = await db.get(BinaryTransferRow, descriptor.transfer_id)
        assert row.state == "failed"
        assert not await store.async_exists(row.namespace, row.artifact_id, row.filename)


async def test_receiving_and_consuming_can_use_different_controller_instances(
    env, tmp_path: Path
) -> None:
    factory, store, service, app, client, _ = env
    app.state.binary_transfers = BinaryTransferService(factory, store)
    source = tmp_path / "shared.bin"
    source.write_bytes(b"shared across controllers")
    receipt = await client.upload_file(source)
    third_controller = BinaryTransferService(factory, store)
    staged = await third_controller.claim(
        receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL
    )
    assert await store.async_load(staged.namespace, staged.artifact_id, staged.filename) == (
        b"shared across controllers",
        "application/octet-stream",
    )


async def test_new_attempt_fences_old_unpublished_receipts(env, tmp_path: Path) -> None:
    _, _, service, _, client, _ = env
    source = tmp_path / "attempt.bin"
    source.write_bytes(b"old attempt")
    receipt = await client.upload_file(source)
    await service.issue(
        owner_email=OWNER,
        agent_id=AGENT,
        session_id=SESSION,
        conversation_id=CONVERSATION,
        call_id=CALL,
        tool_name="artifact_publish",
        executor_id="binary-transfer-executor",
    )
    with pytest.raises(TransferRejected):
        await service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)


@pytest.mark.parametrize("superseding", [False, True])
async def test_concurrent_receipt_publication_is_atomic_and_recoverable(
    env, tmp_path: Path, superseding: bool
) -> None:
    factory, store, service, _, client, _ = env
    source = tmp_path / "claim.bin"
    source.write_bytes(b"one publication owner")
    supersedes = None
    receipt = await client.upload_file(source)
    claims = await asyncio.gather(
        *(
            service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
            for _ in range(2)
        ),
    )
    async with factory() as db:
        assert (await db.get(BinaryTransferRow, receipt.transfer_id)).state == "uploaded"

    async def publish(staged):
        return await write_artifact_document(
            session_factory=factory,
            artifact_store=store,
            actor=ContentActor(OWNER, AGENT, CONVERSATION, SESSION),
            staged=staged,
            filename=staged.filename,
            mime_type=staged.mime_type,
            kind="file",
            purpose="artifact_publish",
            supersedes=supersedes,
        )

    if superseding:
        root, _ = await publish(claims[0])
        supersedes = root.artifact_id
        receipt = await client.upload_file(source)
        claims = await asyncio.gather(
            *(
                service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
                for _ in range(2)
            )
        )
        # Both detached receipts precede the commit that makes supersedes stale.
        first, recovered = await publish(claims[0]), await publish(claims[1])
    else:
        first, recovered = await asyncio.gather(*(publish(staged) for staged in claims))
    assert first[0].artifact_id == recovered[0].artifact_id
    assert first[0].revision == recovered[0].revision == (2 if superseding else 1)


async def test_json_publication_validation_is_streamed(env, tmp_path: Path) -> None:
    factory, store, _, _, client, _ = env
    source = tmp_path / "invalid.json"
    source.write_bytes(b'{"large": [1,2,]}')
    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_file(source, mime_type="application/json")
    async with factory() as db:
        row = await db.scalar(select(BinaryTransferRow))
        assert row.state == "failed"
        assert not await store.async_exists(row.namespace, row.artifact_id, row.filename)


async def test_unreachable_controller_local_storage_cannot_publish(env, tmp_path: Path) -> None:
    factory, _, _, _, client, _ = env
    source = tmp_path / "not-shared.bin"
    source.write_bytes(b"must not become a broken public artifact")
    receipt = await client.upload_file(source)
    isolated = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "other-controller")))
    consumer = BinaryTransferService(factory, isolated)
    with pytest.raises(TransferRejected, match="shared storage"):
        await consumer.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
    async with factory() as db:
        row = await db.get(BinaryTransferRow, receipt.transfer_id)
        assert row.state == "uploaded"


async def test_lost_upload_response_reconciles_without_resending(env, tmp_path: Path) -> None:
    _, _, _, app, original_client, _ = env

    class LostAcknowledgement(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=app)
            self.puts = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await self.inner.handle_async_request(request)
            if request.method == "PUT":
                self.puts += 1
                await response.aclose()
                raise httpx.ReadError("Lost upload acknowledgement", request=request)
            return response

    transport = LostAcknowledgement()
    client = BinaryTransferClient(original_client.grant, transport=transport)
    source = tmp_path / "ack.bin"
    source.write_bytes(os.urandom(3 * 1024 * 1024))
    receipt = await client.upload_file(source)
    assert receipt.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert transport.puts == 1


async def test_call_scoped_router_publish_and_save_are_metadata_only(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from cognis.core.tool_router import ToolRouter
    from cognis.models.agent import AgentDefinition
    from cognis.models.session import SessionModel
    from cognis.models.tool import ExecutorCapabilities, ExecutorHandle, ToolCall
    from cognis.tools.executor.document import ARTIFACT_PUBLISH_TOOL, handle_artifact_publish
    from cognis.tools.registry import RegisteredTool

    factory, store, _, app, _, _ = env
    monkeypatch.setattr(
        BinaryTransferClient,
        "_http",
        lambda self: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=None),
    )
    router = ToolRouter(guardrails=Mock(), session_factory=factory, artifact_store=store)
    session = SessionModel(
        session_id=SESSION, user_email=OWNER, agent_id=AGENT, conversation_id=CONVERSATION
    )
    executor = SimpleNamespace(
        handle=ExecutorHandle(
            executor_id="binary-transfer-executor",
            executor_type="in_process",
            capabilities=ExecutorCapabilities(binary_transfers=True),
        )
    )
    source = tmp_path / "routed.bin"
    source.write_bytes(os.urandom(2 * 1024 * 1024))
    call = ToolCall(call_id=CALL, name="artifact_publish", arguments={"path": str(source)})
    result = await router._execute_binary_capable_tool(
        call,
        session=session,
        executor=executor,
        local_handler=True,
        registered_tool=RegisteredTool(
            definition=ARTIFACT_PUBLISH_TOOL, handler=handle_artifact_publish
        ),
    )
    assert "content_b64" not in result.model_dump_json()
    assert len(result.model_dump_json()) < 4096
    published = await router._materialize_inline_attachments(
        result, session, call.name, arguments=call.arguments, call_id=CALL
    )
    artifact_id = published.attachments[0]["artifact_id"]
    prepared = await router._prepare_local_tool_call(
        ToolCall(
            call_id="save-native",
            name="artifact_save",
            arguments={
                "source_artifact_id": artifact_id,
                "file_path": str(tmp_path / "saved.bin"),
            },
        ),
        session,
        AgentDefinition(agent_id=AGENT, name="Binary transfer agent", owner_email=OWNER),
        binary_transfers=True,
    )
    assert "source_artifact_content_b64" not in prepared.arguments
    assert prepared.arguments["source_artifact_download"]["size_bytes"] == source.stat().st_size


async def test_transfer_schema_bootstrap_and_large_sizes(env) -> None:
    from cognis.store.binary_transfer_schema import ensure_binary_transfer_schema
    from cognis.store.models import ArtifactRecordRow

    factory, _, _, _, _, _ = env
    async with factory() as db:
        engine = db.bind
    async with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(
                text("ALTER TABLE artifacts ALTER COLUMN size_bytes TYPE INTEGER")
            )
        await connection.run_sync(ensure_binary_transfer_schema)
        await connection.run_sync(ensure_binary_transfer_schema)
        if connection.dialect.name == "postgresql":
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("artifacts"))
            assert isinstance(
                next(c for c in columns if c["name"] == "size_bytes")["type"], BigInteger
            )
    async with factory() as db:
        row = ArtifactRecordRow(
            artifact_id="art_" + uuid.uuid4().hex,
            owner_email=OWNER,
            namespace="artifacts",
            object_id="large-metadata-record",
            filename="large.bin",
            mime_type="application/octet-stream",
            kind="file",
            purpose="transfer-validation",
            size_bytes=3 * 1024 * 1024 * 1024,
        )
        db.add(row)
        await db.commit()
        assert (await db.get(ArtifactRecordRow, row.artifact_id)).size_bytes > 2**31
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import update

    migration = import_module("cognis.store.migrations.versions.174_native_binary_transfers")

    def migrate(connection):
        with Operations.context(MigrationContext.configure(connection)):
            if connection.dialect.name == "postgresql":
                with pytest.raises(RuntimeError, match="exceed 2 GiB"):
                    migration.downgrade()
                assert inspect(connection).has_table("binary_transfers")
                connection.execute(
                    update(ArtifactRecordRow)
                    .where(ArtifactRecordRow.artifact_id == row.artifact_id)
                    .values(size_bytes=1024)
                )
            migration.downgrade()
            assert not inspect(connection).has_table("binary_transfers")
            migration.upgrade()
            migration.upgrade()
            assert inspect(connection).has_table("binary_transfers")
            assert inspect(connection).has_table("binary_transfer_scopes")

    async with engine.begin() as connection:
        await connection.run_sync(migrate)


@pytest.mark.parametrize("phase", ["upload", "publication"])
async def test_lost_database_commit_acknowledgement_preserves_bytes(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    factory, store, service, _, client, scope_id = env
    assert client.grant is not None
    content = os.urandom(1024 * 1024 + 7)
    path = tmp_path / "uncertain.bin"
    path.write_bytes(content)
    descriptor = await service.reserve(
        client.grant.token, UploadRequest(slot=0, filename=path.name, size_bytes=len(content))
    )
    if phase == "publication":
        receipt = await client.upload_file(path)
        await service.close_scope(scope_id)
        staged = await service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
    original = AsyncSession.commit
    injected = False

    async def uncertain_commit(db: AsyncSession) -> None:
        nonlocal injected
        await db.flush()
        state = await db.scalar(
            select(BinaryTransferRow.state).where(
                BinaryTransferRow.transfer_id == descriptor.transfer_id
            )
        )
        await original(db)
        if state == ("uploaded" if phase == "upload" else "consumed") and not injected:
            injected = True
            raise OSError("Database commit acknowledgement lost")

    monkeypatch.setattr(AsyncSession, "commit", uncertain_commit)

    async def chunks():
        yield content

    async def publish(staged):
        return await write_artifact_document(
            session_factory=factory,
            artifact_store=store,
            actor=ContentActor(
                user_email=OWNER,
                agent_id=AGENT,
                conversation_id=CONVERSATION,
                session_id=SESSION,
            ),
            staged=staged,
            filename=staged.filename,
            mime_type=staged.mime_type,
            kind="file",
            purpose="artifact_publish",
        )

    with pytest.raises(OSError, match="acknowledgement"):
        if phase == "upload":
            await service.receive(client.grant.token, descriptor.transfer_id, chunks())
        else:
            await publish(staged)
    assert injected
    async with factory() as db:
        row = await db.get(BinaryTransferRow, descriptor.transfer_id)
        assert row is not None
    assert (await store.async_load(row.namespace, row.artifact_id, row.filename))[0] == content
    if phase == "upload":
        reconciled = await service.status(client.grant.token, descriptor.transfer_id)
        assert reconciled.receipt is not None
    else:
        replay = await service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
        published, _ = await publish(replay)
        assert published.artifact_id == row.artifact_id


async def test_native_revision_conflict_returns_latest_head(env, tmp_path: Path) -> None:
    from unittest.mock import Mock

    from cognis.core.tool_router import ToolRouter
    from cognis.models.session import SessionModel
    from cognis.models.tool import ToolResult

    factory, store, _, _, client, _ = env
    router = ToolRouter(guardrails=Mock(), session_factory=factory, artifact_store=store)
    session = SessionModel(
        session_id=SESSION, user_email=OWNER, agent_id=AGENT, conversation_id=CONVERSATION
    )
    source = tmp_path / "revised.bin"
    source.write_bytes(b"revision bytes")

    async def publish(supersedes=None):
        receipt = await client.upload_file(source)
        return await router._materialize_inline_attachments(
            ToolResult(output="", attachments=[{"transfer_receipt": receipt.model_dump()}]),
            session,
            "artifact_publish",
            call_id=CALL,
            arguments={"supersedes": supersedes} if supersedes else {},
        )

    first = await publish()
    first_id = first.attachments[0]["artifact_id"]
    second = await publish(first_id)
    rejected = await publish(first_id)
    assert rejected.is_error
    assert rejected.metadata["reason"] == "supersedes_not_latest"
    assert rejected.metadata["latest_artifact_id"] == second.attachments[0]["artifact_id"]


async def test_native_attachment_preserves_revocable_conversation_scope(
    env, tmp_path: Path
) -> None:
    from unittest.mock import Mock

    from cognis.core.content_projects import move_conversation_documents
    from cognis.core.tool_router import ToolRouter
    from cognis.models.session import SessionModel
    from cognis.models.tool import ToolResult
    from cognis.store.models import ArtifactRecordRow, Conversation
    from cognis.store.queries import create_project

    factory, store, _, _, client, _ = env
    project_id = "proj_" + uuid.uuid4().hex
    async with factory() as db:
        await create_project(db, owner_email=OWNER, name="Transfer sharing", project_id=project_id)
        conversation = await db.get(Conversation, CONVERSATION)
        conversation.project_id, conversation.visibility = project_id, "project"
        await db.commit()
    source = tmp_path / "capture.bin"
    source.write_bytes(b"private screenshot bytes")
    receipt = await client.upload_file(source)
    router = ToolRouter(guardrails=Mock(), session_factory=factory, artifact_store=store)
    session = SessionModel(
        session_id=SESSION, user_email=OWNER, agent_id=AGENT, conversation_id=CONVERSATION
    )
    result = await router._materialize_inline_attachments(
        ToolResult(output="", attachments=[{"transfer_receipt": receipt.model_dump()}]),
        session,
        "browser_screenshot",
        call_id=CALL,
    )
    artifact_id = result.attachments[0]["artifact_id"]
    async with factory() as db:
        record = await db.get(ArtifactRecordRow, artifact_id)
        assert record.project_id is None
        assert record.purpose == "browser_screenshot"
        conversation = await db.get(Conversation, CONVERSATION)
        conversation.visibility = "private"
        await move_conversation_documents(db, CONVERSATION, old_scope=project_id, new_scope=None)
        await db.commit()
    async with factory() as db:
        record = await db.get(ArtifactRecordRow, artifact_id)
        assert record.project_id is None
        upload = await db.get(BinaryTransferRow, receipt.transfer_id)
        assert upload.state == "consumed"
    replay = await router._materialize_inline_attachments(
        ToolResult(output="", attachments=[{"transfer_receipt": receipt.model_dump()}]),
        session,
        "browser_screenshot",
        call_id=CALL,
    )
    assert replay.attachments[0]["artifact_id"] == artifact_id


async def test_native_document_asset_bypasses_unrelated_remote_url_limit(
    env, tmp_path: Path
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from cognis.core.tool_router import ToolRouter
    from cognis.models.agent import AgentDefinition
    from cognis.models.session import SessionModel
    from cognis.models.tool import ToolCall
    from cognis.tools.executor.document import _load_asset_bytes

    factory, store, service, _, client, _ = env
    source = tmp_path / "large-asset.png"
    content = os.urandom(11 * 1024 * 1024)
    source.write_bytes(content)
    receipt = await client.upload_file(source, mime_type="image/png")
    staged = await service.claim(receipt, owner_email=OWNER, session_id=SESSION, call_id=CALL)
    row, _ = await write_artifact_document(
        session_factory=factory,
        artifact_store=store,
        actor=ContentActor(OWNER, AGENT, CONVERSATION, SESSION),
        staged=staged,
        filename=staged.filename,
        mime_type=staged.mime_type,
        kind="image",
        purpose="artifact_publish",
    )
    router = ToolRouter(guardrails=Mock(), session_factory=factory, artifact_store=store)
    prepared = await router._prepare_local_tool_call(
        ToolCall(
            call_id=uuid.uuid4().hex,
            name="document_generate",
            arguments={
                "content": "Asset",
                "assets": [{"name": "image", "artifact_id": row.artifact_id}],
            },
        ),
        SessionModel(
            session_id=SESSION, user_email=OWNER, agent_id=AGENT, conversation_id=CONVERSATION
        ),
        AgentDefinition(agent_id=AGENT, name="Transfer agent", owner_email=OWNER),
        binary_transfers=True,
    )
    asset = prepared.arguments["assets"][0]
    assert "artifact_download" in asset and "url" not in asset and "content_b64" not in asset
    downloaded, mime_type, _ = await _load_asset_bytes(
        asset, SimpleNamespace(binary_transfers=client)
    )
    assert downloaded == content and mime_type == "image/png"
