from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "cognis"


def _render(*args: str) -> list[dict]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [helm, "template", "cognis", str(CHART), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(rendered) if item]


def _workload(documents: list[dict], kind: str) -> dict:
    return next(document for document in documents if document.get("kind") == kind)


@pytest.mark.parametrize(
    ("kind", "mode_args"),
    [
        ("Deployment", ()),
        (
            "StatefulSet",
            (
                "--set",
                "mode=ha",
                "--set",
                "replicaCount=2",
                "--set",
                "database.type=postgresql",
                "--set",
                "database.existingSecret=db",
                "--set",
                "crypto.requireExternal=true",
                "--set",
                "crypto.existingSecret=crypto",
                "--set",
                "artifacts.backend=s3",
                "--set",
                "artifacts.s3.endpoint=http://minio",
                "--set",
                "artifacts.s3.bucket=artifacts",
                "--set",
                "artifacts.s3.existingSecret=s3",
                "--set",
                "toolOutputs.backend=s3",
                "--set",
                "toolOutputs.s3.endpoint=http://minio",
                "--set",
                "toolOutputs.s3.bucket=outputs",
                "--set",
                "toolOutputs.s3.existingSecret=s3",
                "--set",
                "migration.enabled=true",
                "--set",
                "persistence.enabled=false",
                "--set",
                "podDisruptionBudget.enabled=true",
                "--set-json",
                'topologySpreadConstraints=[{"maxSkew":1,"topologyKey":"kubernetes.io/hostname","whenUnsatisfiable":"ScheduleAnyway","labelSelector":{"matchLabels":{"app.kubernetes.io/name":"cognis"}}}]',
            ),
        ),
    ],
)
@pytest.mark.parametrize(("port", "expected"), [(8080, "8080"), (9090, "9090")])
def test_metrics_annotations_follow_service_port(
    kind: str,
    mode_args: tuple[str, ...],
    port: int,
    expected: str,
) -> None:
    documents = _render(*mode_args, "--set", f"service.port={port}")
    annotations = _workload(documents, kind)["spec"]["template"]["metadata"]["annotations"]

    assert annotations["prometheus.io/scrape"] == "true"
    assert annotations["prometheus.io/path"] == "/api/metrics"
    assert annotations["prometheus.io/port"] == expected


def test_user_pod_annotations_can_override_metrics_defaults() -> None:
    documents = _render(
        "--set-string",
        "podAnnotations.prometheus\\.io/port=9191",
        "--set-string",
        "podAnnotations.example\\.com/owner=platform",
    )
    annotations = _workload(documents, "Deployment")["spec"]["template"]["metadata"]["annotations"]

    assert annotations["prometheus.io/port"] == "9191"
    assert annotations["example.com/owner"] == "platform"
