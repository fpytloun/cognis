"""Dedicated recipient-resolution tests for Google Chat and BlueBubbles."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cognis.channels.adapters.bluebubbles import (
    BlueBubblesAdapter,
    BlueBubblesRecipientError,
    _BlueBubblesConfig,
)
from cognis.channels.adapters.google_chat import (
    GoogleChatAdapter,
    GoogleChatRecipientError,
)
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelRecipient,
    OutboundMessage,
)


def _config(channel_type: str) -> ChannelAccountConfig:
    return ChannelAccountConfig(
        account_id=f"{channel_type}-account",
        channel_type=channel_type,
        display_name=channel_type,
        agent_id="agent-1",
        user_email="owner@example.test",
    )


def _google_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GoogleChatAdapter:
    adapter = GoogleChatAdapter()
    adapter._config = _config("google_chat")
    adapter._client = httpx.AsyncClient(
        base_url="https://chat.googleapis.com/v1",
        transport=httpx.MockTransport(handler),
    )
    return adapter


def _bluebubbles_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    version: tuple[int, int, int] | None = (0, 4, 0),
) -> BlueBubblesAdapter:
    adapter = BlueBubblesAdapter()
    adapter._config = _config("bluebubbles")
    adapter._bb_config = _BlueBubblesConfig(
        {},
        {"server_url": "https://bluebubbles.example.test", "password": "server-secret"},
    )
    adapter._server_version = version
    adapter._client = httpx.AsyncClient(
        base_url="https://bluebubbles.example.test",
        transport=httpx.MockTransport(handler),
    )
    return adapter


class _FakeGoogleCredentials:
    def __init__(self, scopes: list[str], *, token: str = "token") -> None:
        self.scopes = scopes
        self.token = token
        self.valid = True
        self.expired = False
        self.subject: str | None = None
        self.refresh_calls = 0

    def with_subject(self, subject: str) -> _FakeGoogleCredentials:
        child = _FakeGoogleCredentials(self.scopes, token="delegated-token")
        child.subject = subject
        return child

    def refresh(self, request: Any) -> None:
        del request
        self.refresh_calls += 1


@pytest.mark.asyncio
async def test_google_canonical_space_returns_direct_or_group_without_http() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    adapter = _google_adapter(handler)
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="google_chat",
                address="spaces/group-space",
                address_kind="google_chat_space",
                chat_kind="group",
            ),
            resolution_key="resolution-1",
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert target.chat_id == "spaces/group-space"
    assert target.chat_kind == "group"
    assert requests == []


@pytest.mark.asyncio
async def test_google_user_lookup_uses_app_auth_and_canonical_resource() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"name": "spaces/direct-123"})

    adapter = _google_adapter(handler)
    fake = _FakeGoogleCredentials(["chat.bot"])
    adapter._app_credentials = fake  # type: ignore[assignment]
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="google_chat",
                address="users/123456",
                address_kind="google_workspace_user",
                allow_resolution=True,
            ),
            resolution_key="resolution-2",
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert target.chat_id == "spaces/direct-123"
    assert seen[0].url.path == "/v1/spaces:findDirectMessage"
    assert dict(seen[0].url.params) == {"name": "users/123456"}
    assert seen[0].headers["authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_google_404_is_safe_not_found_and_email_is_unsupported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    adapter = _google_adapter(handler)
    adapter._app_credentials = _FakeGoogleCredentials(["chat.bot"])  # type: ignore[assignment]
    try:
        with pytest.raises(GoogleChatRecipientError) as error:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="google_chat",
                    address="users/not-found",
                    address_kind="google_workspace_user",
                    allow_resolution=True,
                ),
                resolution_key="resolution-3",
            )
        assert error.value.code == "recipient_not_found"
        assert "not-found" not in str(error.value)
        with pytest.raises(GoogleChatRecipientError) as email_error:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="google_chat",
                    address="person@example.test",
                    address_kind="google_workspace_user",
                    allow_resolution=True,
                ),
                resolution_key="resolution-4",
            )
        assert email_error.value.code == "unsupported_address"
        assert "person@example.test" not in str(email_error.value)
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_google_creation_uses_delegated_auth_and_request_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"name": "spaces/new-direct/messages/message-1"})
        return httpx.Response(200, json={"name": "spaces/new-direct"})

    adapter = _google_adapter(handler)
    adapter._app_credentials = _FakeGoogleCredentials(["chat.bot"])  # type: ignore[assignment]
    adapter._setup_credentials = _FakeGoogleCredentials(  # type: ignore[assignment]
        ["chat.spaces.create"], token="delegated-token"
    )
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="google_chat",
                address="users/987",
                address_kind="google_workspace_user",
                allow_creation=True,
            ),
            resolution_key="intent-key-123",
        )
        message_id = await adapter.send_message(
            OutboundMessage(
                channel_type="google_chat",
                account_id="google_chat-account",
                chat_id=target.chat_id,
                content="created",
            )
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert target.chat_id == "spaces/new-direct"
    assert seen[0].method == "POST"
    assert dict(seen[0].url.params) == {}
    assert json.loads(seen[0].content) == {
        "space": {"spaceType": "DIRECT_MESSAGE"},
        "memberships": [{"member": {"name": "users/987"}}],
        "requestId": "intent-key-123",
    }
    assert seen[0].headers["authorization"] == "Bearer delegated-token"
    assert message_id == "spaces/new-direct/messages/message-1"
    assert seen[1].url.path == "/v1/spaces/new-direct/messages"
    assert seen[1].headers["authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_google_expired_app_token_refreshes_without_default_header_mutation() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"name": "spaces/refreshed"})

    adapter = _google_adapter(handler)
    credentials = _FakeGoogleCredentials(["chat.bot"], token="refreshed-token")
    credentials.expired = True
    adapter._app_credentials = credentials  # type: ignore[assignment]
    try:
        await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="google_chat",
                address="users/expired",
                address_kind="google_workspace_user",
                allow_resolution=True,
            ),
            resolution_key="refresh-1",
        )
        assert credentials.refresh_calls == 1
        assert seen[0].headers["authorization"] == "Bearer refreshed-token"
        assert "authorization" not in adapter._client.headers  # type: ignore[union-attr]
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_google_concurrent_requests_keep_app_and_delegated_identities_isolated() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["authorization"]))
        if request.url.path.endswith("findDirectMessage"):
            return httpx.Response(200, json={"name": "spaces/found"})
        if request.url.path.endswith("spaces:setup"):
            return httpx.Response(200, json={"name": "spaces/created"})
        return httpx.Response(200, json={"name": "spaces/created/messages/message-1"})

    adapter = _google_adapter(handler)
    adapter._app_credentials = _FakeGoogleCredentials(["chat.bot"])  # type: ignore[assignment]
    adapter._setup_credentials = _FakeGoogleCredentials(  # type: ignore[assignment]
        ["chat.spaces.create"], token="delegated-token"
    )
    try:
        await asyncio.gather(
            adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="google_chat",
                    address="users/lookup",
                    address_kind="google_workspace_user",
                    allow_resolution=True,
                ),
                resolution_key="concurrent-lookup",
            ),
            adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="google_chat",
                    address="users/setup",
                    address_kind="google_workspace_user",
                    allow_creation=True,
                ),
                resolution_key="concurrent-setup",
            ),
            adapter.send_message(
                OutboundMessage(
                    channel_type="google_chat",
                    account_id="google_chat-account",
                    chat_id="spaces/created",
                    content="send",
                )
            ),
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert any(
        path.endswith("findDirectMessage") and token == "Bearer token" for path, token in seen
    )
    assert any(
        path.endswith("spaces:setup") and token == "Bearer delegated-token" for path, token in seen
    )
    assert any(path.endswith("/messages") and token == "Bearer token" for path, token in seen)


@pytest.mark.asyncio
async def test_google_connect_uses_real_google_auth_scopes_and_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_FakeGoogleCredentials] = []

    def factory(info: dict[str, Any], scopes: list[str]) -> _FakeGoogleCredentials:
        assert info["type"] == "service_account"
        credential = _FakeGoogleCredentials(scopes)
        created.append(credential)
        return credential

    monkeypatch.setattr(
        "cognis.channels.adapters.google_chat.service_account.Credentials.from_service_account_info",
        factory,
    )
    adapter = GoogleChatAdapter()
    adapter._config = _config("google_chat").model_copy(
        update={"settings": {"delegated_user": "operator@example.test"}}
    )
    adapter._credentials = {
        "service_account_json": json.dumps({"type": "service_account"}),
    }
    await adapter._connect()
    try:
        assert [credential.scopes for credential in created] == [
            ["https://www.googleapis.com/auth/chat.bot"],
            ["https://www.googleapis.com/auth/chat.spaces.create"],
        ]
        assert adapter._setup_credentials is not None
        assert adapter._setup_credentials.subject == "operator@example.test"  # type: ignore[union-attr]
        assert adapter.capabilities.recipient_capabilities.supports_creation is True
        assert adapter._access_token == "token"
    finally:
        await adapter._disconnect()


@pytest.mark.asyncio
async def test_google_creation_without_delegated_user_is_explicitly_unsupported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    adapter = _google_adapter(handler)
    try:
        with pytest.raises(GoogleChatRecipientError) as error:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="google_chat",
                    address="users/without-delegation",
                    address_kind="google_workspace_user",
                    allow_creation=True,
                ),
                resolution_key="google-unsupported",
            )
        assert error.value.code == "creation_unsupported"
        assert "without-delegation" not in str(error.value)
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_bluebubbles_canonical_chat_preserves_group_and_does_not_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    adapter = _bluebubbles_adapter(handler)
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="bluebubbles",
                address="iMessage;+;group-1",
                address_kind="bluebubbles_chat_guid",
                chat_kind="group",
            ),
            resolution_key="blue-1",
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert target.chat_id == "iMessage;+;group-1"
    assert target.chat_kind == "group"
    assert requests == []


@pytest.mark.asyncio
async def test_bluebubbles_exact_handle_reuses_existing_chat() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"guid": "iMessage;-;+15551234567"}]})

    adapter = _bluebubbles_adapter(handler)
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="bluebubbles",
                address="+15551234567",
                address_kind="imessage_handle",
                allow_resolution=True,
            ),
            resolution_key="blue-2",
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert target.chat_id == "iMessage;-;+15551234567"
    assert seen[0].url.path == "/api/v1/chat"
    assert dict(seen[0].url.params) == {
        "address": "+15551234567",
        "password": "server-secret",
    }


@pytest.mark.asyncio
async def test_bluebubbles_creation_has_version_gate_and_no_initial_message() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": {"guid": "iMessage;-;+15551230000"}})

    adapter = _bluebubbles_adapter(handler)
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="bluebubbles",
                address="+15551230000",
                address_kind="imessage_handle",
                allow_creation=True,
            ),
            resolution_key="blue-3",
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert target.chat_id == "iMessage;-;+15551230000"
    assert [request.method for request in seen] == ["GET", "POST"]
    assert json.loads(seen[1].content) == {"addresses": ["+15551230000"]}
    assert "message" not in json.loads(seen[1].content)


@pytest.mark.asyncio
async def test_bluebubbles_server_info_probes_creation_capability() -> None:
    responses = [
        httpx.Response(200, json={"data": {"server_version": "0.4.0"}}),
        httpx.Response(200, json={"data": {"server_version": "0.3.9"}}),
        httpx.Response(200, json={"data": {}}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    adapter = _bluebubbles_adapter(handler, version=None)
    try:
        await adapter._probe_server_version()
        assert adapter.capabilities.recipient_capabilities.supports_creation is True
        await adapter._probe_server_version()
        assert adapter.capabilities.recipient_capabilities.supports_creation is False
        await adapter._probe_server_version()
        assert adapter.capabilities.recipient_capabilities.supports_creation is False
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version",
    [None, (0, 3, 9)],
)
async def test_bluebubbles_unknown_or_old_server_rejects_creation(
    version: tuple[int, int, int] | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    adapter = _bluebubbles_adapter(handler, version=version)
    try:
        with pytest.raises(BlueBubblesRecipientError) as error:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="bluebubbles",
                    address="person@example.test",
                    address_kind="imessage_handle",
                    allow_creation=True,
                ),
                resolution_key="blue-4",
            )
        assert error.value.code == "creation_unsupported"
        assert error.value.side_effect_certainty == "none"
        assert "person@example.test" not in str(error.value)
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize("server_version", ["0.4.0-beta.1", "00.4.0", "0.4", "unknown"])
async def test_bluebubbles_prerelease_or_malformed_version_does_not_advertise_creation(
    server_version: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"server_version": server_version}})

    adapter = _bluebubbles_adapter(handler, version=None)
    try:
        await adapter._probe_server_version()
        assert adapter.capabilities.recipient_capabilities.supports_creation is False
        with pytest.raises(BlueBubblesRecipientError) as error:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="bluebubbles",
                    address="+15551230001",
                    address_kind="imessage_handle",
                    allow_creation=True,
                ),
                resolution_key="blue-version",
            )
        assert error.value.code == "creation_unsupported"
        assert error.value.side_effect_certainty == "none"
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_bluebubbles_known_create_forbidden_error_is_not_uncertain() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(403)

    adapter = _bluebubbles_adapter(handler)
    try:
        with pytest.raises(BlueBubblesRecipientError) as error:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="bluebubbles",
                    address="+15551230002",
                    address_kind="imessage_handle",
                    allow_creation=True,
                ),
                resolution_key="blue-forbidden",
            )
        assert error.value.code == "creation_failed"
        assert error.value.side_effect_certainty == "none"
        assert methods == ["GET", "POST"]
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_bluebubbles_uncertain_create_requeries_without_retrying_create() -> None:
    methods: list[str] = []
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        methods.append(request.method)
        if request.method == "GET":
            query_count += 1
            return httpx.Response(
                200,
                json={"data": [{"guid": "iMessage;-;+15551239999"}]}
                if query_count == 2
                else {"data": []},
            )
        raise httpx.ReadTimeout("create response was lost")

    adapter = _bluebubbles_adapter(handler)
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="bluebubbles",
                address="+15551239999",
                address_kind="imessage_handle",
                allow_creation=True,
            ),
            resolution_key="blue-5",
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert target.chat_id == "iMessage;-;+15551239999"
    assert methods == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_bluebubbles_send_message_contract_is_unchanged() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"guid": "message-1"}})

    adapter = _bluebubbles_adapter(handler)
    try:
        result = await adapter.send_message(
            OutboundMessage(
                channel_type="bluebubbles",
                account_id="bluebubbles-account",
                chat_id="chat-guid",
                content="hello",
            )
        )
    finally:
        await adapter._client.aclose()  # type: ignore[union-attr]
    assert result == "message-1"
    assert seen[0].url.path == "/api/v1/message/text"
    assert json.loads(seen[0].content) == {
        "chatGuid": "chat-guid",
        "message": "hello",
        "method": "private-api",
    }
