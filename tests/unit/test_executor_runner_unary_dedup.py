from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from cognis.executor.runner import ExecutorRunner
from cognis.executor.unary_dedup import UnaryCallConflict, UnaryDedupCache
from cognis.models.local_models import OllamaRuntimeStatus
from cognis.models.tool import ExecutorConfig


class _MessageWebSocket:
    def __init__(self, messages: list[dict[str, Any]], *, fail_first_send: bool = False) -> None:
        self._messages = [json.dumps(message) for message in messages]
        self.fail_first_send = fail_first_send
        self.sent: list[dict[str, Any]] = []

    def __aiter__(self) -> _MessageWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, raw: str) -> None:
        if self.fail_first_send:
            self.fail_first_send = False
            raise RuntimeError("response transport lost")
        self.sent.append(json.loads(raw))


def _request(
    *,
    transport_id: str,
    stable_id: str,
    params: dict[str, Any] | None = None,
    method: str = "tool.list",
) -> dict[str, Any]:
    clean_params = params or {}
    return {
        "jsonrpc": "2.0",
        "method": method,
        "id": transport_id,
        "params": {
            **clean_params,
            "_cognis_replay_safe_unary": True,
            "_cognis_unary_call_id": stable_id,
            "_cognis_unary_payload_digest": UnaryDedupCache.digest(
                {"method": method, "params": clean_params}
            ),
        },
    }


def test_executor_cache_replays_terminal_result_and_rejects_conflict() -> None:
    cache = UnaryDedupCache()
    digest = cache.digest({"method": "tool.list", "params": {}})
    cache.put("stable", digest, {"jsonrpc": "2.0", "result": {"tools": []}, "id": "one"})
    assert cache.get("stable", digest)["result"] == {"tools": []}
    with pytest.raises(UnaryCallConflict):
        cache.get("stable", cache.digest({"method": "tool.list", "params": {"x": 1}}))


def test_executor_cache_has_ttl_lru_and_restart_semantics() -> None:
    cache = UnaryDedupCache(max_entries=1, ttl_seconds=0.1)
    digest = cache.digest({"method": "lsp.status", "params": {}})
    cache.put("first", digest, {"result": {"ok": True}})
    cache.put("second", digest, {"result": {"ok": True}})
    assert cache.get("first", digest) is None
    assert cache.get("second", digest) is not None
    time.sleep(0.12)
    assert cache.get("second", digest) is None
    restarted = UnaryDedupCache()
    assert restarted.get("second", digest) is None


@pytest.mark.asyncio
async def test_executor_cache_terminal_errors_terminate_joiners_and_replay() -> None:
    cache = UnaryDedupCache()
    digest = cache.digest({"method": "tool.list", "params": {}})
    future, owner = cache.join_or_claim("call", digest)
    assert owner and future is not None
    joined, owner = cache.join_or_claim("call", digest)
    assert not owner and joined is future
    cache.complete_error("call", digest, message="owner failed")
    terminal = await joined
    assert terminal["error"]["message"] == "owner failed"
    replay, owner = cache.join_or_claim("call", digest)
    assert not owner and replay is not None
    assert (await replay)["error"]["message"] == "owner failed"
    assert cache._inflight == {}

    future, owner = cache.join_or_claim("failed", digest)
    assert owner and future is not None
    cache.evict("failed", digest)
    assert (await future)["error"]["code"] == -32099
    assert cache.complete_error("failed", digest)["error"]["code"] == -32099
    assert cache._inflight == {}


def test_mutating_and_stream_calls_have_no_replay_marker_contract() -> None:
    cache = UnaryDedupCache()
    mutating_digest = cache.digest({"method": "tool.execute", "params": {"name": "x"}})
    # The runner never calls put for tool.execute/llm.complete.  This cache
    # test documents that no generic cache API can accidentally imply safety.
    assert cache.get("mutating", mutating_digest) is None


@pytest.mark.asyncio
async def test_runner_rejects_replay_safe_unary_digest_mismatch() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    request = _request(transport_id="invalid", stable_id="logical")
    request["params"]["_cognis_unary_payload_digest"] = "invalid"
    ws = _MessageWebSocket([request])

    await runner._message_loop(ws)

    assert ws.sent == [
        {
            "jsonrpc": "2.0",
            "error": {"code": -32602, "message": "Invalid unary dedup metadata"},
            "id": "invalid",
        }
    ]


@pytest.mark.asyncio
async def test_runner_strips_transport_metadata_before_strict_local_model_handlers() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _RuntimeHandler:
        def __init__(self) -> None:
            self.shown: list[str] = []

        async def inspect(self) -> OllamaRuntimeStatus:
            return OllamaRuntimeStatus(
                management_enabled=True,
                reachable=True,
                installed=[{"name": "qwen3:8b"}],
            )

        async def inspect_model(self, runtime_name: str) -> dict[str, str]:
            self.shown.append(runtime_name)
            return {"model": runtime_name}

    handler = _RuntimeHandler()
    runner._ollama_runtime_handler = handler  # type: ignore[assignment]
    ws = _MessageWebSocket(
        [
            _request(
                method="local_model.status",
                transport_id="status",
                stable_id="status-logical",
            ),
            _request(
                method="local_model.show",
                transport_id="show",
                stable_id="show-logical",
                params={"runtime_name": "qwen3:8b"},
            ),
        ]
    )

    await runner._message_loop(ws)
    await asyncio.gather(*tuple(runner._replay_safe_tasks.values()))

    assert handler.shown == ["qwen3:8b"]
    assert next(frame for frame in ws.sent if frame["id"] == "status")["result"]["reachable"]
    assert next(frame for frame in ws.sent if frame["id"] == "show")["result"] == {
        "model": "qwen3:8b"
    }


@pytest.mark.asyncio
async def test_runner_replays_cached_strict_local_model_result_after_response_loss() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    executions = 0

    class _RuntimeHandler:
        async def inspect_model(self, runtime_name: str) -> dict[str, str]:
            nonlocal executions
            executions += 1
            return {"model": runtime_name}

    runner._ollama_runtime_handler = _RuntimeHandler()  # type: ignore[assignment]
    request = _request(
        method="local_model.show",
        transport_id="lost",
        stable_id="logical",
        params={"runtime_name": "qwen3:8b"},
    )
    first = _MessageWebSocket([request], fail_first_send=True)
    await runner._message_loop(first)
    await asyncio.sleep(0.01)

    retry_request = _request(
        method="local_model.show",
        transport_id="retry",
        stable_id="logical",
        params={"runtime_name": "qwen3:8b"},
    )
    reconnect = _MessageWebSocket([retry_request])
    await runner._message_loop(reconnect)
    await asyncio.sleep(0.01)

    assert executions == 1
    assert reconnect.sent == [
        {
            "jsonrpc": "2.0",
            "result": {"model": "qwen3:8b"},
            "id": "retry",
        }
    ]


@pytest.mark.asyncio
async def test_runner_strips_transport_metadata_from_param_consuming_unary_handler() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    received: list[dict[str, Any]] = []

    async def handle_background_status(
        ws: Any,
        msg_id: str | None,
        params: dict[str, Any],
    ) -> None:
        received.append(params)
        await runner._send_rpc_result(ws, msg_id, {"processes": []})

    runner._handle_background_shell_status = handle_background_status  # type: ignore[method-assign]
    ws = _MessageWebSocket(
        [
            _request(
                method="shell.background_status",
                transport_id="status",
                stable_id="logical",
                params={"include_completed": True},
            )
        ]
    )

    await runner._message_loop(ws)
    await asyncio.gather(*tuple(runner._replay_safe_tasks.values()))

    assert received == [{"include_completed": True}]
    assert ws.sent[0]["result"] == {"processes": []}


@pytest.mark.asyncio
async def test_runner_replays_same_terminal_unary_and_rejects_payload_conflict() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    executions = 0

    async def handle_tool_list(ws: Any, msg_id: str | None) -> None:
        nonlocal executions
        executions += 1
        await runner._send_rpc_result(ws, msg_id, {"tools": [{"name": "bash"}]})

    runner._handle_tool_list = handle_tool_list  # type: ignore[method-assign]
    ws = _MessageWebSocket(
        [
            _request(transport_id="one", stable_id="logical"),
            _request(transport_id="two", stable_id="logical"),
            _request(transport_id="three", stable_id="logical", params={"changed": True}),
        ]
    )

    await runner._message_loop(ws)

    assert executions == 1
    assert [frame["id"] for frame in ws.sent] == ["one", "two", "three"]
    assert ws.sent[0]["result"] == ws.sent[1]["result"]
    assert ws.sent[2]["error"]["code"] == -32061


@pytest.mark.asyncio
async def test_runner_caches_before_send_reconnects_and_restart_loses_cache() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    executions = 0

    async def handle_tool_list(ws: Any, msg_id: str | None) -> None:
        nonlocal executions
        executions += 1
        await runner._send_rpc_result(ws, msg_id, {"tools": [{"name": "bash"}]})

    runner._handle_tool_list = handle_tool_list  # type: ignore[method-assign]
    first = _MessageWebSocket(
        [_request(transport_id="lost", stable_id="logical")],
        fail_first_send=True,
    )
    await runner._message_loop(first)

    reconnect = _MessageWebSocket([_request(transport_id="retry", stable_id="logical")])
    await runner._message_loop(reconnect)
    assert executions == 1
    assert reconnect.sent[0]["id"] == "retry"
    assert reconnect.sent[0]["result"]["tools"] == [{"name": "bash"}]

    restarted = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    restarted_executions = 0

    async def restarted_handle(ws: Any, msg_id: str | None) -> None:
        nonlocal restarted_executions
        restarted_executions += 1
        await restarted._send_rpc_result(ws, msg_id, {"tools": []})

    restarted._handle_tool_list = restarted_handle  # type: ignore[method-assign]
    after_restart = _MessageWebSocket([_request(transport_id="restart", stable_id="logical")])
    await restarted._message_loop(after_restart)
    assert restarted_executions == 1
    assert after_restart.sent[0]["result"]["tools"] == []


@pytest.mark.asyncio
async def test_runner_disconnect_reconnect_joins_blocked_owner_before_release() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    started = asyncio.Event()
    release = asyncio.Event()
    executions = 0

    async def handle_tool_list(ws: Any, msg_id: str | None) -> None:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        raise RuntimeError("owner exploded")

    runner._handle_tool_list = handle_tool_list  # type: ignore[method-assign]
    old = _MessageWebSocket(
        [_request(transport_id="old", stable_id="blocked")],
        fail_first_send=True,
    )
    await runner._message_loop(old)
    await started.wait()

    conflict = _MessageWebSocket(
        [_request(transport_id="conflict", stable_id="blocked", params={"changed": True})]
    )
    await runner._message_loop(conflict)
    assert conflict.sent[0]["error"]["code"] == -32061
    assert executions == 1

    reconnect = _MessageWebSocket([_request(transport_id="new", stable_id="blocked")])
    reconnect_task = asyncio.create_task(runner._message_loop(reconnect))
    await asyncio.sleep(0)
    assert not reconnect.sent
    release.set()
    await reconnect_task
    await asyncio.sleep(0.01)
    assert executions == 1
    assert reconnect.sent[0]["id"] == "new"
    assert (
        reconnect.sent[0]["error"]["message"] == "Replay-safe unary call failed before completion"
    )
    later = _MessageWebSocket([_request(transport_id="later", stable_id="blocked")])
    await runner._message_loop(later)
    assert executions == 1
    assert later.sent[0]["error"]["message"] == reconnect.sent[0]["error"]["message"]
    assert runner._replay_safe_tasks == {}
    assert runner._unary_dedup._inflight == {}


@pytest.mark.asyncio
async def test_cancelled_reconnect_joiner_does_not_cancel_shared_unary_result() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    started = asyncio.Event()
    release = asyncio.Event()
    executions = 0

    async def handle_tool_list(ws: Any, msg_id: str | None) -> None:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        await runner._send_rpc_result(ws, msg_id, {"tools": [{"name": "bash"}]})

    runner._handle_tool_list = handle_tool_list  # type: ignore[method-assign]

    owner = _MessageWebSocket([_request(transport_id="owner", stable_id="shared")])
    await runner._message_loop(owner)
    await asyncio.wait_for(started.wait(), timeout=1)

    disconnected_joiner = _MessageWebSocket([_request(transport_id="joiner-a", stable_id="shared")])
    await runner._message_loop(disconnected_joiner)
    await asyncio.sleep(0)
    assert executions == 1
    assert disconnected_joiner.sent == []

    release.set()
    await asyncio.wait_for(runner._replay_safe_tasks["shared"], timeout=1)

    final_joiner = _MessageWebSocket([_request(transport_id="joiner-b", stable_id="shared")])
    await runner._message_loop(final_joiner)

    assert executions == 1
    assert final_joiner.sent == [
        {
            "jsonrpc": "2.0",
            "result": {"tools": [{"name": "bash"}]},
            "id": "joiner-b",
        }
    ]


@pytest.mark.asyncio
async def test_runner_inflight_join_conflict_and_eviction_are_bounded() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    started = asyncio.Event()
    release = asyncio.Event()
    executions = 0

    async def handle_tool_list(ws: Any, msg_id: str | None) -> None:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        await runner._send_rpc_result(ws, msg_id, {"tools": [{"name": "bash"}]})

    runner._handle_tool_list = handle_tool_list  # type: ignore[method-assign]
    first = _MessageWebSocket(
        [
            _request(transport_id="one", stable_id="logical"),
            _request(transport_id="two", stable_id="logical"),
            _request(transport_id="three", stable_id="logical", params={"changed": True}),
        ]
    )
    loop_task = asyncio.create_task(runner._message_loop(first))
    await started.wait()
    release.set()
    await loop_task
    await asyncio.sleep(0.01)
    assert executions == 1
    assert {frame["id"] for frame in first.sent} == {"one", "two", "three"}
    assert next(frame for frame in first.sent if frame["id"] == "three")["error"]["code"] == -32061

    bounded = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    bounded._unary_dedup.max_entries = 1
    first_cache = _request(transport_id="a", stable_id="a")
    second_cache = _request(transport_id="b", stable_id="b")
    first_digest = first_cache["params"]["_cognis_unary_payload_digest"]
    second_digest = second_cache["params"]["_cognis_unary_payload_digest"]
    evicted_future, _ = bounded._unary_dedup.join_or_claim("a", str(first_digest))
    bounded._unary_dedup.join_or_claim("b", str(second_digest))
    assert "a" not in bounded._unary_dedup._inflight
    assert evicted_future is not None
    assert (await evicted_future)["error"]["code"] == -32099


@pytest.mark.asyncio
async def test_runner_owner_failure_sends_cached_error_to_connected_owner() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    executions = 0

    async def failed_tool_list(ws: Any, msg_id: str | None) -> None:
        nonlocal executions
        executions += 1
        raise RuntimeError("owner failed")

    runner._handle_tool_list = failed_tool_list  # type: ignore[method-assign]
    owner = _MessageWebSocket([_request(transport_id="owner", stable_id="failed")])
    await runner._message_loop(owner)
    await asyncio.sleep(0.01)
    assert executions == 1
    assert owner.sent[0]["id"] == "owner"
    assert owner.sent[0]["error"]["code"] == -32098

    joiner = _MessageWebSocket([_request(transport_id="joiner", stable_id="failed")])
    await runner._message_loop(joiner)
    assert joiner.sent[0]["id"] == "joiner"
    assert joiner.sent[0]["error"] == owner.sent[0]["error"]
    assert executions == 1


@pytest.mark.asyncio
async def test_runner_eviction_sends_cached_error_to_evicted_owner() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    runner._unary_dedup.max_entries = 1
    release = asyncio.Event()

    async def blocked_tool_list(ws: Any, msg_id: str | None) -> None:
        await release.wait()
        await runner._send_rpc_result(ws, msg_id, {"tools": []})

    runner._handle_tool_list = blocked_tool_list  # type: ignore[method-assign]
    owner = _MessageWebSocket([_request(transport_id="owner", stable_id="evicted")])
    await runner._message_loop(owner)
    survivor = _MessageWebSocket([_request(transport_id="survivor", stable_id="survivor")])
    await runner._message_loop(survivor)
    await asyncio.sleep(0.01)
    assert owner.sent[0]["id"] == "owner"
    assert owner.sent[0]["error"]["code"] == -32099

    later = _MessageWebSocket([_request(transport_id="later", stable_id="evicted")])
    await runner._message_loop(later)
    assert later.sent[0]["id"] == "later"
    assert later.sent[0]["error"] == owner.sent[0]["error"]
    release.set()
