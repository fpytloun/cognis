from __future__ import annotations

import hashlib
import logging
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
from cryptography.hazmat.primitives import serialization


@pytest.fixture(autouse=True)
def preserve_logging_state() -> Iterator[None]:
    """Prevent Alembic fileConfig and app startup from leaking logger state."""
    manager = logging.Logger.manager
    logger_dict = dict(manager.loggerDict)
    loggers = [logging.getLogger()]
    loggers.extend(
        logger for logger in manager.loggerDict.values() if isinstance(logger, logging.Logger)
    )
    state = {
        logger: (
            logger.disabled,
            logger.level,
            list(logger.handlers),
            logger.propagate,
        )
        for logger in loggers
    }
    try:
        yield
    finally:
        for logger in manager.loggerDict.values():
            if isinstance(logger, logging.Logger) and logger not in state:
                logger.disabled = False
                logger.setLevel(logging.NOTSET)
                logger.handlers.clear()
                logger.propagate = True
        for logger, (disabled, level, handlers, propagate) in state.items():
            logger.disabled = disabled
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
        manager.loggerDict.clear()
        manager.loggerDict.update(logger_dict)


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
        actual_subject = subject or contract_user_email
        payload: dict[str, object] = {
            "sub": actual_subject,
            "iss": "cognis",
            "aud": [audience],
            "typ": "service",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
            payload["agent_owner_email"] = actual_subject
        private_key = serialization.load_pem_private_key(
            jwt_private_key.encode(),
            password=None,
        )
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        kid = hashlib.sha256(public_key).hexdigest()[:16]
        return jwt.encode(
            payload,
            jwt_private_key,
            algorithm="ES256",
            headers={"kid": kid},
        )

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
