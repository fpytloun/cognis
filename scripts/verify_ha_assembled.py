#!/usr/bin/env python3
"""Black-box assembled HA acceptance verifier for the Compose qualification stack."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets.sync.client

ROOT = Path(__file__).resolve().parents[1]
ADMIN_EMAIL = "admin@cognis-e2e.localdev.me"
ADMIN_PASSWORD = "cognis-local-admin"
EXECUTOR_1 = "ha-e2e-executor-1"
EXECUTOR_2 = "ha-e2e-executor-2"


class VerificationError(RuntimeError):
    """Raised when an assembled HA invariant is not observed."""


class Verifier:
    def __init__(self, *, project: str, env_file: Path, public_url: str) -> None:
        self.project = project
        self.env_file = env_file
        self.public_url = public_url.rstrip("/")
        self.compose_prefix = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "compose.local.yml"),
            "-f",
            str(ROOT / "compose.e2e.yml"),
            "-f",
            str(ROOT / "compose.ha-e2e.yml"),
        ]
        self.http = httpx.Client(timeout=20.0)
        self.token = ""

    def compose(self, *args: str, capture: bool = False) -> str:
        result = subprocess.run(
            [*self.compose_prefix, *args],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=capture,
        )
        return result.stdout if capture else ""

    def psql(self, query: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "cognis",
            "-d",
            "cognis",
            "-At",
            "-c",
            query,
            capture=True,
        ).strip()

    def headers(self, controller: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if controller:
            headers["X-Cognis-HA-Controller"] = controller
        return headers

    def login(self) -> None:
        response = self.http.post(
            f"{self.public_url}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            headers={"X-Cognis-HA-Controller": "controller-1"},
        )
        response.raise_for_status()
        self.token = response.json()["token"]

    def wait_until(
        self,
        description: str,
        predicate: Any,
        *,
        timeout: float = 60.0,
        interval: float = 0.2,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(interval)
        raise VerificationError(f"Timed out waiting for {description}; last={last!r}")

    def assert_routing(self) -> None:
        upstreams = []
        for controller in ("controller-1", "controller-2"):
            response = self.http.get(
                f"{self.public_url}/api/readyz",
                headers={"X-Cognis-HA-Controller": controller},
            )
            response.raise_for_status()
            upstreams.append(response.headers.get("X-Cognis-HA-Upstream"))
        if not all(upstreams) or upstreams[0] == upstreams[1]:
            raise VerificationError(f"deterministic LB routes are not distinct: {upstreams}")
        print(f"ASSERT deterministic_lb_routes: {upstreams}")

    def wait_executors(self) -> None:
        def ready() -> bool:
            response = self.http.get(
                f"{self.public_url}/api/v1/executors",
                headers=self.headers("controller-1"),
            )
            if response.status_code != 200:
                return False
            states = {item["executor_id"]: item["runtime_state"] for item in response.json()}
            return states.get(EXECUTOR_1) == "active" and states.get(EXECUTOR_2) == "active"

        self.wait_until("both HA executors ready", ready, timeout=120)
        owners = self.psql(
            "select replace(resource_key,'executor_connection:','') || ':' || owner_id "
            "from coordination_leases where resource_key in "
            f"('executor_connection:{EXECUTOR_1}','executor_connection:{EXECUTOR_2}') "
            "order by resource_key"
        ).splitlines()
        if not any(f"{EXECUTOR_1}:controller-1:" in row for row in owners):
            raise VerificationError(f"executor 1 is not physically owned by controller 1: {owners}")
        if not any(f"{EXECUTOR_2}:controller-2:" in row for row in owners):
            raise VerificationError(f"executor 2 is not physically owned by controller 2: {owners}")
        print(f"ASSERT physical_executor_owners: {owners}")

    def create_conversation(self) -> str:
        response = self.http.post(
            f"{self.public_url}/api/v1/conversations",
            headers=self.headers("controller-1"),
            json={
                "agent_id": "e2e-test-agent",
                "title": f"HA assembled {uuid.uuid4().hex[:8]}",
                "context": {
                    "type": "web",
                    "ref": None,
                    "platform_data": {"ha_assembled": True},
                    "memory_labels": {},
                },
            },
        )
        response.raise_for_status()
        return response.json()["conversation_id"]

    def connect_clients(self, conversation_id: str) -> list[Any]:
        ws_url = self.public_url.replace("http://", "ws://").replace("https://", "wss://")
        clients = []
        upstreams = []
        for controller in ("controller-1", "controller-2"):
            ws = websockets.sync.client.connect(
                f"{ws_url}/api/ws",
                additional_headers={"X-Cognis-HA-Controller": controller},
                open_timeout=10,
                close_timeout=5,
            )
            ws.send(json.dumps({"type": "auth", "token": self.token}))
            authenticated = json.loads(ws.recv(timeout=10))
            if authenticated.get("type") != "authenticated":
                raise VerificationError(f"websocket authentication failed: {authenticated}")
            snapshot = self.http.get(
                f"{self.public_url}/api/v1/chat/v2/conversations/{conversation_id}/snapshot",
                headers=self.headers(controller),
            )
            snapshot.raise_for_status()
            cursor = snapshot.json()["cursor"]
            ws.send(
                json.dumps(
                    {
                        "type": "chat_v2_subscribe",
                        "scope": {"kind": "conversation", "conversation_id": conversation_id},
                        "cursor": cursor,
                    }
                )
            )
            clients.append(ws)
            upstreams.append(snapshot.headers.get("X-Cognis-HA-Upstream"))
        if upstreams[0] == upstreams[1]:
            raise VerificationError(f"websocket clients were not routed independently: {upstreams}")
        print(f"ASSERT two_controller_subscribers: {upstreams}")
        return clients

    def admit(self, conversation_id: str, content: str, controller: str) -> str:
        transaction = f"ha-{uuid.uuid4().hex}"
        response = self.http.put(
            f"{self.public_url}/api/v1/chat/v2/conversations/{conversation_id}/messages/"
            f"{transaction}",
            headers=self.headers(controller),
            json={
                "content": content,
                "attachments": [],
                "client_message_id": transaction,
                "chat_mode": "default",
            },
        )
        response.raise_for_status()
        return transaction

    def direct_row(self, conversation_id: str) -> dict[str, Any] | None:
        raw = self.psql(
            "select row_to_json(t) from (select request_id,turn_id,status,"
            "owner_controller_id,attempt_count,outcome from direct_turn_requests "
            f"where conversation_id='{conversation_id}' order by admission_order desc limit 1) t"
        )
        return json.loads(raw) if raw else None

    def snapshot_items(self, conversation_id: str) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{self.public_url}/api/v1/chat/v2/conversations/{conversation_id}/snapshot",
            headers=self.headers("controller-1"),
        )
        response.raise_for_status()
        return response.json()["timeline"]["items"]

    def verify_crash_recovery(self, conversation_id: str) -> None:
        marker = f"HA recovery marker {uuid.uuid4().hex}"
        self.admit(conversation_id, marker, "controller-2")

        def safe_checkpoint() -> dict[str, Any] | None:
            row = self.direct_row(conversation_id)
            if not row or row["status"] not in {"claimed", "running"}:
                return None
            phase = (row.get("outcome") or {}).get("phase")
            return (
                row
                if phase in {"claimed", "user_append_pending", "user_appended", "model_wait"}
                else None
            )

        row = self.wait_until(
            "pre-model durable checkpoint", safe_checkpoint, timeout=30, interval=0.03
        )
        owner = row["owner_controller_id"]
        if owner != "controller-2":
            raise VerificationError(
                f"controller-2 routed admission was claimed by {owner!r}: {row}"
            )
        self.compose("kill", "-s", "KILL", "cognis-2")

        terminal = self.wait_until(
            "recovered direct turn completion",
            lambda: (
                current
                if (current := self.direct_row(conversation_id))
                and current["status"] in {"completed", "ambiguous"}
                else None
            ),
            timeout=300,
        )
        if terminal["status"] != "completed":
            raise VerificationError(f"pre-dispatch recovery became ambiguous: {terminal}")
        if terminal["attempt_count"] != 2:
            raise VerificationError(f"recovered request was not claimed exactly twice: {terminal}")
        self.compose("up", "-d", "cognis-2")
        self.wait_until(
            "restarted controller 2 readiness",
            lambda: (
                self.http.get(
                    f"{self.public_url}/api/readyz",
                    headers={"X-Cognis-HA-Controller": "controller-2"},
                ).status_code
                == 200
            ),
            timeout=120,
        )
        self.wait_executors()
        items = self.snapshot_items(conversation_id)
        user_items = [item for item in items if marker in json.dumps(item)]
        completions = [
            item
            for item in items
            if item.get("kind") == "message" and item.get("role") == "assistant"
        ]
        if len(user_items) != 1 or len(completions) != 1:
            raise VerificationError(
                f"recovery canonical counts mismatch: users={len(user_items)} completions={len(completions)}"
            )
        print(
            "ASSERT crash_recovery_exactly_once: "
            f"owner={owner} attempt_count=2 user_events=1 completions=1"
        )

    def verify_reconnect_and_failover(self, conversation_id: str) -> None:
        response = self.http.put(
            f"{self.public_url}/api/v1/executors/{EXECUTOR_2}",
            headers=self.headers("controller-2"),
            json={"labels": {"deployment": "ha-e2e", "role": "primary"}},
        )
        response.raise_for_status()
        self.wait_executors()
        before = self.http.get(
            f"{self.public_url}/api/v1/conversations/{conversation_id}",
            headers=self.headers("controller-1"),
        ).json()
        if before.get("active_executor_id") != EXECUTOR_1:
            raise VerificationError(f"conversation is not pinned to executor 1: {before}")
        generation = before.get("active_executor_generation")

        self.compose("restart", "cognis-executor")
        self.wait_executors()
        after_restart = self.http.get(
            f"{self.public_url}/api/v1/conversations/{conversation_id}",
            headers=self.headers("controller-2"),
        ).json()
        if (
            after_restart.get("active_executor_id") != EXECUTOR_1
            or after_restart.get("active_executor_generation") != generation
        ):
            raise VerificationError(f"same-ID reconnect changed the pin: {after_restart}")
        print("ASSERT same_id_reconnect_grace: pin and generation unchanged")

        self.compose("stop", "cognis-executor")
        # Wait past the 45-second physical ownership lease before exercising
        # the admission-level reconnect grace and selector fallback.
        time.sleep(50)
        self.admit(
            conversation_id,
            f"HA unavailable probe {uuid.uuid4().hex}",
            "controller-2",
        )
        self.wait_until(
            "persisted executor unavailability observation",
            lambda: (
                self.psql(
                    "select active_executor_unavailable_since is not null from conversations "
                    f"where conversation_id='{conversation_id}'"
                )
                == "t"
            ),
            timeout=30,
        )
        time.sleep(17)
        marker = f"HA failover marker {uuid.uuid4().hex}"
        self.admit(conversation_id, marker, "controller-2")

        def failed_over() -> dict[str, Any] | None:
            response = self.http.get(
                f"{self.public_url}/api/v1/conversations/{conversation_id}",
                headers=self.headers("controller-1"),
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if payload.get("active_executor_id") == EXECUTOR_2 else None

        switched = self.wait_until("selector-primary failover", failed_over, timeout=90)
        transition_count = int(
            self.psql(
                "select count(*) from executor_pin_transitions "
                f"where scope_type='conversation' and scope_id='{conversation_id}'"
            )
            or "0"
        )
        delivered_notice_count = int(
            self.wait_until(
                "durable executor failover notice delivery",
                lambda: (
                    self.psql(
                        "select count(*) from executor_pin_notice_outbox "
                        f"where conversation_id='{conversation_id}' "
                        "and delivered_at is not null"
                    )
                    or None
                ),
                timeout=30,
            )
        )
        appended_notice_count = int(
            self.psql(
                "select count(*) from executor_pin_transitions "
                f"where scope_type='conversation' and scope_id='{conversation_id}' "
                "and notice_appended_at is not null"
            )
            or "0"
        )
        if transition_count != 1 or delivered_notice_count != 1 or appended_notice_count != 1:
            raise VerificationError(
                "expected one durable failover transition and delivered canonical notice, "
                f"found transitions={transition_count}, delivered={delivered_notice_count}, "
                f"appended={appended_notice_count}"
            )
        print(
            "ASSERT selector_failover_atomic: "
            f"executor={switched['active_executor_id']} durable_notices=1"
        )
        self.compose("start", "cognis-executor")

    def run(self) -> None:
        self.login()
        self.assert_routing()
        self.wait_executors()
        conversation_id = self.create_conversation()
        clients = self.connect_clients(conversation_id)
        try:
            self.verify_crash_recovery(conversation_id)
            self.verify_reconnect_and_failover(conversation_id)
        finally:
            for client in clients:
                client.close()
        print(f"HA assembled acceptance passed for {conversation_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--public-url", required=True)
    args = parser.parse_args()
    verifier = Verifier(
        project=args.project,
        env_file=args.env_file,
        public_url=args.public_url,
    )
    try:
        verifier.run()
    except Exception:
        verifier.compose("ps")
        verifier.compose(
            "logs",
            "--tail",
            "250",
            "cognis",
            "cognis-2",
            "cognis-lb",
            "cognis-executor",
            "cognis-executor-2",
        )
        raise
    finally:
        verifier.http.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
