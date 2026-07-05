from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cognis.channels.adapters.matrix import MatrixAdapter
from cognis.channels.protocol import NonRetryableChannelError
from cognis.channels.registry import MATRIX_META
from cognis.models.channel import ChannelAccountConfig, MediaAttachment, OutboundMessage


def _config(settings: dict[str, Any] | None = None) -> ChannelAccountConfig:
    return ChannelAccountConfig(
        account_id="matrix-account",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent",
        user_email="user@example.com",
        settings=settings or {},
    )


def _adapter(settings: dict[str, Any] | None = None) -> MatrixAdapter:
    settings = settings or {}
    adapter = MatrixAdapter()
    adapter._config = _config(settings)  # noqa: SLF001
    adapter._user_id = "@bot:example.org"  # noqa: SLF001
    adapter._display_name = "Cognis Bot"  # noqa: SLF001
    adapter._require_mention = settings.get("require_mention") in {True, "true"}  # noqa: SLF001
    adapter._live_sync_established = True  # noqa: SLF001
    adapter._started_at_ms = int(datetime.now(UTC).timestamp() * 1000)  # noqa: SLF001
    return adapter


class _JoinedMembersClient:
    def __init__(self, joined: dict[str, dict[str, Any]]) -> None:
        self.joined = joined
        self.paths: list[str] = []

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        self.paths.append(path)
        request = httpx.Request("GET", f"https://matrix.example.org{path}")
        return httpx.Response(200, json={"joined": self.joined}, request=request)


class _EventLookupClient:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event
        self.paths: list[str] = []

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        self.paths.append(path)
        request = httpx.Request("GET", f"https://matrix.example.org{path}")
        return httpx.Response(200, json=self.event, request=request)


class _SyncClient:
    def __init__(self, sync_payload: dict[str, Any]) -> None:
        self.sync_payload = sync_payload
        self.post_paths: list[str] = []
        self.stop_event: asyncio.Event | None = None

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        request = httpx.Request("GET", f"https://matrix.example.org{path}")
        if path.endswith("/sync"):
            if self.stop_event is not None:
                self.stop_event.set()
            return httpx.Response(200, json=self.sync_payload, request=request)
        return httpx.Response(200, json={"joined": {}}, request=request)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        self.post_paths.append(path)
        request = httpx.Request("POST", f"https://matrix.example.org{path}")
        return httpx.Response(200, json={"event_id": "$joined"}, request=request)


async def _collect(
    adapter: MatrixAdapter,
    event: dict[str, Any],
    *,
    room_id: str = "!room:example.org",
    **kwargs: Any,
) -> list[Any]:
    messages: list[Any] = []

    async def on_message(message: Any) -> None:
        messages.append(message)

    adapter._on_message = on_message  # noqa: SLF001
    await adapter._handle_event(room_id, event, **kwargs)  # noqa: SLF001
    return messages


def _message_event(
    *,
    event_id: str = "$event",
    sender: str = "@alice:example.org",
    body: str = "hello @bot:example.org",
    content: dict[str, Any] | None = None,
    ts: int = 1_700_000_000_000,
) -> dict[str, Any]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": content or {"msgtype": "m.text", "body": body},
    }


def test_matrix_registry_matches_adapter_capabilities() -> None:
    assert MATRIX_META.capabilities.supports_threads is True
    assert MATRIX_META.capabilities.supports_media is True
    assert MATRIX_META.capabilities.supports_markdown is True
    assert MATRIX_META.capabilities.supports_read_receipts is True
    assert MATRIX_META.capabilities.supports_reactions is False
    assert MATRIX_META.capabilities.supports_edits is False
    assert MATRIX_META.capabilities.max_message_length == 4000


def test_matrix_registry_exposes_thread_conversation_settings() -> None:
    fields = {field.name: field for field in MATRIX_META.setting_fields}

    assert fields["allowed_rooms"].label == "Allowed group rooms"
    assert "DM rooms are exempt" in fields["allowed_rooms"].description
    assert fields["direct_rooms"].label == "Rooms treated as DM"
    assert fields["group_rooms"].label == "Rooms treated as group"

    assert fields["dm_conversation_mode"].field_type == "select"
    assert fields["dm_conversation_mode"].default == "default"
    assert fields["dm_conversation_mode"].options == ["default", "threads"]

    assert fields["group_conversation_mode"].field_type == "select"
    assert fields["group_conversation_mode"].default == "default"
    assert fields["group_conversation_mode"].options == ["default", "threads"]

    assert fields["thread_start_mode"].field_type == "select"
    assert fields["thread_start_mode"].default == "fork"
    assert fields["thread_start_mode"].options == ["fork", "fresh"]


@pytest.mark.asyncio
async def test_handle_event_filters_own_appservice_notice_edits_and_duplicates() -> None:
    adapter = _adapter()

    assert await _collect(adapter, _message_event(sender="@bot:example.org")) == []
    assert await _collect(adapter, _message_event(sender="@_bridge:example.org")) == []
    assert (
        await _collect(
            adapter,
            _message_event(content={"msgtype": "m.notice", "body": "notice"}),
        )
        == []
    )
    assert (
        await _collect(
            adapter,
            _message_event(
                content={
                    "msgtype": "m.text",
                    "body": "edited",
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$old"},
                }
            ),
        )
        == []
    )

    first = await _collect(adapter, _message_event(event_id="$dedupe"))
    second = await _collect(adapter, _message_event(event_id="$dedupe"))
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_handle_event_uses_mentions_reply_fallback_and_thread_context() -> None:
    adapter = _adapter({"require_mention": True})
    content = {
        "msgtype": "m.text",
        "body": "> <@bob:example.org> old text\n\nfresh reply",
        "m.mentions": {"user_ids": ["@bot:example.org"]},
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": "$thread",
            "m.in_reply_to": {"event_id": "$reply"},
        },
    }

    messages = await _collect(
        adapter,
        _message_event(content=content),
        room_data={"summary": {"m.joined_member_count": 4}},
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.content == "fresh reply"
    assert message.was_mentioned is True
    assert message.chat_type == "group"
    assert message.thread_id == "$thread"
    assert message.reply_to_id == "$reply"


@pytest.mark.asyncio
async def test_handle_event_require_mention_does_not_drop_direct_room_messages() -> None:
    adapter = _adapter({"require_mention": True})

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "plain dm"}),
        room_data={
            "summary": {"m.joined_member_count": 2},
            "state": {
                "events": [
                    {
                        "type": "m.room.member",
                        "state_key": "@alice:example.org",
                        "content": {"displayname": "Alice Example"},
                    }
                ]
            },
        },
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "direct"
    assert messages[0].chat_name == "Alice Example"
    assert messages[0].was_mentioned is False


@pytest.mark.parametrize("require_mention", [False, True])
@pytest.mark.asyncio
async def test_handle_event_marks_unmentioned_thread_followup_candidate(
    *, require_mention: bool
) -> None:
    adapter = _adapter({"require_mention": require_mention})
    content = {
        "msgtype": "m.text",
        "body": "follow up",
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
    }

    messages = await _collect(
        adapter,
        _message_event(content=content),
        room_data={"summary": {"m.joined_member_count": 4}},
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "group"
    assert messages[0].thread_id == "$thread"
    assert messages[0].was_mentioned is False
    assert messages[0].platform_data["unmentioned_thread_followup_candidate"] is True


@pytest.mark.asyncio
async def test_handle_event_uses_sender_display_name_for_unnamed_group_thread_titles() -> None:
    """Threads in unnamed group rooms (e.g. small private rooms via allowed_rooms)
    should use the sender's display name rather than the raw room ID."""
    adapter = _adapter({"require_mention": True})
    adapter._allow_rooms = {"!room:example.org"}  # noqa: SLF001
    content = {
        "msgtype": "m.text",
        "body": "hello @bot:example.org",
        "m.mentions": {"user_ids": ["@bot:example.org"]},
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-event-id"},
    }

    messages = await _collect(
        adapter,
        _message_event(
            sender="@alice:example.org",
            content=content,
        ),
        room_data={
            "summary": {"m.joined_member_count": 2},
            "state": {
                "events": [
                    {
                        "type": "m.room.member",
                        "state_key": "@alice:example.org",
                        "content": {"displayname": "Alice"},
                    }
                ]
            },
        },
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "group"
    # Title should use sender display name, not the raw room ID
    assert messages[0].chat_name.startswith("Alice")
    assert "!room:example.org" not in messages[0].chat_name
    assert "thread" in messages[0].chat_name


@pytest.mark.asyncio
async def test_handle_event_uses_room_name_for_group_thread_titles() -> None:
    adapter = _adapter({"require_mention": True})
    content = {
        "msgtype": "m.text",
        "body": "hello @bot:example.org",
        "m.mentions": {"user_ids": ["@bot:example.org"]},
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-event-id"},
    }

    messages = await _collect(
        adapter,
        _message_event(content=content),
        room_data={
            "summary": {"m.joined_member_count": 3},
            "state": {
                "events": [
                    {
                        "type": "m.room.name",
                        "content": {"name": "Ops Room"},
                    }
                ]
            },
        },
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "group"
    assert messages[0].chat_name == "Ops Room · thread $thread-even"


@pytest.mark.asyncio
async def test_handle_event_require_mention_does_not_match_localpart_substrings() -> None:
    adapter = _adapter({"require_mention": True})

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "robot status update"}),
        room_data={"summary": {"m.joined_member_count": 4}},
    )

    assert messages == []


@pytest.mark.asyncio
async def test_handle_event_direct_room_setting_handles_missing_sync_summary() -> None:
    adapter = _adapter({"require_mention": True})
    adapter._direct_rooms = {"!room:example.org"}  # noqa: SLF001

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "plain direct room"}),
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "direct"


@pytest.mark.asyncio
async def test_handle_event_marks_matrix_voice_audio_as_voice_input() -> None:
    adapter = _adapter()
    content = {
        "msgtype": "m.audio",
        "body": "voice-message-recording.ogg",
        "url": "mxc://example.org/media-id",
        "info": {"mimetype": "audio/ogg", "size": 1234},
        "org.matrix.msc3245.voice": {},
        "org.matrix.msc1767.audio": {"duration": 1200, "waveform": [1, 2, 3]},
    }

    messages = await _collect(
        adapter,
        _message_event(content=content),
        room_data={"summary": {"m.joined_member_count": 2}},
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.content == ""
    assert message.platform_data["voice_input"] is True
    assert len(message.media) == 1
    assert message.media[0].url == "mxc://example.org/media-id"
    assert message.media[0].mime_type == "audio/ogg"
    assert message.media[0].filename == "voice-message-recording.ogg"


@pytest.mark.asyncio
async def test_handle_event_group_room_setting_overrides_two_member_dm_fallback() -> None:
    adapter = _adapter({"require_mention": True})
    adapter._group_rooms = {"!room:example.org"}  # noqa: SLF001

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "hello @bot:example.org"}),
        room_data={"summary": {"m.joined_member_count": 2}},
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "group"
    assert messages[0].chat_name == "!room:example.org"


@pytest.mark.asyncio
async def test_handle_event_group_room_setting_has_precedence_over_direct_room_setting() -> None:
    adapter = _adapter({"require_mention": True})
    adapter._group_rooms = {"!room:example.org"}  # noqa: SLF001
    adapter._direct_rooms = {"!room:example.org"}  # noqa: SLF001

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "hello @bot:example.org"}),
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "group"


@pytest.mark.asyncio
async def test_handle_event_allowed_room_setting_treats_two_member_room_as_group() -> None:
    adapter = _adapter({"require_mention": True})
    adapter._allow_rooms = {"!room:example.org"}  # noqa: SLF001

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "hello @bot:example.org"}),
        room_data={"summary": {"m.joined_member_count": 2}},
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "group"
    assert messages[0].chat_name == "!room:example.org"


@pytest.mark.asyncio
async def test_handle_event_direct_room_setting_has_precedence_over_allowed_room_setting() -> None:
    adapter = _adapter({"require_mention": True})
    adapter._allow_rooms = {"!room:example.org"}  # noqa: SLF001
    adapter._direct_rooms = {"!room:example.org"}  # noqa: SLF001

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "plain direct room"}),
        room_data={"summary": {"m.joined_member_count": 2}},
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "direct"


@pytest.mark.asyncio
async def test_handle_event_probes_joined_members_when_sync_summary_is_missing() -> None:
    adapter = _adapter({"require_mention": True})
    client = _JoinedMembersClient({"@bot:example.org": {}, "@alice:example.org": {}})
    adapter._client = client  # type: ignore[assignment] # noqa: SLF001

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "plain dm"}),
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "direct"
    assert client.paths[0] == "/_matrix/client/v3/rooms/!room:example.org/joined_members"


@pytest.mark.asyncio
async def test_handle_event_treats_named_two_member_room_as_group() -> None:
    adapter = _adapter({"require_mention": True})

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "hello @bot:example.org"}),
        room_data={
            "summary": {"m.joined_member_count": 2},
            "state": {
                "events": [
                    {
                        "type": "m.room.name",
                        "content": {"name": "Build room"},
                    }
                ]
            },
        },
    )

    assert len(messages) == 1
    assert messages[0].chat_type == "group"
    assert messages[0].chat_name == "Build room"


@pytest.mark.asyncio
async def test_handle_event_includes_thread_root_context() -> None:
    adapter = _adapter()
    client = _EventLookupClient(
        {
            "event_id": "$root",
            "sender": "@alice:example.org",
            "content": {"msgtype": "m.text", "body": "original"},
        }
    )
    adapter._client = client  # type: ignore[assignment] # noqa: SLF001

    messages = await _collect(
        adapter,
        _message_event(
            event_id="$reply",
            content={
                "msgtype": "m.text",
                "body": "continuation",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            },
        ),
        room_data={
            "summary": {"m.joined_member_count": 3},
            "state": {"events": [{"type": "m.room.name", "content": {"name": "Test Room"}}]},
        },
    )

    assert len(messages) == 1
    assert messages[0].thread_id == "$root"
    assert messages[0].platform_data["thread_root_event_id"] == "$root"
    assert messages[0].platform_data["thread_root"] == {
        "event_id": "$root",
        "sender": "@alice:example.org",
        "msgtype": "m.text",
        "body": "original",
    }
    assert client.paths == ["/_matrix/client/v3/rooms/%21room%3Aexample.org/event/%24root"]


@pytest.mark.asyncio
async def test_handle_event_keeps_require_mention_for_unadvertised_group_rooms() -> None:
    adapter = _adapter({"require_mention": True})
    adapter._client = _JoinedMembersClient(  # type: ignore[assignment] # noqa: SLF001
        {
            "@bot:example.org": {},
            "@alice:example.org": {},
            "@bob:example.org": {},
        }
    )

    messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "plain group message"}),
    )

    assert messages == []


def test_room_allowlist_matches_only_configured_rooms() -> None:
    adapter = _adapter()
    adapter._allow_rooms = {"!allowed:example.org"}  # noqa: SLF001

    assert adapter._room_is_allowed("!allowed:example.org") is True  # noqa: SLF001
    assert adapter._room_is_allowed("!other:example.org") is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_group_room_allowlist_drops_unlisted_group_but_not_dm() -> None:
    adapter = _adapter({"allowed_rooms": "!allowed:example.org"})
    adapter._allow_rooms = {"!allowed:example.org"}  # noqa: SLF001

    group_messages = await _collect(
        adapter,
        _message_event(content={"msgtype": "m.text", "body": "hello @bot:example.org"}),
        room_data={"summary": {"m.joined_member_count": 4}},
    )
    assert group_messages == []

    dm_messages = await _collect(
        adapter,
        _message_event(event_id="$dm", content={"msgtype": "m.text", "body": "hello"}),
        room_id="!dm:example.org",
        room_data={
            "summary": {"m.joined_member_count": 2},
            "state": {
                "events": [
                    {
                        "type": "m.room.member",
                        "state_key": "@alice:example.org",
                        "content": {"displayname": "Alice Example"},
                    }
                ]
            },
        },
    )
    assert len(dm_messages) == 1
    assert dm_messages[0].chat_type == "direct"


@pytest.mark.asyncio
async def test_auto_join_invites_respects_allowed_rooms() -> None:
    adapter = _adapter({"auto_join_invites": True, "allowed_rooms": "!allowed:example.org"})
    adapter._allow_rooms = {"!allowed:example.org"}  # noqa: SLF001
    adapter._auto_join_invites = True  # noqa: SLF001
    client = _SyncClient(
        {
            "next_batch": "batch-1",
            "rooms": {
                "invite": {
                    "!allowed:example.org": {},
                    "!sparse-unallowed:example.org": {
                        "invite_state": {
                            "events": [
                                {"type": "m.room.member", "state_key": "@bot:example.org"},
                                {"type": "m.room.member", "state_key": "@alice:example.org"},
                            ]
                        }
                    },
                    "!other:example.org": {
                        "invite_state": {
                            "events": [
                                {"type": "m.room.member", "state_key": "@bot:example.org"},
                                {"type": "m.room.member", "state_key": "@alice:example.org"},
                                {"type": "m.room.member", "state_key": "@bob:example.org"},
                            ]
                        }
                    },
                },
                "join": {},
            },
        }
    )
    adapter._client = client  # type: ignore[assignment] # noqa: SLF001
    adapter._next_batch = "initial"  # noqa: SLF001
    client.stop_event = adapter._stop_event  # noqa: SLF001

    await adapter._run()  # noqa: SLF001

    assert any("/rooms/!allowed:example.org/join" in path for path in client.post_paths)
    assert all(
        "/rooms/!sparse-unallowed:example.org/join" not in path for path in client.post_paths
    )
    assert all("/rooms/!other:example.org/join" not in path for path in client.post_paths)


@pytest.mark.asyncio
async def test_handle_event_keeps_media_even_when_body_is_empty() -> None:
    adapter = _adapter()

    messages = await _collect(
        adapter,
        _message_event(
            content={
                "msgtype": "m.image",
                "body": "",
                "url": "mxc://example.org/media",
                "info": {"mimetype": "image/png", "size": 9},
            }
        ),
    )

    assert len(messages) == 1
    assert messages[0].media[0].url == "mxc://example.org/media"
    assert messages[0].media[0].mime_type == "image/png"


@pytest.mark.asyncio
async def test_handle_event_clears_matrix_media_filename_body() -> None:
    adapter = _adapter()

    messages = await _collect(
        adapter,
        _message_event(
            content={
                "msgtype": "m.image",
                "body": "obrazek.png",
                "url": "mxc://example.org/media",
                "info": {"mimetype": "image/png", "size": 9},
            }
        ),
    )

    assert len(messages) == 1
    assert messages[0].content == ""
    assert messages[0].media[0].filename == "obrazek.png"
    assert messages[0].media[0].url == "mxc://example.org/media"


@pytest.mark.asyncio
async def test_handle_event_drops_encrypted_media_without_e2ee_support() -> None:
    adapter = _adapter()

    messages = await _collect(
        adapter,
        _message_event(
            content={
                "msgtype": "m.image",
                "body": "",
                "file": {"url": "mxc://example.org/encrypted", "key": {"kty": "oct"}},
                "info": {"mimetype": "image/png", "size": 9},
            }
        ),
    )

    assert messages == []


@pytest.mark.asyncio
async def test_startup_replay_suppression_drops_old_events_before_live_sync() -> None:
    adapter = _adapter()
    adapter._live_sync_established = False  # noqa: SLF001
    adapter._started_at_ms = 2_000_000  # noqa: SLF001

    messages = await _collect(adapter, _message_event(ts=1_000_000))

    assert messages == []


@pytest.mark.asyncio
async def test_sync_loop_propagates_non_retryable_auth_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_matrix/client/v3/sync"
        return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})

    adapter = _adapter()
    adapter._next_batch = "token"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example.org",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NonRetryableChannelError):
            await adapter._run()  # noqa: SLF001
    finally:
        await adapter._client.aclose()  # noqa: SLF001


@pytest.mark.asyncio
async def test_send_message_formats_matrix_html_mentions_and_thread_relations() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"event_id": "$sent"})

    adapter = _adapter()
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example.org",
        transport=httpx.MockTransport(handler),
    )
    try:
        event_id = await adapter.send_message(
            OutboundMessage(
                channel_type="matrix",
                account_id="matrix-account",
                chat_id="!room:example.org",
                content="Hello **Matrix** @alice:example.org",
                thread_id="$thread",
                reply_to_id="$reply",
            )
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert event_id == "$sent"
    payload = requests[0].read()
    assert b'"format":"org.matrix.custom.html"' in payload
    assert b"<strong>Matrix</strong>" in payload
    assert b'"m.mentions":{"user_ids":["@alice:example.org"]}' in payload
    assert b'"rel_type":"m.thread"' in payload
    assert b'"m.in_reply_to":{"event_id":"$reply"}' in payload


@pytest.mark.asyncio
async def test_send_media_uploads_content_b64_and_sends_file_event() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/_matrix/media/v3/upload":
            return httpx.Response(200, json={"content_uri": "mxc://example.org/media"})
        return httpx.Response(200, json={"event_id": "$media"})

    adapter = _adapter()
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example.org",
        transport=httpx.MockTransport(handler),
    )
    try:
        await adapter._send_media(  # noqa: SLF001
            "!room:example.org",
            MediaAttachment(
                filename="pixel.png",
                mime_type="image/png",
                content_b64=base64.b64encode(b"png-bytes").decode(),
            ),
            thread_id="$thread",
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert len(requests) == 2
    assert requests[0].url.path == "/_matrix/media/v3/upload"
    assert requests[0].read() == b"png-bytes"
    assert requests[1].url.path.startswith("/_matrix/client/v3/rooms/")
    sent_payload = requests[1].read()
    assert b'"msgtype":"m.image"' in sent_payload
    assert b'"url":"mxc://example.org/media"' in sent_payload
    assert b'"rel_type":"m.thread"' in sent_payload


def _stub_inbound_message() -> Any:
    """Return a minimal InboundMessage for download_attachment tests."""
    from datetime import UTC, datetime

    from cognis.models.channel import InboundMessage

    return InboundMessage(
        channel_type="matrix",
        account_id="matrix-account",
        message_id="$evt",
        sender_id="@alice:example.org",
        chat_id="!room:example.org",
        content="",
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_download_attachment_uses_authenticated_endpoint_first() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"file", headers={"content-type": "text/plain"})

    adapter = _adapter()
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example.org",
        headers={"Authorization": "Bearer tok"},
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await adapter.download_attachment(
            message=_stub_inbound_message(),
            attachment=MediaAttachment(url="mxc://example.org/media", filename="file.txt"),
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert result == (b"file", "text/plain", "file.txt")
    assert len(requests) == 1
    assert requests[0].url.path == "/_matrix/client/v1/media/download/example.org/media"
    assert requests[0].headers.get("authorization") == "Bearer tok"


@pytest.mark.asyncio
async def test_download_attachment_falls_back_to_legacy_on_404() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "client/v1/media" in request.url.path:
            return httpx.Response(404, json={"errcode": "M_UNRECOGNIZED"})
        return httpx.Response(200, content=b"legacy", headers={"content-type": "image/png"})

    adapter = _adapter()
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example.org",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await adapter.download_attachment(
            message=_stub_inbound_message(),
            attachment=MediaAttachment(url="mxc://example.org/media", filename="photo.png"),
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert result == (b"legacy", "image/png", "photo.png")
    assert len(requests) == 2
    assert "client/v1/media" in requests[0].url.path
    assert "media/v3/download" in requests[1].url.path


@pytest.mark.asyncio
async def test_download_attachment_does_not_fall_back_on_auth_error() -> None:
    """401/403 from the authenticated endpoint must propagate, not fall back."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})

    adapter = _adapter()
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example.org",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NonRetryableChannelError):
            await adapter.download_attachment(
                message=_stub_inbound_message(),
                attachment=MediaAttachment(url="mxc://example.org/media", filename="file.txt"),
            )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    # Only the authenticated endpoint was attempted; legacy was never called
    assert len(requests) == 1
    assert "client/v1/media" in requests[0].url.path


@pytest.mark.asyncio
async def test_download_attachment_fetches_mxc_media() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Accept either authenticated or legacy endpoint
        assert "example.org/media" in request.url.path
        return httpx.Response(200, content=b"file", headers={"content-type": "text/plain"})

    adapter = _adapter()
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://matrix.example.org",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await adapter.download_attachment(
            message=_stub_inbound_message(),
            attachment=MediaAttachment(url="mxc://example.org/media", filename="file.txt"),
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert result == (b"file", "text/plain", "file.txt")
