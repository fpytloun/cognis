import asyncio
import json

import httpx
import pytest

from cognis.testing.barrier import RequestBarrier
from tests.e2e.append_barrier import AppendBarrierProxy


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [200, 503])
async def test_proxy_health_preserves_real_status_without_auth_rewrite(upstream_status):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(upstream_status, content=b'{"status":"fixture-health"}')

    proxy = AppendBarrierProxy("http://127.0.0.1:8123", transport=httpx.MockTransport(upstream))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 1234)),
            base_url="http://fixture",
        ) as client:
            response = await client.get("/health", headers={"authorization": "Bearer synthetic"})
            assert response.status_code == upstream_status
            assert response.content == b'{"status":"fixture-health"}'
            assert seen[0].url.path == "/health"
            assert seen[0].headers["authorization"] == "Bearer synthetic"
            assert (await client.post("/health")).status_code == 400
            assert len(seen) == 1
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_proxy_ignores_environment_and_preserves_unmatched_query(monkeypatch):
    seen = []
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:9999")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.1:9999")
    monkeypatch.delenv("NO_PROXY", raising=False)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False
        return real_client(**kwargs)

    monkeypatch.setattr("tests.e2e.append_barrier.httpx.AsyncClient", client_factory)

    def upstream(request):
        seen.append(request)
        return httpx.Response(307, headers={"location": "http://192.0.2.2/blocked"})

    proxy = AppendBarrierProxy("http://127.0.0.1:8123", transport=httpx.MockTransport(upstream))
    proxy.arm("fixture-session", "fixture-admission")
    try:
        async with real_client(
            transport=httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 1234)),
            base_url="http://fixture",
            follow_redirects=False,
        ) as client:
            response = await client.get("/api/v1/session/other/events?after_seq=7&limit=1")
            assert response.status_code == 307
            assert len(seen) == 1
            assert seen[0].url.query == b"after_seq=7&limit=1"
            assert not proxy.barrier.claimed
        async with real_client(
            transport=httpx.ASGITransport(app=proxy.app, client=("192.0.2.3", 1234)),
            base_url="http://fixture",
        ) as remote:
            assert (await remote.get("/api/v1/session/other/events")).status_code == 403
            assert len(seen) == 1
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_timed_out_append_is_never_forwarded():
    def upstream(_request):
        pytest.fail("Expired append must never reach real upstream")

    proxy = AppendBarrierProxy("http://127.0.0.1:8123", transport=httpx.MockTransport(upstream))
    gate = proxy.arm("fixture-session", "fixture-admission")
    gate.timeout = 0.001
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 1234)),
            base_url="http://fixture",
        ) as client:
            with pytest.raises(TimeoutError):
                await client.post(
                    "/api/v1/session/fixture-session/events",
                    json=[
                        {
                            "type": "user_message",
                            "data": {"client_message_id": "fixture-admission"},
                        },
                    ],
                )
        with pytest.raises(RuntimeError, match="cancelled"):
            gate.release()
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_mock_llm_barrier_control_is_loopback_only_and_cancelled_on_shutdown(tmp_path):
    from cognis.testing.mock_llm.server import create_app

    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 1234)),
            base_url="http://fixture",
        ) as remote:
            assert (await remote.post("/__mock/barrier", json={})).status_code == 403
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://fixture",
        ) as local:
            assert (
                await local.post(
                    "/__mock/barrier",
                    json={
                        "scenario_id": "fixture-scenario",
                        "turn_index": 0,
                    },
                )
            ).status_code == 201
            gate = app.state.release_barrier[2]
            task = asyncio.create_task(gate.hold())
            assert (await local.get("/__mock/barrier")).status_code == 204
    with pytest.raises(RuntimeError, match="cancelled"):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["expired", "cancelled", "released"])
async def test_mock_llm_barrier_never_reports_terminal_gate_as_held(tmp_path, terminal):
    from cognis.testing.mock_llm.server import create_app

    app = create_app(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://fixture",
        ) as client,
    ):
        assert (
            await client.post(
                "/__mock/barrier", json={"scenario_id": "fixture-scenario", "turn_index": 0}
            )
        ).status_code == 201
        gate = app.state.release_barrier[2]
        if terminal == "expired":
            gate.timeout = 0.001
        task = asyncio.create_task(gate.hold())
        await gate.entered.wait()
        if terminal == "expired":
            with pytest.raises(TimeoutError):
                await task
        elif terminal == "cancelled":
            gate.cancel()
            with pytest.raises(RuntimeError, match="cancelled"):
                await task
        else:
            gate.release()
            await task
        assert (await client.get("/__mock/barrier")).status_code == 410
        cancelled = await client.delete("/__mock/barrier")
        assert cancelled.status_code == (410 if terminal == "expired" else 204)
        assert app.state.release_barrier is None
        assert (
            await client.post(
                "/__mock/barrier", json={"scenario_id": "fixture-scenario", "turn_index": 0}
            )
        ).status_code == 201


@pytest.mark.asyncio
async def test_barrier_timeout_and_cancel_never_release_successfully():
    gate = RequestBarrier(timeout=0.001)
    with pytest.raises(TimeoutError):
        await gate.hold()
    with pytest.raises(RuntimeError, match="cancelled"):
        gate.release()
    gate = RequestBarrier()
    task = asyncio.create_task(gate.hold())
    await gate.entered.wait()
    gate.cancel()
    with pytest.raises(RuntimeError, match="cancelled"):
        await task


@pytest.mark.asyncio
async def test_append_proxy_holds_exact_identity_and_forwards_bytes_and_auth():
    received = []

    async def upstream(request):
        received.append(request)
        return httpx.Response(200, content=b'{"first_seq":1,"last_seq":1}')

    proxy = AppendBarrierProxy("http://127.0.0.1:8123", transport=httpx.MockTransport(upstream))
    gate = proxy.arm("fixture-session", "fixture-admission")
    raw = json.dumps(
        [
            {
                "type": "user_message",
                "data": {
                    "client_message_id": "fixture-admission",
                    "content": "synthetic",
                },
            }
        ]
    ).encode()
    transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 1234))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            task = asyncio.create_task(
                client.post(
                    "/api/v1/session/fixture-session/events",
                    content=raw,
                    headers={
                        "authorization": "Bearer synthetic-test-token",
                        "content-type": "application/json",
                    },
                )
            )
            await asyncio.wait_for(gate.entered.wait(), timeout=1)
            assert not received
            with pytest.raises(RuntimeError, match="Only one"):
                proxy.arm("other-session", "other-admission")
            gate.release()
            response = await task
            assert response.status_code == 200
            assert received[0].content == raw
            assert received[0].headers["authorization"] == "Bearer synthetic-test-token"
            metadata = proxy.snapshot_request_metadata()
            assert len(metadata) == 1
            assert metadata[0] == {
                "operation": "record_events",
                "method": "POST",
                "scheme": "http",
                "host": "127.0.0.1",
                "port": 8123,
                "status_code": 200,
                "duration_ms": metadata[0]["duration_ms"],
            }
            assert "synthetic" not in json.dumps(metadata)
            assert "fixture-session" not in json.dumps(metadata)
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_append_proxy_shutdown_cancels_held_request_without_forwarding():
    def upstream(_request):
        pytest.fail("Cancelled append must not be forwarded")

    proxy = AppendBarrierProxy("http://127.0.0.1:8123", transport=httpx.MockTransport(upstream))
    gate = proxy.arm("fixture-session", "fixture-admission")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 1234)),
        base_url="http://fixture",
    ) as client:
        task = asyncio.create_task(
            client.post(
                "/api/v1/session/fixture-session/events",
                json=[
                    {"type": "user_message", "data": {"client_message_id": "fixture-admission"}},
                ],
            )
        )
        await asyncio.wait_for(gate.entered.wait(), timeout=1)
        await proxy.close()
        with pytest.raises(RuntimeError, match="cancelled"):
            await task
