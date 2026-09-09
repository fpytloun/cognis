#!/usr/bin/env python3
"""Watch a Cognis rolling update from outside its control plane."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RolloutState:
    """One Kubernetes rollout sample."""

    replicas: int
    ready_replicas: int
    updated_replicas: int
    ready_endpoints: int
    current_revision: str
    update_revision: str
    image: str

    @property
    def unavailable_replicas(self) -> int:
        return max(self.replicas - self.ready_replicas, 0)

    def is_complete(self, target_image: str) -> bool:
        return bool(
            self.image == target_image
            and self.replicas > 0
            and self.ready_replicas == self.replicas
            and self.updated_replicas == self.replicas
            and self.current_revision
            and self.current_revision == self.update_revision
        )


def _kubectl_json(arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["kubectl", *arguments, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError("kubectl did not return a JSON object")
    return value


def read_rollout_state(namespace: str, statefulset: str, service: str) -> RolloutState:
    """Read the controller and public-service state from Kubernetes."""
    workload = _kubectl_json(["-n", namespace, "get", "statefulset", statefulset])
    slices = _kubectl_json(
        [
            "-n",
            namespace,
            "get",
            "endpointslice",
            "-l",
            f"kubernetes.io/service-name={service}",
        ]
    )
    spec = workload.get("spec") or {}
    status = workload.get("status") or {}
    template = spec.get("template") or {}
    pod_spec = template.get("spec") or {}
    containers = pod_spec.get("containers") or []
    if not containers or not isinstance(containers[0], dict):
        raise ValueError("StatefulSet does not contain a controller container")
    ready_endpoints = sum(
        1
        for item in slices.get("items") or []
        for endpoint in (item.get("endpoints") or [])
        if (endpoint.get("conditions") or {}).get("ready") is True
    )
    return RolloutState(
        replicas=int(spec.get("replicas") or 0),
        ready_replicas=int(status.get("readyReplicas") or 0),
        updated_replicas=int(status.get("updatedReplicas") or 0),
        ready_endpoints=ready_endpoints,
        current_revision=str(status.get("currentRevision") or ""),
        update_revision=str(status.get("updateRevision") or ""),
        image=str(containers[0].get("image") or ""),
    )


def health_status(url: str, timeout_seconds: float) -> int:
    """Return the external readiness HTTP status without response content."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def unsafe_reason(state: RolloutState, http_status: int) -> str | None:
    """Return a privacy-safe rollback reason for an unsafe sample."""
    if state.replicas < 2:
        return "controller replica count is less than two"
    if state.unavailable_replicas > 1:
        return "more than one controller is unavailable"
    if state.ready_endpoints < 1:
        return "the public Service has zero ready endpoints"
    if http_status == 503:
        return "the external readiness endpoint returned 503"
    if http_status < 200 or http_status >= 300:
        return f"the external readiness endpoint returned HTTP {http_status}"
    return None


def _run_rollback(
    *,
    argo_namespace: str,
    application: str,
    revision: str,
) -> int:
    patch = json.dumps(
        {
            "spec": {"source": {"targetRevision": revision}},
            "operation": {"sync": {"revision": revision, "prune": True}},
        }
    )
    return subprocess.run(
        [
            "kubectl",
            "-n",
            argo_namespace,
            "patch",
            "application",
            application,
            "--type",
            "merge",
            "-p",
            patch,
        ],
        check=False,
    ).returncode


def _rollback_is_complete(
    *,
    argo_namespace: str,
    application: str,
    revision: str,
    namespace: str,
    statefulset: str,
    service: str,
    health_url: str,
    http_timeout_seconds: float,
) -> bool:
    application_state = _kubectl_json(["-n", argo_namespace, "get", "application", application])
    status = application_state.get("status") or {}
    source = (application_state.get("spec") or {}).get("source") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    rollout = read_rollout_state(namespace, statefulset, service)
    return bool(
        source.get("targetRevision") == revision
        and sync.get("revision") == revision
        and sync.get("status") == "Synced"
        and health.get("status") == "Healthy"
        and rollout.replicas >= 2
        and rollout.ready_replicas == rollout.replicas
        and rollout.current_revision
        and rollout.current_revision == rollout.update_revision
        and rollout.ready_endpoints >= 1
        and 200 <= health_status(health_url, http_timeout_seconds) < 300
    )


def _execute_rollback(args: argparse.Namespace) -> int:
    if (
        _run_rollback(
            argo_namespace=args.argo_namespace,
            application=args.application,
            revision=args.rollback_revision,
        )
        != 0
    ):
        return 3
    deadline = time.monotonic() + args.rollback_timeout_seconds
    while time.monotonic() < deadline:
        try:
            if _rollback_is_complete(
                argo_namespace=args.argo_namespace,
                application=args.application,
                revision=args.rollback_revision,
                namespace=args.namespace,
                statefulset=args.statefulset,
                service=args.service,
                health_url=args.health_url,
                http_timeout_seconds=args.http_timeout_seconds,
            ):
                print("ROLLED BACK: previous revision is healthy with a ready endpoint")
                return 2
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        time.sleep(args.interval_seconds)
    print("ROLLBACK FAILED: previous revision did not become healthy", file=sys.stderr)
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor a Cognis StatefulSet rolling update. Run this program on Maitrea, "
            "outside the Cognis controller deployment."
        )
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--statefulset", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--argo-namespace", default="argocd")
    parser.add_argument("--application", required=True)
    parser.add_argument("--rollback-revision", required=True)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--rollout-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--rollback-timeout-seconds", type=float, default=600.0)
    return parser


def run_watchdog(args: argparse.Namespace) -> int:
    """Run one bounded rollout watch with verified rollback completion."""
    if args.failure_threshold < 1:
        raise ValueError("--failure-threshold must be at least one")
    if (
        min(
            args.interval_seconds,
            args.http_timeout_seconds,
            args.rollout_timeout_seconds,
            args.rollback_timeout_seconds,
        )
        <= 0
    ):
        raise ValueError("watchdog timeouts and intervals must be positive")
    deadline = time.monotonic() + args.rollout_timeout_seconds
    consecutive_failures = 0
    last_reason = ""
    while time.monotonic() < deadline:
        try:
            state = read_rollout_state(args.namespace, args.statefulset, args.service)
            status = health_status(args.health_url, args.http_timeout_seconds)
            reason = unsafe_reason(state, status)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            reason = f"the watchdog sample failed: {type(exc).__name__}"
            state = None
        if reason is not None:
            consecutive_failures += 1
            last_reason = reason
            if consecutive_failures >= args.failure_threshold:
                print(f"ROLLBACK: {last_reason}", file=sys.stderr)
                return _execute_rollback(args)
            time.sleep(args.interval_seconds)
            continue
        consecutive_failures = 0
        assert state is not None
        if state.is_complete(args.target_image):
            print("SAFE: rolling update completed with a ready endpoint")
            return 0
        time.sleep(args.interval_seconds)
    print("ROLLBACK: the rolling update timed out", file=sys.stderr)
    return _execute_rollback(args)


def main() -> int:
    return run_watchdog(_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
