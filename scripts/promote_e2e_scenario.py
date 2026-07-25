#!/usr/bin/env python3
"""Promote an interactively-reproduced scenario to a static test.

Usage:
    python scripts/promote_e2e_scenario.py my-scenario-name

This script:
1. Fetches the last injected scenario from the mock-llm control plane.
2. Saves it to tests/e2e/scenarios/<name>.yaml.
3. Fetches the last captured WS event stream from the mock-llm history.
4. Saves it to tests/e2e/golden/<name>.jsonl.

After running, the scenario is part of the static test suite and will be
checked by both pytest (backend invariants) and vitest (client-store invariants).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import httpx
    import yaml
except ImportError:
    print("Required: pip install httpx pyyaml")
    sys.exit(1)

MOCK_LLM_URL = os.environ.get("MOCK_LLM_URL", "http://localhost:8090")
SCENARIOS_DIR = Path(__file__).parent.parent / "tests" / "e2e" / "scenarios"
GOLDEN_DIR = Path(__file__).parent.parent / "tests" / "e2e" / "golden"
CANONICAL_CAPTURE_DIR = Path(__file__).parent.parent / "ui" / "src" / "lib" / "chat-v2" / "captures"


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/promote_e2e_scenario.py <scenario-name>")
        return 1

    scenario_name = sys.argv[1].strip()
    if not scenario_name:
        print("Error: scenario name cannot be empty")
        return 1

    print(f"Promoting scenario: {scenario_name!r}")

    # Fetch history from mock-llm
    try:
        resp = httpx.get(f"{MOCK_LLM_URL}/__mock/history?limit=1", timeout=5.0)
        resp.raise_for_status()
        history = resp.json().get("history", [])
    except Exception as exc:
        print(f"Error fetching mock-llm history: {exc}")
        print(f"Is the mock-llm server running at {MOCK_LLM_URL}?")
        return 1

    if not history:
        print("No history found. Send a message first to capture a scenario.")
        return 1

    last_entry = history[-1]
    scenario_id = last_entry.get("scenario_id")

    if not scenario_id:
        print("Last request did not match any scenario.")
        return 1

    # Fetch the scenario definition
    try:
        resp = httpx.get(f"{MOCK_LLM_URL}/__mock/scenarios", timeout=5.0)
        resp.raise_for_status()
        scenarios = {s["id"]: s for s in resp.json().get("scenarios", [])}
    except Exception as exc:
        print(f"Error fetching scenarios: {exc}")
        return 1

    if scenario_id not in scenarios:
        print(f"Scenario {scenario_id!r} not found in catalog.")
        return 1

    # Save scenario YAML
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    scenario_path = SCENARIOS_DIR / f"{scenario_name}.yaml"

    # Build a minimal scenario file
    scenario_data = {
        "id": scenario_name,
        "description": f"Promoted from interactive session (original: {scenario_id})",
        "trigger": f"scenario:{scenario_name}",
        "promoted_from": scenario_id,
    }

    with scenario_path.open("w") as f:
        yaml.dump(scenario_data, f, default_flow_style=False)

    print(f"Saved scenario: {scenario_path}")
    print("  Note: Edit the YAML to add the full step sequence.")

    # Promote the canonical ChatV2 records from the live WS golden.  The
    # replay target discovers every JSONL file in this directory, so promotion
    # must write the corpus consumed by production sync-engine replay rather
    # than a disconnected placeholder.
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path = GOLDEN_DIR / f"{scenario_name}.jsonl"

    if golden_path.exists():
        events = [json.loads(line) for line in golden_path.read_text().splitlines() if line.strip()]
        canonical = [
            event
            for event in events
            if event.get("type") == "chat_v2_frame"
            or event.get("type") in {"snapshot", "sync", "frame"}
        ]
        if not any(event.get("type") == "chat_v2_frame" for event in canonical):
            print(f"\nGolden file has no canonical chat_v2_frame records: {golden_path}")
            print("Run the live ChatV2 capture first; no replay capture was promoted.")
            return 1
        CANONICAL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        capture_path = CANONICAL_CAPTURE_DIR / f"promoted-{scenario_name}.jsonl"
        with capture_path.open("w") as f:
            for event in canonical:
                f.write(json.dumps(event) + "\n")
        print(f"Promoted canonical capture: {capture_path}")
    else:
        print(f"\nNo golden file found at {golden_path}.")
        print("Run the live e2e tests to capture the canonical stream:")
        print(f"  uv run pytest tests/e2e/ -m e2e -k {scenario_name}")
        return 1

    print(f"\nScenario {scenario_name!r} promoted successfully.")
    print("Next steps:")
    print(f"  1. Edit {scenario_path} to add the full step sequence.")
    print(f"  2. Run: uv run pytest tests/e2e/ -m e2e -k {scenario_name}")
    print("  3. Run: make e2e-events-replay")

    return 0


if __name__ == "__main__":
    sys.exit(main())
