#!/usr/bin/env python3
"""Admin API-key client for bounded controller diagnostics; standard library only."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare observations, never claim additive ownership or causal savings."""
    output: dict[str, Any] = {"comparisons": {}, "warnings": ["workload_not_controlled"]}
    old_reports = before.get("reports", {})
    new_reports = after.get("reports", {})
    if set(old_reports) != set(new_reports):
        output["warnings"].append("target_set_changed_or_process_restarted")
    for owner in sorted(set(old_reports) & set(new_reports)):
        warnings: list[str] = []
        first, second = old_reports[owner], new_reports[owner]
        old, new = first.get("resources", {}), second.get("resources", {})
        result: dict[str, Any] = {
            "warnings": warnings,
            "process_comparable": False,
            "owner_bytes_comparable": False,
        }
        output["comparisons"][owner] = result
        if any(r.get("errors") or r.get("truncated") for r in (first, second, old, new)):
            warnings.append("report_incomplete")
            continue
        if not old.get("memory") or not new.get("memory"):
            warnings.append("memory_samples_missing")
            continue
        a, b = old["memory"][-1], new["memory"][-1]
        runtimes = (old.get("runtime", {}), new.get("runtime", {}))
        if any(r.get("build_source") != "environment" for r in runtimes):
            warnings.append("exact_build_identity_unavailable")
        if (
            not runtimes[0].get("build")
            or runtimes[0].get("build") != runtimes[1].get("build")
            or old.get("schema_version") != new.get("schema_version")
        ):
            warnings.append("build_or_schema_changed_or_missing")
            continue
        if not a.get("timestamp") or not b.get("timestamp") or b["timestamp"] <= a["timestamp"]:
            warnings.append("ordered_sample_timestamps_missing")
            continue
        result["process_comparable"] = True
        result["sample_gap_seconds"] = b["timestamp"] - a["timestamp"]
        result["process_counter_deltas"] = {
            key: b["process"][key] - value
            for key, value in a.get("process", {}).items()
            if key in b.get("process", {}) and type(value) is int and type(b["process"][key]) is int
        }
        owners = (a.get("owners", {}), b.get("owners", {}))
        accounting = [r.get("accounting", {}) for r in owners]
        if (
            all(r.get("coverage_complete") for r in accounting)
            and accounting[0].get("schema_version") == accounting[1].get("schema_version")
            and owners[0].get("sessions") == owners[1].get("sessions")
            and set(accounting[0].get("fields", {})) == set(accounting[1].get("fields", {}))
        ):
            result["owner_bytes_comparable"] = True
            result["non_additive_field_byte_deltas"] = {
                key: accounting[1]["fields"][key]["estimated_bytes"] - row["estimated_bytes"]
                for key, row in accounting[0]["fields"].items()
            }
        else:
            warnings.append("owner_coverage_incomplete_or_changed")
        if any(owners[0].get(key) != owners[1].get(key) for key in ("sessions", "owner_counts")):
            warnings.append("observed_owner_cardinalities_changed")
        options = [report.get("meta", {}).get("options", {}) for report in (before, after)]
        cpu_options = (
            "level",
            "sample_interval_ms",
            "traceback_depth",
            "allocation_snapshots",
            "allocation_interval_seconds",
        )
        cpu_comparable = all(options[0].get(key) == options[1].get(key) for key in cpu_options)
        if not cpu_comparable:
            warnings.append("cpu_capture_options_changed")
        if any(o.get("level") in ("allocations", "full") for o in options):
            warnings.append("allocation_tracing_observer_overhead")
        result["cpu_windows"] = [
            {
                "elapsed_seconds": r["cpu"].get("elapsed_seconds"),
                "average_cores": (
                    r["cpu"].get("process_cpu_seconds", 0) / r["cpu"]["elapsed_seconds"]
                    if r["cpu"].get("elapsed_seconds", 0) > 0
                    else None
                ),
                "sampler_thread_cpu_seconds": r["cpu"].get("sampler_thread_cpu_seconds"),
                "unattributed_process_cpu_seconds": r["cpu"].get(
                    "unattributed_process_cpu_seconds"
                ),
                "missing_thread_stat_samples": r["cpu"].get("missing_thread_stat_samples"),
            }
            if r.get("cpu")
            else None
            for r in (old, new)
        ]
        result["cpu_options_comparable"] = cpu_comparable
    if not output["comparisons"]:
        output["warnings"].append("no_common_process_incarnation")
    return output


def read_report(path: str) -> dict[str, Any]:
    """Read at most two MiB, covering the bounded eight-controller API report."""
    with Path(path).open("rb") as stream:
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("report_file_too_large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("report_must_be_an_object")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("discover", "start", "status", "report", "stop", "compare")
    )
    parser.add_argument("--capture-id", type=UUID)
    parser.add_argument("--level", choices=("light", "cpu", "allocations", "full"), default="cpu")
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--target", action="append")
    parser.add_argument("--sample-interval-ms", type=int, default=100)
    parser.add_argument("--traceback-depth", type=int, default=5)
    parser.add_argument("--allocation-interval-seconds", type=int, default=60)
    parser.add_argument(
        "--allocation-snapshots", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--before")
    parser.add_argument("--after")
    args = parser.parse_args()
    if args.action == "compare":
        if not args.before or not args.after:
            parser.error("compare requires --before and --after saved reports")
        try:
            result = compare_reports(read_report(args.before), read_report(args.after))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            print(json.dumps({"error": "invalid_comparison_report"}), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    base = os.environ.get("COGNIS_URL", "").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error("COGNIS_URL must be an HTTPS origin without credentials or query")
    key = os.environ.get("COGNIS_ADMIN_API_KEY", "")
    if not key:
        parser.error("Inject COGNIS_ADMIN_API_KEY from your credential store")
    if args.action != "discover" and not args.capture_id:
        parser.error("--capture-id is required; reuse it when retrying an uncertain start")
    path = "/api/v1/system/profiling"
    body = None
    method = "GET"
    if args.action == "start":
        path += "/captures"
        method = "POST"
        body = json.dumps(
            {
                "capture_id": str(args.capture_id),
                "targets": args.target,
                "options": {
                    "level": args.level,
                    "duration_seconds": args.duration,
                    "sample_interval_ms": args.sample_interval_ms,
                    "traceback_depth": args.traceback_depth,
                    "allocation_interval_seconds": args.allocation_interval_seconds,
                    "allocation_snapshots": args.allocation_snapshots,
                },
            }
        ).encode()
    elif args.action != "discover":
        path += f"/captures/{args.capture_id}"
        if args.action in ("report", "stop"):
            path += "/" + args.action
        if args.action == "stop":
            method = "POST"
    request = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        print(json.dumps({"error": "api_request_failed", "status": exc.code}), file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError):
        print("Request outcome uncertain; retry with the same capture ID.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
