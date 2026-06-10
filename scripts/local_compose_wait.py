#!/usr/bin/env python3
"""Wait for the Local Compose Cognis stack to become reachable."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str


def _check(endpoint: Endpoint, timeout_seconds: float) -> tuple[bool, str]:
    try:
        response = httpx.get(endpoint.url, timeout=timeout_seconds)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if 200 <= response.status_code < 400:
        return True, f"HTTP {response.status_code}"
    return False, f"HTTP {response.status_code}: {response.text[:160]}"


def wait_for(endpoints: list[Endpoint], *, timeout_seconds: float, interval_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    remaining = {endpoint.name: endpoint for endpoint in endpoints}
    last_status: dict[str, str] = {}

    while remaining and time.monotonic() < deadline:
        for name, endpoint in list(remaining.items()):
            ok, status = _check(endpoint, timeout_seconds=min(interval_seconds, 5.0))
            last_status[name] = status
            if ok:
                print(f"{name}: ready ({status})")
                del remaining[name]
        if remaining:
            time.sleep(interval_seconds)

    if not remaining:
        return 0

    print("Timed out waiting for Local Compose services:", file=sys.stderr)
    for name in sorted(remaining):
        print(f"- {name}: {last_status.get(name, 'not checked')}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cognis-url",
        default="http://localhost:8080",
        help="Public Cognis controller URL (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--mnemory-url",
        default="http://localhost:8050",
        help="Public Mnemory URL (default: http://localhost:8050)",
    )
    parser.add_argument(
        "--intaris-url",
        default="http://localhost:8060",
        help="Public Intaris URL (default: http://localhost:8060)",
    )
    parser.add_argument(
        "--qdrant-url",
        default="http://localhost:6333",
        help="Public Qdrant URL (default: http://localhost:6333)",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    endpoints = [
        Endpoint("qdrant", f"{args.qdrant_url.rstrip('/')}/readyz"),
        Endpoint("mnemory", f"{args.mnemory_url.rstrip('/')}/health"),
        Endpoint("intaris", f"{args.intaris_url.rstrip('/')}/ready"),
        Endpoint("cognis", f"{args.cognis_url.rstrip('/')}/api/health"),
    ]
    return wait_for(endpoints, timeout_seconds=args.timeout, interval_seconds=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
