"""Validate replay expectations against authored scenarios, never captures."""

import json
from pathlib import Path

import yaml


def test_tool_free_replay_contract_matches_scenario_definitions() -> None:
    directory = Path(__file__).parent / "scenarios"
    contracts = json.loads((directory / "replay-contracts.json").read_text())
    declared = contracts["tool_free_scenarios"]
    assert len(declared) == len(set(declared))
    expected = set()
    for path in directory.glob("*.yaml"):
        scenario = yaml.safe_load(path.read_text())
        steps = [step for turn in scenario["turns"] for step in turn["steps"]]
        if not any(step["type"] == "tool_call" for step in steps):
            expected.add(scenario["id"])
    assert set(declared) == expected
