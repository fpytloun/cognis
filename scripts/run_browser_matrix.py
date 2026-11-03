"""Run every browser case in its owning harness, preserving device coverage."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
BUDGET_SECONDS = 3600
type Case = tuple[str, int, int, tuple[str, ...]]
type Pair = tuple[Case, str]


def cases(report: dict[str, Any]) -> dict[Pair, dict[str, Any]]:
    """Read exact logical identity and project independently of reporter IDs."""
    if report.get("errors"):
        raise ValueError("Playwright reported collection or execution errors")
    found: dict[Pair, dict[str, Any]] = {}

    def visit(suite: dict[str, Any], titles: tuple[str, ...]) -> None:
        titles += (suite["title"],)
        for spec in suite.get("specs", []):
            filename = Path(spec["file"])
            if filename.is_absolute():
                filename = filename.relative_to(UI / "e2e")
            if ".." in filename.parts:
                raise ValueError("Case path escapes the browser test directory")
            case = (filename.as_posix(), spec["line"], spec["column"], titles + (spec["title"],))
            for test in spec["tests"]:
                pair = (case, test["projectName"])
                if pair in found:
                    raise ValueError(f"Ambiguous case/project identity: {pair}")
                found[pair] = test
        for child in suite.get("suites", []):
            visit(child, titles)

    for suite in report.get("suites", []):
        visit(suite, ())
    if not found:
        raise ValueError("A browser group collected zero cases")
    return found


def partition(
    universe: set[Case],
    groups: list[dict[str, Any]],
    listings: dict[str, dict[Pair, dict[str, Any]]],
) -> None:
    """Require one logical owner and every explicitly required device."""
    if not universe:
        raise ValueError("Empty unfiltered browser inventory")
    names = [group["name"] for group in groups]
    if len(set(names)) != len(names) or names.count("compose") != 1:
        raise ValueError("Harness names must be unique with one compose owner")
    expected: dict[str, set[Case]] = {name: set() for name in names}
    for case in universe:
        owners = [
            group["name"]
            for group in groups
            if any(fnmatch.fnmatch(case[0], spec) for spec in group.get("specs", []))
            and (not group.get("grep") or re.search(group["grep"], " ".join(case[3])))
        ]
        if len(owners) > 1:
            raise ValueError(f"Duplicate logical owner: {case}")
        expected[owners[0] if owners else "compose"].add(case)
    for group in groups:
        name = group["name"]
        projects = group["projects"]
        if not projects or len(set(projects)) != len(projects):
            raise ValueError(f"Invalid required project list: {name}")
        pairs = {(case, project) for case in expected[name] for project in projects}
        if not pairs or pairs != set(listings[name]):
            raise ValueError(f"Missing, extra or misrouted cases/devices in {name}")


def run_command(
    command: list[str],
    log: Path,
    environment: dict[str, str],
    deadline: float,
) -> None:
    """Propagate failure and enforce one deadline across listing and execution."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("The existing browser matrix budget is exhausted")
    with log.open("w") as output:
        process = subprocess.Popen(
            command,
            cwd=UI,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            status = process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise TimeoutError(
                    f"Browser process group {process.pid} did not stop; no later group will run"
                ) from error
            if isinstance(error, KeyboardInterrupt):
                raise
            raise TimeoutError("The existing browser matrix budget is exhausted") from error
        if status:
            raise subprocess.CalledProcessError(status, command)


def execution_results(
    planned: dict[Pair, dict[str, Any]],
    report: dict[str, Any],
    name: str,
) -> dict[str, int]:
    """Validate actual execution, separately from successful case collection."""
    actual = cases(report)
    if set(actual) != set(planned):
        raise ValueError(f"Executed cases/devices differ from inventory: {name}")
    statuses = Counter(
        result["status"] for test in actual.values() for result in test.get("results", [])
    )
    if not statuses or not any(status != "skipped" for status in statuses):
        raise ValueError(f"Zero executed browser cases: {name}")
    if any(not test.get("results") for test in actual.values()):
        raise ValueError(f"Missing execution results in {name}")
    if any(test.get("status") == "unexpected" for test in actual.values()):
        raise ValueError(f"Unexpected browser result in {name}")
    return dict(statuses)


def main() -> None:
    """Collect independently, prove partition parity, then execute the plan."""
    started = time.monotonic()

    def cancelled(_signal: int, _frame: Any) -> None:
        raise KeyboardInterrupt("Browser matrix cancelled")

    signal.signal(signal.SIGTERM, cancelled)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    if args.output_root is not None:
        output = args.output_root.resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        parent = UI / "test-results/browser-matrix"
        parent.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    print(f"Browser matrix reports: {output}", flush=True)
    deadline = started + BUDGET_SECONDS - 5  # Reserve cleanup inside the existing total budget.
    groups = json.loads((UI / "browser-harnesses.json").read_text())
    executable = str(UI / "node_modules/.bin/playwright")
    environment = {**os.environ, "PLAYWRIGHT_HTML_OPEN": "never"}
    listings: dict[str, dict[Pair, dict[str, Any]]] = {}

    def collect(config: Path, name: str) -> dict[Pair, dict[str, Any]]:
        destination = output / f"{name}-inventory.json"
        env = {**environment, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(destination)}
        run_command(
            [
                executable,
                "test",
                "--config",
                str(config),
                "--list",
                "--reporter=json",
                "--forbid-only",
                "--workers=1",
            ],
            output / f"{name}-inventory.log",
            env,
            deadline,
        )
        return cases(json.loads(destination.read_text()))

    # This config has no ownership filters or inherited project selectors.
    with tempfile.TemporaryDirectory(prefix="cognis-browser-inventory-") as temporary:
        inventory = Path(temporary) / "playwright.config.mjs"
        inventory.write_text(
            "export default "
            + json.dumps(
                {
                    "testDir": str(UI / "e2e"),
                    "forbidOnly": True,
                    "projects": [{"name": "inventory"}],
                }
            )
            + ";\n"
        )
        universe = {case for case, _project in collect(inventory, "unfiltered")}
    for group in groups:
        listings[group["name"]] = collect(UI / group["config"], group["name"])
    partition(universe, groups, listings)
    receipt: dict[str, Any] = {
        "logical_cases": len(universe),
        "required_device_executions": sum(len(value) for value in listings.values()),
        "budget_seconds": BUDGET_SECONDS,
        "inventory_only": args.inventory_only,
        "groups": [],
        "complete": False,
    }
    receipt_path = output / "results.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    if args.inventory_only:
        print(json.dumps(receipt, indent=2))
        return
    for group in sorted(groups, key=lambda item: item["name"] == "compose"):
        name = group["name"]
        directory = output / name
        directory.mkdir()
        report = directory / "report.json"
        env = {
            **environment,
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report),
            "PLAYWRIGHT_HTML_REPORT": str(directory / "html"),
        }
        print(f"START browser harness {name}", flush=True)
        run_command(
            [
                executable,
                "test",
                "--config",
                str(UI / group["config"]),
                "--workers=1",
                "--forbid-only",
                "--reporter=list,json,html",
                "--output",
                str(directory / "artifacts"),
            ],
            directory / "browser.log",
            env,
            deadline,
        )
        statuses = execution_results(listings[name], json.loads(report.read_text()), name)
        receipt["groups"].append(
            {
                "name": name,
                "config": group["config"],
                "required_projects": group["projects"],
                "results": dict(statuses),
            }
        )
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"END browser harness {name}", flush=True)
    receipt["complete"] = True
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
