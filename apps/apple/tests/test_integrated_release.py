"""Integration invariants for the Mac prerelease candidate."""

from __future__ import annotations

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

APPLE = Path(__file__).resolve().parents[1]
ROOT = APPLE.parents[1]
sys.path.insert(0, str(APPLE))
sign_bundle = __import__("sign_bundle")


class IntegratedReleaseTests(unittest.TestCase):
    def test_canonical_workspace_versions_remain_aligned(self):
        version = json.loads((ROOT / "ui/package.json").read_text())["version"]
        self.assertRegex(version, r"\A(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
        for relative in (
            "pyproject.toml",
            "packages/common/pyproject.toml",
            "packages/executor/pyproject.toml",
        ):
            package = tomllib.loads((ROOT / relative).read_text())["project"]
            self.assertEqual(package["version"], version)
            if relative != "packages/common/pyproject.toml":
                self.assertIn(f"cognis-common=={version}", package["dependencies"])
        lock = json.loads((ROOT / "ui/package-lock.json").read_text())
        self.assertEqual(lock["version"], version)
        self.assertEqual(lock["packages"][""]["version"], version)
        packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        self.assertEqual(
            {
                p["version"]
                for p in packages
                if p["name"] in {"cognis-controller", "cognis-common", "cognis-executor"}
            },
            {version},
        )
        self.assertIn(
            f'__version__ = "{version}"',
            (ROOT / "packages/common/src/cognis/__init__.py").read_text(),
        )

    def test_adhoc_modes_clear_entitlements_without_changing_release_signing(self):
        modes = (
            {"COGNIS_SIGNING_MODE": "development"},
            {"COGNIS_SIGNING_MODE": "adhoc", "COGNIS_RELEASE_BUILD": "1"},
            {
                "COGNIS_SIGNING_MODE": "release",
                "COGNIS_RELEASE_BUILD": "1",
                "COGNIS_SIGNING_IDENTITY": "A" * 40,
                "COGNIS_SIGNING_TEAM": "TESTONLY01",
                "COGNIS_NOTARY_PROFILE": "test-notary-profile",
            },
        )
        for env in modes:
            with (
                self.subTest(mode=env["COGNIS_SIGNING_MODE"]),
                tempfile.TemporaryDirectory() as tmp,
            ):
                app = Path(tmp) / "Cognis.app"
                app.mkdir()
                with (
                    patch.object(sign_bundle.sys, "platform", "darwin"),
                    patch.object(sign_bundle, "nested_code", return_value=[app]),
                    patch.object(sign_bundle, "verify_signature"),
                    patch.object(sign_bundle, "run", return_value="A" * 40) as command,
                ):
                    sign_bundle.sign_bundle(app, env)
                args = command.call_args.args
                self.assertEqual("--entitlements" in args, env["COGNIS_SIGNING_MODE"] != "release")
                if "--entitlements" in args:
                    self.assertEqual(
                        args[args.index("--entitlements") + 1],
                        str(APPLE / "adhoc.entitlements"),
                    )


if __name__ == "__main__":
    unittest.main()
