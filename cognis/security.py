"""Authentication and security helpers."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


def create_password_hasher() -> PasswordHasher:
    """Create the argon2id password hasher with spec parameters."""
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def generate_api_key_material() -> tuple[str, str]:
    """Generate an API key ID and plaintext token."""
    key_id = f"ck{uuid.uuid4().hex[:16]}"
    secret_part = secrets.token_urlsafe(32)
    return key_id, f"cognis_{key_id}_{secret_part}"


def parse_api_key(api_key: str) -> tuple[str, str] | None:
    """Parse a plaintext API key into key_id and secret part."""
    prefix = "cognis_"
    if not api_key.startswith(prefix):
        return None
    remainder = api_key[len(prefix) :]
    try:
        key_id, secret_part = remainder.split("_", 1)
    except ValueError:
        return None
    if not key_id.startswith("ck"):
        return None
    return key_id, secret_part


def verify_api_key(hasher: PasswordHasher, plaintext_api_key: str, stored_hash: str) -> bool:
    """Verify a plaintext API key against a stored argon2 hash."""
    try:
        return hasher.verify(stored_hash, plaintext_api_key)
    except VerifyMismatchError:
        return False


@dataclass
class LoginRateLimitState:
    failures: list[datetime]


@dataclass
class ApiRateLimitState:
    requests: deque[datetime]


class LoginRateLimiter:
    """Simple in-memory login rate limiter."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window = timedelta(seconds=window_seconds)
        self._state: dict[str, LoginRateLimitState] = {}

    def _prune(self, email: str) -> None:
        now = datetime.now(UTC)
        state = self._state.get(email)
        if state is None:
            return
        state.failures = [ts for ts in state.failures if now - ts < self.window]
        if not state.failures:
            self._state.pop(email, None)

    def is_limited(self, email: str) -> bool:
        self._prune(email)
        state = self._state.get(email)
        return state is not None and len(state.failures) >= self.max_attempts

    def record_failure(self, email: str) -> None:
        self._prune(email)
        state = self._state.setdefault(email, LoginRateLimitState(failures=[]))
        state.failures.append(datetime.now(UTC))

    def clear(self, email: str) -> None:
        self._state.pop(email, None)


class RequestRateLimiter:
    """In-memory aggregate per-user read/write request rate limiter."""

    def __init__(
        self,
        *,
        read_requests_per_minute: int = 600,
        write_requests_per_minute: int = 200,
        window_seconds: int = 60,
    ) -> None:
        self.read_requests_per_minute = read_requests_per_minute
        self.write_requests_per_minute = write_requests_per_minute
        self.window = timedelta(seconds=window_seconds)
        self._state: dict[str, ApiRateLimitState] = {}
        self._lock = asyncio.Lock()

    def update_limits(
        self,
        *,
        read_requests_per_minute: int | None = None,
        write_requests_per_minute: int | None = None,
    ) -> None:
        if read_requests_per_minute is not None:
            self.read_requests_per_minute = read_requests_per_minute
        if write_requests_per_minute is not None:
            self.write_requests_per_minute = write_requests_per_minute

    async def allow(self, *, user_key: str, path: str, method: str) -> bool:
        del path
        async with self._lock:
            now = datetime.now(UTC)
            bucket_name = "read" if method.upper() in {"GET", "HEAD", "OPTIONS"} else "write"
            state_key = f"{user_key}:{bucket_name}"
            bucket = self._state.setdefault(state_key, ApiRateLimitState(requests=deque()))
            while bucket.requests and now - bucket.requests[0] >= self.window:
                bucket.requests.popleft()
            if not bucket.requests:
                self._state.pop(state_key, None)
                bucket = self._state.setdefault(state_key, ApiRateLimitState(requests=deque()))
            limit = (
                self.read_requests_per_minute
                if method.upper() in {"GET", "HEAD", "OPTIONS"}
                else self.write_requests_per_minute
            )
            if len(bucket.requests) >= limit:
                return False
            bucket.requests.append(now)
            return True
