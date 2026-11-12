"""Installed Responses SSE decoding retains provider provenance across UTF-8 fragments."""

from __future__ import annotations

import json

import pytest
from openai import AsyncOpenAI

from cognis.providers.llm.usage_binding import observed_sdk
from tests.unit.test_usage_capture_runtime import Sink
from tests.unit.test_usage_sdk_decoder import _peer, binding, finalized


@pytest.mark.parametrize("provider_usage", [False, True])
async def test_installed_responses_stream_projects_only_provider_events(provider_usage):
    sink = Sink()
    text = "PRIVATE Příliš žluťoučký 🚀"
    response = {
        "id": "resp-provider-decoded",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "gpt-4o-mini",
        "output": [],
    }
    if provider_usage:
        response["usage"] = {
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    events = [
        {
            "type": "response.output_text.delta",
            "sequence_number": 0,
            "item_id": "msg-provider-decoded",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {"type": "response.completed", "sequence_number": 1, "response": response},
    ]
    body = b"".join(
        b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n" for event in events
    )
    sends = 0

    async def reply(reader, writer):
        nonlocal sends
        try:
            await reader.readuntil(b"\r\n\r\n")
            sends += 1
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            for offset in range(0, len(body), 7):
                writer.write(body[offset : offset + 7])
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with (
        _peer(reply) as endpoint,
        AsyncOpenAI(api_key="capture-fixture-key", base_url=endpoint, max_retries=2) as client,
    ):
        stream = await observed_sdk(
            client.responses.create,
            binding(sink),
            operation="responses",
        )(model="gpt-4o-mini", input="PRIVATE PROMPT", stream=True)
        deltas = [
            event.delta async for event in stream if event.type == "response.output_text.delta"
        ]
    assert deltas == [text]
    assert sends == 1 and len(sink.offers) == 2
    final = finalized(sink)[0]
    assert final.outcome == "success"
    assert final.usage.input_tokens.value == (2 if provider_usage else None)
    assert final.usage.output_tokens.value == (3 if provider_usage else None)
    assert final.performance.ttft_ms is not None
    assert "PRIVATE" not in json.dumps([event.model_dump(mode="json") for event in sink.offers])
