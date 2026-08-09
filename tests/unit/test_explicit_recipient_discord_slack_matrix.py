from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from cognis.bootstrap import run_schema_bootstrap
from cognis.channels.adapters.discord import (
    DiscordAdapter,
    DiscordRecipientResolutionError,
)
from cognis.channels.adapters.matrix import MatrixAdapter, MatrixRecipientResolutionError
from cognis.channels.adapters.slack import SlackAdapter, SlackRecipientResolutionError
from cognis.channels.recipients import RecipientResolutionService
from cognis.channels.target_refs import ChannelTargetRefCodec
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelRecipient,
    OutboundMessage,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import create_agent, create_channel_account, create_user


def _config(channel_type: str, account_id: str) -> ChannelAccountConfig:
    return ChannelAccountConfig(
        account_id=account_id,
        channel_type=channel_type,
        display_name=channel_type,
        agent_id="agent",
        user_email="owner@example.org",
    )


def _recipient(
    channel_type: str,
    address: str,
    address_kind: str,
    chat_kind: str,
    *,
    allow_resolution: bool = False,
    allow_creation: bool = False,
) -> ChannelRecipient:
    return ChannelRecipient(
        channel_type=channel_type,
        address=address,
        address_kind=address_kind,
        chat_kind=chat_kind,  # type: ignore[arg-type]
        allow_resolution=allow_resolution,
        allow_creation=allow_creation,
    )


@pytest.mark.asyncio
async def test_discord_canonical_and_creation_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "987654321098765"}, request=request)

    adapter = DiscordAdapter()
    adapter._config = _config("discord", "discord-account")  # noqa: SLF001
    adapter._rest_client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://discord.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        canonical = await adapter.resolve_recipient(
            _recipient("discord", "123456789012345", "discord_channel_id", "group"),
            resolution_key="canonical",
        )
        created = await adapter.resolve_recipient(
            _recipient(
                "discord",
                "123456789012346",
                "discord_user_id",
                "direct",
                allow_creation=True,
            ),
            resolution_key="create",
        )
    finally:
        await adapter._rest_client.aclose()  # noqa: SLF001

    assert canonical.chat_id == "123456789012345"
    assert created.chat_id == "987654321098765"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/users/@me/channels"
    assert json.loads(requests[0].content) == {"recipient_id": "123456789012346"}


@pytest.mark.asyncio
async def test_discord_user_lookup_only_is_rejected_without_enumeration() -> None:
    adapter = DiscordAdapter()
    recipient = _recipient(
        "discord",
        "123456789012346",
        "discord_user_id",
        "direct",
        allow_resolution=True,
    )

    with pytest.raises(DiscordRecipientResolutionError) as caught:
        await adapter.resolve_recipient(recipient, resolution_key="lookup")

    assert caught.value.code == "creation_required"
    assert recipient.address not in str(caught.value)


@pytest.mark.asyncio
async def test_slack_conversation_and_user_gates_use_exact_open_payloads() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "channel": {"id": "D123"}},
            request=request,
        )

    adapter = SlackAdapter()
    adapter._config = _config("slack", "slack-account")  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://slack.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        canonical = await adapter.resolve_recipient(
            _recipient("slack", "C123", "slack_conversation_id", "group"),
            resolution_key="canonical",
        )
        lookup = await adapter.resolve_recipient(
            _recipient(
                "slack",
                "U123",
                "slack_user_id",
                "direct",
                allow_resolution=True,
            ),
            resolution_key="lookup",
        )
        creation = await adapter.resolve_recipient(
            _recipient(
                "slack",
                "U124",
                "slack_user_id",
                "direct",
                allow_creation=True,
            ),
            resolution_key="create",
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert canonical.chat_id == "C123"
    assert lookup.chat_id == creation.chat_id == "D123"
    assert payloads == [
        {"users": "U123", "prevent_creation": True},
        {"users": "U124", "prevent_creation": False},
    ]


@pytest.mark.asyncio
async def test_slack_api_error_keeps_provider_classification_without_pii() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "user_not_found"}, request=request)

    adapter = SlackAdapter()
    adapter._config = _config("slack", "slack-account")  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://slack.example",
        transport=httpx.MockTransport(handler),
    )
    recipient = _recipient(
        "slack",
        "U999",
        "slack_user_id",
        "direct",
        allow_resolution=True,
    )
    try:
        with pytest.raises(SlackRecipientResolutionError) as caught:
            await adapter.resolve_recipient(recipient, resolution_key="error")
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert caught.value.code == "slack_user_not_found"
    assert caught.value.provider_code == "user_not_found"
    assert recipient.address not in str(caught.value)


@pytest.mark.asyncio
async def test_recipient_work_does_not_change_existing_send_routes() -> None:
    discord_paths: list[str] = []

    def discord_handler(request: httpx.Request) -> httpx.Response:
        discord_paths.append(request.url.path)
        return httpx.Response(200, json={"id": "message"}, request=request)

    discord = DiscordAdapter()
    discord._config = _config("discord", "discord-account")  # noqa: SLF001
    discord._rest_client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://discord.example",
        transport=httpx.MockTransport(discord_handler),
    )

    slack_paths: list[str] = []

    def slack_handler(request: httpx.Request) -> httpx.Response:
        slack_paths.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "ts": "message"}, request=request)

    slack = SlackAdapter()
    slack._config = _config("slack", "slack-account")  # noqa: SLF001
    slack._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://slack.example",
        transport=httpx.MockTransport(slack_handler),
    )
    try:
        assert (
            await discord.send_message(
                OutboundMessage(
                    channel_type="discord",
                    account_id="discord-account",
                    chat_id="123456789012345",
                    content="hello",
                )
            )
            == "message"
        )
        assert (
            await slack.send_message(
                OutboundMessage(
                    channel_type="slack",
                    account_id="slack-account",
                    chat_id="C123",
                    content="hello",
                )
            )
            == "message"
        )
    finally:
        await discord._rest_client.aclose()  # noqa: SLF001
        await slack._client.aclose()  # noqa: SLF001

    assert discord_paths == ["/channels/123456789012345/messages"]
    assert slack_paths == ["/chat.postMessage"]


@pytest.mark.asyncio
async def test_matrix_alias_resolves_and_joins_when_not_joined() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        if "directory/room" in request.url.path:
            return httpx.Response(200, json={"room_id": "!room:example.org"}, request=request)
        if request.url.path.endswith("/joined_rooms"):
            return httpx.Response(200, json={"joined_rooms": []}, request=request)
        if "/join/" in request.url.path:
            return httpx.Response(200, json={"room_id": "!room:example.org"}, request=request)
        raise AssertionError(f"unexpected Matrix request: {request.url.path}")

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._allow_rooms = {"!room:example.org"}  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        target = await adapter.resolve_recipient(
            _recipient(
                "matrix",
                "#room:example.org",
                "matrix_room_alias",
                "group",
                allow_resolution=True,
            ),
            resolution_key="alias",
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert target.chat_id == "!room:example.org"
    assert [method for method, _, _ in calls] == ["GET", "GET", "POST"]


@pytest.mark.asyncio
async def test_matrix_direct_reuses_account_data_then_creates_and_updates_it() -> None:
    account_data: dict[str, Any] = {"@alice:example.org": ["!existing:example.org"]}
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=account_data, request=request)
        if request.url.path.endswith("/createRoom"):
            return httpx.Response(200, json={"room_id": "!created:example.org"}, request=request)
        account_data.update(body or {})
        return httpx.Response(200, json={}, request=request)

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        existing = await adapter.resolve_recipient(
            _recipient(
                "matrix",
                "@alice:example.org",
                "matrix_user_id",
                "direct",
                allow_resolution=True,
            ),
            resolution_key="existing",
        )
        created = await adapter.resolve_recipient(
            _recipient(
                "matrix",
                "@bob:example.org",
                "matrix_user_id",
                "direct",
                allow_creation=True,
            ),
            resolution_key="created",
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert existing.chat_id == "!existing:example.org"
    assert created.chat_id == "!created:example.org"
    assert account_data["@bob:example.org"] == ["!created:example.org"]
    assert [method for method, _, _ in calls] == ["GET", "POST", "GET", "PUT"]


@pytest.mark.asyncio
async def test_matrix_creation_timeout_reconciles_without_pii() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            raise httpx.ReadTimeout("timeout", request=request)
        calls += 1
        return httpx.Response(
            200,
            json={"@alice:example.org": ["!reconciled:example.org"]},
            request=request,
        )

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    recipient = _recipient(
        "matrix",
        "@alice:example.org",
        "matrix_user_id",
        "direct",
        allow_creation=True,
    )
    try:
        target = await adapter.resolve_recipient(recipient, resolution_key="timeout")
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert target.chat_id == "!reconciled:example.org"
    assert calls == 1


@pytest.mark.asyncio
async def test_matrix_creation_timeout_is_uncertain_when_reconciliation_finds_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadTimeout("connection dropped", request=request)
        return httpx.Response(200, json={}, request=request)

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    recipient = _recipient(
        "matrix",
        "@alice:example.org",
        "matrix_user_id",
        "direct",
        allow_creation=True,
    )
    try:
        with pytest.raises(MatrixRecipientResolutionError) as caught:
            await adapter.resolve_recipient(recipient, resolution_key="dropped")
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert caught.value.code == "matrix_creation_uncertain"
    assert caught.value.side_effect_certainty == "uncertain"
    assert recipient.address not in str(caught.value)


@pytest.mark.asyncio
async def test_matrix_deterministic_creation_rejection_is_not_uncertain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(403, json={"errcode": "M_FORBIDDEN"}, request=request)
        raise AssertionError(f"unexpected Matrix request: {request.url.path}")

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    recipient = _recipient(
        "matrix",
        "@alice:example.org",
        "matrix_user_id",
        "direct",
        allow_creation=True,
    )
    try:
        with pytest.raises(MatrixRecipientResolutionError) as caught:
            await adapter.resolve_recipient(recipient, resolution_key="rejected")
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert caught.value.code == "matrix_creation_rejected"
    assert caught.value.side_effect_certainty == "none"
    assert recipient.address not in str(caught.value)


@pytest.mark.asyncio
async def test_matrix_cached_room_repairs_failed_direct_mapping_before_returning() -> None:
    account_data: dict[str, Any] = {}
    put_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal put_attempts
        if request.method == "GET":
            return httpx.Response(200, json=account_data, request=request)
        if request.method == "POST":
            return httpx.Response(200, json={"room_id": "!cached:example.org"}, request=request)
        put_attempts += 1
        if put_attempts == 1:
            return httpx.Response(503, json={}, request=request)
        account_data.update(json.loads(request.content))
        return httpx.Response(200, json={}, request=request)

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    recipient = _recipient(
        "matrix",
        "@alice:example.org",
        "matrix_user_id",
        "direct",
        allow_creation=True,
    )
    try:
        with pytest.raises(MatrixRecipientResolutionError):
            await adapter.resolve_recipient(recipient, resolution_key="repair")
        target = await adapter.resolve_recipient(recipient, resolution_key="repair")
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert target.chat_id == "!cached:example.org"
    assert account_data["@alice:example.org"] == ["!cached:example.org"]
    assert put_attempts == 2


@pytest.mark.asyncio
async def test_matrix_direct_mapping_updates_are_serialized() -> None:
    account_data: dict[str, Any] = {}
    create_count = 0
    put_payloads: list[dict[str, Any]] = []
    first_put_started = asyncio.Event()
    release_first_put = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        if request.method == "GET":
            return httpx.Response(200, json=dict(account_data), request=request)
        if request.url.path.endswith("/createRoom"):
            create_count += 1
            return httpx.Response(
                200,
                json={"room_id": f"!created-{create_count}:example.org"},
                request=request,
            )
        payload = json.loads(request.content)
        put_payloads.append(payload)
        if len(put_payloads) == 1:
            first_put_started.set()
            await release_first_put.wait()
        account_data.clear()
        account_data.update(payload)
        return httpx.Response(200, json={}, request=request)

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        first = asyncio.create_task(
            adapter.resolve_recipient(
                _recipient(
                    "matrix",
                    "@alice:example.org",
                    "matrix_user_id",
                    "direct",
                    allow_creation=True,
                ),
                resolution_key="alice",
            )
        )
        await first_put_started.wait()
        second = asyncio.create_task(
            adapter.resolve_recipient(
                _recipient(
                    "matrix",
                    "@bob:example.org",
                    "matrix_user_id",
                    "direct",
                    allow_creation=True,
                ),
                resolution_key="bob",
            )
        )
        await asyncio.sleep(0)
        release_first_put.set()
        first_target, second_target = await asyncio.gather(first, second)
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert first_target.chat_id == "!created-1:example.org"
    assert second_target.chat_id == "!created-2:example.org"
    assert set(account_data) == {"@alice:example.org", "@bob:example.org"}
    assert len(put_payloads) == 2


@pytest.mark.asyncio
async def test_matrix_lookup_only_requires_creation_when_no_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    adapter = MatrixAdapter()
    adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example",
        transport=httpx.MockTransport(handler),
    )
    recipient = _recipient(
        "matrix",
        "@alice:example.org",
        "matrix_user_id",
        "direct",
        allow_resolution=True,
    )
    try:
        with pytest.raises(MatrixRecipientResolutionError) as caught:
            await adapter.resolve_recipient(recipient, resolution_key="lookup")
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert caught.value.code == "creation_required"
    assert recipient.address not in str(caught.value)


@pytest.mark.asyncio
async def test_core_reconstruction_does_not_retry_uncertain_matrix_creation(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'uncertain-recipient.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    try:
        async with factory() as session:
            await create_user(session, "owner@example.com", "Owner", "hash")
            await create_agent(
                session,
                agent_id="agent-owner",
                owner_email="owner@example.com",
                name="Owner",
                status="active",
            )
            await create_channel_account(
                session,
                account_id="matrix-account",
                channel_type="matrix",
                display_name="Matrix",
                agent_id="agent-owner",
                user_email="owner@example.com",
            )
            await session.commit()

        def timed_out(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                raise httpx.ReadTimeout("connection dropped", request=request)
            return httpx.Response(200, json={}, request=request)

        first_adapter = MatrixAdapter()
        first_adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
        first_adapter._user_id = "@bot:example.org"  # noqa: SLF001
        first_adapter._client = httpx.AsyncClient(  # noqa: SLF001
            base_url="https://matrix.example",
            transport=httpx.MockTransport(timed_out),
        )

        class _Manager:
            def __init__(self, adapter: MatrixAdapter) -> None:
                self.adapter = adapter

            def get_adapter(self, account_id: str) -> MatrixAdapter:
                assert account_id == "matrix-account"
                return self.adapter

        service = RecipientResolutionService(
            factory,
            codec=ChannelTargetRefCodec("uncertain-recovery-secret"),
            channel_manager_ref=lambda: _Manager(first_adapter),
        )
        recipient = _recipient(
            "matrix",
            "@alice:example.org",
            "matrix_user_id",
            "direct",
            allow_creation=True,
        )
        result = await service.send(
            user_email="owner@example.com",
            recipient=recipient,
            content="Hello",
            artifact_metadata=[],
            idempotency_key="uncertain-matrix",
            conversation_id="conversation-1",
        )
        assert result.status == "uncertain"
        await first_adapter._client.aclose()  # noqa: SLF001

        create_calls = 0

        def reconstructed(request: httpx.Request) -> httpx.Response:
            nonlocal create_calls
            if request.method == "POST":
                create_calls += 1
            return httpx.Response(200, json={}, request=request)

        second_adapter = MatrixAdapter()
        second_adapter._config = _config("matrix", "matrix-account")  # noqa: SLF001
        second_adapter._user_id = "@bot:example.org"  # noqa: SLF001
        second_adapter._client = httpx.AsyncClient(  # noqa: SLF001
            base_url="https://matrix.example",
            transport=httpx.MockTransport(reconstructed),
        )
        service = RecipientResolutionService(
            factory,
            codec=ChannelTargetRefCodec("uncertain-recovery-secret"),
            channel_manager_ref=lambda: _Manager(second_adapter),
        )
        assert await service.recover_pending() == 0
        assert create_calls == 0
        await second_adapter._client.aclose()  # noqa: SLF001
    finally:
        await engine.dispose()
