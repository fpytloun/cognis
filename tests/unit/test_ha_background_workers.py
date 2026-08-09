from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import cognis.core.mcp_oauth as mcp_oauth_module
import cognis.knowledgebase.indexer as kb_indexer_module
from cognis.core.local_model_runtime import LocalModelRuntimeManager
from cognis.core.mcp_oauth import MCPOAuthError, MCPOAuthService
from cognis.knowledgebase.indexer import KnowledgebaseIndexer, _KnowledgebaseLeaseLost
from cognis.store.coordination import DatabaseLeaseStore
from cognis.store.models import (
    Base,
    CoordinationLeaseRow,
    KnowledgebaseIndexJobRow,
    LocalModelOperation,
    MCPOAuthTokenRow,
    MCPOAuthTransactionRow,
    MCPServerRow,
    User,
)


class _WebsocketProvider:
    def register_local_model_callbacks(self, **kwargs: object) -> None:
        del kwargs


def _indexer(
    session_factory: async_sessionmaker,
    *,
    owner_id: str,
) -> KnowledgebaseIndexer:
    return KnowledgebaseIndexer(
        session_factory=session_factory,
        artifact_store=object(),
        llm=object(),
        vector_backend=object(),
        enabled=True,
        poll_interval_seconds=0.01,
        max_artifact_size_bytes=1024,
        max_chunks_per_artifact=10,
        chunk_target_tokens=100,
        chunk_overlap_tokens=10,
        embedding_batch_size=2,
        controller_owner_id=owner_id,
    )


@pytest.mark.asyncio
async def test_two_kb_indexers_claim_one_job(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb-claim.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgebaseIndexJobRow(
                job_id="kbj-1",
                knowledgebase_id="kb-1",
                job_type="delete_artifact_index",
                status="queued",
            )
        )
        await session.commit()

    first = _indexer(session_factory, owner_id="controller-a")
    second = _indexer(session_factory, owner_id="controller-b")
    results = await asyncio.gather(first.run_once(), second.run_once())

    assert sorted(results) == [False, True]
    async with session_factory() as session:
        job = await session.get(KnowledgebaseIndexJobRow, "kbj-1")
        assert job is not None
        assert job.status == "succeeded"
        assert job.attempts == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_queued_kb_job_is_not_starved_by_live_running_leases(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb-starvation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                KnowledgebaseIndexJobRow(
                    job_id=f"kbj-running-{index}",
                    knowledgebase_id="kb-1",
                    job_type="delete_artifact_index",
                    status="running",
                    priority=1,
                )
                for index in range(20)
            ]
        )
        session.add(
            KnowledgebaseIndexJobRow(
                job_id="kbj-queued",
                knowledgebase_id="kb-1",
                job_type="delete_artifact_index",
                status="queued",
                priority=100,
            )
        )
        await session.commit()
    lease_store = DatabaseLeaseStore(session_factory)
    for index in range(20):
        assert (
            await lease_store.acquire(
                f"knowledgebase-index-job:kbj-running-{index}",
                "controller-a",
                ttl_seconds=60,
            )
            is not None
        )

    assert await _indexer(session_factory, owner_id="controller-b").run_once() is True
    async with session_factory() as session:
        queued = await session.get(KnowledgebaseIndexJobRow, "kbj-queued")
        assert queued is not None
        assert queued.status == "succeeded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_kb_owner_cannot_settle_after_takeover(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb-fence.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgebaseIndexJobRow(
                job_id="kbj-1",
                knowledgebase_id="kb-1",
                job_type="delete_artifact_index",
                status="running",
            )
        )
        await session.commit()
    first = _indexer(session_factory, owner_id="controller-a")
    second = _indexer(session_factory, owner_id="controller-b")
    old_lease = await first._lease_store.acquire(
        "knowledgebase-index-job:kbj-1", "controller-a", ttl_seconds=60
    )
    assert old_lease is not None
    async with session_factory() as session:
        row = await session.get(CoordinationLeaseRow, old_lease.resource_key)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    new_lease = await second._lease_store.acquire(
        "knowledgebase-index-job:kbj-1", "controller-b", ttl_seconds=60
    )
    assert new_lease is not None

    with pytest.raises(_KnowledgebaseLeaseLost):
        await first._finish_job("kbj-1", old_lease, status="succeeded")
    async with session_factory() as session:
        job = await session.get(KnowledgebaseIndexJobRow, "kbj-1")
        assert job is not None
        assert job.status == "running"
    await engine.dispose()


@pytest.mark.asyncio
async def test_kb_renewal_exception_fences_job_and_loop_processes_next(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb-renew-error.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                KnowledgebaseIndexJobRow(
                    job_id="kbj-renew-fails",
                    knowledgebase_id="kb-1",
                    job_type="delete_artifact_index",
                    status="queued",
                    priority=1,
                ),
                KnowledgebaseIndexJobRow(
                    job_id="kbj-next",
                    knowledgebase_id="kb-1",
                    job_type="delete_artifact_index",
                    status="queued",
                    priority=2,
                ),
            ]
        )
        await session.commit()
    monkeypatch.setattr(kb_indexer_module, "_JOB_LEASE_SECONDS", 2.0)
    indexer = _indexer(session_factory, owner_id="controller-a")
    original_renew = indexer._lease_store.renew
    renewal_calls = 0

    async def failing_renew(*args: object, **kwargs: object):
        nonlocal renewal_calls
        renewal_calls += 1
        if renewal_calls == 1:
            raise RuntimeError("database unavailable")
        return await original_renew(*args, **kwargs)

    async def slow_delete(job, lease, lost):
        del lease, lost
        if job.job_id == "kbj-renew-fails":
            await asyncio.sleep(0.8)
        return 0

    indexer._lease_store.renew = failing_renew
    indexer._delete_index = slow_delete

    assert await indexer.run_once() is True
    assert await indexer.run_once() is True
    async with session_factory() as session:
        failed_renewal = await session.get(KnowledgebaseIndexJobRow, "kbj-renew-fails")
        next_job = await session.get(KnowledgebaseIndexJobRow, "kbj-next")
        assert failed_renewal is not None
        assert failed_renewal.status == "running"
        assert next_job is not None
        assert next_job.status == "succeeded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_kb_lease_loss_during_current_check_is_observed(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb-current-race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    indexer = _indexer(session_factory, owner_id="controller-a")
    lease = await indexer._lease_store.acquire(
        "knowledgebase-index-job:kbj-current-race",
        "controller-a",
        ttl_seconds=60,
    )
    assert lease is not None
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_current(candidate):
        del candidate
        entered.set()
        await release.wait()
        return True

    indexer._lease_store.is_current = blocked_current
    lost = asyncio.Event()
    check = asyncio.create_task(indexer._require_current(lease, lost=lost))
    await entered.wait()
    lost.set()
    release.set()
    with pytest.raises(_KnowledgebaseLeaseLost):
        await check
    await engine.dispose()


@pytest.mark.asyncio
async def test_local_model_startup_respects_live_other_owner_and_recovers_expired(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'local-model-owner.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            LocalModelOperation(
                operation_id="lmo-1",
                deployment_id="lmd-1",
                executor_id="exec-1",
                generation=1,
                action="pull",
                state="running",
                progress_seq=0,
                progress_bytes=0,
                idempotency_key="key-1",
                request_hash="hash-1",
            )
        )
        await session.commit()
    lease_store = DatabaseLeaseStore(session_factory)
    lease = await lease_store.acquire("executor_connection:exec-1", "controller-a", ttl_seconds=60)
    assert lease is not None
    manager = LocalModelRuntimeManager(
        session_factory, _WebsocketProvider(), controller_owner_id="controller-b"
    )
    await manager.start()
    async with session_factory() as session:
        operation = await session.get(LocalModelOperation, "lmo-1")
        assert operation is not None
        assert operation.state == "running"
        row = await session.get(CoordinationLeaseRow, lease.resource_key)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await manager.stop()

    manager = LocalModelRuntimeManager(
        session_factory, _WebsocketProvider(), controller_owner_id="controller-b"
    )
    await manager.start()
    async with session_factory() as session:
        operation = await session.get(LocalModelOperation, "lmo-1")
        assert operation is not None
        assert operation.state == "interrupted"
    await manager.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_oauth_services_make_one_refresh_exchange_per_lease_epoch(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth-owner.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(User(email="user@example.com", password_hash="x", role="user"))
        session.add(
            MCPServerRow(
                server_id="mcp-1",
                name="test-mcp",
                transport="streamable_http",
                url="https://mcp.example/mcp",
                owner_email="user@example.com",
                auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
            )
        )
        session.add(
            MCPOAuthTokenRow(
                token_id="token-1",
                user_email="user@example.com",
                mcp_server_id="mcp-1",
                issuer="https://issuer.example",
                resource="https://mcp.example/mcp",
                resource_key="https://mcp.example/mcp",
                client_id="client-1",
                encrypted_payload=b"token",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"1" * 32))
    first = MCPOAuthService(
        session_factory=session_factory,
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        controller_owner_id="controller-a",
    )
    second = MCPOAuthService(
        session_factory=session_factory,
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        controller_owner_id="controller-b",
    )
    server = SimpleNamespace(
        server_id="mcp-1",
        url="https://mcp.example/mcp",
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
    )
    started = asyncio.Event()
    release = asyncio.Event()
    exchanges = 0

    async def exchange(self: MCPOAuthService, **kwargs: object) -> bool:
        nonlocal exchanges
        del self, kwargs
        exchanges += 1
        started.set()
        await release.wait()
        async with session_factory() as session:
            row = await session.get(MCPOAuthTokenRow, "token-1")
            assert row is not None
            row.version += 1
            row.last_refresh_at = datetime.now(UTC)
            row.last_refresh_error_code = None
            row.expires_at = datetime.now(UTC) + timedelta(hours=1)
            await session.commit()
        return True

    first._refresh_token_for_server_once = MethodType(exchange, first)
    second._refresh_token_for_server_once = MethodType(exchange, second)
    first_task = asyncio.create_task(
        first.refresh_token_for_server(
            user_email="user@example.com", server=server, token_id="token-1", force=True
        )
    )
    await started.wait()
    second_task = asyncio.create_task(
        second.refresh_token_for_server(
            user_email="user@example.com", server=server, token_id="token-1", force=True
        )
    )
    await asyncio.sleep(0.05)
    release.set()

    assert await first_task is True
    assert await second_task is True
    assert exchanges == 1
    await first.shutdown()
    await second.shutdown()
    await engine.dispose()


async def _oauth_service_with_token(tmp_path, *, filename: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / filename}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    key_path = tmp_path / f"{filename}.key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"3" * 32))
    service = MCPOAuthService(
        session_factory=session_factory,
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        controller_owner_id="controller-a",
    )
    async with session_factory() as session:
        session.add(User(email="renew@example.com", password_hash="x", role="user"))
        session.add(
            MCPServerRow(
                server_id="mcp-renew",
                name=f"renew-{filename}",
                transport="streamable_http",
                url="http://127.0.0.1/mcp",
                owner_email="renew@example.com",
                auth_config={"type": "oauth2", "issuer": "http://127.0.0.1"},
            )
        )
        session.add(
            MCPOAuthTokenRow(
                token_id="token-renew",
                user_email="renew@example.com",
                mcp_server_id="mcp-renew",
                issuer="http://127.0.0.1",
                resource="http://127.0.0.1/mcp",
                resource_key="http://127.0.0.1/mcp",
                client_id="client-renew",
                encrypted_payload=service._encrypt(
                    {
                        "access_token": "old-access",
                        "refresh_token": "refresh",
                        "token_endpoint": "http://127.0.0.1/token",
                    }
                ),
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()
    server = SimpleNamespace(
        server_id="mcp-renew",
        url="http://127.0.0.1/mcp",
        headers={},
        auth_config={"type": "oauth2", "issuer": "http://127.0.0.1"},
    )
    return engine, session_factory, service, server


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_succeeds", [True, False])
async def test_oauth_renew_exception_fences_refresh_without_overriding_provider_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    provider_succeeds: bool,
) -> None:
    engine, session_factory, service, server = await _oauth_service_with_token(
        tmp_path, filename=f"oauth-renew-{provider_succeeds}.db"
    )
    monkeypatch.setattr(mcp_oauth_module, "_OAUTH_LEASE_SECONDS", 0.15)

    async def failing_renew(*args: object, **kwargs: object):
        del args, kwargs
        raise RuntimeError("database unavailable")

    async def refresh(**kwargs: object):
        del kwargs
        await asyncio.sleep(0.12)
        if provider_succeeds:
            return {"access_token": "new-access", "expires_in": 3600}
        raise MCPOAuthError(
            "provider rejected refresh",
            reason="refresh_backend_failed",
            retryable=True,
        )

    service._lease_store.renew = failing_renew
    service._refresh_token = refresh
    if provider_succeeds:
        assert (
            await service.refresh_token_for_server(
                user_email="renew@example.com",
                server=server,
                token_id="token-renew",
                force=True,
            )
            is False
        )
    else:
        with pytest.raises(MCPOAuthError, match="provider rejected refresh"):
            await service.refresh_token_for_server(
                user_email="renew@example.com",
                server=server,
                token_id="token-renew",
                force=True,
            )
    async with session_factory() as session:
        token = await session.get(MCPOAuthTokenRow, "token-renew")
        assert token is not None
        assert token.version == 1
        assert token.last_refresh_at is None
        assert token.last_refresh_error_code is None
        assert token.next_refresh_attempt_at is None
    await service.shutdown()
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("backoff", [False, True])
async def test_oauth_real_token_no_refresh_and_backoff_branches(
    tmp_path,
    backoff: bool,
) -> None:
    engine, session_factory, service, server = await _oauth_service_with_token(
        tmp_path, filename=f"oauth-branch-{backoff}.db"
    )
    async with session_factory() as session:
        token = await session.get(MCPOAuthTokenRow, "token-renew")
        assert token is not None
        if backoff:
            token.next_refresh_attempt_at = datetime.now(UTC) + timedelta(minutes=1)
            token.last_refresh_error_code = "refresh_backend_failed"
        else:
            token.expires_at = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()
    provider_calls = 0

    async def refresh(**kwargs: object):
        nonlocal provider_calls
        del kwargs
        provider_calls += 1
        return {"access_token": "unexpected"}

    service._refresh_token = refresh
    if backoff:
        with pytest.raises(MCPOAuthError) as exc_info:
            await service.refresh_token_for_server(
                user_email="renew@example.com",
                server=server,
                token_id="token-renew",
            )
        assert exc_info.value.retryable is True
        assert exc_info.value.reason == "refresh_backend_failed"
    else:
        assert (
            await service.refresh_token_for_server(
                user_email="renew@example.com",
                server=server,
                token_id="token-renew",
            )
            is False
        )
    assert provider_calls == 0
    await service.shutdown()
    await engine.dispose()


@pytest.mark.asyncio
async def test_device_poller_checks_ownership_before_exchange_and_takeover(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth-device-owner.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    key_path = tmp_path / "device-key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    first = MCPOAuthService(
        session_factory=session_factory,
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        controller_owner_id="controller-a",
    )
    second = MCPOAuthService(
        session_factory=session_factory,
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        controller_owner_id="controller-b",
    )
    async with session_factory() as session:
        session.add(User(email="device@example.com", password_hash="x", role="user"))
        session.add(
            MCPServerRow(
                server_id="mcp-device",
                name="device-mcp",
                transport="streamable_http",
                url="https://mcp.example/device",
                owner_email="device@example.com",
            )
        )
        session.add(
            MCPOAuthTransactionRow(
                transaction_id="tx-device",
                user_email="device@example.com",
                mcp_server_id="mcp-device",
                issuer="https://issuer.example",
                authorization_server="https://issuer.example",
                resource="https://mcp.example/device",
                resource_key="https://mcp.example/device",
                redirect_uri="http://127.0.0.1/oauth/callback",
                client_id="client-device",
                code_challenge="challenge",
                state_hash="state",
                encrypted_payload=first._encrypt(
                    {
                        "flow": "device_code",
                        "device_code": "device-code",
                        "token_endpoint": "http://127.0.0.1/token",
                        "interval": 1,
                    }
                ),
                expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
        )
        await session.commit()
    resource_key = "mcp-oauth-device:tx-device"
    old_lease = await first._lease_store.acquire(resource_key, "controller-a", ttl_seconds=60)
    assert old_lease is not None
    async with session_factory() as session:
        lease_row = await session.get(CoordinationLeaseRow, resource_key)
        assert lease_row is not None
        lease_row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    new_lease = await second._lease_store.acquire(resource_key, "controller-b", ttl_seconds=60)
    assert new_lease is not None
    assert await second._lease_store.is_current(new_lease)
    exchanges = 0

    async def exchange(**kwargs: object) -> dict[str, object]:
        nonlocal exchanges
        del kwargs
        exchanges += 1
        return {"access_token": "access", "expires_in": 3600}

    first._exchange_device_code = exchange
    second._exchange_device_code = exchange

    async def first_is_current(session: object, lease: object) -> bool:
        del session, lease
        return False

    async def second_is_current(session: object, lease: object) -> bool:
        del session, lease
        return True

    first._oauth_lease_is_current = first_is_current
    second._oauth_lease_is_current = second_is_current
    await first._poll_device_authorization("tx-device", old_lease, asyncio.Event())
    async with session_factory() as session:
        pending = await session.get(MCPOAuthTransactionRow, "tx-device")
        assert pending is not None
        assert pending.status == "pending"
    await second._poll_device_authorization("tx-device", new_lease, asyncio.Event())

    assert exchanges == 1
    async with session_factory() as session:
        transaction = await session.get(MCPOAuthTransactionRow, "tx-device")
        assert transaction is not None
        assert transaction.status == "completed"
    await first.shutdown()
    await second.shutdown()
    await engine.dispose()


@pytest.mark.asyncio
async def test_device_renew_exception_stops_before_provider_exchange(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'device-renew-error.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    key_path = tmp_path / "device-renew-key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"4" * 32))
    service = MCPOAuthService(
        session_factory=session_factory,
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        controller_owner_id="controller-a",
    )
    async with session_factory() as session:
        session.add(User(email="device-renew@example.com", password_hash="x", role="user"))
        session.add(
            MCPServerRow(
                server_id="mcp-device-renew",
                name="device-renew",
                transport="streamable_http",
                url="http://127.0.0.1/device",
                owner_email="device-renew@example.com",
            )
        )
        session.add(
            MCPOAuthTransactionRow(
                transaction_id="tx-device-renew",
                user_email="device-renew@example.com",
                mcp_server_id="mcp-device-renew",
                issuer="http://127.0.0.1",
                authorization_server="http://127.0.0.1",
                resource="http://127.0.0.1/device",
                resource_key="http://127.0.0.1/device",
                redirect_uri="http://127.0.0.1/callback",
                client_id="client-device-renew",
                code_challenge="challenge",
                state_hash="state",
                encrypted_payload=service._encrypt(
                    {
                        "flow": "device_code",
                        "device_code": "device-code",
                        "token_endpoint": "http://127.0.0.1/token",
                        "interval": 1,
                    }
                ),
                expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
        )
        await session.commit()
    monkeypatch.setattr(mcp_oauth_module, "_OAUTH_LEASE_SECONDS", 4.0)

    async def failing_renew(*args: object, **kwargs: object):
        del args, kwargs
        raise RuntimeError("database unavailable")

    exchanges = 0
    exchange_started = asyncio.Event()
    exchange_release = asyncio.Event()

    async def exchange(**kwargs: object):
        nonlocal exchanges
        del kwargs
        exchanges += 1
        exchange_started.set()
        await exchange_release.wait()
        return {"access_token": "new-access", "expires_in": 3600}

    service._lease_store.renew = failing_renew
    service._exchange_device_code = exchange
    polling = asyncio.create_task(service._poll_device_authorization("tx-device-renew"))
    await exchange_started.wait()
    await asyncio.sleep(0.5)
    exchange_release.set()
    await polling

    assert exchanges == 1
    async with session_factory() as session:
        transaction = await session.get(MCPOAuthTransactionRow, "tx-device-renew")
        assert transaction is not None
        assert transaction.status == "pending"
    await service.shutdown()
    await engine.dispose()
