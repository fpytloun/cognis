from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "cognis"
EXAMPLES = CHART / "examples"


def _helm(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


def _render(values: str) -> list[dict[str, Any]]:
    result = _helm(
        "template",
        "cognis",
        str(CHART),
        "--namespace",
        "cognis",
        "-f",
        str(EXAMPLES / values),
    )
    return [item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)]


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _one(resources: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [resource for resource in resources if resource.get("kind") == kind]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(
    "values",
    [
        "values-simple.yaml",
        "values-single-production.yaml",
        "values-ha.yaml",
        "values-ha-redis.yaml",
    ],
)
def test_example_values_validate_and_render(values: str) -> None:
    schema = json.loads((CHART / "values.schema.json").read_text())
    document = _merge(
        yaml.safe_load((CHART / "values.yaml").read_text()),
        yaml.safe_load((EXAMPLES / values).read_text()),
    )
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda item: item.path)
    assert errors == []
    assert _render(values)


def test_ha_renders_one_public_and_one_headless_service() -> None:
    resources = _render("values-ha.yaml")
    services = [resource for resource in resources if resource.get("kind") == "Service"]
    assert len(services) == 2
    assert sum(service["spec"].get("clusterIP") == "None" for service in services) == 1
    assert len([resource for resource in resources if resource.get("kind") == "Ingress"]) == 1


def test_ha_replaces_legacy_deployment_workload_with_statefulset() -> None:
    resources = _render("values-ha.yaml")
    workloads = [
        resource for resource in resources if resource.get("kind") in {"Deployment", "StatefulSet"}
    ]
    assert [(resource["kind"], resource["metadata"]["name"]) for resource in workloads] == [
        ("StatefulSet", "cognis-cognis")
    ]


def test_ha_controller_rollout_and_runtime_contract() -> None:
    resources = _render("values-ha.yaml")
    assert not [resource for resource in resources if resource.get("kind") == "Deployment"]
    statefulset = _one(resources, "StatefulSet")
    spec = statefulset["spec"]
    assert spec["replicas"] == 2
    assert spec["serviceName"] == "cognis-cognis-internal"
    assert spec["podManagementPolicy"] == "Parallel"
    assert spec["updateStrategy"] == {"type": "RollingUpdate"}
    assert spec["minReadySeconds"] == 5
    pod_spec = spec["template"]["spec"]
    assert pod_spec["enableServiceLinks"] is False
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["serviceAccountName"] == "default"
    assert pod_spec["terminationGracePeriodSeconds"] == 75
    assert pod_spec["topologySpreadConstraints"]
    _one(resources, "PodDisruptionBudget")
    container = pod_spec["containers"][0]
    assert container["startupProbe"]["httpGet"]["path"] == "/api/livez"
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/api/livez"
    assert container["lifecycle"]["preStop"]["exec"]["command"] == ["sh", "-c", "sleep 5"]
    assert container["securityContext"]
    env = {item["name"]: item for item in container["env"]}
    assert env["COGNIS_RUNTIME_MODE"]["value"] == "ha"
    assert env["COGNIS_SCHEMA_MODE"]["value"] == "validate"
    assert env["COGNIS_CONTROLLER_ID"]["value"] == "$(POD_NAME)"
    assert env["COGNIS_CONTROLLER_INTERNAL_URL"]["value"] == (
        "http://$(POD_NAME).cognis-cognis-internal.$(POD_NAMESPACE).svc:8080"
    )
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "cognis-database"
    assert env["COGNIS_ARTIFACT_BACKEND"]["value"] == "s3"
    assert env["COGNIS_TOOL_OUTPUT_BACKEND"]["value"] == "s3"
    assert env["COGNIS_EVENT_CACHE_TTL_SECONDS"]["value"] == "3600"
    assert env["COGNIS_EVENT_CACHE_SLIDING_TTL"]["value"] == "true"
    assert env["COGNIS_EVENT_CACHE_COMPRESSION_ENABLED"]["value"] == "true"
    assert env["COGNIS_EVENT_CACHE_COMPRESSION_THRESHOLD_BYTES"]["value"] == "65536"
    assert env["COGNIS_EVENT_CACHE_MAX_VALUE_BYTES"]["value"] == "2097152"
    data_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "data")
    assert data_volume["emptyDir"] == {}
    assert container["volumeMounts"][0] == {"name": "data", "mountPath": "/data"}
    crypto_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "crypto")
    assert crypto_volume["secret"]["defaultMode"] == 0o440


def test_ha_migration_job_is_preinstall_preupgrade_hook() -> None:
    job = _one(_render("values-ha.yaml"), "Job")
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == "default"
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert container["command"] == ["/bin/sh", "-ec"]
    migration_script = container["args"][0]
    assert "postgresql+asyncpg://*" in migration_script
    assert "exec cognis-controller db upgrade" in migration_script
    rejected = subprocess.run(
        container["command"] + container["args"],
        env={**os.environ, "DATABASE_URL": "sqlite+aiosqlite:////tmp/cognis.db"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "requires postgresql+asyncpg DATABASE_URL" in rejected.stderr
    env = {item["name"]: item for item in container["env"]}
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "cognis-database"
    assert env["COGNIS_CONTROLLER_ID"]["value"] == "migration-hook"
    assert env["COGNIS_SCHEMA_MODE"]["value"] == "validate"


def test_migration_job_renders_image_pull_secrets() -> None:
    result = _helm(
        "template",
        "cognis",
        str(CHART),
        "-f",
        str(EXAMPLES / "values-ha.yaml"),
        "--set",
        "imagePullSecrets[0].name=private-registry",
    )
    resources = [item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)]
    job = _one(resources, "Job")
    assert job["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "private-registry"}]


def test_simple_sqlite_uses_auto_bootstrap_without_migration_job() -> None:
    resources = _render("values-simple.yaml")
    assert not [resource for resource in resources if resource.get("kind") == "Job"]
    assert not [resource for resource in resources if resource.get("kind") == "StatefulSet"]
    deployment = _one(resources, "Deployment")
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }
    assert "minReadySeconds" not in deployment["spec"]
    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["COGNIS_SCHEMA_MODE"]["value"] == "auto"
    assert "DATABASE_URL" not in env


def test_schema_rejects_sqlite_migration_job() -> None:
    schema = json.loads((CHART / "values.schema.json").read_text())
    document = yaml.safe_load((CHART / "values.yaml").read_text())
    document["migration"]["enabled"] = True
    errors = list(Draft202012Validator(schema).iter_errors(document))
    assert any(tuple(error.path) == ("database", "type") for error in errors)
    assert any(tuple(error.path) == ("database", "existingSecret") for error in errors)


def test_template_rejects_sqlite_migration_job() -> None:
    result = _helm(
        "template",
        "cognis",
        str(CHART),
        "--skip-schema-validation",
        "--set",
        "migration.enabled=true",
        check=False,
    )
    assert result.returncode != 0
    assert "migration.enabled=true requires database.type=postgresql" in result.stderr


@pytest.mark.parametrize(
    ("set_values", "message"),
    [
        (
            ["mode=ha", "replicaCount=2", "database.type=postgresql"],
            "/database/existingSecret",
        ),
        (
            [
                "mode=ha",
                "replicaCount=2",
                "database.type=postgresql",
                "database.existingSecret=db",
                "crypto.requireExternal=true",
                "crypto.existingSecret=crypto",
                "artifacts.backend=s3",
                "artifacts.s3.endpoint=http://s3",
                "artifacts.s3.existingSecret=s3",
                "toolOutputs.backend=s3",
                "toolOutputs.s3.endpoint=http://s3",
                "toolOutputs.s3.existingSecret=s3",
                "persistence.enabled=false",
            ],
            "/migration/enabled",
        ),
        (["replicaCount=2"], "mode"),
    ],
)
def test_invalid_ha_values_are_rejected(set_values: list[str], message: str) -> None:
    args = ["template", "cognis", str(CHART)]
    for value in set_values:
        args.extend(["--set", value])
    result = _helm(*args, check=False)
    assert result.returncode != 0
    assert message in result.stderr
