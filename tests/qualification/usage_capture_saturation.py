"""Synchronous real-capture admission at a saturated writer boundary."""

from __future__ import annotations

import asyncio
import json
import statistics
import time

import pytest
from sqlalchemy import event

from cognis.core.usage.capture_models import UsageWriterLimits
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.registry import build_provider_registry
from cognis.store.models import Base, LLMProvider
from tests.unit.test_provider_registry import _test_config

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.parametrize("usage_storage", ["sqlite"], indirect=True)
async def test_saturated_real_capture(usage_storage):
    h = usage_storage
    limits = UsageWriterLimits()
    await h.writer.drain()
    capture = UsageCapture(
        sink=h.writer,
        controller_owner_id=h.writer.controller_owner_id,
        coverage_epoch_id=h.writer.coverage_epoch_id,
    )
    attribution = h.event().context.attribution
    observations = [h.event(i + 10000) for i in range(limits.queue_items)]
    sql_count = 0

    def count_sql(connection, cursor, statement, parameters, context, executemany):
        nonlocal sql_count
        sql_count += 1

    event.listen(h.engine.sync_engine, "before_cursor_execute", count_sql)
    try:
        # No await: the task-owned writer cannot drain between filling and capture.
        for observation in observations:
            assert h.writer.try_emit(observation)
        assert h.writer.diagnostics().pending_items == limits.queue_items
        samples = []
        for _ in range(32):
            logical = capture.request(attribution=attribution, requested_model="gpt-4o-mini")
            binding = UsageBinding(logical, "provider-test", "gpt-4o-mini")
            started = time.perf_counter_ns()
            attempt = binding.begin(operation="responses", streaming=False, parameters={})
            assert attempt is not None
            attempt.finish()
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        diagnostics = h.writer.diagnostics()
        assert sql_count == 0
        assert capture.rejected_events == 64
        assert diagnostics.pending_items == limits.queue_items
        assert diagnostics.pending_bytes <= limits.queue_bytes
        print(
            "saturated_capture "
            + json.dumps(
                {
                    "samples": len(samples),
                    "p50_ms": statistics.median(samples),
                    "p95_ms": sorted(samples)[30],
                    "capture_rejected_events": capture.rejected_events,
                    "pending_items": diagnostics.pending_items,
                    "pending_bytes": diagnostics.pending_bytes,
                    "sql_during_synchronous_offers": sql_count,
                }
            )
        )
    finally:
        event.remove(h.engine.sync_engine, "before_cursor_execute", count_sql)


@pytest.mark.parametrize("usage_storage", ["sqlite"], indirect=True)
async def test_factory_inference_during_saturation(usage_storage, tmp_path, monkeypatch):
    """Fill the writer at the transport boundary, then complete one real SDK call."""
    h = usage_storage
    await h.writer.drain()
    async with h.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    observations = [h.event(i + 20000) for i in range(1024)]
    peer_counts = {"requests": 0, "filled": 0}
    body = json.dumps(
        {
            "id": "saturation-response",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "gpt-4o-mini",
            "output": [
                {
                    "id": "saturation-message",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        }
    ).encode()

    async def peer(reader, stream):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            await reader.readexactly(length)
            peer_counts["requests"] += 1
            for observation in observations:
                if h.writer.try_emit(observation):
                    peer_counts["filled"] += 1
            assert h.writer.diagnostics().pending_items == 1024
            stream.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await stream.drain()
        finally:
            stream.close()
            await stream.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _test_config(tmp_path, str(h.engine.url))
    capture = UsageCapture(
        sink=h.writer,
        controller_owner_id=h.writer.controller_owner_id,
        coverage_epoch_id=h.writer.coverage_epoch_id,
    )
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    registry = build_provider_registry(config, h.sessions, auth, usage_capture=capture)
    monkeypatch.setenv("COGNIS_FACTORY_TEST_API_KEY", "factory-test-key")
    async with h.sessions.begin() as session:
        session.add(
            LLMProvider(
                provider_id="saturation-provider",
                display_name="Saturation fixture",
                owner_email=SYSTEM_USER_EMAIL,
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-4o-mini",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "api_base": f"http://127.0.0.1:{port}/v1",
                    "auth_config": {"mode": "env", "env_var": "COGNIS_FACTORY_TEST_API_KEY"},
                },
                status="active",
            )
        )
    try:
        started = time.perf_counter()
        result = await registry.llm.generate(
            messages=[{"role": "user", "content": "synthetic"}],
            model="gpt-4o-mini",
            provider_id="saturation-provider",
            acting_user_email="perf-owner@usage.test",
            max_retries=0,
            service_tier="priority",
        )
        elapsed = time.perf_counter() - started
        assert result["choices"][0]["message"]["content"] == "ok"
        assert peer_counts["requests"] == 1
        assert capture.rejected_events > 0
        print(
            "factory_saturation "
            + json.dumps(
                {
                    **peer_counts,
                    "inference_seconds": elapsed,
                    "capture_rejected_events": capture.rejected_events,
                    "writer_dropped_finalized": h.writer.diagnostics().dropped_finalized,
                }
            )
        )
    finally:
        server.close()
        await server.wait_closed()
