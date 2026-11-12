"""Logical delivery uses actual ASGI sends, not provider or client clock guesses."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from cognis.models.usage import (
    UsageAttribution,
    UsageClientSpan,
    UsageHTTPMetadata,
    UsageIdentity,
    UsageRequestStats,
)
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.llm.usage_delivery import (
    UsageDeliveryMiddleware,
    bind_usage_request,
    emit_client_span,
)


def setup_capture():
    offers = []

    class Sink:
        def try_emit(self, observation):
            offers.append(observation)
            return True

    capture = UsageCapture(
        sink=Sink(),
        controller_owner_id="controller-test",
        coverage_epoch_id="coverage-test",
    )
    request = capture.request(
        attribution=UsageAttribution(
            identity=UsageIdentity(
                principal_type="service",
                principal_subject="mnemory",
                usage_subject_kind="service",
                usage_subject_id="mnemory",
                service_id="mnemory",
            ),
            purpose="recall",
        )
    )
    return offers, capture, request


async def test_observation_only_after_actual_final_send_completion():
    offers, capture, request = setup_capture()
    send_started = asyncio.Event()
    allow_send = asyncio.Event()
    sent = []

    async def app(scope, receive, send):
        bind_usage_request(scope, request)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"response", "more_body": False})

    async def send(message):
        if message["type"] == "http.response.body":
            send_started.set()
            await allow_send.wait()
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    task = asyncio.create_task(
        UsageDeliveryMiddleware(app, capture=capture)(
            {"type": "http", "state": {}},
            receive,
            send,
        )
    )
    await send_started.wait()
    assert offers == []
    allow_send.set()
    await task
    assert len(offers) == 1
    stats = offers[0]
    assert isinstance(stats, UsageRequestStats)
    assert stats.logical_outcome == "success"
    assert stats.server.first_sent_at is not None
    assert stats.server.last_sent_at is not None
    assert stats.server.completed_at is not None
    assert stats.server.ttft_ms is None  # Body send is not semantic generation.
    assert stats.client is None
    assert stats.request_id == request.request_id
    assert (b"x-cognis-request-id", request.request_id.encode()) in sent[0]["headers"]


async def test_failed_send_does_not_claim_completed_or_client_received():
    offers, capture, request = setup_capture()

    async def app(scope, receive, send):
        bind_usage_request(scope, request)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"response", "more_body": False})

    async def send(message):
        if message["type"] == "http.response.body":
            raise ConnectionError("sensitive network exception")

    async def receive():
        return {"type": "http.request", "body": b""}

    with pytest.raises(ConnectionError):
        await UsageDeliveryMiddleware(app, capture=capture)(
            {"type": "http", "state": {}},
            receive,
            send,
        )
    assert len(offers) == 1
    stats = offers[0]
    assert stats.logical_outcome == "error"
    assert stats.server.completed_at is None
    assert stats.server.first_sent_at is None
    assert stats.client is None
    assert "sensitive" not in stats.model_dump_json()


async def test_bare_header_does_not_bind_foreign_request():
    offers, capture, request = setup_capture()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        pass

    async def receive():
        return {"type": "http.request"}

    await UsageDeliveryMiddleware(app, capture=capture)(
        {"type": "http", "headers": [(b"x-cognis-request-id", request.request_id.encode())]},
        receive,
        send,
    )
    assert offers == []


async def test_sse_error_keeps_actual_http_status_and_logical_failure():
    offers, capture, request = setup_capture()
    request.http_metadata = UsageHTTPMetadata(
        request_path="/v1/chat/completions", streaming=True, user_agent="OpenAI/Python"
    )
    request.gateway_outcome = "error"

    async def app(scope, receive, send):
        bind_usage_request(scope, request)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})

    async def send(message):
        pass

    async def receive():
        return {"type": "http.request"}

    await UsageDeliveryMiddleware(app, capture=capture)(
        {"type": "http", "state": {}}, receive, send
    )
    assert len(offers) == 1
    assert offers[0].logical_outcome == "error"
    assert offers[0].http_metadata.http_status_code == 200
    assert offers[0].http_metadata.streaming is True


def test_client_observation_is_source_labelled_and_cannot_set_server_outcome():
    offers, capture, request = setup_capture()
    assert emit_client_span(
        capture=capture,
        request=request,
        request_started_at=datetime.now(UTC),
        span=UsageClientSpan(source="authenticated_report", elapsed_ms=120, ttft_ms=30),
    )
    stats = offers[0]
    assert stats.stats_scope == "client"
    assert stats.client.source == "authenticated_report"
    assert stats.server is None
    assert stats.logical_outcome is None
    assert stats.client.elapsed_ms == 120
