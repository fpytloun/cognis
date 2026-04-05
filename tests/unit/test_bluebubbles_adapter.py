"""Unit tests for BlueBubbles adapter — config, webhook, inbound normalization."""

from __future__ import annotations

import json

import pytest

from cognis.channels.adapters.bluebubbles import BlueBubblesAdapter, _BlueBubblesConfig
from cognis.models.channel import (
    ChannelAccountConfig,
    InboundMessage,
)

# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------


class TestBlueBubblesConfig:
    def test_defaults(self) -> None:
        config = _BlueBubblesConfig(
            {}, {"server_url": "http://localhost:1234", "password": "secret"}
        )
        assert config.server_url == "http://localhost:1234"
        assert config.password == "secret"
        assert config.send_read_receipts is True
        assert config.enable_typing is True

    def test_trailing_slash_stripped(self) -> None:
        config = _BlueBubblesConfig({}, {"server_url": "http://localhost:1234/", "password": "s"})
        assert config.server_url == "http://localhost:1234"

    def test_features_disabled(self) -> None:
        config = _BlueBubblesConfig(
            {"send_read_receipts": "false", "enable_typing": False},
            {"server_url": "http://x", "password": "p"},
        )
        assert config.send_read_receipts is False
        assert config.enable_typing is False

    def test_missing_credentials(self) -> None:
        config = _BlueBubblesConfig({}, {})
        assert config.server_url == ""
        assert config.password == ""


# ---------------------------------------------------------------------------
# Adapter lifecycle tests
# ---------------------------------------------------------------------------


class TestBlueBubblesAdapterConnect:
    @pytest.mark.asyncio
    async def test_connect_requires_server_url(self) -> None:
        adapter = BlueBubblesAdapter()
        adapter._config = ChannelAccountConfig(
            account_id="acct-1",
            channel_type="bluebubbles",
            display_name="iMessage",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
        )
        adapter._credentials = {"password": "secret"}
        with pytest.raises(ValueError, match="server_url"):
            await adapter._connect()

    @pytest.mark.asyncio
    async def test_connect_requires_password(self) -> None:
        adapter = BlueBubblesAdapter()
        adapter._config = ChannelAccountConfig(
            account_id="acct-1",
            channel_type="bluebubbles",
            display_name="iMessage",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
        )
        adapter._credentials = {"server_url": "http://localhost:1234"}
        with pytest.raises(ValueError, match="password"):
            await adapter._connect()


# ---------------------------------------------------------------------------
# Webhook verification tests
# ---------------------------------------------------------------------------


class TestWebhookVerification:
    @pytest.mark.asyncio
    async def test_valid_password_header(self) -> None:
        adapter = BlueBubblesAdapter()
        result = await adapter.verify_webhook(
            headers={"x-password": "my-secret"},
            body=b"{}",
            secret="my-secret",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_valid_guid_header(self) -> None:
        adapter = BlueBubblesAdapter()
        result = await adapter.verify_webhook(
            headers={"x-guid": "my-secret"},
            body=b"{}",
            secret="my-secret",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_password_rejected(self) -> None:
        adapter = BlueBubblesAdapter()
        result = await adapter.verify_webhook(
            headers={"x-password": "wrong"},
            body=b"{}",
            secret="my-secret",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_secret_rejected(self) -> None:
        adapter = BlueBubblesAdapter()
        result = await adapter.verify_webhook(
            headers={"x-password": "anything"},
            body=b"{}",
            secret="",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_auth_header_rejected(self) -> None:
        """When no auth header or query param is present, reject."""
        adapter = BlueBubblesAdapter()
        result = await adapter.verify_webhook(
            headers={},
            body=b"{}",
            secret="my-secret",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_query_param_password_accepted(self) -> None:
        """BlueBubbles sends password in query string, injected as x-query-password."""
        adapter = BlueBubblesAdapter()
        result = await adapter.verify_webhook(
            headers={"x-query-password": "my-secret"},
            body=b"{}",
            secret="my-secret",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_query_param_wrong_password_rejected(self) -> None:
        adapter = BlueBubblesAdapter()
        result = await adapter.verify_webhook(
            headers={"x-query-password": "wrong"},
            body=b"{}",
            secret="my-secret",
        )
        assert result is False


# ---------------------------------------------------------------------------
# Webhook payload handling tests
# ---------------------------------------------------------------------------


class TestWebhookPayload:
    def _make_adapter(self) -> BlueBubblesAdapter:
        adapter = BlueBubblesAdapter()
        adapter._config = ChannelAccountConfig(
            account_id="acct-1",
            channel_type="bluebubbles",
            display_name="iMessage",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
        )
        return adapter

    @pytest.mark.asyncio
    async def test_new_message_dm(self) -> None:
        adapter = self._make_adapter()
        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        payload = {
            "type": "new-message",
            "data": {
                "guid": "msg-guid-1",
                "text": "Hello from iMessage!",
                "isFromMe": False,
                "dateCreated": 1700000000000,
                "handle": {
                    "address": "+15555550123",
                    "firstName": "John",
                    "lastName": "Doe",
                },
                "chats": [
                    {
                        "guid": "iMessage;-;+15555550123",
                        "displayName": None,
                    }
                ],
                "attachments": [],
            },
        }

        result = await adapter.handle_webhook_payload(json.dumps(payload).encode())
        assert result == {"status": "ok"}
        assert len(dispatched) == 1
        msg = dispatched[0]
        assert msg.content == "Hello from iMessage!"
        assert msg.sender_id == "+15555550123"
        assert msg.sender_name == "John Doe"
        assert msg.chat_id == "iMessage;-;+15555550123"
        assert msg.chat_type == "direct"
        assert msg.message_id == "msg-guid-1"

    @pytest.mark.asyncio
    async def test_new_message_group(self) -> None:
        adapter = self._make_adapter()
        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        payload = {
            "type": "new-message",
            "data": {
                "guid": "msg-guid-2",
                "text": "Group hello",
                "isFromMe": False,
                "dateCreated": 1700000000000,
                "handle": {"address": "+15555550123"},
                "chats": [
                    {
                        "guid": "iMessage;+;chat123456",
                        "displayName": "Family Chat",
                        "groupId": "group-abc",
                    }
                ],
                "attachments": [],
            },
        }

        result = await adapter.handle_webhook_payload(json.dumps(payload).encode())
        assert result == {"status": "ok"}
        assert len(dispatched) == 1
        msg = dispatched[0]
        assert msg.chat_type == "group"
        assert msg.chat_name == "Family Chat"
        assert msg.chat_id == "iMessage;+;chat123456"

    @pytest.mark.asyncio
    async def test_self_message_ignored(self) -> None:
        adapter = self._make_adapter()
        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        payload = {
            "type": "new-message",
            "data": {
                "guid": "msg-guid-3",
                "text": "My own message",
                "isFromMe": True,
                "handle": {"address": "+15555550123"},
                "chats": [{"guid": "iMessage;-;+15555550123"}],
            },
        }

        result = await adapter.handle_webhook_payload(json.dumps(payload).encode())
        assert result == {"status": "ok"}
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_unsupported_event_ignored(self) -> None:
        adapter = self._make_adapter()
        payload = {"type": "group-name-change", "data": {"chatGuid": "x"}}
        result = await adapter.handle_webhook_payload(json.dumps(payload).encode())
        assert result == {"status": "ignored", "event_type": "group-name-change"}

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self) -> None:
        adapter = self._make_adapter()
        result = await adapter.handle_webhook_payload(b"not json")
        assert result is None

    @pytest.mark.asyncio
    async def test_attachment_only_message(self) -> None:
        adapter = self._make_adapter()
        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        payload = {
            "type": "new-message",
            "data": {
                "guid": "msg-guid-4",
                "text": "",
                "isFromMe": False,
                "handle": {"address": "+15555550123"},
                "chats": [{"guid": "iMessage;-;+15555550123"}],
                "attachments": [
                    {
                        "guid": "att-guid-1",
                        "mimeType": "image/jpeg",
                        "transferName": "photo.jpg",
                        "totalBytes": 12345,
                    }
                ],
            },
        }

        result = await adapter.handle_webhook_payload(json.dumps(payload).encode())
        assert result == {"status": "ok"}
        assert len(dispatched) == 1
        msg = dispatched[0]
        assert msg.content == ""
        assert len(msg.media) == 1
        assert msg.media[0].platform_id == "att-guid-1"
        assert msg.media[0].mime_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_duplicate_guid_deduplicated(self) -> None:
        adapter = self._make_adapter()
        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        payload = {
            "type": "new-message",
            "data": {
                "guid": "msg-guid-dedup",
                "text": "Hello",
                "isFromMe": False,
                "handle": {"address": "+15555550123"},
                "chats": [{"guid": "iMessage;-;+15555550123"}],
            },
        }
        body = json.dumps(payload).encode()

        await adapter.handle_webhook_payload(body)
        await adapter.handle_webhook_payload(body)

        assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# Typing / read receipt tests
# ---------------------------------------------------------------------------


class TestTypingAndReadReceipts:
    @pytest.mark.asyncio
    async def test_typing_disabled_by_config(self) -> None:
        adapter = BlueBubblesAdapter()
        adapter._bb_config = _BlueBubblesConfig(
            {"enable_typing": "false"},
            {"server_url": "http://x", "password": "p"},
        )
        # Should not raise even without a client
        await adapter.send_typing("chat-1")

    @pytest.mark.asyncio
    async def test_read_receipts_disabled_by_config(self) -> None:
        adapter = BlueBubblesAdapter()
        adapter._bb_config = _BlueBubblesConfig(
            {"send_read_receipts": "false"},
            {"server_url": "http://x", "password": "p"},
        )
        await adapter.mark_read("chat-1", "msg-1")
