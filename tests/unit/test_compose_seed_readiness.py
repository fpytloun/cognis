"""Seed processes must not race controller schema bootstrap."""

from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    ("filename", "service"),
    [("compose.local.yml", "seed"), ("compose.e2e.yml", "seed-e2e")],
)
def test_seed_waits_for_controller_bootstrap(filename: str, service: str) -> None:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / filename).read_text())
    assert config["services"][service]["depends_on"]["cognis"]["condition"] == "service_healthy"
    assert "/api/health" in (root / "Dockerfile").read_text()
