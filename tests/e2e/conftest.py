"""E2E test fixtures — self-contained stack.

Starts the full Cognis + Mnemory + Intaris + mock-llm stack as subprocesses,
seeds a capability-off e2e agent, and provides helpers for scenario injection
and WS event capture.

No dependency on the integration conftest — fully self-contained.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
import pytest

from cognis.bootstrap import ensure_data_dir, ensure_jwt_keypair, ensure_secrets_key
from cognis.config import load_config

E2E_AGENT_ID = "e2e-test-agent"
E2E_PROVIDER_ID = "e2e-mock-llm"
SCENARIOS_DIR = Path(__file__).parent / "scenarios"
GOLDEN_DIR = Path(__file__).parent / "golden"
CANONICAL_CAPTURE_DIR = Path(__file__).parents[2] / "ui" / "src" / "lib" / "chat-v2" / "captures"
PROJECTION_RESET_LOCK = Lock()


# ---------------------------------------------------------------------------
# Port + process helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(url: str, *, timeout: float = 120.0, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/health", timeout=5.0)
            return
        except httpx.ConnectError as exc:
            last_error = exc
        except Exception as exc:
            last_error = exc
        time.sleep(interval)
    raise RuntimeError(
        f"Service at {url} did not become healthy within {timeout}s"
        + (f": {last_error}" if last_error else "")
    )


def _start_proc(
    command: list[str],
    env: dict[str, str],
    *,
    label: str,
    log_dir: Path,
) -> subprocess.Popen:
    base = dict(os.environ)
    for key in (
        "COGNIS_DATA_DIR",
        "COGNIS_PORT",
        "COGNIS_HOST",
        "DATABASE_URL",
        "COGNIS_MNEMORY_URL",
        "COGNIS_INTARIS_URL",
        "COGNIS_INITIAL_ADMIN_EMAIL",
        "COGNIS_INITIAL_ADMIN_PASSWORD",
        "COGNIS_LOG_FORMAT",
        "COGNIS_LOG_LEVEL",
        "COGNIS_E2E_MODE",
    ):
        base.pop(key, None)
    base.update(env)
    log_path = log_dir / f"{label}.log"
    log_handle = open(log_path, "w")  # noqa: SIM115
    proc = subprocess.Popen(command, env=base, stdout=log_handle, stderr=subprocess.STDOUT)
    proc._log_handle = log_handle  # type: ignore[attr-defined]
    return proc


def _stop_proc(proc: subprocess.Popen, label: str) -> None:
    log_handle = getattr(proc, "_log_handle", None)
    if proc.poll() is not None:
        if log_handle:
            log_handle.close()
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    if log_handle:
        log_handle.close()


def _dump_log(label: str, proc: subprocess.Popen, log_dir: Path) -> None:
    log_path = log_dir / f"{label}.log"
    print(f"\n=== {label} (pid={proc.pid}, poll={proc.poll()}) ===")
    if log_path.exists():
        print(log_path.read_text()[-4096:])


# ---------------------------------------------------------------------------
# E2EStack dataclass
# ---------------------------------------------------------------------------


@dataclass
class E2EStack:
    cognis_url: str
    ws_url: str
    mock_llm_url: str
    admin_email: str
    admin_password: str
    admin_token: str
    e2e_agent_id: str
    e2e_conversation_id: str | None
    http: httpx.Client

    def admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.admin_headers(), **kwargs.pop("headers", {})}
        return self.http.post(f"{self.cognis_url}{path}", headers=headers, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.admin_headers(), **kwargs.pop("headers", {})}
        return self.http.get(f"{self.cognis_url}{path}", headers=headers, **kwargs)


# ---------------------------------------------------------------------------
# Session-scoped e2e_stack fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[E2EStack]:
    """Start the full e2e stack: Cognis + Mnemory + Intaris + mock-llm."""
    dict(os.environ)
    base_dir = tmp_path_factory.mktemp("e2e")
    log_dir = base_dir / "logs"
    log_dir.mkdir()

    cognis_port = _free_port()
    mnemory_port = _free_port()
    intaris_port = _free_port()
    mock_llm_port = _free_port()

    cognis_url = f"http://127.0.0.1:{cognis_port}"
    mnemory_url = f"http://127.0.0.1:{mnemory_port}"
    intaris_url = f"http://127.0.0.1:{intaris_port}"
    mock_llm_url = f"http://127.0.0.1:{mock_llm_port}"
    ws_url = f"ws://127.0.0.1:{cognis_port}/api/ws"

    admin_email = "e2e-admin@cognis-test.example.com"
    admin_password = "e2e-test-password-xyz"

    uvx_path = shutil.which("uvx")
    uv_path = shutil.which("uv")
    if uvx_path is None or uv_path is None:
        pytest.skip("uvx/uv not found on PATH")

    # Bootstrap Cognis keys before starting anything
    cognis_dir = base_dir / "cognis"
    cognis_dir.mkdir()
    from dataclasses import replace

    bootstrap_config = replace(
        load_config(),
        data_dir=cognis_dir,
        host="127.0.0.1",
        port=cognis_port,
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        database_url=f"sqlite+aiosqlite:///{cognis_dir / 'cognis.db'}",
        jwt_private_key_path=cognis_dir / "keys" / "private.pem",
        jwt_public_key_path=cognis_dir / "keys" / "public.pem",
        secrets_key_path=cognis_dir / "secrets.key",
        log_level="warning",
        log_format="text",
        serve_ui=False,
        cors_origins=["*"],
        artifact_path=cognis_dir / "artifacts",
        vapid_private_key_path=cognis_dir / "keys" / "vapid_private.pem",
        initial_admin_email=admin_email,
        initial_admin_password=admin_password,
    )
    ensure_data_dir(bootstrap_config)
    ensure_jwt_keypair(bootstrap_config)
    ensure_secrets_key(bootstrap_config)
    public_key_path = str(cognis_dir / "keys" / "public.pem")

    procs: list[tuple[str, subprocess.Popen]] = []

    try:
        # Start mock-llm
        mock_llm_proc = _start_proc(
            [
                uv_path,
                "run",
                "python",
                "-m",
                "cognis.testing.mock_llm",
                "--port",
                str(mock_llm_port),
                "--host",
                "127.0.0.1",
            ],
            {
                "MOCK_LLM_PORT": str(mock_llm_port),
                "MOCK_LLM_HOST": "127.0.0.1",
                "MOCK_LLM_SCENARIOS_DIR": str(SCENARIOS_DIR),
                "MOCK_LLM_LOG_LEVEL": "warning",
            },
            label="mock-llm",
            log_dir=log_dir,
        )
        procs.append(("mock-llm", mock_llm_proc))

        # Start Mnemory
        mnemory_dir = base_dir / "mnemory"
        mnemory_dir.mkdir()
        mnemory_proc = _start_proc(
            [uvx_path, "mnemory"],
            {
                "DATA_DIR": str(mnemory_dir),
                "MCP_HOST": "127.0.0.1",
                "MCP_PORT": str(mnemory_port),
                "MNEMORY_JWT_PUBLIC_KEY": public_key_path,
                "LLM_API_KEY": "mock-key",
                "LLM_BASE_URL": f"{mock_llm_url}/v1",
                "EMBED_API_KEY": "mock-key",
                "EMBED_BASE_URL": f"{mock_llm_url}/v1",
                "LOG_LEVEL": "warning",
            },
            label="mnemory",
            log_dir=log_dir,
        )
        procs.append(("mnemory", mnemory_proc))

        # Start Intaris
        intaris_dir = base_dir / "intaris"
        intaris_dir.mkdir()
        intaris_proc = _start_proc(
            [uvx_path, "intaris"],
            {
                "DATA_DIR": str(intaris_dir),
                "INTARIS_HOST": "127.0.0.1",
                "INTARIS_PORT": str(intaris_port),
                "INTARIS_JWT_PUBLIC_KEY": public_key_path,
                "LLM_API_KEY": "mock-key",
                "LLM_BASE_URL": f"{mock_llm_url}/v1",
                "ANALYSIS_ENABLED": "false",
                "LOG_LEVEL": "warning",
            },
            label="intaris",
            log_dir=log_dir,
        )
        procs.append(("intaris", intaris_proc))

        # Wait for mock-llm, mnemory, intaris
        _wait_healthy(mock_llm_url, timeout=30)
        _wait_healthy(mnemory_url, timeout=120)
        _wait_healthy(intaris_url, timeout=120)

        # Start Cognis
        cognis_proc = _start_proc(
            [uv_path, "run", "cognis-controller", "serve"],
            {
                "COGNIS_DATA_DIR": str(cognis_dir),
                "COGNIS_HOST": "127.0.0.1",
                "COGNIS_PORT": str(cognis_port),
                "COGNIS_MNEMORY_URL": mnemory_url,
                "COGNIS_INTARIS_URL": intaris_url,
                "COGNIS_INITIAL_ADMIN_EMAIL": admin_email,
                "COGNIS_INITIAL_ADMIN_PASSWORD": admin_password,
                "COGNIS_LOG_FORMAT": "text",
                "COGNIS_LOG_LEVEL": "warning",
                "COGNIS_CORS_ORIGINS": "*",
                "COGNIS_E2E_MODE": "true",
                "COGNIS_DEFAULT_MEMORY_BACKEND": "none",
                "COGNIS_DEFAULT_GUARDRAILS_BACKEND": "none",
                "COGNIS_LOCAL_LLM_API_KEY": "mock-key",
            },
            label="cognis",
            log_dir=log_dir,
        )
        procs.append(("cognis", cognis_proc))

        _wait_healthy(f"{cognis_url}/api", timeout=120)

    except Exception:
        for label, proc in procs:
            _dump_log(label, proc, log_dir)
            _stop_proc(proc, label)
        raise

    # Login and get admin token
    http = httpx.Client(timeout=30.0)
    login_resp = http.post(
        f"{cognis_url}/api/auth/login",
        json={"email": admin_email, "password": admin_password},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
    admin_token = login_resp.json()["token"]

    # Seed e2e resources
    e2e_conversation_id = _seed_e2e_resources(cognis_url, admin_token, mock_llm_url, http)

    stack = E2EStack(
        cognis_url=cognis_url,
        ws_url=ws_url,
        mock_llm_url=mock_llm_url,
        admin_email=admin_email,
        admin_password=admin_password,
        admin_token=admin_token,
        e2e_agent_id=E2E_AGENT_ID,
        e2e_conversation_id=e2e_conversation_id,
        http=http,
    )

    yield stack

    # Teardown
    http.close()
    for label, proc in reversed(procs):
        _stop_proc(proc, label)


def _seed_e2e_resources(
    cognis_url: str,
    admin_token: str,
    mock_llm_url: str,
    http: httpx.Client,
) -> str | None:
    """Seed e2e provider, agent, and a test conversation."""
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Create mock-llm provider
    provider_resp = http.post(
        f"{cognis_url}/api/v1/llm-providers",
        headers=headers,
        json={
            "provider_id": E2E_PROVIDER_ID,
            "display_name": "Mock LLM (e2e)",
            "location": "controller",
            "backend": "litellm",
            "config": {
                "scope": "system",
                "preset": "litellm_proxy",
                "api_base": f"{mock_llm_url}/v1",
                "default_model": "mock-model",
                "models": [
                    {
                        "model_id": "mock-model",
                        "display_name": "Mock LLM",
                        "supports_tools": True,
                        "supports_streaming": True,
                        "context_window": 131072,
                        "max_output_tokens": 8192,
                        "tier": "cheap",
                    }
                ],
                "auth_config": {"mode": "env", "env_var": "COGNIS_LOCAL_LLM_API_KEY"},
            },
        },
    )
    if provider_resp.status_code not in (200, 409):
        raise RuntimeError(f"Failed to create e2e provider: {provider_resp.text}")

    # Create an in-process executor for the e2e agent
    E2E_EXECUTOR_ID = "e2e-inprocess-executor"
    executor_resp = http.post(
        f"{cognis_url}/api/v1/executors",
        headers=headers,
        json={
            "executor_id": E2E_EXECUTOR_ID,
            "name": "E2E In-Process Executor",
            "executor_type": "in_process",
            "labels": {"role": "e2e"},
            "enabled_tool_groups": [],
            "enabled_tools": ["bash"],
            "is_default": True,
            "config": {},
        },
    )
    if executor_resp.status_code not in (200, 409):
        # Executor may already exist — try to continue
        pass

    # Create e2e agent with capabilities disabled
    agent_resp = http.post(
        f"{cognis_url}/api/v1/agents",
        headers=headers,
        json={
            "agent_id": E2E_AGENT_ID,
            "name": "E2E Test Agent",
            "display_name": "E2E Test Agent",
            "description": "Deterministic e2e test agent",
            "system_prompt": "You are a deterministic test assistant. Respond concisely.",
            "capabilities": {
                "memory_backend": "none",
                "guardrails_backend": "none",
            },
            "tools": {
                "builtin_tools": ["memory_search", "memory_add", "bash"],
                "tool_groups": [],
            },
            "permissions": {
                "tool_permissions": {"*": "allow"},
                "can_delegate": False,
            },
            "llm_config": {
                "provider_id": E2E_PROVIDER_ID,
                "model": "mock-model",
            },
            "execution": {
                "executor_id": E2E_EXECUTOR_ID,
            },
            "agent_type": "primary",
            "status": "draft",
        },
    )
    if agent_resp.status_code not in (200, 409):
        raise RuntimeError(f"Failed to create e2e agent: {agent_resp.text}")

    # Activate agent
    http.post(f"{cognis_url}/api/v1/agents/{E2E_AGENT_ID}/activate", headers=headers)

    # Create a test conversation
    conv_resp = http.post(
        f"{cognis_url}/api/v1/conversations",
        headers=headers,
        json={
            "agent_id": E2E_AGENT_ID,
            "title": "E2E test conversation",
            "context": {"type": "web", "ref": None, "platform_data": {}, "memory_labels": {}},
        },
    )
    if conv_resp.status_code == 200:
        return conv_resp.json().get("conversation_id")
    return None


# ---------------------------------------------------------------------------
# WS event capture helper
# ---------------------------------------------------------------------------


def capture_ws_events(
    stack: E2EStack,
    conversation_id: str,
    message: str,
    *,
    timeout: float = 90,
    post_completion_window: float = 3.0,
    capture_reconnect_snapshot: bool = True,
) -> list[dict[str, Any]]:
    """Send a message via WebSocket and collect all events.

    Captures events until ``message_complete`` (or ``error``), then continues
    for ``post_completion_window`` seconds to collect post-completion events
    (``conversation_state_delta``, ``workflow_step_completed``, etc.).

    When ``capture_reconnect_snapshot`` is True (default), after the turn
    completes the function opens a **new** WS connection and subscribes to
    the conversation.  The server sends a ``conversation_runtime_snapshot``
    on every (re)connect.  This snapshot is appended to the event stream so
    the golden replay can assert that reconnecting after a completed turn
    never re-injects ``streaming:true`` items.

    This is the critical capture that was missing: the reconnect snapshot is
    the exact event that caused the hanging-spinner bug (stale active_thinking
    re-emitted on reconnect with no subsequent message_complete to finalize).
    """
    import websockets.sync.client as wsc

    events: list[dict[str, Any]] = []
    last_seq: int = 0
    active_session_id: str | None = None

    with wsc.connect(stack.ws_url, close_timeout=5, open_timeout=10) as ws:
        ws.send(json.dumps({"type": "auth", "token": stack.admin_token}))
        auth_msg = json.loads(ws.recv(timeout=15))
        assert auth_msg["type"] == "authenticated", f"WS auth failed: {auth_msg}"
        _subscribe_chat_v2(ws, stack, conversation_id, events=events)

        ws.send(
            json.dumps(
                {
                    "type": "reconnect",
                    "conversation_id": conversation_id,
                    "last_seq": 0,
                }
            )
        )
        time.sleep(0.3)

        ws.send(
            json.dumps(
                {
                    "type": "message",
                    "conversation_id": conversation_id,
                    "content": message,
                }
            )
        )

        deadline = time.monotonic() + timeout
        message_complete_at: float | None = None

        while time.monotonic() < deadline:
            try:
                # After message_complete, use the post-completion window
                if message_complete_at is not None:
                    remaining = max(
                        0.05, message_complete_at + post_completion_window - time.monotonic()
                    )
                    if remaining <= 0.05:
                        break
                else:
                    remaining = max(1.0, deadline - time.monotonic())

                raw = ws.recv(timeout=remaining)
                event = json.loads(raw)
                events.append(event)

                # Track the highest seq and active session for the reconnect
                if isinstance(event.get("seq"), int) and event["seq"] > last_seq:
                    last_seq = event["seq"]
                if event.get("type") == "reconnected":
                    active_session_id = event.get("session_id") or active_session_id
                if event.get("type") == "message_complete":
                    active_session_id = event.get("session_id") or active_session_id
                    if event.get("seq"):
                        last_seq = max(last_seq, event["seq"])

                if event.get("type") in ("message_complete", "error"):
                    if message_complete_at is None:
                        message_complete_at = time.monotonic()
                    if event.get("type") == "error":
                        break  # Stop immediately on error
                    # Continue for post_completion_window to capture trailing events

            except TimeoutError:
                break
            except Exception:
                break

    if not capture_reconnect_snapshot or message_complete_at is None:
        return events

    # Open a fresh WS connection and subscribe — the server sends a
    # conversation_runtime_snapshot on every (re)connect.  Append it to the
    # golden stream so the replay can assert no streaming items survive.
    time.sleep(0.5)  # Brief pause so the server has settled after the turn
    try:
        with wsc.connect(stack.ws_url, close_timeout=5, open_timeout=10) as ws2:
            ws2.send(json.dumps({"type": "auth", "token": stack.admin_token}))
            auth2 = json.loads(ws2.recv(timeout=10))
            if auth2.get("type") != "authenticated":
                return events
            _subscribe_chat_v2(ws2, stack, conversation_id, events=events)

            ws2.send(
                json.dumps(
                    {
                        "type": "reconnect",
                        "conversation_id": conversation_id,
                        "last_seq": last_seq,
                        "session_id": active_session_id,
                    }
                )
            )

            # Collect the initial burst (state_snapshot + runtime_snapshot + reconnected)
            reconnect_deadline = time.monotonic() + 5.0
            while time.monotonic() < reconnect_deadline:
                try:
                    raw = ws2.recv(timeout=max(0.1, reconnect_deadline - time.monotonic()))
                    event = json.loads(raw)
                    events.append(event)
                    # Stop after we've seen the reconnected ack — that's the
                    # full initial burst (state_snapshot, runtime_snapshot, reconnected)
                    if event.get("type") == "reconnected":
                        break
                except TimeoutError:
                    break
                except Exception:
                    break
    except Exception:
        pass  # Best-effort — don't fail the test if reconnect capture fails

    # Complete the canonical producer sequence with a server-owned reset and
    # recovery snapshot.  Use the first live frame cursor so the response is
    # produced by real range reconciliation rather than fabricated JSON.
    scope = {"kind": "conversation", "conversation_id": conversation_id}
    snapshot_response = stack.get(_scope_snapshot_path(scope))
    if snapshot_response.status_code == 200:
        current_snapshot = snapshot_response.json()
        frame = next(
            (event for event in events if event.get("type") == "chat_v2_frame"),
            None,
        )
        reset_cursor = frame.get("cursor_before") if frame else current_snapshot["cursor"]
        reset_payload, recovery_payload = _capture_reset_recovery(
            stack,
            scope,
            reset_cursor,
            current_snapshot,
        )
        events.append({"type": "sync", **reset_payload})
        events.append({"type": "snapshot", **recovery_payload})
    return events


def _scope_snapshot_path(scope: dict[str, Any]) -> str:
    """Return the native REST snapshot route for a backend-issued scope."""
    if scope["kind"] == "session":
        return f"/api/v1/chat/v2/sessions/{scope['session_id']}/snapshot"
    if scope["kind"] == "task_step":
        return f"/api/v1/chat/v2/task-steps/{scope['step_run_id']}/snapshot"
    return f"/api/v1/chat/v2/conversations/{scope['conversation_id']}/snapshot"


def _assert_reset_recovery_snapshot(
    *,
    pre_reset: dict[str, Any],
    reset: dict[str, Any],
    recovery: dict[str, Any],
) -> None:
    """Require a recovery snapshot to use the reset response's projection."""
    expected = (reset["schema_version"], reset["projection_version"])
    assert (recovery["schema_version"], recovery["projection_version"]) == expected
    assert (pre_reset["schema_version"], pre_reset["projection_version"]) != expected


def _capture_reset_recovery(
    stack: E2EStack,
    scope: dict[str, Any],
    cursor: str,
    pre_reset: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically advance, reset, and fetch a fresh scoped recovery snapshot."""
    with PROJECTION_RESET_LOCK:
        sync_path = _scope_snapshot_path(scope).removesuffix("/snapshot") + "/sync"
        control = stack.post("/api/v1/chat/v2/e2e/projection-generation")
        assert control.status_code == 200, control.text
        expected_projection_version = control.json()["projection_version"]
        reset = stack.get(sync_path, params={"cursor": cursor, "limit": 1})
        assert reset.status_code == 200, reset.text
        reset_payload = reset.json()
        assert reset_payload["reset_required"] is True, reset_payload
        assert reset_payload["projection_version"] == expected_projection_version, reset_payload
        recovery = stack.get(_scope_snapshot_path(scope))
        assert recovery.status_code == 200, recovery.text
        recovery_payload = recovery.json()
        _assert_reset_recovery_snapshot(
            pre_reset=pre_reset,
            reset=reset_payload,
            recovery=recovery_payload,
        )
    return reset_payload, recovery_payload


def _subscribe_chat_v2(
    ws: Any,
    stack: E2EStack,
    conversation_id: str,
    *,
    events: list[dict[str, Any]] | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Subscribe to a production ChatV2 scope and optionally record its snapshot."""
    snapshot_scope = scope or {
        "kind": "conversation",
        "conversation_id": conversation_id,
    }
    snapshot = stack.get(_scope_snapshot_path(snapshot_scope))
    assert snapshot.status_code == 200, snapshot.text
    payload = snapshot.json()
    if events is not None:
        # The authoritative snapshot must precede every runtime frame.  This
        # is intentionally fetched through the same native scoped route used
        # by ChatV2Store, rather than reconstructed from websocket messages.
        events.append({"type": "snapshot", **payload})
    ws.send(
        json.dumps(
            {
                "type": "chat_v2_subscribe",
                "scope": payload["scope"],
                "cursor": payload["cursor"],
            }
        )
    )
    return payload


def capture_scoped_scope_events(
    stack: E2EStack,
    scope: dict[str, Any],
    *,
    timeout: float = 10,
) -> list[dict[str, Any]]:
    """Capture a live backend-issued session/task-step scope.

    The scope is taken from the linked task/session resources returned by the
    backend.  Both the REST snapshot and websocket subscription use the
    scope's native route and exact cursor.
    """
    import websockets.sync.client as wsc

    def lifecycle_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
        items = payload.get("timeline", {}).get("items", [])
        tools = tuple(
            (
                item.get("id"),
                item.get("status"),
                item.get("result_preview"),
            )
            for item in items
            if item.get("kind") == "tool_call"
        )
        completed_messages = tuple(
            (item.get("id"), item.get("role"), item.get("content"))
            for item in items
            if item.get("kind") == "message" and item.get("stable")
        )
        return payload.get("scope", {}).get("status"), tools, completed_messages

    conversation_id = scope["conversation_id"]
    events: list[dict[str, Any]] = []
    snapshot_response = stack.get(_scope_snapshot_path(scope))
    assert snapshot_response.status_code == 200, snapshot_response.text
    snapshot = snapshot_response.json()
    events.append({"type": "snapshot", **snapshot})
    current_cursor = snapshot["cursor"]
    last_snapshot_cursor = current_cursor
    last_lifecycle_signature = lifecycle_signature(snapshot)
    with wsc.connect(stack.ws_url, close_timeout=5, open_timeout=10) as ws:
        ws.send(json.dumps({"type": "auth", "token": stack.admin_token}))
        auth_msg = json.loads(ws.recv(timeout=15))
        assert auth_msg["type"] == "authenticated", f"WS auth failed: {auth_msg}"
        # This is an actual reconnect request on the native scope, not a
        # synthetic record.  It makes the producer exercise the same
        # reconnect path used by the UI before it records any frames.
        events.append(
            {
                "type": "reconnect",
                "scope": snapshot["scope"],
                "cursor": snapshot["cursor"],
            }
        )
        ws.send(
            json.dumps(
                {
                    "type": "reconnect",
                    "conversation_id": conversation_id,
                    "session_id": snapshot["scope"].get("session_id"),
                    "last_seq": 0,
                    "chat_v2_cursor": snapshot["cursor"],
                }
            )
        )
        ws.send(
            json.dumps(
                {
                    "type": "chat_v2_subscribe",
                    "scope": snapshot["scope"],
                    "cursor": snapshot["cursor"],
                }
            )
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = json.loads(
                    ws.recv(timeout=min(0.1, max(0.05, deadline - time.monotonic())))
                )
                if (
                    event.get("type") == "chat_v2_frame"
                    and event.get("cursor_before") != current_cursor
                ):
                    # Sync polling may already have consumed the same durable
                    # events. Do not promote a now-stale realtime duplicate
                    # after the newer cursor; the live client would reject it.
                    continue
                events.append(event)
                if event.get("type") == "chat_v2_frame" and event.get("cursor_after"):
                    current_cursor = event["cursor_after"]
            except TimeoutError:
                # Capture authoritative lifecycle snapshots while the native
                # subscription remains attached. A snapshot advances the
                # replay boundary atomically, avoiding partial-sync cursors
                # racing ahead of a later full snapshot.
                live_snapshot_response = stack.get(_scope_snapshot_path(scope))
                assert live_snapshot_response.status_code == 200, live_snapshot_response.text
                live_snapshot = live_snapshot_response.json()
                if live_snapshot["cursor"] != last_snapshot_cursor:
                    signature = lifecycle_signature(live_snapshot)
                    if signature != last_lifecycle_signature:
                        events.append({"type": "snapshot", **live_snapshot})
                        last_lifecycle_signature = signature
                    last_snapshot_cursor = live_snapshot["cursor"]
                    current_cursor = live_snapshot["cursor"]
            except Exception:
                break

    # Anchor the reset request to a snapshot that is present in the promoted
    # sequence. Lifecycle-signature compaction may have skipped cursor-only
    # snapshots while still advancing ``current_cursor``.
    pre_reset = stack.get(_scope_snapshot_path(scope))
    assert pre_reset.status_code == 200, pre_reset.text
    pre_reset_payload = pre_reset.json()
    last_promoted_snapshot = next(
        event for event in reversed(events) if event.get("type") == "snapshot"
    )
    if pre_reset_payload["cursor"] != last_promoted_snapshot["cursor"]:
        events.append({"type": "snapshot", **pre_reset_payload})
    current_cursor = pre_reset_payload["cursor"]

    # Ask the live scoped endpoint to reconcile an unsupported cursor.  The
    # server owns the reset_required response; the capture never manufactures
    # one after the fact.  A fresh snapshot records the recovery boundary.
    reset_payload, recovery_payload = _capture_reset_recovery(
        stack,
        scope,
        current_cursor,
        pre_reset_payload,
    )
    events.append({"type": "sync", **reset_payload})
    events.append({"type": "snapshot", **recovery_payload})
    return events


# ---------------------------------------------------------------------------
# Scenario injection helpers
# ---------------------------------------------------------------------------


def inject_scenario(mock_llm_url: str, scenario_id: str) -> None:
    """Set the active scenario on the mock-llm server."""
    resp = httpx.post(
        f"{mock_llm_url}/__mock/active",
        json={"id": scenario_id},
        timeout=5.0,
    )
    resp.raise_for_status()


def clear_active_scenario(mock_llm_url: str) -> None:
    """Clear the active scenario override."""
    with contextlib.suppress(Exception):
        httpx.post(
            f"{mock_llm_url}/__mock/active",
            json={"id": None},
            timeout=5.0,
        )


import contextlib  # noqa: E402 (needed after the function definition)
