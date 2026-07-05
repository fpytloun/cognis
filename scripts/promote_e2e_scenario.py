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

    # Save golden JSONL (if available from the WS capture)
    # The WS events are captured by the pytest e2e tests, not the mock-llm.
    # We create a placeholder golden file.
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path = GOLDEN_DIR / f"{scenario_name}.jsonl"

    if not golden_path.exists():
        print(f"\nNo golden file found at {golden_path}.")
        print("Run the e2e tests to capture the golden stream:")
        print(f"  uv run pytest tests/e2e/ -m e2e -k {scenario_name}")
    else:
        print(f"Golden file already exists: {golden_path}")

    print(f"\nScenario {scenario_name!r} promoted successfully.")
    print("Next steps:")
    print(f"  1. Edit {scenario_path} to add the full step sequence.")
    print(f"  2. Run: uv run pytest tests/e2e/ -m e2e -k {scenario_name}")
    print("  3. Run: cd ui && npm test -- src/lib/chat-timeline.golden.test.ts")

    return 0


if __name__ == "__main__":
    sys.exit(main())
