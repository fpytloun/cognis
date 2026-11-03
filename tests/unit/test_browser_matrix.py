from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import run_browser_matrix as matrix

CASE = ("fixture.spec.ts", 3, 1, ("fixture.spec.ts", "fixture behavior"))
COMPOSE = ("compose.spec.ts", 4, 1, ("compose.spec.ts", "compose behavior"))


def plan():
    groups = [
        {"name": "compose", "projects": ["chromium"]},
        {"name": "fixture", "specs": ["fixture.spec.ts"], "projects": ["chromium", "webkit"]},
    ]
    listings = {
        "compose": {(COMPOSE, "chromium"): {}},
        "fixture": {(CASE, "chromium"): {}, (CASE, "webkit"): {}},
    }
    return groups, listings


def test_partition_retains_each_logical_case_and_required_device() -> None:
    groups, listings = plan()
    matrix.partition({CASE, COMPOSE}, groups, listings)


@pytest.mark.parametrize(
    "mutation", ["missing-case", "missing-device", "extra-case", "duplicate-owner"]
)
def test_partition_rejects_lost_or_ambiguous_coverage(mutation: str) -> None:
    groups, listings = plan()
    if mutation == "missing-case":
        listings["compose"].clear()
    elif mutation == "missing-device":
        del listings["fixture"][(CASE, "webkit")]
    elif mutation == "extra-case":
        listings["fixture"][(COMPOSE, "webkit")] = {}
    else:
        groups.append({**deepcopy(groups[1]), "name": "second-fixture"})
    with pytest.raises(ValueError):
        matrix.partition({CASE, COMPOSE}, groups, listings)


def test_subset_owner_leaves_other_cases_in_shared_file() -> None:
    selected = ("shared.spec.ts", 3, 1, ("shared.spec.ts", "selection copy preserves content"))
    neighboring = ("shared.spec.ts", 8, 1, ("shared.spec.ts", "ordinary rendering"))
    groups = [
        {"name": "compose", "projects": ["chromium"]},
        {
            "name": "copy",
            "specs": ["shared.spec.ts"],
            "grep": "selection copy preserves",
            "projects": ["chromium", "webkit-ipad"],
        },
    ]
    matrix.partition(
        {selected, neighboring},
        groups,
        {
            "compose": {(neighboring, "chromium"): {}},
            "copy": {(selected, "chromium"): {}, (selected, "webkit-ipad"): {}},
        },
    )


def test_empty_collection_cannot_pass() -> None:
    with pytest.raises(ValueError, match="zero"):
        matrix.cases({"suites": []})


def test_exhausted_budget_cannot_start_another_process(monkeypatch, tmp_path: Path) -> None:
    create = MagicMock()
    monkeypatch.setattr(matrix.subprocess, "Popen", create)
    monkeypatch.setattr(matrix.time, "monotonic", lambda: 5)
    with pytest.raises(TimeoutError):
        matrix.run_command(["playwright"], tmp_path / "expired.log", {}, deadline=4)
    create.assert_not_called()


def test_failing_group_propagates_failure(monkeypatch, tmp_path: Path) -> None:
    process = MagicMock()
    process.wait.return_value = 1
    monkeypatch.setattr(matrix.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(matrix.time, "monotonic", lambda: 1)
    with pytest.raises(subprocess.CalledProcessError):
        matrix.run_command(["playwright"], tmp_path / "failed.log", {}, deadline=4)


@pytest.mark.parametrize("cancelled", [False, True])
def test_timeout_and_cancellation_terminate_owned_process_group(monkeypatch, tmp_path, cancelled):
    process = MagicMock()
    error = KeyboardInterrupt() if cancelled else subprocess.TimeoutExpired(["playwright"], 3)
    process.wait.side_effect = [error, 0]
    terminate = MagicMock()
    monkeypatch.setattr(matrix.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(matrix.time, "monotonic", lambda: 1)
    monkeypatch.setattr(matrix.os, "killpg", terminate)
    with pytest.raises(KeyboardInterrupt if cancelled else TimeoutError):
        matrix.run_command(["playwright"], tmp_path / "stopped.log", {}, deadline=4)
    terminate.assert_called_once_with(process.pid, matrix.signal.SIGTERM)
    assert process.wait.call_args_list[0].kwargs["timeout"] == 3
    assert process.wait.call_args_list[1].kwargs["timeout"] == 5
