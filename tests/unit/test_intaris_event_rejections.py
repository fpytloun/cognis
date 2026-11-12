"""Conservative event rejection handling against old and coded receivers."""

import asyncio
from unittest.mock import Mock

import httpx
import pytest

from cognis.models.session import SessionEvent
from cognis.providers.guardrails.event_keys import EventIdempotencyKeyError
from cognis.providers.guardrails.intaris import IntarisProvider


@pytest.mark.asyncio
async def test_invalid_event_key_is_rejected_before_network_delivery(monkeypatch):
    provider = IntarisProvider("http://localhost:8060", Mock())
    calls = []

    async def post(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid keys must not reach the receiver")

    monkeypatch.setattr(provider.client, "post", post)
    try:
        with pytest.raises(EventIdempotencyKeyError):
            await provider.record_events(
                "ingestion-session",
                [SessionEvent(type="assistant_message", data={})],
                idempotency_key="cancelled-active-stream:ingestion-session:turn-checkpoint",
            )
        assert calls == []
    finally:
        await provider.client.aclose()


@pytest.mark.parametrize(
    "status,code,attempts",
    [
        (404, "session_not_found", 4),
        (404, "recording_disabled", 1),
        (404, None, 1),
        (404, "unrecognized_code", 1),
        (409, "append_in_progress", 1),
        (409, "append_uncertain", 1),
        (400, "batch_too_large", 1),
        (401, "unauthenticated", 1),
        (403, "forbidden", 1),
        (422, "validation_failed", 1),
        (503, None, 4),
        (500, "append_uncertain", 1),
    ],
)
def test_bounded_attempts_and_unchanged_identity(status, code, attempts, monkeypatch):
    async def run():
        provider = IntarisProvider("http://localhost:8060", Mock())
        monkeypatch.setattr(provider, "_headers", lambda **kwargs: {})
        calls = []

        async def post(url, **kwargs):
            calls.append((url, kwargs["json"], kwargs["params"]))
            return httpx.Response(
                status,
                headers={"X-Intaris-Event-Error": code} if code else {},
                # Legacy text must not enable the missing-session fallback.
                json={"detail": "Session ingestion-session not found."},
                request=httpx.Request("POST", "http://localhost:8060" + url),
            )

        monkeypatch.setattr(provider.client, "post", post)
        try:
            call = provider.record_events(
                "ingestion-session",
                [SessionEvent(type="message", data={"index": 1})],
                idempotency_key="ingestion-session:batch:1",
                retry_missing_session=True,
            )
            if code == "session_not_found":
                assert not (await call).ok
            else:
                with pytest.raises(httpx.HTTPStatusError):
                    await call
            assert len(calls) == attempts
            assert all(call == calls[0] for call in calls)
        finally:
            await provider.client.aclose()

    asyncio.run(run())


def test_sender_cancellation_does_not_spawn_retry(monkeypatch):
    async def run():
        provider = IntarisProvider("http://localhost:8060", Mock())
        monkeypatch.setattr(provider, "_headers", lambda **kwargs: {})
        started = asyncio.Event()
        calls = 0

        async def post(*args, **kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(provider.client, "post", post)
        try:
            task = asyncio.create_task(
                provider.record_events(
                    "ingestion-session",
                    [SessionEvent(type="message", data={})],
                    idempotency_key="ingestion-session:batch:1",
                )
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.01)
            assert calls == 1
        finally:
            await provider.client.aclose()

    asyncio.run(run())
