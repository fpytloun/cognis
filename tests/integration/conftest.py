"""Integration test fixtures — auto-starts Mnemory + Intaris via uvx.

All three services use isolated temp directories under pytest's
tmp_path_factory. Cognis runs in-process via TestClient; Mnemory and
Intaris run as uvx subprocesses pointed at the Cognis-generated JWT key.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.bootstrap import ensure_data_dir, ensure_jwt_keypair, ensure_secrets_key
from cognis.config import CognisConfig, load_config


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


def _bootstrap_config(
    *,
    cognis_dir: Path,
    host: str,
    port: int,
    mnemory_url: str,
    intaris_url: str,
    admin_email: str,
    admin_password: str,
) -> CognisConfig:
    """Build a complete test CognisConfig for bootstrap-only helpers."""

    return replace(
        load_config(),
        data_dir=cognis_dir,
        host=host,
        port=port,
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        public_mnemory_ui_url=mnemory_url,
        public_intaris_ui_url=intaris_url,
        public_base_url=f"http://{host}:{port}" if port else "",
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


@dataclass
class LiveStack:
    """Running integration stack with all services as subprocesses.

    Uses real HTTP and WebSocket connections — no TestClient limitations.
    """

    cognis_url: str
    ws_url: str
    admin_email: str
    admin_password: str
    admin_token: str
    cognis_data_dir: Path
    mnemory_url: str
    intaris_url: str
    cognis_process: subprocess.Popen[bytes]
    mnemory_process: subprocess.Popen[bytes]
    intaris_process: subprocess.Popen[bytes]
    http: httpx.Client
    cognis_command: list[str]
    cognis_env: dict[str, str]
    clean_env: dict[str, str]

    def admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.admin_headers(), **kwargs.pop("headers", {})}
        return self.http.get(f"{self.cognis_url}{path}", headers=headers, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.admin_headers(), **kwargs.pop("headers", {})}
        return self.http.post(f"{self.cognis_url}{path}", headers=headers, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.admin_headers(), **kwargs.pop("headers", {})}
        return self.http.put(f"{self.cognis_url}{path}", headers=headers, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.admin_headers(), **kwargs.pop("headers", {})}
        return self.http.delete(f"{self.cognis_url}{path}", headers=headers, **kwargs)


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
        "COGNIS_CONTROLLER_URL",
        "COGNIS_EXECUTOR_TOKEN",
        "COGNIS_EXECUTOR_WORKSPACE",
        "COGNIS_EXECUTOR_WORKDIR",
        "COGNIS_EXECUTOR_ALLOW_INSECURE_WS",
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
        start_new_session=True,
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
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if log_handle:
            log_handle.close()
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
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
    bootstrap_config = _bootstrap_config(
        cognis_dir=cognis_dir,
        host="127.0.0.1",
        port=0,
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        admin_email=admin_email,
        admin_password=admin_password,
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
            "LLM_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
            "OPENAI_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
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
            "METRICS_HOST": "127.0.0.1",
            "METRICS_PORT": str(_free_port()),
            "INTARIS_JWT_PUBLIC_KEY": public_key_path,
            "LLM_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
            "OPENAI_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
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
        json={"email": admin_email, "password": admin_password, "mode": "native"},
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
            "config": {
                "scope": "system",
                "default_model": llm_model,
                "models": [
                    {
                        "model_id": llm_model,
                        "display_name": "Integration test model",
                        "supports_tools": True,
                        "supports_streaming": True,
                    }
                ],
            },
        },
    )
    assert provider_response.status_code == 200, (
        f"LLM provider creation failed: {provider_response.text}"
    )

    routing_response = client.put(
        "/api/v1/model-routing",
        headers=auth_headers,
        json={"default": {"model": llm_model, "reasoning_effort": None}},
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


@pytest.fixture(scope="session")
def live_stack(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[LiveStack]:
    """Session-scoped fixture: all three services as subprocesses.

    Unlike integration_stack (Cognis in-process), this starts Cognis as
    a real uvicorn subprocess so WebSocket + async agent loop works.
    """
    clean_env = dict(os.environ)

    base_dir = tmp_path_factory.mktemp("live")
    cognis_dir = base_dir / "cognis"
    mnemory_dir = base_dir / "mnemory"
    intaris_dir = base_dir / "intaris"
    cognis_dir.mkdir()
    mnemory_dir.mkdir()
    intaris_dir.mkdir()

    cognis_port = _free_port()
    mnemory_port = _free_port()
    intaris_port = _free_port()
    cognis_url = f"http://127.0.0.1:{cognis_port}"
    mnemory_url = f"http://127.0.0.1:{mnemory_port}"
    intaris_url = f"http://127.0.0.1:{intaris_port}"
    ws_url = f"ws://127.0.0.1:{cognis_port}/api/ws"

    admin_email = "admin@cognis-live-test.example.com"
    admin_password = "live-test-password-456"
    llm_model = clean_env.get("COGNIS_TEST_LLM_MODEL", "gpt-4.1-nano")

    # Bootstrap keys first
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

    # Start Mnemory
    mnemory_proc = _start_service(
        [uvx_path, "mnemory"],
        {
            "DATA_DIR": str(mnemory_dir),
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(mnemory_port),
            "MNEMORY_JWT_PUBLIC_KEY": public_key_path,
            "LLM_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
            "OPENAI_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
            "LOG_LEVEL": "warning",
        },
        label="mnemory",
        clean_env=clean_env,
    )

    # Start Intaris
    intaris_proc = _start_service(
        [uvx_path, "intaris"],
        {
            "DATA_DIR": str(intaris_dir),
            "INTARIS_HOST": "127.0.0.1",
            "INTARIS_PORT": str(intaris_port),
            "METRICS_HOST": "127.0.0.1",
            "METRICS_PORT": str(_free_port()),
            "INTARIS_JWT_PUBLIC_KEY": public_key_path,
            "LLM_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
            "OPENAI_API_KEY": clean_env.get("OPENAI_API_KEY", "test-api-key"),
            "LOG_LEVEL": "warning",
        },
        label="intaris",
        clean_env=clean_env,
    )

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

    # Start Cognis as subprocess
    cognis_command = [uv_path, "run", "cognis-controller", "serve"]
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
        "DATA_DIR": str(cognis_dir),  # for log file path in _start_service
    }
    cognis_proc = _start_service(
        cognis_command,
        cognis_env,
        label="cognis",
        clean_env=clean_env,
    )

    try:
        _wait_healthy(f"{cognis_url}/api", timeout=120)
    except RuntimeError:
        _dump_logs(
            [
                ("cognis", cognis_proc, cognis_dir),
                ("mnemory", mnemory_proc, mnemory_dir),
                ("intaris", intaris_proc, intaris_dir),
            ]
        )
        _stop_service(cognis_proc, "cognis")
        _stop_service(mnemory_proc, "mnemory")
        _stop_service(intaris_proc, "intaris")
        raise

    http_client = httpx.Client(timeout=30.0)

    # Login and get admin token
    login_response = http_client.post(
        f"{cognis_url}/api/auth/login",
        json={"email": admin_email, "password": admin_password, "mode": "native"},
    )
    assert login_response.status_code == 200, f"Admin login failed: {login_response.text}"
    admin_token = login_response.json()["token"]
    auth_headers = {"Authorization": f"Bearer {admin_token}"}

    # Seed LLM provider
    provider_response = http_client.post(
        f"{cognis_url}/api/v1/llm-providers",
        headers=auth_headers,
        json={
            "provider_id": "default",
            "display_name": "OpenAI (test)",
            "location": "controller",
            "backend": "litellm",
            "config": {"scope": "system", "default_model": llm_model},
        },
    )
    assert provider_response.status_code == 200, (
        f"LLM provider creation failed: {provider_response.text}"
    )

    routing_response = http_client.put(
        f"{cognis_url}/api/v1/model-routing",
        headers=auth_headers,
        json={"default": {"model": llm_model, "reasoning_effort": None}},
    )
    assert routing_response.status_code == 200, (
        f"Model routing update failed: {routing_response.text}"
    )

    live = LiveStack(
        cognis_url=cognis_url,
        ws_url=ws_url,
        admin_email=admin_email,
        admin_password=admin_password,
        admin_token=admin_token,
        cognis_data_dir=cognis_dir,
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        cognis_process=cognis_proc,
        mnemory_process=mnemory_proc,
        intaris_process=intaris_proc,
        http=http_client,
        cognis_command=cognis_command,
        cognis_env=cognis_env,
        clean_env=clean_env,
    )

    yield live

    # Teardown
    http_client.close()
    _stop_service(cognis_proc, "cognis")
    _stop_service(mnemory_proc, "mnemory")
    _stop_service(intaris_proc, "intaris")


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
            "execution": {"executor_id": "default_inprocess"},
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


# ---------------------------------------------------------------------------
# Live-stack helpers (use real HTTP, not TestClient)
# ---------------------------------------------------------------------------


def live_create_agent(
    live: LiveStack,
    agent_id: str,
    *,
    system_prompt: str = (
        "You are a helpful test assistant. Do not use tools. Answer directly and briefly."
    ),
    capabilities: dict[str, Any] | None = None,
    tool_permissions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create an active agent via live HTTP."""
    response = live.post(
        "/api/v1/agents",
        json={
            "agent_id": agent_id,
            "name": f"Test Agent {agent_id}",
            "display_name": "Test Agent",
            "description": "Integration test agent",
            "system_prompt": system_prompt,
            "execution": {"executor_id": "default_inprocess"},
            "capabilities": capabilities
            or {"memory_backend": "none", "guardrails_backend": "none"},
            "personality": {
                "tone": "concise",
                "temperament": "cooperative",
                "purpose": "integration testing",
            },
            "permissions": {
                "tool_permissions": tool_permissions or {"*": "deny"},
                "can_delegate": False,
            },
        },
    )
    assert response.status_code == 200, f"Agent creation failed: {response.text}"
    activate = live.post(f"/api/v1/agents/{agent_id}/activate")
    assert activate.status_code == 200, f"Agent activation failed: {activate.text}"
    return activate.json()


def live_create_conversation(live: LiveStack, agent_id: str) -> dict[str, Any]:
    """Create a conversation via live HTTP."""
    response = live.post(
        "/api/v1/conversations",
        json={
            "agent_id": agent_id,
            "title": "Live integration test conversation",
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


def live_assistant_text(live: LiveStack, conversation_id: str) -> str:
    """Return persisted assistant message text from the Chat v2 snapshot."""
    deadline = time.monotonic() + 10
    last_response: httpx.Response | None = None
    while time.monotonic() < deadline:
        response = live.get(f"/api/v1/chat/v2/conversations/{conversation_id}/snapshot")
        last_response = response
        assert response.status_code == 200, f"Snapshot failed: {response.text}"
        items = response.json()["timeline"]["items"]
        content = "\n".join(
            str(item.get("content") or "")
            for item in items
            if item.get("kind") == "message" and item.get("role") == "assistant"
        )
        if content:
            return content
        time.sleep(0.5)
    assert last_response is not None
    return ""


def assistant_text_from_events(events: list[dict[str, Any]]) -> str:
    """Extract assistant text from legacy chunks or Chat v2 frame payloads."""
    parts: list[str] = [
        str(event.get("content") or "") for event in events if event.get("type") == "chunk"
    ]

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if (
                value.get("kind") == "message"
                and value.get("role") == "assistant"
                and isinstance(value.get("content"), str)
            ):
                parts.append(value["content"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for event in events:
        visit(event)
    return "\n".join(part for part in parts if part)


def _frame_contains_turn_activity(
    frame: dict[str, Any],
    *,
    conversation_id: str,
    client_message_id: str,
) -> bool:
    if (
        frame.get("type") == "sidebar_conversation_upsert"
        and frame.get("conversation_id") == conversation_id
        and frame.get("conversation", {}).get("has_active_turn") is True
    ):
        return True
    runtime = frame.get("runtime")
    if isinstance(runtime, dict) and runtime.get("has_active_turn") is True:
        return True

    def visit(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("client_message_id") == client_message_id:
                return True
            if value.get("kind") == "message" and value.get("role") == "assistant":
                return True
            return any(visit(child) for child in value.values())
        if isinstance(value, list):
            return any(visit(child) for child in value)
        return False

    return visit(frame.get("ops"))


def live_chat_ws(
    live: LiveStack,
    conversation_id: str,
    message: str,
    *,
    timeout: float = 90,
) -> list[dict[str, Any]]:
    """Submit a Chat v2 message and collect realtime frames through completion."""
    import json

    import websockets.sync.client as wsc

    events: list[dict[str, Any]] = []
    snapshot_path = f"/api/v1/chat/v2/conversations/{conversation_id}/snapshot"
    initial_response = live.get(snapshot_path)
    assert initial_response.status_code == 200, f"Snapshot failed: {initial_response.text}"
    initial = initial_response.json()
    initial_assistant_ids = {
        item["id"]
        for item in initial["timeline"]["items"]
        if item.get("kind") == "message" and item.get("role") == "assistant"
    }
    with wsc.connect(live.ws_url, close_timeout=5, open_timeout=10) as ws:
        ws.send(json.dumps({"type": "auth", "token": live.admin_token}))
        auth_msg = json.loads(ws.recv(timeout=15))
        assert auth_msg["type"] == "authenticated", f"WS auth failed: {auth_msg}"

        ws.send(
            json.dumps(
                {
                    "type": "chat_v2_subscribe",
                    "scope": initial["scope"],
                    "cursor": initial["cursor"],
                }
            )
        )

        subscribe_deadline = time.monotonic() + 15
        while True:
            remaining = subscribe_deadline - time.monotonic()
            assert remaining > 0, "Chat v2 subscription did not produce its initial frame"
            initial_event = json.loads(ws.recv(timeout=remaining))
            events.append(initial_event)
            if initial_event.get("type") == "chat_v2_frame":
                break
            assert initial_event.get("type") != "error", (
                f"Chat v2 subscription failed: {initial_event}"
            )

        client_txn_id = f"integration-{uuid.uuid4().hex}"
        client_message_id = f"message-{uuid.uuid4().hex}"
        admission = live.put(
            f"/api/v1/chat/v2/conversations/{conversation_id}/messages/{client_txn_id}",
            json={
                "client_message_id": client_message_id,
                "content": message,
                "attachments": [],
            },
        )
        assert admission.status_code == 202, f"Message admission failed: {admission.text}"

        deadline = time.monotonic() + timeout
        post_admission_turn_frame_seen = False
        last_snapshot: dict[str, Any] | None = None
        idle_since: float | None = None
        while time.monotonic() < deadline:
            try:
                remaining = max(0.1, deadline - time.monotonic())
                raw = ws.recv(timeout=min(1.0, remaining))
                event = json.loads(raw)
                events.append(event)
                post_admission_turn_frame_seen = (
                    post_admission_turn_frame_seen
                    or _frame_contains_turn_activity(
                        event,
                        conversation_id=conversation_id,
                        client_message_id=client_message_id,
                    )
                )
                if event.get("type") == "error":
                    break
            except TimeoutError:
                pass
            except Exception:
                break

            snapshot_response = live.get(snapshot_path)
            if (
                snapshot_response.status_code == 503
                and snapshot_response.json().get("error", {}).get("code")
                == "event_store_inconsistent"
            ):
                time.sleep(0.1)
                continue
            assert snapshot_response.status_code == 200, (
                f"Snapshot failed: {snapshot_response.text}"
            )
            snapshot = snapshot_response.json()
            last_snapshot = snapshot
            assistant_items = [
                item
                for item in snapshot["timeline"]["items"]
                if item.get("kind") == "message"
                and item.get("role") == "assistant"
                and item.get("id") not in initial_assistant_ids
            ]
            settled = (
                assistant_items
                and not snapshot["runtime"]["has_active_turn"]
                and not snapshot["state"]["active_turn"]["has_active_turn"]
                and snapshot["queue"]["queued_count"] == 0
            )
            if not settled:
                idle_since = None
                continue
            if idle_since is None:
                idle_since = time.monotonic()
                continue
            if time.monotonic() - idle_since >= 1.0:
                assert post_admission_turn_frame_seen, (
                    "Chat v2 subscription did not receive a frame for the admitted turn"
                )
                events.append({"type": "chat_v2_snapshot", "snapshot": snapshot})
                events.append(
                    {
                        "type": "message_complete",
                        "cursor": snapshot["cursor"],
                    }
                )
                break

    if not any(event.get("type") in {"message_complete", "error"} for event in events):
        raise AssertionError(
            "Chat v2 turn did not complete before timeout: "
            f"post_admission_turn_frame_seen={post_admission_turn_frame_seen}, "
            f"runtime={None if last_snapshot is None else last_snapshot.get('runtime')}, "
            f"timeline_items={0 if last_snapshot is None else len(last_snapshot['timeline']['items'])}"
        )
    return events
