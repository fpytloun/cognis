"""Tests for agent profile sync to channel adapters."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from cognis.channels.manager import ChannelManager, ChannelOwnershipLost
from cognis.core.events import Event, EventBus, EventType
from cognis.models.channel import AgentProfile, ChannelAccountConfig, OutboundMessage
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import create_agent, create_user, update_agent


class _FakeAdapter:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.synced_profiles: list[AgentProfile] = []
        self.channel_type = "signal"
        self.capabilities = None
        self._status = "connected"
        self.on_message: Any = None
        self.sent_messages: list[OutboundMessage] = []

    async def start(self, config: Any, credentials: Any, on_message: Any) -> None:
        self.started = True
        self.on_message = on_message

    async def stop(self) -> None:
        self.stopped = True

    async def sync_profile(self, profile: AgentProfile) -> None:
        self.synced_profiles.append(profile)

    async def send_message(self, message: OutboundMessage) -> str:
        self.sent_messages.append(message)
        return "message-1"

    async def get_status(self) -> Any:
        from cognis.models.channel import ChannelAccountStatus, ChannelStatus

        return ChannelAccountStatus(
            account_id="ch_test",
            channel_type="signal",
            status=ChannelStatus.CONNECTED,
        )


class _FailingSyncAdapter(_FakeAdapter):
    async def sync_profile(self, profile: AgentProfile) -> None:
        raise RuntimeError("sync boom")


class _FakeSecrets:
    async def get_secret(self, **kwargs: Any) -> str:
        return "tok"


class _FakeWebSocketProvider:
    def __init__(self) -> None:
        self._handles: dict[str, Any] = {}

    def get_connection(self, executor_id: str) -> None:
        del executor_id
        return None

    def iter_local_ready_handles(self) -> tuple[Any, ...]:
        return ()


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.signed_url_calls: list[tuple[str, str, str]] = []
        self.load_calls: list[tuple[str, str, str]] = []

    async def async_get_signed_url(
        self,
        bucket: str,
        artifact_id: str,
        name: str,
        *,
        ttl_seconds: int,
    ) -> str:
        del ttl_seconds
        self.signed_url_calls.append((bucket, artifact_id, name))
        if name != "image":
            raise FileNotFoundError(name)
        return "https://example.com/avatar.png"

    async def async_load(self, bucket: str, artifact_id: str, name: str) -> tuple[bytes, str]:
        self.load_calls.append((bucket, artifact_id, name))
        if name != "image":
            raise FileNotFoundError(name)
        return b"avatar-bytes", "image/png"


class _FakeHTTPResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeHTTPClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any] | None]] = []

    async def post(
        self, url: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> _FakeHTTPResponse:
        self.posts.append((url, json))
        return _FakeHTTPResponse({"ok": True, "ts": "123.456", "user_id": "U123"})

    async def get(self, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        return _FakeHTTPResponse({"ok": True})

    async def aclose(self) -> None:
        pass


def _config(agent_id: str = "agent-1") -> ChannelAccountConfig:
    return ChannelAccountConfig(
        account_id="ch_test",
        channel_type="signal",
        display_name="Test",
        credential_refs={},
        agent_id=agent_id,
        user_email="owner@example.com",
    )


async def _setup(tmp_path: Any) -> tuple[Any, Any, EventBus]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}")
    session_factory = create_session_factory(engine)
    from cognis.bootstrap import run_schema_bootstrap

    await run_schema_bootstrap(engine)
    async with session_factory() as session:
        await create_user(
            session, email="owner@example.com", name="Owner", password_hash="x", role="user"
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="owner@example.com",
            name="TestBot",
            display_name="Bot Display",
            status="active",
        )
        await create_agent(
            session,
            agent_id="agent-2",
            owner_email="owner@example.com",
            name="OtherBot",
            status="active",
        )
        await session.commit()
    event_bus = EventBus()
    return engine, session_factory, event_bus


async def _persist_config(session_factory: Any, config: ChannelAccountConfig) -> None:
    from cognis.store.models import ChannelAccountRow

    async with session_factory() as session:
        if await session.get(ChannelAccountRow, config.account_id) is None:
            session.add(
                ChannelAccountRow(
                    account_id=config.account_id,
                    channel_type=config.channel_type,
                    display_name=config.display_name,
                    credential_refs=config.credential_refs,
                    agent_id=config.agent_id,
                    user_email=config.user_email,
                    enabled=True,
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_start_all_defers_executor_channel_without_connected_executor(
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    try:
        caplog.set_level(logging.INFO, logger="cognis.channels.manager")
        from cognis.store.models import ChannelAccountRow

        async with session_factory() as session:
            session.add(
                ChannelAccountRow(
                    account_id="ch_executor",
                    channel_type="signal",
                    display_name="Executor Signal",
                    credential_refs={},
                    agent_id="agent-1",
                    user_email="owner@example.com",
                    adapter_location="executor",
                    executor_id=None,
                    enabled=True,
                )
            )
            await session.commit()

        manager = ChannelManager(
            session_factory=session_factory,
            inbound_pipeline=None,  # type: ignore[arg-type]
            secrets_provider=_FakeSecrets(),
            artifact_store=None,
            event_bus=event_bus,
            ws_provider=_FakeWebSocketProvider(),
        )

        await manager.start_all()

        assert "ch_executor" not in manager._adapters
        assert any(
            record.message == "channel manager: deferred executor-hosted account startup"
            for record in caplog.records
        )
        await manager.stop_all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_channel_account_single_owner_takeover_and_stale_stop_is_fenced(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    adapters: list[_FakeAdapter] = []
    managers: list[ChannelManager] = []
    try:
        from cognis.store.models import ChannelAccountRow

        async with session_factory() as session:
            session.add(
                ChannelAccountRow(
                    account_id="ch_test",
                    channel_type="signal",
                    display_name="HA Signal",
                    credential_refs={},
                    agent_id="agent-1",
                    user_email="owner@example.com",
                    enabled=True,
                )
            )
            await session.commit()

        def _adapter_factory(_: str) -> _FakeAdapter:
            adapter = _FakeAdapter()
            adapters.append(adapter)
            return adapter

        monkeypatch.setattr("cognis.channels.manager._create_adapter", _adapter_factory)
        monkeypatch.setattr("cognis.channels.manager._CHANNEL_LEASE_TTL_SECONDS", 1.0)
        monkeypatch.setattr("cognis.channels.manager._CHANNEL_RECONCILE_SECONDS", 0.1)
        managers = [
            ChannelManager(
                session_factory=session_factory,
                inbound_pipeline=None,  # type: ignore[arg-type]
                secrets_provider=_FakeSecrets(),
                artifact_store=None,
                event_bus=event_bus,
                controller_owner_id=f"controller-{index}",
            )
            for index in (1, 2)
        ]
        await asyncio.gather(*(manager.start_all() for manager in managers))
        assert sum(manager.get_adapter("ch_test") is not None for manager in managers) == 1
        owner = next(manager for manager in managers if manager.get_adapter("ch_test"))
        standby = next(manager for manager in managers if manager is not owner)
        old_adapter = owner._adapters["ch_test"]  # noqa: SLF001
        assert isinstance(old_adapter, _FakeAdapter)

        assert owner._ownership_task is not None  # noqa: SLF001
        owner._ownership_task.cancel()  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            await owner._ownership_task  # noqa: SLF001
        owner._ownership_task = None  # noqa: SLF001

        await asyncio.sleep(1.3)
        assert standby.get_adapter("ch_test") is not None
        assert old_adapter.on_message is not None
        await old_adapter.on_message(object())
        newer_adapter = standby.get_adapter("ch_test")
        newer_raw = standby._adapters["ch_test"]  # noqa: SLF001
        assert standby._ownership_task is not None  # noqa: SLF001
        standby._ownership_task.cancel()  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            await standby._ownership_task  # noqa: SLF001
        standby._ownership_task = None  # noqa: SLF001
        await owner.stop_account("ch_test")
        assert newer_adapter is not None
        assert not newer_raw.stopped
    finally:
        for manager in managers:
            await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_channel_ownership_loop_survives_renewal_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    manager = ChannelManager(
        session_factory=session_factory,
        inbound_pipeline=None,  # type: ignore[arg-type]
        secrets_provider=_FakeSecrets(),
        artifact_store=None,
        event_bus=event_bus,
        controller_owner_id="controller-1",
    )
    adapter = _FakeAdapter()
    try:
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)
        monkeypatch.setattr("cognis.channels.manager._CHANNEL_RECONCILE_SECONDS", 0.01)
        await _persist_config(session_factory, _config())
        await manager.start_account(_config())

        async def _fail_renew(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(manager._lease_store, "renew", _fail_renew)  # noqa: SLF001
        manager._ownership_task = asyncio.create_task(manager._ownership_loop())  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert manager._ownership_task is not None  # noqa: SLF001
        assert not manager._ownership_task.done()  # noqa: SLF001
        assert adapter.stopped
    finally:
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_account_syncs_profile(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    try:
        adapter = _FakeAdapter()
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)
        manager = ChannelManager(
            session_factory=session_factory,
            inbound_pipeline=None,  # type: ignore[arg-type]
            secrets_provider=_FakeSecrets(),
            artifact_store=None,
            event_bus=event_bus,
        )
        await _persist_config(session_factory, _config())
        await manager.start_account(_config())
        assert adapter.started
        assert len(adapter.synced_profiles) == 1
        assert adapter.synced_profiles[0].name == "TestBot"
        assert adapter.synced_profiles[0].effective_name == "Bot Display"
    finally:
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_account_loads_agent_avatar_image_artifact(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            await update_agent(
                session,
                "agent-1",
                updates={"avatar_image_id": "avatar-image-1"},
            )
            await session.commit()

        adapter = _FakeAdapter()
        artifact_store = _FakeArtifactStore()
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)
        manager = ChannelManager(
            session_factory=session_factory,
            inbound_pipeline=None,  # type: ignore[arg-type]
            secrets_provider=_FakeSecrets(),
            artifact_store=artifact_store,
            event_bus=event_bus,
        )

        await _persist_config(session_factory, _config())
        await manager.start_account(_config())

        assert len(adapter.synced_profiles) == 1
        profile = adapter.synced_profiles[0]
        assert profile.avatar_url == "https://example.com/avatar.png"
        assert profile.avatar_bytes == b"avatar-bytes"
        assert profile.avatar_content_type == "image/png"
        assert artifact_store.signed_url_calls == [("avatars", "avatar-image-1", "image")]
        assert artifact_store.load_calls == [("avatars", "avatar-image-1", "image")]
    finally:
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_profile_update_event_resyncs_affected_adapters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    try:
        adapter_a = _FakeAdapter()
        adapter_b = _FakeAdapter()
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter_a)
        manager = ChannelManager(
            session_factory=session_factory,
            inbound_pipeline=None,  # type: ignore[arg-type]
            secrets_provider=_FakeSecrets(),
            artifact_store=None,
            event_bus=event_bus,
        )
        await _persist_config(session_factory, _config("agent-1"))
        await manager.start_account(_config("agent-1"))
        # Manually inject second adapter for agent-2
        manager._adapters["ch_other"] = adapter_b
        manager._configs["ch_other"] = _config("agent-2")

        # Clear initial sync calls
        adapter_a.synced_profiles.clear()
        adapter_b.synced_profiles.clear()

        await event_bus.publish(
            Event(type=EventType.AGENT_PROFILE_UPDATED, data={"agent_id": "agent-1"})
        )

        assert len(adapter_a.synced_profiles) == 1
        assert len(adapter_b.synced_profiles) == 0
    finally:
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_profile_sync_failure_does_not_block_start(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    try:
        adapter = _FailingSyncAdapter()
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)
        manager = ChannelManager(
            session_factory=session_factory,
            inbound_pipeline=None,  # type: ignore[arg-type]
            secrets_provider=_FakeSecrets(),
            artifact_store=None,
            event_bus=event_bus,
        )
        # Should not raise despite sync_profile raising
        await _persist_config(session_factory, _config())
        await manager.start_account(_config())
        assert adapter.started
        assert "ch_test" in manager._adapters
    finally:
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_profile_sync_failure_does_not_block_event(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    try:
        adapter = _FailingSyncAdapter()
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)
        manager = ChannelManager(
            session_factory=session_factory,
            inbound_pipeline=None,  # type: ignore[arg-type]
            secrets_provider=_FakeSecrets(),
            artifact_store=None,
            event_bus=event_bus,
        )
        await _persist_config(session_factory, _config())
        await manager.start_account(_config())
        # Should not raise
        await event_bus.publish(
            Event(type=EventType.AGENT_PROFILE_UPDATED, data={"agent_id": "agent-1"})
        )
    finally:
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_slack_send_includes_agent_identity() -> None:
    from cognis.channels.adapters.slack import SlackAdapter

    adapter = SlackAdapter()
    client = _FakeHTTPClient()
    adapter._client = client
    adapter._bot_user_id = "U123"

    await adapter.sync_profile(
        AgentProfile(name="Bot", display_name="My Bot", avatar_url="https://example.com/avatar.png")
    )
    await adapter.send_message(
        OutboundMessage(channel_type="slack", account_id="a1", chat_id="C123", content="hello")
    )

    assert len(client.posts) == 1
    url, payload = client.posts[0]
    assert url == "/chat.postMessage"
    assert payload is not None
    assert payload.get("username") == "My Bot"
    assert payload.get("icon_url") == "https://example.com/avatar.png"


@pytest.mark.asyncio
async def test_slack_send_without_profile_omits_identity() -> None:
    from cognis.channels.adapters.slack import SlackAdapter

    adapter = SlackAdapter()
    client = _FakeHTTPClient()
    adapter._client = client
    adapter._bot_user_id = "U123"

    await adapter.send_message(
        OutboundMessage(channel_type="slack", account_id="a1", chat_id="C123", content="hello")
    )

    assert len(client.posts) == 1
    _, payload = client.posts[0]
    assert payload is not None
    assert "username" not in payload
    assert "icon_url" not in payload


@pytest.mark.asyncio
async def test_standby_revoke_invalidates_owner_view_before_external_send(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    managers: list[ChannelManager] = []
    try:
        await _persist_config(session_factory, _config())
        adapters: list[_FakeAdapter] = []

        def _factory(_: str) -> _FakeAdapter:
            adapter = _FakeAdapter()
            adapters.append(adapter)
            return adapter

        monkeypatch.setattr("cognis.channels.manager._create_adapter", _factory)
        managers = [
            ChannelManager(
                session_factory=session_factory,
                inbound_pipeline=None,  # type: ignore[arg-type]
                secrets_provider=_FakeSecrets(),
                artifact_store=None,
                event_bus=event_bus,
                controller_owner_id=f"controller-{index}",
            )
            for index in (1, 2)
        ]
        await asyncio.gather(*(manager.start_all() for manager in managers))
        owner = next(manager for manager in managers if manager.get_adapter("ch_test"))
        standby = next(manager for manager in managers if manager is not owner)
        stale_view = owner.get_adapter("ch_test")
        assert stale_view is not None

        assert await standby.revoke_account("ch_test") is False
        with pytest.raises(ChannelOwnershipLost):
            await stale_view.send_message(
                OutboundMessage(
                    channel_type="signal",
                    account_id="ch_test",
                    chat_id="chat-1",
                    content="must not send",
                )
            )
        assert all(adapter.sent_messages == [] for adapter in adapters)
    finally:
        for manager in managers:
            await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_standby_revoke_waits_for_admitted_owner_send(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    managers: list[ChannelManager] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingAdapter(_FakeAdapter):
        async def send_message(self, message: OutboundMessage) -> str:
            entered.set()
            await release.wait()
            return await super().send_message(message)

    try:
        await _persist_config(session_factory, _config())
        monkeypatch.setattr(
            "cognis.channels.manager._create_adapter",
            lambda _: _BlockingAdapter(),
        )
        managers = [
            ChannelManager(
                session_factory=session_factory,
                inbound_pipeline=None,  # type: ignore[arg-type]
                secrets_provider=_FakeSecrets(),
                artifact_store=None,
                event_bus=event_bus,
                controller_owner_id=f"controller-{index}",
            )
            for index in (1, 2)
        ]
        await asyncio.gather(*(manager.start_all() for manager in managers))
        owner = next(manager for manager in managers if manager.get_adapter("ch_test"))
        standby = next(manager for manager in managers if manager is not owner)
        view = owner.get_adapter("ch_test")
        assert view is not None
        send_task = asyncio.create_task(
            view.send_message(
                OutboundMessage(
                    channel_type="signal",
                    account_id="ch_test",
                    chat_id="chat-1",
                    content="admitted",
                )
            )
        )
        await entered.wait()
        revoke_task = asyncio.create_task(standby.revoke_account("ch_test"))
        await asyncio.sleep(0.05)
        assert not revoke_task.done()
        release.set()
        await send_task
        assert await revoke_task is False
    finally:
        release.set()
        for manager in managers:
            await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_adapter_guard_is_reentrant_for_nested_adapter_operation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)

    class _NestedAdapter(_FakeAdapter):
        view: Any = None

        async def send_message(self, message: OutboundMessage) -> str:
            await self.view.sync_profile(AgentProfile(name="Nested"))
            return await super().send_message(message)

    adapter = _NestedAdapter()
    manager = ChannelManager(
        session_factory=session_factory,
        inbound_pipeline=None,  # type: ignore[arg-type]
        secrets_provider=_FakeSecrets(),
        artifact_store=None,
        event_bus=event_bus,
    )
    try:
        await _persist_config(session_factory, _config())
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)
        await manager.start_account(_config())
        adapter.view = manager.get_adapter("ch_test")
        assert adapter.view is not None
        await asyncio.wait_for(
            adapter.view.send_message(
                OutboundMessage(
                    channel_type="signal",
                    account_id="ch_test",
                    chat_id="chat-1",
                    content="nested",
                )
            ),
            timeout=1,
        )
        assert adapter.sent_messages
    finally:
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_adapter_guard_does_not_leak_into_spawned_child_task(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    gate = asyncio.Event()

    class _SpawningAdapter(_FakeAdapter):
        view: Any = None
        child: asyncio.Task[Any] | None = None

        async def send_message(self, message: OutboundMessage) -> str:
            async def _delayed_nested_send() -> None:
                await gate.wait()
                await self.view.sync_profile(AgentProfile(name="Stale child"))

            self.child = asyncio.create_task(_delayed_nested_send())
            return await super().send_message(message)

    adapter = _SpawningAdapter()
    manager = ChannelManager(
        session_factory=session_factory,
        inbound_pipeline=None,  # type: ignore[arg-type]
        secrets_provider=_FakeSecrets(),
        artifact_store=None,
        event_bus=event_bus,
    )
    try:
        await _persist_config(session_factory, _config())
        monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)
        await manager.start_account(_config())
        adapter.view = manager.get_adapter("ch_test")
        assert adapter.view is not None
        await adapter.view.send_message(
            OutboundMessage(
                channel_type="signal",
                account_id="ch_test",
                chat_id="chat-1",
                content="spawn",
            )
        )
        assert adapter.child is not None
        await manager.revoke_account("ch_test")
        gate.set()
        with pytest.raises(ChannelOwnershipLost):
            await adapter.child
    finally:
        gate.set()
        await manager.stop_all()
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_positive_operation_counter_is_reset_during_drain(
    tmp_path: Any,
) -> None:
    engine, session_factory, event_bus = await _setup(tmp_path)
    from datetime import UTC, datetime, timedelta

    from cognis.store.models import ChannelAccountOperationRow

    manager = ChannelManager(
        session_factory=session_factory,
        inbound_pipeline=None,  # type: ignore[arg-type]
        secrets_provider=_FakeSecrets(),
        artifact_store=None,
        event_bus=event_bus,
        controller_owner_id="standby",
    )
    try:
        await _persist_config(session_factory, _config())
        async with session_factory() as session:
            session.add(
                ChannelAccountOperationRow(
                    account_id="ch_test",
                    owner_id="dead-owner",
                    fencing_token=7,
                    active_count=2,
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
            await session.commit()
        assert await manager.wait_until_relinquished("ch_test", timeout_seconds=1)
        async with session_factory() as session:
            state = await session.get(ChannelAccountOperationRow, "ch_test")
            assert state is not None
            assert state.active_count == 0
    finally:
        await manager.stop_all()
        await engine.dispose()
