"""Evaluate embedded tag policy and assert workflow trust/event boundaries."""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


def embedded_python(name: str) -> str:
    text = (ROOT / ".github/workflows" / name).read_text()
    return text.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]


class WorkflowTests(unittest.TestCase):
    def test_all_embedded_python_blocks_compile_with_actual_yaml_indentation(self):
        for name, expected in (("macos-release.yml", 4), ("project-release-gate.yml", 1)):
            text = (ROOT / ".github/workflows" / name).read_text()
            blocks = re.findall(r"(?ms)^([ ]*)python3 - <<'PY'\n(.*?)^\1PY$", text)
            self.assertEqual(len(blocks), expected)
            for index, (indent, block) in enumerate(blocks):
                source = "\n".join(line.removeprefix(indent) for line in block.splitlines())
                with self.subTest(workflow=name, block=index):
                    compile(source, f"{name}:python:{index}", "exec")

    def test_actual_semver_gate_not_prefix_matching(self):
        import textwrap

        source = textwrap.dedent(embedded_python("project-release-gate.yml"))
        for tag, project in (
            ("v0.17.1", True),
            ("v1.0.0-rc.1", True),
            ("v1.0.0+build.7", True),
            ("macos-v0.18.0", False),
            ("v1.2.3evil", False),
            ("v01.2.3", False),
            ("v1.2.3-01", False),
            ("v1.2", False),
        ):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output"
                with patch.dict(os.environ, {"RELEASE_TAG": tag, "GITHUB_OUTPUT": str(output)}):
                    exec(source, {})
                self.assertEqual(output.read_text().strip(), f"project={str(project).lower()}")

    def test_mac_plan_requires_explicit_class_and_namespace(self):
        import textwrap

        source = textwrap.dedent(embedded_python("macos-release.yml"))
        for tag, mode, valid in (
            ("macos-v0.18.0", "adhoc", True),
            ("macos-v0.18.0", "release", True),
            ("v0.17.1", "adhoc", False),
            ("macos-v01.18.0", "adhoc", False),
            ("macos-v0.18.0", "development", False),
            ("macos-v0.18.0", "", False),
        ):
            with self.subTest(tag=tag, mode=mode), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output"
                with patch.dict(
                    os.environ, {"RELEASE_TAG": tag, "MODE": mode, "GITHUB_OUTPUT": str(output)}
                ):
                    if valid:
                        exec(source, {})
                        self.assertIn(f"mode={mode}", output.read_text())
                    else:
                        with self.assertRaises(AssertionError):
                            exec(source, {})

    def test_publication_and_secret_isolation(self):
        directory = ROOT / ".github/workflows"
        mac = (directory / "macos-release.yml").read_text()
        self.assertNotIn("\n  push:", mac)
        self.assertNotIn("pull_request", mac)
        secret_step = mac.split("- name: Configure ephemeral release identity", 1)[1]
        self.assertTrue(secret_step.lstrip().startswith("if: needs.plan.outputs.mode == 'release'"))
        self.assertEqual(mac.count("secrets."), 3)
        self.assertNotIn("--clobber", mac)
        self.assertNotIn("gh release create", mac)
        self.assertNotIn("gh release edit", mac)
        self.assertIn(".isDraft == true and .isPrerelease == true", mac)
        self.assertIn(".tagName == env.RELEASE_TAG", mac)
        self.assertIn('record["identity"]["source"] == source', mac)
        docker = (directory / "docker-publish.yml").read_text()
        self.assertEqual(docker.count("if: needs.release-gate.outputs.project == 'true'"), 2)
        python = (directory / "python-publish.yml").read_text()
        self.assertIn("needs.release-gate.outputs.project == 'true'", python)
        self.assertIn("needs: common-build", python)
        self.assertIn("needs: common-publish", python)

    def test_gui_waiver_is_explicit_and_rejected_for_trusted_releases(self):
        import textwrap

        source = textwrap.dedent(embedded_python("macos-release.yml"))
        for mode, waiver, valid in (
            ("adhoc", "false", True),
            ("adhoc", "true", True),
            ("release", "false", True),
            ("release", "true", False),
            ("adhoc", "unrecognized", False),
        ):
            with self.subTest(mode=mode, waiver=waiver), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output"
                with patch.dict(
                    os.environ,
                    {
                        "RELEASE_TAG": "macos-v0.18.0",
                        "MODE": mode,
                        "SKIP_GUI_TESTS": waiver,
                        "GITHUB_OUTPUT": str(output),
                    },
                ):
                    if valid:
                        exec(source, {})
                    else:
                        with self.assertRaises(AssertionError):
                            exec(source, {})
                self.assertEqual(output.exists(), valid)
        workflow = (ROOT / ".github/workflows/macos-release.yml").read_text()
        self.assertIn("default: false", workflow)
        self.assertIn("if: ${{ !inputs.skip_gui_tests }}", workflow)
        self.assertIn("not a passing GUI qualification", workflow)

    def test_executor_asset_dispatch_requires_a_canonical_tag(self):
        import subprocess
        import textwrap

        workflow = (ROOT / ".github/workflows/executor-release.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        source = textwrap.dedent(workflow.split("run: |\n", 1)[1].split("\n  build:", 1)[0])
        version = tomllib.loads((ROOT / "packages/executor/pyproject.toml").read_text())["project"][
            "version"
        ]
        major, minor, patch = map(int, version.split("."))
        mismatched_tag = f"v{major}.{minor}.{patch + 1}"
        for ref_type, tag, valid in (
            ("tag", f"v{version}", True),
            ("branch", f"v{version}", False),
            ("tag", version, False),
            ("tag", mismatched_tag, False),
        ):
            with self.subTest(ref_type=ref_type, tag=tag):
                result = subprocess.run(
                    ["bash", "-e", "-c", source],
                    cwd=ROOT,
                    env={**os.environ, "GITHUB_REF_TYPE": ref_type, "RELEASE_TAG": tag},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, valid, result.stderr)

    def test_unit_waiver_is_explicit_and_rejected_for_trusted_releases(self):
        import textwrap

        source = textwrap.dedent(embedded_python("macos-release.yml"))
        for mode, waiver, valid in (
            ("adhoc", "false", True),
            ("adhoc", "true", True),
            ("release", "false", True),
            ("release", "true", False),
            ("adhoc", "unrecognized", False),
        ):
            with self.subTest(mode=mode, waiver=waiver), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output"
                with patch.dict(
                    os.environ,
                    {
                        "RELEASE_TAG": "macos-v0.18.1",
                        "MODE": mode,
                        "SKIP_GUI_TESTS": "false",
                        "SKIP_UNIT_TESTS": waiver,
                        "GITHUB_OUTPUT": str(output),
                    },
                ):
                    if valid:
                        exec(source, {})
                    else:
                        with self.assertRaises(AssertionError):
                            exec(source, {})
                self.assertEqual(output.exists(), valid)

    def test_packaging_checks_isolate_the_release_flavor(self):
        import shlex
        import subprocess

        workflow = (ROOT / ".github/workflows/macos-release.yml").read_text()
        command = next(
            line.strip() for line in workflow.splitlines() if "-m unittest discover" in line
        )
        result = subprocess.run(
            [*shlex.split(command), "-k", "acceptance_bundle_uses_only_isolated_m2_origin"],
            cwd=ROOT,
            env={**os.environ, "COGNIS_PACKAGE_FLAVOR": "standard"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
