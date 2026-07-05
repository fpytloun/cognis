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
from typing import Any

import httpx
import pytest

from cognis.bootstrap import ensure_data_dir, ensure_jwt_keypair, ensure_secrets_key
from cognis.config import load_config

E2E_AGENT_ID = "e2e-test-agent"
E2E_PROVIDER_ID = "e2e-mock-llm"
SCENARIOS_DIR = Path(__file__).parent / "scenarios"
GOLDEN_DIR = Path(__file__).parent / "golden"


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
            "enabled_tools": [],
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
                "builtin_tools": ["memory_search", "memory_add"],
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

    _append_adversarial_refresh(events)
    return events


def _append_adversarial_refresh(events: list[dict[str, Any]]) -> None:
    """Append a synthetic ``conversation_view_refresh`` modelling the disappear bug.

    In production a refresh (reloadConversationSubloads → replaceAll) can land in
    the window after a turn produced an assistant message but before that event
    is durably queryable from Intaris, so the projection omits it. To reproduce
    that deterministically in the golden replay, we build a refresh projection
    from the full set of items seen during the turn MINUS the final assistant
    message, and emit it as ``conversation_view_refresh``.

    The golden replay routes this through ``ChatTimeline.replaceAll`` and
    ``INV-REFRESH-NO-DROP`` asserts the omitted assistant message is NOT evicted
    (the symptom-1 guard preserves unconfirmed-live items).
    """
    # Collect the latest projected item per id across all timeline_patch /
    # runtime snapshot events (the union the client would have on screen).
    latest: dict[str, dict[str, Any]] = {}
    final_assistant_id: str | None = None
    for event in events:
        items: list[dict[str, Any]] = []
        if event.get("type") == "timeline_patch":
            items = event.get("items", []) or []
        elif event.get("type") == "conversation_runtime_snapshot":
            items = event.get("timeline_items", []) or []
        for item in items:
            item_id = item.get("id")
            if not isinstance(item_id, str):
                continue
            latest[item_id] = item
            if item.get("kind") == "message" and item.get("role") == "assistant":
                final_assistant_id = item_id

    if not latest or final_assistant_id is None:
        return

    # Build the adversarial projection: everything EXCEPT the final assistant
    # message (simulating the refresh-before-persist gap).
    projection = [item for item_id, item in latest.items() if item_id != final_assistant_id]

    events.append(
        {
            "type": "conversation_view_refresh",
            "timeline_items": projection,
            "_synthetic": True,
            "_omitted_id": final_assistant_id,
        }
    )


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
