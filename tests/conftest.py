from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest


def _healthcheck(url: str) -> None:
    try:
        response = httpx.get(f"{url.rstrip('/')}/health", timeout=5.0)
        # JWT-protected services may return 401 on /health while still being up.
        if response.status_code not in {200, 401, 403}:
            response.raise_for_status()
    except Exception as exc:  # pragma: no cover - integration guard
        pytest.skip(f"Live service unavailable at {url}: {exc}")


@pytest.fixture(scope="session")
def contract_run_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def contract_user_email(contract_run_id: str) -> str:
    return os.getenv(
        "COGNIS_TEST_USER_EMAIL",
        f"cognis-contract-{contract_run_id}@example.com",
    )


@pytest.fixture(scope="session")
def contract_agent_id() -> str:
    return os.getenv("COGNIS_TEST_AGENT_ID", "cognis-contract-agent")


@pytest.fixture(scope="session")
def mnemory_url() -> str:
    url = os.getenv("COGNIS_TEST_MNEMORY_URL", "http://127.0.0.1:8050")
    _healthcheck(url)
    return url.rstrip("/")


@pytest.fixture(scope="session")
def intaris_url() -> str:
    url = os.getenv("COGNIS_TEST_INTARIS_URL", "http://127.0.0.1:8060")
    _healthcheck(url)
    return url.rstrip("/")


@pytest.fixture(scope="session")
def jwt_private_key() -> str:
    path = os.getenv("COGNIS_TEST_JWT_PRIVATE_KEY_PATH")
    if not path:
        pytest.skip("COGNIS_TEST_JWT_PRIVATE_KEY_PATH is not configured")
    key_path = Path(path)
    if not key_path.is_file():
        pytest.skip(f"JWT private key not found: {key_path}")
    return key_path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def make_service_jwt(
    jwt_private_key: str,
    contract_user_email: str,
) -> Callable[..., str]:
    def _make_service_jwt(
        audience: str,
        *,
        agent_id: str | None = None,
        subject: str | None = None,
        expires_in_seconds: int = 3600,
    ) -> str:
        now = datetime.now(UTC)
        payload: dict[str, object] = {
            "sub": subject or contract_user_email,
            "iss": "cognis",
            "aud": [audience],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        return jwt.encode(payload, jwt_private_key, algorithm="ES256")

    return _make_service_jwt


@pytest.fixture
def unique_session_id(contract_run_id: str) -> Callable[[str], str]:
    def _unique_session_id(prefix: str) -> str:
        return f"{prefix}-{contract_run_id}-{uuid.uuid4().hex[:8]}"

    return _unique_session_id


@pytest.fixture
def unique_label(contract_run_id: str) -> str:
    return f"contract-{contract_run_id}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def http_client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=10.0) as client:
        yield client


@pytest.fixture
def mnemory_cleanup(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
) -> Iterator[list[str]]:
    session_ids: list[str] = []
    yield session_ids

    headers = {
        "Authorization": f"Bearer {make_service_jwt('mnemory', agent_id=contract_agent_id)}",
        "X-Agent-Id": contract_agent_id,
    }
    for session_id in session_ids:
        with suppress(Exception):
            http_client.delete(f"{mnemory_url}/api/sessions/{session_id}", headers=headers)


@pytest.fixture
def maybe_mnemory_api_key() -> str | None:
    return os.getenv("COGNIS_TEST_MNEMORY_API_KEY") or None


@pytest.fixture
def maybe_intaris_api_key() -> str | None:
    return os.getenv("COGNIS_TEST_INTARIS_API_KEY") or None


def wait_for(condition: Callable[[], bool], *, timeout: float = 3.0, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise AssertionError("Timed out waiting for condition")
