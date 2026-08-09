from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields

import httpx
import pytest

from cognis.models.session import SessionEvent
from cognis.providers.guardrails.events import (
    MAX_EVENT_NOTIFICATION_ID_LENGTH,
    EventAppendNotification,
    EventStoreAuthority,
)
from cognis.providers.guardrails.intaris import MAX_EVENT_APPEND_LISTENERS, IntarisProvider
from cognis.runtime_context import scoped_runtime_context


class _AuthProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def sign_service_jwt(
        self,
        subject: str,
        agent_id: str,
        audience: list[str],
        *,
        agent_owner_email: str | None = None,
    ) -> str:
        assert audience == ["intaris"]
        self.calls.append((subject, agent_id, agent_owner_email))
        return "token"


class _AppendResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        count: int = 1,
        first_seq: int = 7,
        last_seq: int = 7,
    ) -> None:
        self.status_code = status_code
        self._payload = {
            "ok": status_code < 400,
            "count": count,
            "first_seq": first_seq,
            "last_seq": last_seq,
        }
        self.request = httpx.Request(
            "POST",
            "http://intaris.invalid/api/v1/session/private-session/events",
        )

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("append failed", request=self.request, response=response)

    def json(self) -> dict[str, object]:
        return self._payload


def _install_append_response(
    provider: IntarisProvider,
    response: _AppendResponse,
) -> None:
    async def _post(*_: object, **__: object) -> _AppendResponse:
        return response

    provider.client.post = _post  # type: ignore[method-assign]


async def _record(provider: IntarisProvider, **identity: str) -> None:
    await provider.record_events(
        "private-session",
        [SessionEvent(type="user_message", data={"content": "private-content"})],
        **identity,
    )


def test_event_authority_is_normalized_validated_and_immutable() -> None:
    authority = EventStoreAuthority(
        user_email=" User@Example.COM ",
        agent_id=" agent-1 ",
        agent_owner_email=" OWNER@Example.COM ",
    )

    assert authority == EventStoreAuthority(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="owner@example.com",
    )
    with pytest.raises(FrozenInstanceError):
        authority.agent_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="must not be empty"):
        EventStoreAuthority("", "agent", "owner@example.com")
    with pytest.raises(ValueError, match="maximum length"):
        EventStoreAuthority(
            "user@example.com",
            "x" * (MAX_EVENT_NOTIFICATION_ID_LENGTH + 1),
            "owner@example.com",
        )


def test_notification_is_immutable_and_strictly_validated() -> None:
    authority = EventStoreAuthority("user@example.com", "agent", "owner@example.com")
    notification = EventAppendNotification(authority, "session", 4, 5, 2)

    assert {field.name for field in fields(notification)} == {
        "authority",
        "session_id",
        "first_seq",
        "last_seq",
        "event_count",
        "events",
        "payload_bytes",
    }
    assert "content" not in repr(notification).casefold()
    with pytest.raises(FrozenInstanceError):
        notification.last_seq = 6  # type: ignore[misc]
    with pytest.raises(ValueError, match="zero sequence range"):
        EventAppendNotification(authority, "session", 4, 4, 0)
    with pytest.raises(ValueError, match="must match"):
        EventAppendNotification(authority, "session", 4, 6, 2)


@pytest.mark.asyncio
async def test_record_events_explicit_authority_precedes_context() -> None:
    auth = _AuthProvider()
    provider = IntarisProvider("http://localhost:8060", auth)
    _install_append_response(provider, _AppendResponse())
    received: list[EventAppendNotification] = []

    async def listener(notification: EventAppendNotification) -> None:
        received.append(notification)

    provider.add_event_append_listener(listener)
    try:
        with scoped_runtime_context(
            user_email="context@example.com",
            agent_id="context-agent",
            agent_owner_email="context-owner@example.com",
        ):
            await _record(
                provider,
                user_email=" Explicit@Example.COM ",
                agent_id=" explicit-agent ",
                agent_owner_email=" Explicit-Owner@Example.COM ",
            )
    finally:
        await provider.client.aclose()

    assert received[0].authority == EventStoreAuthority(
        "explicit@example.com",
        "explicit-agent",
        "explicit-owner@example.com",
    )
    assert received[0].events == (
        SessionEvent(type="user_message", data={"content": "private-content"}),
    )
    assert auth.calls[-1] == (
        "explicit@example.com",
        "explicit-agent",
        "explicit-owner@example.com",
    )


@pytest.mark.asyncio
async def test_record_events_uses_complete_context_authority() -> None:
    provider = IntarisProvider("http://localhost:8060", _AuthProvider())
    _install_append_response(provider, _AppendResponse(count=2, first_seq=8, last_seq=9))
    received: list[EventAppendNotification] = []

    async def listener(notification: EventAppendNotification) -> None:
        received.append(notification)

    provider.add_event_append_listener(listener)
    try:
        with scoped_runtime_context(
            user_email=" Context@Example.COM ",
            agent_id=" context-agent ",
            agent_owner_email=" Owner@Example.COM ",
        ):
            await _record(provider)
    finally:
        await provider.client.aclose()

    assert received == [
        EventAppendNotification(
            EventStoreAuthority(
                "context@example.com",
                "context-agent",
                "owner@example.com",
            ),
            "private-session",
            8,
            9,
            2,
        )
    ]


@pytest.mark.asyncio
async def test_blank_explicit_authority_does_not_fall_back_to_context() -> None:
    provider = IntarisProvider("http://localhost:8060", _AuthProvider())
    _install_append_response(provider, _AppendResponse())
    received: list[EventAppendNotification] = []

    async def listener(notification: EventAppendNotification) -> None:
        received.append(notification)

    provider.add_event_append_listener(listener)
    try:
        with scoped_runtime_context(
            user_email="context@example.com",
            agent_id="context-agent",
            agent_owner_email="context-owner@example.com",
        ):
            result = await provider.record_events(
                "private-session",
                [SessionEvent(type="user_message", data={"content": "private-content"})],
                agent_id=" ",
            )
    finally:
        await provider.client.aclose()

    assert result.ok is True
    assert received == []


@pytest.mark.asyncio
async def test_record_events_missing_authority_preserves_success_without_notification(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = IntarisProvider("http://localhost:8060", _AuthProvider())
    _install_append_response(provider, _AppendResponse())
    received: list[EventAppendNotification] = []

    async def listener(notification: EventAppendNotification) -> None:
        received.append(notification)

    provider.add_event_append_listener(listener)
    try:
        result = await provider.record_events(
            "private-session",
            [SessionEvent(type="user_message", data={"content": "private-content"})],
            user_email="user@example.com",
        )
    finally:
        await provider.client.aclose()

    assert result.ok is True
    assert received == []
    assert "authority_unavailable" in caplog.records[-1].extra_data["reason"]


@pytest.mark.asyncio
async def test_record_events_notifies_once_only_after_success() -> None:
    provider = IntarisProvider("http://localhost:8060", _AuthProvider())
    _install_append_response(provider, _AppendResponse(count=3, first_seq=11, last_seq=13))
    received: list[EventAppendNotification] = []

    async def listener(notification: EventAppendNotification) -> None:
        received.append(notification)

    provider.add_event_append_listener(listener)
    try:
        await _record(
            provider,
            user_email="user@example.com",
            agent_id="agent",
            agent_owner_email="owner@example.com",
        )
    finally:
        await provider.client.aclose()

    assert len(received) == 1
    assert (received[0].first_seq, received[0].last_seq, received[0].event_count) == (
        11,
        13,
        3,
    )


@pytest.mark.asyncio
async def test_record_events_does_not_notify_on_upstream_failure() -> None:
    provider = IntarisProvider("http://localhost:8060", _AuthProvider())
    _install_append_response(provider, _AppendResponse(status_code=401))
    called = False

    async def listener(_: EventAppendNotification) -> None:
        nonlocal called
        called = True

    provider.add_event_append_listener(listener)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await _record(
                provider,
                user_email="user@example.com",
                agent_id="agent",
                agent_owner_email="owner@example.com",
            )
    finally:
        await provider.client.aclose()

    assert called is False


@pytest.mark.asyncio
async def test_listener_failures_and_timeouts_are_isolated() -> None:
    provider = IntarisProvider(
        "http://localhost:8060",
        _AuthProvider(),
        event_append_listener_timeout=0.01,
    )
    _install_append_response(provider, _AppendResponse())
    completed = False

    async def failing(_: EventAppendNotification) -> None:
        raise RuntimeError("listener failed")

    async def slow(_: EventAppendNotification) -> None:
        await asyncio.sleep(1)

    async def successful(_: EventAppendNotification) -> None:
        nonlocal completed
        completed = True

    provider.add_event_append_listener(failing)
    provider.add_event_append_listener(slow)
    provider.add_event_append_listener(successful)
    try:
        await asyncio.wait_for(
            _record(
                provider,
                user_email="user@example.com",
                agent_id="agent",
                agent_owner_email="owner@example.com",
            ),
            timeout=0.2,
        )
    finally:
        await provider.client.aclose()

    assert completed is True


@pytest.mark.asyncio
async def test_default_listener_timeout_is_bounded_to_one_tenth_second() -> None:
    provider = IntarisProvider("http://localhost:8060", _AuthProvider())
    _install_append_response(provider, _AppendResponse())

    async def slow(_: EventAppendNotification) -> None:
        await asyncio.sleep(1)

    provider.add_event_append_listener(slow)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    try:
        await _record(
            provider,
            user_email="user@example.com",
            agent_id="agent",
            agent_owner_email="owner@example.com",
        )
    finally:
        await provider.client.aclose()

    elapsed = loop.time() - started_at
    assert 0.08 <= elapsed < 0.5


def test_listener_registration_is_identity_based_bounded_and_removable() -> None:
    provider = IntarisProvider("http://localhost:8060", _AuthProvider())

    async def listener(_: EventAppendNotification) -> None:
        return None

    assert provider.add_event_append_listener(listener) is True
    assert provider.add_event_append_listener(listener) is False
    assert provider.remove_event_append_listener(listener) is True
    assert provider.remove_event_append_listener(listener) is False

    listeners = [
        (lambda index: lambda _: asyncio.sleep(0, result=None))(index)
        for index in range(MAX_EVENT_APPEND_LISTENERS)
    ]
    for registered in listeners:
        assert provider.add_event_append_listener(registered) is True
    with pytest.raises(RuntimeError, match="maximum"):
        provider.add_event_append_listener(listener)


@pytest.mark.asyncio
async def test_notification_logging_does_not_leak_payload_or_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = IntarisProvider("https://private-intaris.example", _AuthProvider())
    _install_append_response(provider, _AppendResponse())

    async def failing(_: EventAppendNotification) -> None:
        raise RuntimeError("private-listener-payload")

    provider.add_event_append_listener(failing)
    try:
        await _record(
            provider,
            user_email="private-user@example.com",
            agent_id="private-agent",
            agent_owner_email="private-owner@example.com",
        )
    finally:
        await provider.client.aclose()

    logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        "private-content",
        "private-session",
        "private-user@example.com",
        "private-agent",
        "private-owner@example.com",
        "private-intaris.example",
        "private-listener-payload",
        "Bearer",
    ):
        assert secret not in logs
