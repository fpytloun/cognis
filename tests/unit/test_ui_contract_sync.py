"""Lightweight type-drift linter for UI vs API shapes.

This test asserts that fields present in critical API Pydantic models
are visible in the UI TypeScript interfaces. It is intentionally
conservative — it only checks that the FIELD NAME exists somewhere in
the UI type file. That is enough to catch the regressions we have
actually seen (fields added on the server but forgotten in the UI type).

Strict structural comparison would require generating TS from Pydantic,
which adds tooling complexity. If drift beyond renames becomes a
problem we will upgrade this to a generator-based approach.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cognis.api.models import (
    AgentResponse,
    DeliverableResponse,
    ExecutorConfigResponse,
    MCPServerConfigResponse,
    PendingPauseResponse,
    SessionResponse,
    SkillResponse,
    SkillVersionResponse,
    StepRunResponse,
    TaskResponse,
    WorkflowResponse,
)

UI_API_TYPES = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "types" / "api.ts"


def _load_ui_types() -> str:
    if not UI_API_TYPES.exists():
        pytest.skip(f"UI API types not found at {UI_API_TYPES}")
    return UI_API_TYPES.read_text(encoding="utf-8")


_MODELS_TO_CHECK = (
    ("Agent", AgentResponse),
    ("Task", TaskResponse),
    ("Deliverable", DeliverableResponse),
    ("ExecutorConfig", ExecutorConfigResponse),
    ("MCPServerConfigResponse", MCPServerConfigResponse),
    ("StepRun", StepRunResponse),
    ("PendingPause", PendingPauseResponse),
    ("Session", SessionResponse),
    ("Skill", SkillResponse),
    ("SkillVersion", SkillVersionResponse),
    ("Workflow", WorkflowResponse),
)


def _extract_interface_body(source: str, interface_name: str) -> str:
    # Pull the first `interface <Name> { ... }` block. Handles nested
    # braces via a simple depth counter.
    pattern = rf"interface\s+{re.escape(interface_name)}\s+(?:extends\s+\w+\s+)?\{{"
    match = re.search(pattern, source)
    if match is None:
        raise AssertionError(f"UI interface {interface_name!r} not found in api.ts")
    start = match.end()
    depth = 1
    index = start
    while index < len(source) and depth > 0:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return source[start : index - 1]


def _fields_in_interface(body: str) -> set[str]:
    # Match top-level `<field>?: ...` declarations. We do not parse TS
    # fully — we only need field names for this lint.
    names: set[str] = set()
    depth = 0
    line_start = 0
    for index, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == "\n":
            line = body[line_start:index].strip()
            line_start = index + 1
            if depth != 0 or not line:
                continue
            field_match = re.match(r"^([A-Za-z_][A-Za-z_0-9]*)\??\s*:", line)
            if field_match is not None:
                names.add(field_match.group(1))
    return names


@pytest.mark.parametrize("interface_name,model_cls", _MODELS_TO_CHECK)
def test_ui_interface_covers_api_model_fields(interface_name: str, model_cls: type) -> None:
    source = _load_ui_types()
    try:
        body = _extract_interface_body(source, interface_name)
    except AssertionError as exc:
        pytest.fail(str(exc))
        return

    ui_fields = _fields_in_interface(body)
    api_fields = set(model_cls.model_fields.keys())

    missing = api_fields - ui_fields
    # human_schedule and similar UI-only display fields may exist on the
    # server side without being surfaced — skip a known allowlist where
    # the UI explicitly ignores a server field.
    allowed_missing = {
        "Agent": set(),
        "Task": set(),
        "Deliverable": set(),
        "ExecutorConfig": set(),
        "MCPServerConfigResponse": set(),
        "StepRun": set(),
        "PendingPause": set(),
        "Session": set(),
        "Skill": set(),
        "SkillVersion": set(),
        "Workflow": set(),
    }.get(interface_name, set())
    missing -= allowed_missing

    assert not missing, (
        f"UI interface {interface_name!r} is missing fields present in "
        f"{model_cls.__name__}: {sorted(missing)}. "
        "Either add them to ui/src/lib/types/api.ts or explicitly whitelist "
        "them in tests/unit/test_ui_contract_sync.py."
    )
