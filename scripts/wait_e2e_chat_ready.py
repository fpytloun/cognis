#!/usr/bin/env python3
"""Wait until the local E2E stack can create a Chat v2 turn."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE_URL = os.environ.get("COGNIS_E2E_BASE_URL", "http://localhost:8080").rstrip("/")
ADMIN_EMAIL = (
    os.environ.get("COGNIS_LOCAL_ADMIN_EMAIL")
    or os.environ.get("COGNIS_E2E_ADMIN_EMAIL")
    or "admin@cognis-e2e.localdev.me"
)
ADMIN_PASSWORD = (
    os.environ.get("COGNIS_LOCAL_ADMIN_PASSWORD")
    or os.environ.get("COGNIS_E2E_ADMIN_PASSWORD")
    or "cognis-local-admin"
)
TIMEOUT_SECONDS = float(os.environ.get("COGNIS_E2E_CHAT_READY_TIMEOUT", "60"))


def _request(
    path: str, *, method: str = "GET", token: str | None = None, body: dict[str, Any] | None = None
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read()
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def _try_ready() -> bool:
    login = _request(
        "/api/auth/login",
        method="POST",
        body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    token = login["token"]
    conversation = _request(
        "/api/v1/conversations",
        method="POST",
        token=token,
        body={
            "agent_id": "e2e-test-agent",
            "title": "E2E Chat v2 readiness",
            "context_type": "web",
            "context_ref": f"e2e:chat-ready:{uuid.uuid4().hex}",
        },
    )
    conversation_id = conversation.get("conversation_id") or conversation.get("id")
    if not conversation_id:
        raise RuntimeError(f"conversation creation returned no id: {conversation!r}")
    client_message_id = f"msg_{uuid.uuid4().hex}"
    result = _request(
        f"/api/v1/chat/v2/conversations/{conversation_id}/messages/{client_message_id}",
        method="PUT",
        token=token,
        body={
            "client_message_id": client_message_id,
            "content": "E2E readiness probe",
            "attachments": [],
            "chat_mode": None,
        },
    )
    if result.get("status") not in {"accepted", "queued", "duplicate"}:
        return False

    turn_deadline = time.monotonic() + 30
    while time.monotonic() < turn_deadline:
        snapshot = _request(
            f"/api/v1/chat/v2/conversations/{conversation_id}/snapshot", token=token
        )
        runtime = snapshot.get("runtime") if isinstance(snapshot, dict) else {}
        if isinstance(runtime, dict) and not runtime.get("has_active_turn", True):
            return True
        time.sleep(1)
    return False


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if _try_ready():
                print("E2E Chat v2 readiness probe succeeded")
                return 0
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"{error.code} {error.reason}: {body}")
        except (
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
            KeyError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
        time.sleep(1)
    print(f"E2E Chat v2 readiness probe failed: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
