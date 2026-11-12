"""Installed common-only pricing imports cannot depend on controller or SDK code."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.environ.get("COGNIS_RUN_ISOLATED_PACKAGE_TESTS") != "1",
    reason="set COGNIS_RUN_ISOLATED_PACKAGE_TESTS=1 for installed-wheel acceptance",
)
def test_installed_common_only_pricing_constructor_and_lookup(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheels"
    subprocess.run(
        ["uv", "build", "--package", "cognis-common", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    wheel = next(wheel_dir.glob("cognis_common-*.whl"))
    environment = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    venv = tmp_path / "installed"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv)], check=True, env=environment
    )
    python = venv / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        env=environment,
        capture_output=True,
    )
    subprocess.run(
        ["uv", "pip", "check", "--python", str(python)],
        check=True,
        env=environment,
    )
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            dedent("""
            import importlib.util
            import importlib.metadata
            import inspect
            import sys
            from pathlib import Path
            from cognis.models.config import ModelInfo
            from cognis.models.usage import UsageAttribution, UsageIdentity
            from cognis.models.usage_pricing import (
                UsagePricingLookup, UsagePricingPin, usage_pricing_fingerprint,
            )
            from cognis.providers.llm import usage_capture
            from cognis.providers.llm.usage_binding import UsageBinding

            for package in ("cognis-controller", "cognis-executor"):
                try:
                    importlib.metadata.distribution(package)
                except importlib.metadata.PackageNotFoundError:
                    pass
                else:
                    raise AssertionError(package)
            for absent in ("cognis.store", "cognis.executor.runner", "sqlalchemy", "litellm", "openai"):
                assert importlib.util.find_spec(absent) is None, absent
            assert Path(usage_capture.__file__).resolve().is_relative_to(Path(sys.prefix))
            assert inspect.signature(usage_capture.UsageCapture).parameters[
                "price_lookup"
            ].kind is inspect.Parameter.KEYWORD_ONLY
            info = ModelInfo(model_id="installed-model", tier="priority")
            fingerprint = usage_pricing_fingerprint(
                provider_id="installed-provider", model=info.model_id,
                model_info=info, provider_config={}, service_tier="priority",
            )
            pin = UsagePricingPin("upr_installed_revision", "ups_installed_snapshot")
            def lookup(**key):
                assert key == {
                    "provider_id": "installed-provider",
                    "model": "installed-model",
                    "metadata_fingerprint": fingerprint,
                }
                return pin
            class Sink:
                def try_emit(self, observation):
                    return True
            capture = usage_capture.UsageCapture(
                sink=Sink(), controller_owner_id="installed-controller",
                coverage_epoch_id="installed-coverage", price_lookup=lookup,
            )
            request = capture.request(attribution=UsageAttribution(
                identity=UsageIdentity(
                    principal_type="service", principal_subject="installed-service",
                    usage_subject_kind="service", usage_subject_id="installed-service",
                ), purpose="pricing_test",
            ))
            target = UsageBinding(
                request, "installed-provider", info.model_id,
                model_info=info, provider_config={},
            )
            attempt = target.begin(
                operation="chat", streaming=False, parameters={"service_tier": "priority"},
            )
            assert attempt is not None
            assert attempt.context.selection.captured_price_revision_id == pin.captured_price_revision_id
            assert attempt.context.selection.resolver_snapshot_id == pin.resolver_snapshot_id
            attempt.finish()
        """),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
    )
