"""Integration test fixtures — auto-starts Mnemory + Intaris via uvx.

All three services use isolated temp directories under pytest's
tmp_path_factory. Cognis runs in-process via TestClient; Mnemory and
Intaris run as uvx subprocesses pointed at the Cognis-generated JWT key.
"""

from __future__ import annotations

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
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.bootstrap import ensure_data_dir, ensure_jwt_keypair, ensure_secrets_key
from cognis.config import CognisConfig


def _free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(url: str, *, timeout: float = 120.0, interval: float = 1.0) -> None:
    """Poll a /health endpoint until it responds (any HTTP status).

    Services with JWT auth may return 401 on /health even when healthy.
    We consider any HTTP response (including 401) as "service is up".
    Connection refused means the service hasn't started yet.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/health", timeout=5.0)
            # Any HTTP response means the service is listening
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


@dataclass
class IntegrationStack:
    """Running integration stack with all three services."""

    client: TestClient
    admin_email: str
    admin_password: str
    admin_token: str
    cognis_data_dir: Path
    mnemory_url: str
    intaris_url: str
    mnemory_process: subprocess.Popen[bytes]
    intaris_process: subprocess.Popen[bytes]

    def admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}


def _start_service(
    command: list[str],
    env: dict[str, str],
    *,
    label: str,
    clean_env: dict[str, str],
) -> subprocess.Popen[bytes]:
    """Start a service subprocess with a clean env + extra vars."""
    base = dict(clean_env)
    # Remove vars that could interfere with child services
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
        "COGNIS_CORS_ORIGINS",
    ):
        base.pop(key, None)
    base.update(env)
    log_file = Path(env.get("DATA_DIR", "/tmp")) / f"{label}.log"
    log_handle = open(log_file, "w")  # noqa: SIM115
    process = subprocess.Popen(
        command,
        env=base,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    process._log_handle = log_handle  # type: ignore[attr-defined]
    return process


def _stop_service(process: subprocess.Popen[bytes], label: str) -> None:
    """Gracefully stop a subprocess."""
    log_handle = getattr(process, "_log_handle", None)
    if process.poll() is not None:
        if log_handle:
            log_handle.close()
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if log_handle:
        log_handle.close()


def _dump_logs(
    services: list[tuple[str, subprocess.Popen[bytes], Path]],
) -> None:
    """Print subprocess log files for debugging."""
    for label, proc, data_dir in services:
        log_path = data_dir / f"{label}.log"
        poll = proc.poll()
        print(f"\n=== {label} (pid={proc.pid}, poll={poll}) ===")
        if log_path.exists():
            print(log_path.read_text()[:4096])


@pytest.fixture(scope="session")
def integration_stack(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[IntegrationStack]:
    """Session-scoped fixture that boots the full Cognis + Mnemory + Intaris stack.

    Order:
    1. Generate keys in the Cognis data dir (no app yet)
    2. Start Mnemory + Intaris subprocesses (they validate JWT key at startup)
    3. Wait for both services to become healthy
    4. Start Cognis in-process via TestClient
    5. Seed LLM provider/routing and get admin token
    """
    clean_env = dict(os.environ)

    base_dir = tmp_path_factory.mktemp("integration")
    cognis_dir = base_dir / "cognis"
    mnemory_dir = base_dir / "mnemory"
    intaris_dir = base_dir / "intaris"
    cognis_dir.mkdir()
    mnemory_dir.mkdir()
    intaris_dir.mkdir()

    mnemory_port = _free_port()
    intaris_port = _free_port()
    mnemory_url = f"http://127.0.0.1:{mnemory_port}"
    intaris_url = f"http://127.0.0.1:{intaris_port}"

    admin_email = "admin@cognis-integration-test.example.com"
    admin_password = "integration-test-password-123"
    llm_model = clean_env.get("COGNIS_TEST_LLM_MODEL", "gpt-4.1-nano")

    # Step 1: Bootstrap keys BEFORE starting anything
    bootstrap_config = CognisConfig(
        data_dir=cognis_dir,
        host="127.0.0.1",
        port=0,
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        database_url=f"sqlite+aiosqlite:///{cognis_dir / 'cognis.db'}",
        jwt_private_key_path=cognis_dir / "keys" / "private.pem",
        jwt_public_key_path=cognis_dir / "keys" / "public.pem",
        secrets_key_path=cognis_dir / "secrets.key",
        log_level="warning",
        log_format="text",
        cors_origins=["*"],
        initial_admin_email=admin_email,
        initial_admin_password=admin_password,
    )
    ensure_data_dir(bootstrap_config)
    ensure_jwt_keypair(bootstrap_config)
    ensure_secrets_key(bootstrap_config)

    public_key_path = str(cognis_dir / "keys" / "public.pem")
    assert Path(public_key_path).exists(), f"JWT public key not found at {public_key_path}"

    # Find uvx
    uvx_path = shutil.which("uvx")
    if uvx_path is None:
        pytest.skip("uvx not found on PATH")

    # Step 2: Start Mnemory + Intaris BEFORE Cognis (no event loop conflict)
    mnemory_proc = _start_service(
        [uvx_path, "mnemory"],
        {
            "DATA_DIR": str(mnemory_dir),
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(mnemory_port),
            "MNEMORY_JWT_PUBLIC_KEY": public_key_path,
            "LOG_LEVEL": "warning",
        },
        label="mnemory",
        clean_env=clean_env,
    )

    intaris_proc = _start_service(
        [uvx_path, "intaris"],
        {
            "DATA_DIR": str(intaris_dir),
            "INTARIS_HOST": "127.0.0.1",
            "INTARIS_PORT": str(intaris_port),
            "INTARIS_JWT_PUBLIC_KEY": public_key_path,
            "LOG_LEVEL": "warning",
        },
        label="intaris",
        clean_env=clean_env,
    )

    # Step 3: Wait for health
    try:
        _wait_healthy(mnemory_url, timeout=120)
        _wait_healthy(intaris_url, timeout=120)
    except RuntimeError:
        _dump_logs(
            [
                ("mnemory", mnemory_proc, mnemory_dir),
                ("intaris", intaris_proc, intaris_dir),
            ]
        )
        _stop_service(mnemory_proc, "mnemory")
        _stop_service(intaris_proc, "intaris")
        raise

    # Step 4: Now start Cognis in-process
    os.environ["COGNIS_DATA_DIR"] = str(cognis_dir)
    os.environ["COGNIS_HOST"] = "127.0.0.1"
    os.environ["COGNIS_PORT"] = "0"
    os.environ["COGNIS_MNEMORY_URL"] = mnemory_url
    os.environ["COGNIS_INTARIS_URL"] = intaris_url
    os.environ["COGNIS_INITIAL_ADMIN_EMAIL"] = admin_email
    os.environ["COGNIS_INITIAL_ADMIN_PASSWORD"] = admin_password
    os.environ["COGNIS_LOG_FORMAT"] = "text"
    os.environ["COGNIS_LOG_LEVEL"] = "warning"
    os.environ["COGNIS_CORS_ORIGINS"] = "*"

    app = create_app()
    client = TestClient(app)
    client.__enter__()

    # Get admin token first
    login_response = client.post(
        "/api/auth/login",
        json={"email": admin_email, "password": admin_password},
    )
    assert login_response.status_code == 200, f"Admin login failed: {login_response.text}"
    admin_token = login_response.json()["token"]
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # Step 5: Seed LLM provider and model routing via REST API
    provider_response = client.post(
        "/api/v1/llm-providers",
        headers=auth_headers,
        json={
            "provider_id": "default",
            "display_name": "OpenAI (test)",
            "location": "controller",
            "backend": "litellm",
            "config": {"default_model": llm_model},
        },
    )
    assert provider_response.status_code == 200, (
        f"LLM provider creation failed: {provider_response.text}"
    )

    routing_response = client.put(
        "/api/v1/model-routing",
        headers=auth_headers,
        json={"default": llm_model},
    )
    assert routing_response.status_code == 200, (
        f"Model routing update failed: {routing_response.text}"
    )

    stack = IntegrationStack(
        client=client,
        admin_email=admin_email,
        admin_password=admin_password,
        admin_token=admin_token,
        cognis_data_dir=cognis_dir,
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        mnemory_process=mnemory_proc,
        intaris_process=intaris_proc,
    )

    yield stack

    # Teardown
    _stop_service(mnemory_proc, "mnemory")
    _stop_service(intaris_proc, "intaris")
    client.__exit__(None, None, None)

    for key in (
        "COGNIS_DATA_DIR",
        "COGNIS_HOST",
        "COGNIS_PORT",
        "COGNIS_MNEMORY_URL",
        "COGNIS_INTARIS_URL",
        "COGNIS_INITIAL_ADMIN_EMAIL",
        "COGNIS_INITIAL_ADMIN_PASSWORD",
        "COGNIS_LOG_FORMAT",
        "COGNIS_LOG_LEVEL",
        "COGNIS_CORS_ORIGINS",
    ):
        os.environ.pop(key, None)


@pytest.fixture
def stack(integration_stack: IntegrationStack) -> IntegrationStack:
    """Shorter alias for integration_stack."""
    return integration_stack


@pytest.fixture
def run_id() -> str:
    """Unique ID for this test run to isolate data."""
    import uuid

    return uuid.uuid4().hex[:8]


@pytest.fixture
def agent_id(run_id: str) -> str:
    """Unique agent ID for this test."""
    return f"test-agent-{run_id}"


def create_test_agent(
    stack: IntegrationStack,
    agent_id: str,
    *,
    system_prompt: str = "You are a helpful test assistant. Keep responses brief.",
) -> dict[str, Any]:
    """Create an active agent via the API and return the response body."""
    response = stack.client.post(
        "/api/v1/agents",
        headers=stack.admin_headers(),
        json={
            "agent_id": agent_id,
            "name": f"Test Agent {agent_id}",
            "display_name": "Test Agent",
            "description": "Integration test agent",
            "system_prompt": system_prompt,
            "personality": {
                "tone": "concise",
                "temperament": "cooperative",
                "purpose": "integration testing",
            },
            "permissions": {"tool_permissions": {"*": "allow"}, "can_delegate": True},
        },
    )
    assert response.status_code == 200, f"Agent creation failed: {response.text}"
    activate_response = stack.client.post(
        f"/api/v1/agents/{agent_id}/activate",
        headers=stack.admin_headers(),
    )
    assert activate_response.status_code == 200, (
        f"Agent activation failed: {activate_response.text}"
    )
    return activate_response.json()


def create_test_conversation(
    stack: IntegrationStack,
    agent_id: str,
) -> dict[str, Any]:
    """Create a conversation via the API and return the response body."""
    response = stack.client.post(
        "/api/v1/conversations",
        headers=stack.admin_headers(),
        json={
            "agent_id": agent_id,
            "title": "Integration test conversation",
            "context": {
                "type": "test",
                "ref": None,
                "platform_data": {},
                "memory_labels": {"test": "true"},
            },
        },
    )
    assert response.status_code == 200, f"Conversation creation failed: {response.text}"
    return response.json()
