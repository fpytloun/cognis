from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from cognis.executor.channel_handler import ChannelHandler
from cognis.models.channel import (
    ChannelCapabilities,
    ChannelRecipient,
    InboundMessage,
    OutboundMessage,
    ResolvedChannelTarget,
)


class FakeWS:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, data: str) -> None:
        self.messages.append(json.loads(data))


class FakeAdapter:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.sent: list[OutboundMessage] = []
        self.on_message = None
        self.config = None
        self.credentials = None

    async def start(self, config, credentials, on_message) -> None:
        self.started = True
        self.config = config
        self.credentials = credentials
        self.on_message = on_message

    async def stop(self) -> None:
        self.stopped = True

    async def send_message(self, message: OutboundMessage) -> str:
        self.sent.append(message)
        return "platform-123"


@pytest.mark.asyncio
async def test_start_creates_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())

    result = await handler.start(
        "acct-1",
        "signal",
        {"display_name": "Signal", "agent_id": "a", "user_email": "u@example.com"},
        {"api_url": "http://localhost"},
    )

    assert result == {"status": "started", "account_id": "acct-1"}
    assert handler.active_count == 1
    assert adapter.started is True


@pytest.mark.asyncio
async def test_start_replaces_existing_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeAdapter()
    second = FakeAdapter()
    adapters = iter([first, second])
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: next(adapters))
    handler = ChannelHandler()
    handler.set_ws(FakeWS())

    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    assert first.stopped is True
    assert second.started is True
    assert handler.active_count == 1


@pytest.mark.asyncio
async def test_stop_and_stop_nonexistent(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    stopped = await handler.stop("acct-1")
    missing = await handler.stop("missing")

    assert stopped["status"] == "stopped"
    assert adapter.stopped is True
    assert missing["status"] == "not_found"


@pytest.mark.asyncio
async def test_send_delegates_to_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    result = await handler.send(
        "acct-1",
        {
            "channel_type": "signal",
            "chat_id": "+420",
            "content": "hello",
            "reply_to_id": "1",
            "media": [
                {
                    "platform_id": "remote-1",
                    "mime_type": "image/png",
                }
            ],
            "platform_data": {"format": "rich"},
        },
    )

    assert result == {"status": "sent", "platform_message_id": "platform-123"}
    assert len(adapter.sent) == 1
    assert adapter.sent[0].content == "hello"
    assert adapter.sent[0].media[0].platform_id == "remote-1"
    assert adapter.sent[0].platform_data == {"format": "rich"}


@pytest.mark.asyncio
async def test_send_unknown_account_returns_error() -> None:
    handler = ChannelHandler()
    result = await handler.send("missing", {"content": "hello"})
    assert "error" in result


@pytest.mark.asyncio
async def test_resolve_recipient_validates_account_and_recipient_without_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    adapter.capabilities = ChannelCapabilities(
        recipient_capabilities={
            "address_kinds": ["signal_e164"],
            "supports_resolution": True,
        }
    )
    target = ResolvedChannelTarget(
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        chat_kind="direct",
    )
    adapter.resolve_recipient = AsyncMock(return_value=target)  # type: ignore[attr-defined]
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    started = await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u"}, {})
    assert started["capabilities"]["recipient_capabilities"]["supports_resolution"] is True

    raw_address = "+420111222333"
    result = await handler.resolve_recipient(
        "acct-1",
        ChannelRecipient(
            channel_type="signal",
            address=raw_address,
            address_kind="signal_e164",
            chat_kind="direct",
        ).model_dump(mode="json"),
        "opaque-key",
    )
    assert result == target.model_dump(mode="json")
    adapter.resolve_recipient.assert_awaited_once()
    assert adapter.resolve_recipient.await_args.args[0].address == raw_address

    malformed = await handler.resolve_recipient(
        "acct-1",
        {"channel_type": "signal", "address": raw_address, "unexpected": "value"},
        "opaque-key",
    )
    assert malformed["error"]["code"] == "malformed_recipient"
    assert raw_address not in str(malformed)

    missing = await handler.resolve_recipient(
        "missing",
        {"channel_type": "signal", "address": raw_address},
        "opaque-key",
    )
    assert missing["error"]["code"] == "account_not_found"
    assert raw_address not in str(missing)


@pytest.mark.asyncio
async def test_resolve_recipient_rejects_unsupported_and_sanitizes_adapter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    adapter.capabilities = ChannelCapabilities()
    adapter.resolve_recipient = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("provider leaked +420111222333")
    )
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u"}, {})

    unsupported = await handler.resolve_recipient(
        "acct-1",
        {"channel_type": "signal", "address": "+420111222333"},
        "opaque-key",
    )
    assert unsupported["error"]["code"] == "unsupported_resolution"

    adapter.capabilities = ChannelCapabilities(
        recipient_capabilities={
            "address_kinds": ["signal_e164"],
            "supports_resolution": True,
        }
    )
    failed = await handler.resolve_recipient(
        "acct-1",
        {
            "channel_type": "signal",
            "address": "+420111222333",
            "address_kind": "signal_e164",
            "chat_kind": "direct",
        },
        "opaque-key",
    )
    assert failed["error"]["code"] == "resolution_failed"
    assert failed["error"]["retryable"] is True
    assert "+420111222333" not in str(failed)


@pytest.mark.asyncio
async def test_resolve_recipient_failure_certainty_tracks_creation_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    adapter.capabilities = ChannelCapabilities(
        recipient_capabilities={
            "address_kinds": ["signal_e164"],
            "supports_resolution": True,
            "supports_creation": True,
        }
    )
    adapter.resolve_recipient = AsyncMock(side_effect=RuntimeError("provider failure"))  # type: ignore[attr-defined]
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u"}, {})

    def recipient(*, allow_creation: bool) -> dict[str, object]:
        return ChannelRecipient(
            channel_type="signal",
            address="+420111222333",
            address_kind="signal_e164",
            chat_kind="direct",
            allow_resolution=True,
            allow_creation=allow_creation,
        ).model_dump(mode="json")

    created_failure = await handler.resolve_recipient(
        "acct-1", recipient(allow_creation=True), "opaque-key"
    )
    assert created_failure["error"]["side_effect_certainty"] == "uncertain"

    non_created_failure = await handler.resolve_recipient(
        "acct-1", recipient(allow_creation=False), "opaque-key"
    )
    assert non_created_failure["error"]["side_effect_certainty"] == "none"

    explicit = RuntimeError("provider failure")
    explicit.side_effect_certainty = "known"  # type: ignore[attr-defined]
    adapter.resolve_recipient.side_effect = explicit
    explicit_failure = await handler.resolve_recipient(
        "acct-1", recipient(allow_creation=True), "opaque-key"
    )
    assert explicit_failure["error"]["side_effect_certainty"] == "known"


@pytest.mark.asyncio
async def test_resolve_recipient_allows_discord_creation_without_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    adapter.channel_type = "discord"
    adapter.capabilities = ChannelCapabilities(
        recipient_capabilities={
            "address_kinds": ["discord_user_id"],
            "chat_kinds": ["direct"],
            "supports_resolution": False,
            "supports_creation": True,
        }
    )
    target = ResolvedChannelTarget(
        channel_type="discord",
        account_id="acct-discord",
        chat_id="123456789012345",
        chat_kind="direct",
    )
    adapter.resolve_recipient = AsyncMock(return_value=target)  # type: ignore[attr-defined]
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    await handler.start("acct-discord", "discord", {"agent_id": "a", "user_email": "u"}, {})

    result = await handler.resolve_recipient(
        "acct-discord",
        ChannelRecipient(
            channel_type="discord",
            address="123456789012345",
            address_kind="discord_user_id",
            chat_kind="direct",
            allow_creation=True,
        ).model_dump(mode="json"),
        "opaque-key",
    )

    assert result == target.model_dump(mode="json")
    adapter.resolve_recipient.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_message_sends_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    ws = FakeWS()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(ws)
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    assert adapter.on_message is not None
    await adapter.on_message(
        InboundMessage(
            channel_type="signal",
            account_id="acct-1",
            message_id="m1",
            sender_id="sender",
            chat_id="chat",
            content="hello",
            timestamp=datetime.now(UTC),
        )
    )

    assert ws.messages[0]["method"] == "channel.message"
    assert ws.messages[0]["params"]["account_id"] == "acct-1"


@pytest.mark.asyncio
async def test_send_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    adapter.send_typing = AsyncMock()  # type: ignore[assignment]
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    result = await handler.send_typing("acct-1", "+420111222333")
    assert result == {"status": "ok"}
    adapter.send_typing.assert_called_once_with("+420111222333")


@pytest.mark.asyncio
async def test_send_typing_unknown_account() -> None:
    handler = ChannelHandler()
    result = await handler.send_typing("missing", "+420111222333")
    assert "error" in result


@pytest.mark.asyncio
async def test_mark_read(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    adapter.mark_read = AsyncMock()  # type: ignore[assignment]
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    result = await handler.mark_read("acct-1", "+420111222333", "12345")
    assert result == {"status": "ok"}
    adapter.mark_read.assert_called_once_with("+420111222333", "12345")


@pytest.mark.asyncio
async def test_sync_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    adapter.sync_profile = AsyncMock()  # type: ignore[assignment]
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    result = await handler.sync_profile("acct-1", {"name": "TestBot"})
    assert result == {"status": "ok"}
    adapter.sync_profile.assert_called_once()


@pytest.mark.asyncio
async def test_stop_all(monkeypatch: pytest.MonkeyPatch) -> None:
    adapters = [FakeAdapter(), FakeAdapter()]
    iterator = iter(adapters)
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: next(iterator))
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})
    await handler.start("acct-2", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    await handler.stop_all()

    assert all(adapter.stopped for adapter in adapters)
    assert handler.active_count == 0


@pytest.mark.asyncio
async def test_signal_direct_uses_managed_signal_cli_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.channels.adapters.signal_cli_install import SignalCliStatus

    adapter = FakeAdapter()
    captured = {}
    monkeypatch.delenv("COGNIS_SIGNAL_CLI_COMMAND", raising=False)
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)

    async def fake_ensure_signal_cli(runtime_config):
        captured["runtime_config"] = runtime_config
        return SignalCliStatus(
            available=True,
            auto_install=True,
            version="0.14.5",
            command="/managed/signal-cli",
            installed_from="cache",
        )

    monkeypatch.setattr(
        "cognis.executor.channel_handler.ensure_signal_cli",
        fake_ensure_signal_cli,
    )

    handler = ChannelHandler()
    handler.set_executor_config({"signal": {"direct_enabled": True}})

    await handler.start(
        "acct-1",
        "signal",
        {"settings": {"transport": "direct_jsonrpc"}, "agent_id": "a", "user_email": "u"},
        {"account_number": "+10000000000"},
    )

    assert captured["runtime_config"].command is None
    assert adapter.config.settings["_signal_cli_command"] == "/managed/signal-cli"
    assert adapter.config.settings["_signal_cli_runtime"]["installed_from"] == "cache"


@pytest.mark.asyncio
async def test_signal_direct_config_command_requires_external_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.channels.adapters.signal_cli_install import SignalCliStatus

    adapter = FakeAdapter()
    captured = {}
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)

    async def fake_ensure_signal_cli(runtime_config):
        captured["runtime_config"] = runtime_config
        return SignalCliStatus(
            available=True,
            auto_install=True,
            version="0.14.5",
            command="/managed/signal-cli",
            installed_from="cache",
        )

    monkeypatch.setattr(
        "cognis.executor.channel_handler.ensure_signal_cli",
        fake_ensure_signal_cli,
    )

    handler = ChannelHandler()
    handler.set_executor_config(
        {"signal": {"direct_enabled": True, "command": "/usr/bin/signal-cli"}}
    )

    await handler.start(
        "acct-1",
        "signal",
        {"settings": {"transport": "direct_jsonrpc"}, "agent_id": "a", "user_email": "u"},
        {"account_number": "+10000000000"},
    )

    assert captured["runtime_config"].command is None
    assert captured["runtime_config"].use_external_command is False
    assert adapter.config.settings["_signal_cli_command"] == "/managed/signal-cli"
    assert adapter.config.settings["_signal_cli_runtime"]["installed_from"] == "cache"


@pytest.mark.asyncio
async def test_signal_direct_config_command_can_opt_in_to_external_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.channels.adapters.signal_cli_install import SignalCliStatus

    adapter = FakeAdapter()
    captured = {}
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)

    async def fake_ensure_signal_cli(runtime_config):
        captured["runtime_config"] = runtime_config
        return SignalCliStatus(
            available=True,
            auto_install=True,
            version="0.14.1",
            command="/usr/bin/signal-cli",
            warning="explicit override",
            installed_from="configured_command",
        )

    monkeypatch.setattr(
        "cognis.executor.channel_handler.ensure_signal_cli",
        fake_ensure_signal_cli,
    )

    handler = ChannelHandler()
    handler.set_executor_config(
        {
            "signal": {
                "direct_enabled": True,
                "use_external_command": True,
                "command": "/usr/bin/signal-cli",
            }
        }
    )

    await handler.start(
        "acct-1",
        "signal",
        {"settings": {"transport": "direct_jsonrpc"}, "agent_id": "a", "user_email": "u"},
        {"account_number": "+10000000000"},
    )

    assert captured["runtime_config"].command == "/usr/bin/signal-cli"
    assert captured["runtime_config"].use_external_command is True
    assert adapter.config.settings["_signal_cli_command"] == "/usr/bin/signal-cli"
    assert adapter.config.settings["_signal_cli_runtime"]["installed_from"] == "configured_command"
