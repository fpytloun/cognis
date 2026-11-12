"""Frozen usage exports remain generated, strict, and source-identifiable."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from scripts import export_usage_query_contract as exporter

ROOT = exporter.ROOT
CONTRACTS = ROOT / "ui/e2e/usage/contracts"
VALIDATOR = ROOT / "ui/e2e/usage/validate-fixtures.py"
SOURCE = "3520924c472de4ea20cc69e918f58015fbe475d6"
TREE = "ee87d2c4ed0a8b61ff10ac70d22ba5c0478802f0"
START = datetime(2026, 10, 1, tzinfo=UTC)
models = exporter.query_models


@pytest.fixture(scope="module")
def publication() -> tuple[dict[str, Any], dict[str, Any]]:
    pin = json.loads((CONTRACTS / "current.json").read_bytes())
    directory = CONTRACTS / pin["directory"]
    return pin, json.loads((directory / "usage-query-contract.json").read_bytes())


def _validator_module() -> Any:
    spec = importlib.util.spec_from_file_location("usage_fixture_validator", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate(packets: list[dict[str, Any]], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *arguments],
        input=json.dumps(packets),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT / "ui",
    )


@pytest.fixture(scope="module")
def dto_packets() -> list[dict[str, Any]]:
    metrics = models.UsageMetrics(
        finalized_attempts=3,
        observed_attempts=3,
        unpriced_attempts=1,
        partially_priced_attempts=1,
        unallocatable_cost_attempts=1,
        costs=[
            models.CostSubtotal(
                known_amount="2.50",
                currency="USD",
                basis="api_estimate",
                quality="partial",
                priced_attempts=1,
                partially_priced_attempts=1,
                unpriced_attempts=1,
            )
        ],
    )
    meta = models.UsageMeta(scope=models.UsageScope(), precision="day", complete=False)
    breakdowns = models.UsageCostBreakdowns(
        dimensions=[
            models.UsageCostBreakdown(
                dimension=dimension,
                groups=[models.UsageGroup(key=None, label=None, metrics=metrics)],
            )
            for dimension in ("subject", "agent", "provider", "model")
        ]
    )
    selector = models.UsageSummarySelector(kind="execution", id="usage-contract-execution")
    dtos = [
        models.UsageQuery(start=START, end=START + timedelta(days=1), view="cost_breakdowns"),
        models.UsageQuery(start=START, end=START + timedelta(days=1)),
        models.UsageQueryResponse(meta=meta, total=metrics, cost_breakdowns=breakdowns),
        models.UsageQueryResponse(meta=meta, total=None),
        *[
            models.UsageSessionPage(
                meta=meta,
                items=[
                    models.UsageSessionItem(
                        session_type=kind,
                        metrics=metrics,
                        drill_filters=models.UsageFilters(),
                    )
                ],
            )
            for kind in ("cognis", "proxy")
        ],
        models.UsageSummaryRequest(items=[selector], inclusion="direct"),
        models.UsageSummaryResponse(
            meta=meta,
            items=[
                models.UsageSummaryItem(
                    selector=selector,
                    available=True,
                    inclusion="direct",
                    physical_sessions=2,
                    metrics=metrics,
                    availability=models.UsageAvailability(resolution="retained"),
                )
            ],
        ),
    ]
    return [{"schema": type(dto).__name__, "value": dto.model_dump(mode="json")} for dto in dtos]


def test_usage_export_runtime_provenance() -> None:
    assert sys.version_info[:2] == (3, 12)
    assert Path(models.__file__).resolve() == ROOT / exporter.MODEL_SOURCES[0]
    assert Path(exporter.shared_usage.__file__).resolve() == ROOT / exporter.MODEL_SOURCES[1]
    print(
        json.dumps(
            {
                "cwd": str(Path.cwd()),
                "python": sys.executable,
                "python_version": sys.version,
                "model_files": [models.__file__, exporter.shared_usage.__file__],
                "dependencies": {
                    name: version(name) for name in ("pydantic", "jsonschema", "pytest")
                },
            }
        )
    )


def test_usage_export_deterministic_bundle_and_generated_pins(publication: tuple) -> None:
    pin, _ = publication
    first = exporter.bundle_bytes(SOURCE)
    assert exporter.bundle_bytes(SOURCE) == first
    assert (CONTRACTS / "current.json").read_bytes() == exporter.current_pin_bytes(first)
    assert pin["directory"] == SOURCE[:12]
    for name, raw in first.items():
        assert (CONTRACTS / pin["directory"] / name).read_bytes() == raw
        assert pin["sha256"][name] == hashlib.sha256(raw).hexdigest()
    manifest = json.loads(first["provenance.json"])
    assert manifest["model_source_commit"] == SOURCE
    assert manifest["model_source_tree"] == TREE
    assert manifest["generator"]["request_mode"] == "validation"
    assert manifest["generator"]["response_mode"] == "serialization"


@pytest.mark.parametrize(
    ("name", "mode"),
    [(name, "validation") for name in exporter._REQUESTS]
    + [(name, "serialization") for name in exporter._RESPONSES],
)
def test_usage_export_matches_each_actual_model(publication: tuple, name: str, mode: str) -> None:
    _, schemas = publication
    schema = schemas[name]
    assert schema == getattr(models, name).model_json_schema(mode=mode)
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False


def test_usage_export_validates_all_affected_real_dto_packets(dto_packets: list) -> None:
    result = _validate(dto_packets)
    assert result.returncode == 0, result.stderr
    assert "Validated 8 packets" in result.stdout
    assert "current public contract" in result.stdout


@pytest.mark.parametrize(
    ("schema", "path", "value", "message"),
    [
        ("UsageQuery", ("view",), "unregistered_cost_view", "is not one of"),
        ("UsageQueryResponse", ("unexpected_contract_field",), True, "Additional properties"),
        ("UsageQueryResponse", ("total", "partially_priced_attempts"), -1, "minimum"),
        ("UsageQueryResponse", ("total", "unallocatable_cost_attempts"), 2**53, "maximum"),
        (
            "UsageQueryResponse",
            ("total", "costs", 0, "known_amount"),
            2.5,
            "is not of type",
        ),
        (
            "UsageSessionPage",
            ("items", 0, "metrics", "costs", 0, "partially_priced_attempts"),
            -1,
            "minimum",
        ),
    ],
)
def test_usage_export_retains_strict_validation(
    dto_packets: list, schema: str, path: tuple, value: object, message: str
) -> None:
    packet = copy.deepcopy(next(packet for packet in dto_packets if packet["schema"] == schema))
    target = packet["value"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = _validate([packet])
    assert result.returncode == 1
    assert message in result.stderr


def test_usage_export_preserves_historical_bytes_and_legacy_payloads() -> None:
    validator = _validator_module()
    for name, expected in validator.HISTORICAL_HASHES.items():
        assert hashlib.sha256((CONTRACTS / name).read_bytes()).hexdigest() == expected
    legacy = [
        {"schema": "UsageCatalogRequest", "value": {"scope": {"mode": "personal"}}},
        {
            "schema": "UsageQueryResponse",
            "value": {
                "meta": {"scope": {"mode": "personal"}, "precision": "day", "complete": False},
                "total": None,
            },
        },
    ]
    for contract in ("current", "historical"):
        result = _validate(legacy, "--contract", contract)
        assert result.returncode == 0, result.stderr


def test_usage_export_does_not_relabel_historical_schema(dto_packets: list) -> None:
    result = _validate(dto_packets[:1], "--contract", "historical")
    assert result.returncode == 1
    assert "cost_breakdowns" in result.stderr
    assert "is not one of" in result.stderr


@pytest.mark.parametrize(
    "name", ("usage-query-contract.json", "usage-query-api.md", "provenance.json")
)
def test_usage_export_rejects_changed_artifact_bytes(
    publication: tuple, tmp_path: Path, name: str
) -> None:
    pin, _ = publication
    directory = tmp_path / pin["directory"]
    directory.mkdir()
    (tmp_path / "current.json").write_bytes((CONTRACTS / "current.json").read_bytes())
    for filename in pin["sha256"]:
        raw = (CONTRACTS / pin["directory"] / filename).read_bytes()
        (directory / filename).write_bytes(raw + b"\n" if filename == name else raw)
    with pytest.raises(ValueError, match=f"Contract SHA-256 mismatch: {name}"):
        _validator_module().load_schemas("current", tmp_path)


@pytest.mark.parametrize("relative", (*exporter.MODEL_SOURCES, exporter.SPEC_SOURCE))
def test_usage_export_refuses_uncommitted_source(
    monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    git: Callable[..., bytes] = exporter._git

    def different_source(*arguments: str) -> bytes:
        if arguments == ("show", f"{SOURCE}:{relative}"):
            return b"Not the checked-out source bytes.\n"
        return git(*arguments)

    monkeypatch.setattr(exporter, "_git", different_source)
    with pytest.raises(RuntimeError, match="Checkout does not match committed source"):
        exporter.bundle_bytes(SOURCE)


def test_usage_export_refuses_imports_from_another_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "__file__", str(ROOT.parent / "query_models.py"))
    with pytest.raises(RuntimeError, match="Model import is outside the exporting checkout"):
        exporter.bundle_bytes(SOURCE)
