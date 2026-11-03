from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import run_browser_matrix as matrix


def report_for(filename: str, projects: list[str], *, executed: bool = True) -> dict:
    return {
        "suites": [
            {
                "title": filename,
                "specs": [
                    {
                        "file": filename,
                        "line": 3,
                        "column": 1,
                        "title": "supported behavior",
                        "tests": [
                            {
                                "projectName": project,
                                "status": "expected",
                                "results": [{"status": "passed"}] if executed else [],
                            }
                            for project in projects
                        ],
                    }
                ],
            }
        ],
    }


def test_execution_accepts_exact_planned_cases_and_devices() -> None:
    report = report_for("fixture.spec.ts", ["chromium", "webkit"])
    assert matrix.execution_results(matrix.cases(report), report, "fixture") == {"passed": 2}


@pytest.mark.parametrize(
    "failure",
    [
        "missing-results",
        "zero-executed",
        "unexpected",
        "different-case",
        "missing-device",
        "collection-error",
    ],
)
def test_execution_rejects_successful_collection_without_real_coverage(failure: str) -> None:
    report = report_for("fixture.spec.ts", ["chromium", "webkit"])
    planned = matrix.cases(deepcopy(report))
    spec = report["suites"][0]["specs"][0]
    if failure == "missing-results":
        spec["tests"][0]["results"] = []
    elif failure == "zero-executed":
        for test in spec["tests"]:
            test["results"] = [{"status": "skipped"}]
    elif failure == "unexpected":
        spec["tests"][0]["status"] = "unexpected"
    elif failure == "different-case":
        spec["title"] = "a different behavior"
    elif failure == "missing-device":
        spec["tests"].pop()
    else:
        report["errors"] = [{"message": "collection failed"}]
    with pytest.raises(ValueError):
        matrix.execution_results(planned, report, "fixture")


@pytest.mark.parametrize("mode", ["inventory-only", "failed-group", "complete"])
def test_completion_requires_all_real_groups(monkeypatch, tmp_path: Path, mode: str) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    groups = [
        {"name": "compose", "config": "compose.config.ts", "projects": ["chromium"]},
        {
            "name": "fixture",
            "config": "fixture.config.ts",
            "specs": ["fixture.spec.ts"],
            "projects": ["chromium", "webkit"],
        },
    ]
    (ui / "browser-harnesses.json").write_text(json.dumps(groups))
    output = tmp_path / "reports"
    argv = ["run_browser_matrix.py", "--output-root", str(output)]
    if mode == "inventory-only":
        argv.append("--inventory-only")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(matrix, "UI", ui)
    monkeypatch.setattr(matrix.signal, "signal", lambda *_args: None)
    executions: list[str] = []

    def run(command, _log, environment, _deadline) -> None:
        listing = "--list" in command
        config = Path(command[command.index("--config") + 1])
        if config.suffix == ".mjs":
            report = report_for("compose.spec.ts", ["inventory"], executed=False)
            report["suites"].extend(
                report_for("fixture.spec.ts", ["inventory"], executed=False)["suites"]
            )
        elif config.name == "compose.config.ts":
            report = report_for("compose.spec.ts", ["chromium"], executed=not listing)
        else:
            report = report_for("fixture.spec.ts", ["chromium", "webkit"], executed=not listing)
        Path(environment["PLAYWRIGHT_JSON_OUTPUT_FILE"]).write_text(json.dumps(report))
        if not listing:
            executions.append(config.name)
            if mode == "failed-group":
                raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(matrix, "run_command", run)
    if mode == "failed-group":
        with pytest.raises(subprocess.CalledProcessError):
            matrix.main()
    else:
        matrix.main()
    receipt = json.loads((output / "results.json").read_text())
    assert receipt["complete"] is (mode == "complete")
    if mode == "inventory-only":
        assert executions == []
    elif mode == "failed-group":
        assert executions == ["fixture.config.ts"]
    else:
        assert executions == ["fixture.config.ts", "compose.config.ts"]
