"""Integration tests: graceful shutdown and session recovery.

These tests exercise:
- SIGTERM → clean exit with task re-queuing
- Crash (SIGKILL) → restart → stale session recovery
- Session recovery emits SESSION_RECOVERED events

Uses the live_stack infrastructure to manage Cognis as a subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from cognis.bootstrap import ensure_data_dir, ensure_jwt_keypair, ensure_secrets_key
from cognis.providers.auth.jwt import JWTAuthProvider
from tests.integration.conftest import (
    LiveStack,
    _bootstrap_config,
    _reserve_ports,
    _start_service,
    _stop_service,
    _wait_cognis_ready,
    _wait_healthy,
)


def snapshot(path: Path, phase: str) -> dict:
    """Observe persisted identities only, never prompts or credentials."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        sessions = [
            dict(row) for row in db.execute("SELECT session_id,status,updated_at FROM sessions")
        ]
        requests = [
            dict(row)
            for row in db.execute(
                "SELECT request_id,turn_id,session_id,status,fencing_token,"
                "owner_incarnation_id,attempt_count,terminal_at FROM direct_turn_requests"
            )
        ]
    return {"phase": phase, "sessions": sessions, "requests": requests}


def _observed_controller() -> None:
    """Test subprocess entrypoint: observe real commits without changing them."""
    from cognis.core.direct_turn_runtime import DurableDirectTurnRuntime
    from cognis.core.session import SessionManager
    from cognis.main import app
    from cognis.store.direct_turns import DirectTurnStore

    directory = Path(os.environ["COGNIS_DATA_DIR"])

    def record(phase: str) -> None:
        with (directory / "recovery-trace.jsonl").open("a") as output:
            output.write(json.dumps(snapshot(directory / "cognis.db", phase)) + "\n")

    original_recover = SessionManager.recover_stale_sessions
    original_execute = DurableDirectTurnRuntime._execute
    original_terminal = DirectTurnStore.mark_terminal

    async def terminal(self, *args, **kwargs):
        result = await original_terminal(self, *args, **kwargs)
        if result is not None:
            record("terminal-persisted")
        return result

    async def recover(self, *args, **kwargs):
        record("before-stale-recovery")
        result = await original_recover(self, *args, **kwargs)
        record("after-stale-commit")
        return result

    async def execute(self, *args, **kwargs):
        record("claimed-before-execution")
        try:
            return await original_execute(self, *args, **kwargs)
        finally:
            record("after-execution")

    SessionManager.recover_stale_sessions = recover
    DurableDirectTurnRuntime._execute = execute
    DirectTurnStore.mark_terminal = terminal
    app()


@pytest.mark.integration
@pytest.mark.live_server
def test_graceful_shutdown_completes_without_error(live_stack: LiveStack) -> None:
    """Verify SIGTERM triggers a clean shutdown without crash.

    Sends SIGTERM to the Cognis process, waits for exit, verifies exit code 0.
    Note: live_stack teardown handles restarting for subsequent tests.
    """
    live = live_stack
    cognis_proc = live.cognis_process

    # Verify Cognis is healthy
    health = live.get("/api/health")
    assert health.status_code == 200

    # Send SIGTERM to the service process group. The command is launched
    # through uv, so signalling only the parent wrapper can leave the server
    # child alive and poison subsequent live_stack tests.
    os.killpg(cognis_proc.pid, signal.SIGTERM)

    # Wait for clean exit
    try:
        exit_code = cognis_proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        cognis_proc.kill()
        pytest.fail("Cognis did not shut down within 15 seconds after SIGTERM")

    # Exit code 0 means clean shutdown
    # uvicorn may return 0 or a small signal-based exit code
    assert exit_code is not None, "Process did not terminate"

    live.cognis_process = _start_service(
        live.cognis_command,
        live.cognis_env,
        label="cognis",
        clean_env=live.clean_env,
    )
    _wait_healthy(f"{live.cognis_url}/api", timeout=120)


@pytest.fixture
def recovery_provider(tmp_path):
    """Two request-entry barriers and an explicit response-release barrier."""
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    counter = [0]
    lock = threading.Lock()
    aborted = threading.Event()
    phase = ["initial"]
    trace = tmp_path / "provider-events.jsonl"

    def record(event, **fields):
        with lock, trace.open("a") as output:
            output.write(
                json.dumps({"time": time.monotonic(), "phase": phase[0], "event": event, **fields})
                + "\n"
            )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            companion_request = self.path.startswith("/companion/")
            endpoint = self.path.removeprefix("/companion")
            if endpoint not in ("/v1/chat/completions", "/v1/responses"):
                record("unexpected-endpoint", endpoint=endpoint)
                self.send_error(400)
                return
            inputs = body.get("messages", body.get("input", []))
            users = [
                item for item in inputs if isinstance(item, dict) and item.get("role") == "user"
            ]
            is_turn = not companion_request and bool(
                users and "Hello, test recovery." in json.dumps(users[-1])
            )
            request_id = f"provider-{time.monotonic_ns()}"
            record("request", endpoint=endpoint, request_id=request_id, turn=is_turn)
            with lock:
                index = 0 if phase[0] == "initial" else 1
                counter[0] += int(is_turn)
            if is_turn:
                entered[index].set()
                if not release.wait(120) or aborted.is_set():
                    record("aborted", endpoint=endpoint, request_id=request_id)
                    self.close_connection = True
                    return
            response = {
                "id": "recovery-completion",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Recovered."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            if endpoint == "/v1/responses":
                response = {
                    "id": request_id,
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": body["model"],
                    "output": [
                        {
                            "id": f"msg-{request_id}",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "Recovered.", "annotations": []}
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                if body.get("stream"):
                    events = [
                        {
                            "type": "response.created",
                            "response": {**response, "status": "in_progress", "output": []},
                        },
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                **response["output"][0],
                                "status": "in_progress",
                                "content": [],
                            },
                        },
                        {
                            "type": "response.content_part.added",
                            "item_id": f"msg-{request_id}",
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                        {
                            "type": "response.output_text.delta",
                            "item_id": f"msg-{request_id}",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": "Recovered.",
                        },
                        {
                            "type": "response.output_text.done",
                            "item_id": f"msg-{request_id}",
                            "output_index": 0,
                            "content_index": 0,
                            "text": "Recovered.",
                        },
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": response["output"][0],
                        },
                        {"type": "response.completed", "response": response},
                    ]
                    payload = "".join(
                        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': i})}\n\n"
                        for i, event in enumerate(events)
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(response).encode()
                    content_type = "application/json"
            elif body.get("stream"):
                response["object"] = "chat.completion.chunk"
                response["choices"][0]["delta"] = response["choices"][0].pop("message")
                response["choices"][0]["finish_reason"] = None
                final = {
                    **response,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                payload = f"data: {json.dumps(response)}\n\ndata: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode()
                content_type = "text/event-stream"
            else:
                payload = json.dumps(response).encode()
                content_type = "application/json"
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                record("response", endpoint=endpoint, request_id=request_id, turn=is_turn)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The first controller is deliberately killed.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", entered, release, phase, aborted, record
    finally:
        aborted.set()
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.integration
@pytest.mark.parametrize("contract", ["stale-only", "durable-resume"])
def test_stale_session_recovery_on_restart(
    tmp_path_factory: pytest.TempPathFactory,
    recovery_provider,
    contract: str,
    request: pytest.FixtureRequest,
) -> None:
    """Full crash/restart/recovery cycle:

    1. Start a fresh Cognis + companions
    2. Create a conversation and chat (establishes active session)
    3. Kill Cognis with SIGKILL (simulate crash)
    4. Restart Cognis
    5. Verify the session is recovered (marked idle, stale sessions detected)
    """
    clean_env = dict(os.environ)

    base_dir = tmp_path_factory.mktemp("recovery")
    cognis_dir = base_dir / "cognis"
    mnemory_dir = base_dir / "mnemory"
    intaris_dir = base_dir / "intaris"
    cognis_dir.mkdir()
    mnemory_dir.mkdir()
    intaris_dir.mkdir()

    reservations = _reserve_ports(4)
    request.addfinalizer(lambda: [sock.close() for sock in reservations])
    cognis_port, mnemory_port, intaris_port, metrics_port = [
        sock.getsockname()[1] for sock in reservations
    ]
    assert len({cognis_port, mnemory_port, intaris_port, metrics_port}) == 4
    cognis_url = f"http://127.0.0.1:{cognis_port}"
    mnemory_url = f"http://127.0.0.1:{mnemory_port}"
    intaris_url = f"http://127.0.0.1:{intaris_port}"

    admin_email = "admin@recovery-test.example.com"
    admin_password = "recovery-test-password-789"
    (
        provider_url,
        provider_entered,
        provider_release,
        provider_phase,
        provider_aborted,
        provider_record,
    ) = recovery_provider
    llm_model = "openai/gpt-4.1-nano"

    # Bootstrap keys
    bootstrap_config = _bootstrap_config(
        cognis_dir=cognis_dir,
        host="127.0.0.1",
        port=cognis_port,
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        admin_email=admin_email,
        admin_password=admin_password,
    )
    ensure_data_dir(bootstrap_config)
    ensure_jwt_keypair(bootstrap_config)
    ensure_secrets_key(bootstrap_config)
    public_key_path = str(cognis_dir / "keys" / "public.pem")

    uvx_path = shutil.which("uvx")
    uv_path = shutil.which("uv")
    if uvx_path is None or uv_path is None:
        pytest.skip("uvx/uv not found on PATH")

    # Start Mnemory + Intaris
    reservations[1].close()
    mnemory_proc = _start_service(
        [uvx_path, "mnemory"],
        {
            "DATA_DIR": str(mnemory_dir),
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(mnemory_port),
            "MNEMORY_JWT_PUBLIC_KEY": public_key_path,
            "LLM_API_KEY": "test-api-key",
            "OPENAI_API_KEY": "test-api-key",
            "LOG_LEVEL": "warning",
        },
        label="mnemory",
        clean_env=clean_env,
    )
    request.addfinalizer(lambda: _stop_service(mnemory_proc, "mnemory"))
    reservations[2].close()
    reservations[3].close()
    intaris_env = {
        "DATA_DIR": str(intaris_dir),
        "INTARIS_HOST": "127.0.0.1",
        "INTARIS_PORT": str(intaris_port),
        "METRICS_HOST": "127.0.0.1",
        "METRICS_PORT": str(metrics_port),
        "INTARIS_JWT_PUBLIC_KEY": public_key_path,
        "LLM_API_KEY": "test-api-key",
        "OPENAI_API_KEY": "test-api-key",
        # Companion inference must stay local without entering Cognis's barriers.
        "LLM_BASE_URL": provider_url.removesuffix("/v1") + "/companion/v1",
        "ANALYSIS_ENABLED": "false",
        "LOG_LEVEL": "warning",
    }
    intaris_proc = _start_service(
        [uvx_path, "intaris"],
        intaris_env,
        label="intaris",
        clean_env=clean_env,
    )
    request.addfinalizer(lambda: _stop_service(intaris_proc, "intaris"))

    cognis_env = {
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
        "DATA_DIR": str(cognis_dir),
        "DATABASE_URL": f"sqlite+aiosqlite:///{cognis_dir / 'cognis.db'}",
    }

    try:
        _wait_healthy(mnemory_url, timeout=120)
        _wait_healthy(intaris_url, timeout=120)
    except RuntimeError:
        _stop_service(mnemory_proc, "mnemory")
        _stop_service(intaris_proc, "intaris")
        raise

    # Start Cognis (first time)
    reservations[0].close()
    cognis_proc = _start_service(
        [uv_path, "run", "python", "-m", "tests.integration.test_shutdown_recovery", "serve"],
        cognis_env,
        label="cognis-first",
        clean_env=clean_env,
    )
    request.addfinalizer(lambda: _stop_service(cognis_proc, "cognis"))

    http = httpx.Client(timeout=30.0, trust_env=False)

    try:
        _wait_cognis_ready(cognis_proc, cognis_port, cognis_dir, admin_email)

        # Login
        login = http.post(
            f"{cognis_url}/api/auth/login",
            json={"email": admin_email, "password": admin_password, "mode": "native"},
        )
        assert login.status_code == 200
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Seed LLM provider
        http.post(
            f"{cognis_url}/api/v1/llm-providers",
            headers=headers,
            json={
                "provider_id": "default",
                "display_name": "OpenAI (recovery test)",
                "location": "controller",
                "backend": "litellm",
                "config": {
                    "scope": "system",
                    "default_model": llm_model,
                    "api_base": provider_url,
                    "api_key": "recovery-test-key",
                },
            },
        )
        http.put(
            f"{cognis_url}/api/v1/model-routing",
            headers=headers,
            json={"default": {"model": llm_model, "reasoning_effort": None}},
        )

        # Declare capacity for the controlled provider: it has no discovery
        # endpoint or remote model metadata. Prompt-pressure recovery must not
        # preempt this test's independently controlled crash/restart boundary.
        providers = http.get(f"{cognis_url}/api/v1/llm-providers", headers=headers)
        providers.raise_for_status()
        provider = next(
            item for item in providers.json()["items"] if item["provider_id"] == "default"
        )
        provider_config = dict(provider["config"])
        provider_config["models"] = [
            {
                "model_id": llm_model,
                "context_window": 128_000,
                "max_output_tokens": 4096,
            }
        ]
        configured = http.put(
            f"{cognis_url}/api/v1/llm-providers/{provider['provider_id']}",
            headers=headers,
            json={"config": provider_config},
        )
        configured.raise_for_status()

        # Create agent and conversation
        agent_resp = http.post(
            f"{cognis_url}/api/v1/agents",
            headers=headers,
            json={
                "agent_id": "recovery-agent",
                "name": "Recovery Agent",
                "display_name": "Recovery Agent",
                "description": "Recovery test agent",
                "system_prompt": "You are a test assistant. Keep responses brief.",
                "execution": {"executor_id": "default_inprocess"},
                "personality": {
                    "tone": "concise",
                    "temperament": "cooperative",
                    "purpose": "testing",
                },
                "permissions": {"tool_permissions": {"*": "allow"}, "can_delegate": True},
            },
        )
        assert agent_resp.status_code == 200
        agent_id = agent_resp.json()["agent_id"]
        http.post(f"{cognis_url}/api/v1/agents/recovery-agent/activate", headers=headers)

        conv_resp = http.post(
            f"{cognis_url}/api/v1/conversations",
            headers=headers,
            json={
                "agent_id": "recovery-agent",
                "title": "Recovery test",
                "context": {"type": "test", "ref": None, "platform_data": {}, "memory_labels": {}},
            },
        )
        assert conv_resp.status_code == 200
        cid = conv_resp.json()["conversation_id"]

        # Chat v2 HTTP admission creates the active session. WebSocket is
        # subscription-only and is not part of the mutation contract.
        admission = http.put(
            f"{cognis_url}/api/v1/chat/v2/conversations/{cid}/messages/recovery-crash-turn",
            headers=headers,
            json={
                "client_message_id": "recovery-crash-message",
                "content": "Hello, test recovery.",
                "attachments": [],
            },
        )
        assert admission.status_code == 202
        assert provider_entered[0].wait(30), "Admitted turn never reached controlled provider"
        before = snapshot(cognis_dir / "cognis.db", "before-crash")
        assert len(before["requests"]) == 1
        original_request = before["requests"][0]
        assert original_request["status"] == "running"

        # Verify session exists
        sessions_resp = http.get(
            f"{cognis_url}/api/v1/conversations/{cid}/sessions", headers=headers
        )
        assert sessions_resp.status_code == 200
        sessions = sessions_resp.json()
        assert len(sessions) >= 1

        # KILL Cognis (simulate crash — no graceful shutdown)
        os.killpg(cognis_proc.pid, signal.SIGKILL)
        cognis_proc.wait(timeout=5)

        # Age the crashed session directly. The stale threshold is an internal
        # recovery setting and is intentionally unavailable through public API.
        with sqlite3.connect(cognis_dir / "cognis.db") as database:
            target_session = original_request["session_id"]
            if contract == "stale-only":
                # Seed an orphan session, retaining the unrelated admitted turn.
                # No request references this new identity.
                database.row_factory = sqlite3.Row
                original = dict(
                    database.execute(
                        "SELECT * FROM sessions WHERE session_id = ?", (target_session,)
                    ).fetchone()
                )
                target_session += "-orphan"
                original.update(
                    session_id=target_session,
                    activity_scope_id=target_session,
                    intaris_session_id=None,
                    mnemory_session_id=None,
                )
                columns = ",".join(f'"{name}"' for name in original)
                placeholders = ",".join("?" for _ in original)
                database.execute(
                    f"INSERT INTO sessions ({columns}) VALUES ({placeholders})",
                    tuple(original.values()),
                )
                assert (
                    database.execute(
                        "SELECT count(*) FROM direct_turn_requests WHERE session_id = ?",
                        (target_session,),
                    ).fetchone()[0]
                    == 0
                )
            database.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                ("2000-01-01 00:00:00.000000", target_session),
            )
            # Simulate downtime beyond the original lease, without lengthening
            # the test wait or changing the runtime's TTL/recovery policy.
            database.execute(
                "UPDATE coordination_leases SET lease_expires_at = ? WHERE resource_key = ?",
                ("2000-01-01 00:00:00.000000", f"direct-turn:conversation:{cid}"),
            )
            # The same simulated downtime must expire the crashed provider's
            # pending binding reservation. Otherwise the successor correctly
            # waits for that independent lease despite owning the direct turn.
            expired = database.execute(
                "UPDATE inference_bindings SET claim_until = ? "
                "WHERE owner_email = ? AND claim_token IS NOT NULL",
                ("2000-01-01 00:00:00.000000", admin_email),
            )
            assert expired.rowcount == 1
            database.commit()
        aged = snapshot(cognis_dir / "cognis.db", "after-aging")
        (base_dir / "crash-snapshots.json").write_text(json.dumps([before, aged], indent=2))

        # Restart Cognis (same data dir, so it has the old DB)
        provider_phase[0] = "resumed"
        cognis_proc = _start_service(
            [uv_path, "run", "python", "-m", "tests.integration.test_shutdown_recovery", "serve"],
            cognis_env,
            label="cognis-restarted",
            clean_env=clean_env,
        )
        _wait_cognis_ready(cognis_proc, cognis_port, cognis_dir, admin_email)

        # Re-login (tokens are still valid since keys are the same)
        login2 = http.post(
            f"{cognis_url}/api/auth/login",
            json={"email": admin_email, "password": admin_password, "mode": "native"},
        )
        assert login2.status_code == 200
        token2 = login2.json()["token"]
        headers2 = {"Authorization": f"Bearer {token2}"}

        # Verify sessions are recovered (marked idle)
        sessions_resp2 = http.get(
            f"{cognis_url}/api/v1/conversations/{cid}/sessions",
            headers=headers2,
        )
        assert sessions_resp2.status_code == 200
        sessions2 = sessions_resp2.json()

        trace = [
            json.loads(line)
            for line in (cognis_dir / "recovery-trace.jsonl").read_text().splitlines()
        ]
        recovered = [row for row in trace if row["phase"] == "after-stale-commit"][-1]
        target = next(row for row in recovered["sessions"] if row["session_id"] == target_session)
        assert target["status"] == "idle", recovered
        if contract == "stale-only":
            assert (
                next(s for s in sessions2 if s["session_id"] == target_session)["status"] == "idle"
            )
        else:
            # The reclaim scheduler has its own 30-second cadence. Observe its
            # persisted ownership transition before timing provider progress.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                claimed = snapshot(cognis_dir / "cognis.db", "waiting-for-claim")
                row = claimed["requests"][0]
                if (
                    row["owner_incarnation_id"] != original_request["owner_incarnation_id"]
                    and row["fencing_token"] > original_request["fencing_token"]
                ):
                    break
                threading.Event().wait(0.05)
            assert row["owner_incarnation_id"] != original_request["owner_incarnation_id"], claimed
            assert row["fencing_token"] > original_request["fencing_token"], claimed
            provider_record(
                "successor-claim",
                request_id=row["request_id"],
                turn_id=row["turn_id"],
                session_id=row["session_id"],
            )
            assert provider_entered[1].wait(30), "Durable turn did not resume at provider"
            resumed = snapshot(cognis_dir / "cognis.db", "resumed")
            request = resumed["requests"][0]
            for key in ("request_id", "turn_id", "session_id"):
                assert request[key] == original_request[key]
            assert request["fencing_token"] > original_request["fencing_token"]
            assert request["owner_incarnation_id"] != original_request["owner_incarnation_id"]
            provider_record("test-release")
            provider_release.set()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                terminal = snapshot(cognis_dir / "cognis.db", "terminal")
                # The callback records its trace after the completion transaction
                # commits. Observe both boundaries within the same existing budget.
                terminal_trace = [
                    json.loads(line)
                    for line in (cognis_dir / "recovery-trace.jsonl")
                    .read_text()
                    .splitlines(keepends=True)
                    if line.endswith("\n")
                ]
                if terminal["requests"][0]["status"] == "completed" and any(
                    row["phase"] == "terminal-persisted" for row in terminal_trace
                ):
                    break
                threading.Event().wait(0.05)
            assert terminal["requests"][0]["status"] == "completed", terminal
            assert len(terminal["requests"]) == 1
            assert len([row for row in terminal_trace if row["phase"] == "terminal-persisted"]) == 1
            (base_dir / "crash-snapshots.json").write_text(
                json.dumps([before, aged, recovered, resumed, terminal], indent=2)
            )

            # Flush Cognis, then restart the durable owner before reading through
            # its API. Intaris may compress its segments; filenames are not a
            # cross-service contract, and an in-memory read would not prove restart.
            _stop_service(cognis_proc, "cognis")
            _stop_service(intaris_proc, "intaris")
            intaris_proc = _start_service(
                [uvx_path, "intaris"],
                intaris_env,
                label="intaris-restarted",
                clean_env=clean_env,
            )
            _wait_healthy(intaris_url, timeout=120)
            auth = JWTAuthProvider(
                bootstrap_config.jwt_private_key_path,
                bootstrap_config.jwt_public_key_path,
            )
            with httpx.Client(base_url=intaris_url, timeout=30, trust_env=False) as canonical:
                response = canonical.get(
                    f"/api/v1/session/{original_request['session_id']}/events",
                    headers={
                        "Authorization": (
                            f"Bearer {auth.sign_service_jwt(admin_email, agent_id, ['intaris'])}"
                        ),
                        "X-Agent-Id": agent_id,
                    },
                    params={"after_seq": 0, "limit": 1000},
                )
                response.raise_for_status()
                result = response.json()
                assert result["has_more"] is False
                assert result["history_gap"] is None
                assert result["first_available_seq"] == 1
                canonical_events = result["events"]
            user_appends = [
                event
                for event in canonical_events
                if event.get("type") == "user_message"
                and event.get("data", {}).get("turn_id") == original_request["turn_id"]
            ]
            assert len(user_appends) == 1
            assert not [
                event
                for event in canonical_events
                if event.get("type") == "lifecycle"
                and event.get("data", {}).get("event") == "turn_error"
            ]

    finally:
        for reservation in reservations:
            reservation.close()
        provider_record("teardown-abort")
        provider_aborted.set()
        provider_release.set()
        http.close()
        _stop_service(cognis_proc, "cognis")
        _stop_service(mnemory_proc, "mnemory")
        _stop_service(intaris_proc, "intaris")


if __name__ == "__main__":
    _observed_controller()
