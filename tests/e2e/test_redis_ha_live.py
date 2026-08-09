"""Opt-in destructive Redis HA scenarios against the assembled Compose stack."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any

import httpx
import pytest
import websockets.sync.client

from tests.e2e.redis_ha_contract import (
    assert_final_exactly_once,
    assert_remote_progress_contract,
    assert_remote_terminal_contract,
)

pytestmark = [pytest.mark.e2e]


def _live_url() -> str:
    url = os.getenv("COGNIS_REDIS_HA_E2E_URL", "").rstrip("/")
    if not url:
        pytest.skip("set COGNIS_REDIS_HA_E2E_URL for destructive assembled Redis HA E2E")
    return url


def _compose(*args: str) -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            os.getenv("COGNIS_HA_E2E_PROJECT", "cognis-ha-e2e"),
            "--env-file",
            ".local/cognis-ha-e2e/current/compose.env",
            "-f",
            "compose.local.yml",
            "-f",
            "compose.e2e.yml",
            "-f",
            "compose.ha-e2e.yml",
            "-f",
            "compose.redis-ha-e2e.yml",
            *args,
        ],
        check=True,
    )


def _login(client: httpx.Client, url: str) -> str:
    response = client.post(
        f"{url}/api/auth/login",
        headers={"X-Cognis-HA-Controller": "controller-1"},
        json={
            "email": os.getenv("COGNIS_LOCAL_ADMIN_EMAIL", "admin@cognis-e2e.localdev.me"),
            "password": os.getenv("COGNIS_LOCAL_ADMIN_PASSWORD", "cognis-local-admin"),
        },
    )
    response.raise_for_status()
    return response.json()["token"]


def _headers(token: str, controller: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Cognis-HA-Controller": controller,
    }


def _create_conversation(client: httpx.Client, url: str, token: str) -> str:
    response = client.post(
        f"{url}/api/v1/conversations",
        headers=_headers(token, "controller-1"),
        json={
            "agent_id": "e2e-test-agent",
            "title": f"Redis HA {uuid.uuid4().hex[:8]}",
            "context": {
                "type": "web",
                "ref": None,
                "platform_data": {"redis_ha_e2e": True},
                "memory_labels": {},
            },
        },
    )
    response.raise_for_status()
    return response.json()["conversation_id"]


def _subscribe_remote(
    client: httpx.Client,
    url: str,
    token: str,
    conversation_id: str,
) -> Any:
    snapshot = client.get(
        f"{url}/api/v1/chat/v2/conversations/{conversation_id}/snapshot",
        headers=_headers(token, "controller-2"),
    )
    snapshot.raise_for_status()
    ws_url = url.replace("http://", "ws://").replace("https://", "wss://")
    socket = websockets.sync.client.connect(
        f"{ws_url}/api/ws",
        additional_headers={"X-Cognis-HA-Controller": "controller-2"},
        open_timeout=10,
        close_timeout=5,
    )
    socket.send(json.dumps({"type": "auth", "token": token}))
    assert json.loads(socket.recv(timeout=10))["type"] == "authenticated"
    socket.send(
        json.dumps(
            {
                "type": "chat_v2_subscribe",
                "scope": {
                    "key": f"conversation:{conversation_id}",
                    "kind": "conversation",
                    "conversation_id": conversation_id,
                },
                "cursor": snapshot.json()["cursor"],
            }
        )
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        event = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
        if event.get("type") == "chat_v2_frame":
            return socket
        if event.get("type") == "error":
            pytest.fail(f"Chat v2 subscription failed: {event!r}")
    pytest.fail("timed out waiting for Chat v2 subscription snapshot")


def _admit(client: httpx.Client, url: str, token: str, conversation_id: str) -> None:
    transaction = f"redis-ha-{uuid.uuid4().hex}"
    response = client.put(
        f"{url}/api/v1/chat/v2/conversations/{conversation_id}/messages/{transaction}",
        headers=_headers(token, "controller-1"),
        json={
            "content": "scenario:redis-ha-remote-progress",
            "attachments": [],
            "client_message_id": transaction,
            "chat_mode": "default",
        },
    )
    response.raise_for_status()


def _wait_final(
    client: httpx.Client,
    url: str,
    token: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 120
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = client.get(
            f"{url}/api/v1/chat/v2/conversations/{conversation_id}/snapshot",
            headers=_headers(token, "controller-2"),
        )
        response.raise_for_status()
        last = response.json()["timeline"]["items"]
        assistants = [
            item
            for item in last
            if item.get("kind") == "message" and item.get("role") == "assistant"
        ]
        if assistants:
            return last
        time.sleep(0.5)
    pytest.fail(f"timed out waiting for canonical completion; last={last!r}")


def _wait_remote_terminal(socket: Any, frames: list[dict[str, Any]]) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        event = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
        if event.get("type") != "chat_v2_frame":
            continue
        frames.append(event)
        runtime = event.get("runtime")
        if isinstance(runtime, dict) and runtime.get("has_active_turn") is False:
            assert_remote_terminal_contract(frames)
            return
    pytest.fail("timed out waiting for remote terminal Chat v2 frame")


def test_redis_stop_mid_turn_keeps_readiness_and_final_exactly_once() -> None:
    url = _live_url()
    with httpx.Client(timeout=15) as client:
        token = _login(client, url)
        conversation_id = _create_conversation(client, url, token)
        socket = _subscribe_remote(client, url, token, conversation_id)
        try:
            _admit(client, url, token, conversation_id)
            frames: list[dict[str, Any]] = []
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                event = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
                if event.get("type") == "chat_v2_frame":
                    frames.append(event)
                    if "thinking" in str(event) or "assistant" in str(event):
                        break
            assert_remote_progress_contract(frames)
            _compose("stop", "redis")
            for controller in ("controller-1", "controller-2"):
                response = client.get(
                    f"{url}/api/readyz",
                    headers={"X-Cognis-HA-Controller": controller},
                )
                assert response.status_code == 200
            assert_final_exactly_once(_wait_final(client, url, token, conversation_id))
        finally:
            socket.close()
            _compose("start", "redis")


def test_redis_restart_restores_subsequent_remote_streaming() -> None:
    url = _live_url()
    _compose("start", "redis")
    with httpx.Client(timeout=15) as client:
        token = _login(client, url)
        conversation_id = _create_conversation(client, url, token)
        socket = _subscribe_remote(client, url, token, conversation_id)
        try:
            _admit(client, url, token, conversation_id)
            frames: list[dict[str, Any]] = []
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                event = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
                if event.get("type") == "chat_v2_frame":
                    frames.append(event)
                    if "thinking" in str(event) or "assistant" in str(event):
                        break
            assert_remote_progress_contract(frames)
            _wait_remote_terminal(socket, frames)
            assert_final_exactly_once(_wait_final(client, url, token, conversation_id))
        finally:
            socket.close()


def test_owner_takeover_rejects_late_runtime_frames() -> None:
    _live_url()
    pytest.skip(
        "assembled harness has no production test API for pausing and replaying an "
        "already-fenced runtime envelope; do not invent a frame injection hook"
    )


def test_twenty_clients_measure_intaris_read_amplification() -> None:
    _live_url()
    pytest.skip(
        "the current Intaris E2E service exposes no request counter/reset API; "
        "qualification must wait for a production-path counting proxy or service counter"
    )
