import importlib.util
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SPEC = importlib.util.spec_from_file_location(
    "package_info", Path(__file__).resolve().parents[1] / "package_info.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IdentityTests(unittest.TestCase):
    def test_distinct_package_flavors_and_no_preview_acceptance_feed_or_origin_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Info.plist"
            for flavor, (name, bundle_id) in MODULE.FLAVORS.items():
                with patch.dict(os.environ, {"COGNIS_PACKAGE_FLAVOR": flavor}, clear=True):
                    MODULE.write_info(path, {"identity": MODULE.identity(), "assets": {}})
                info = plistlib.loads(path.read_bytes())
                self.assertEqual(info["CFBundleName"], name)
                self.assertEqual(info["CFBundleIdentifier"], bundle_id)
                self.assertEqual(
                    info.get("CFBundleURLTypes", []),
                    []
                    if flavor == "acceptance"
                    else [
                        {
                            "CFBundleURLName": bundle_id,
                            "CFBundleURLSchemes": [
                                "cognis" if flavor == "standard" else "cognis-preview"
                            ],
                            "CFBundleTypeRole": "Viewer",
                        }
                    ],
                )
                self.assertEqual(info["CognisBuildIdentity"]["flavor"], flavor)
                self.assertEqual("CognisAcceptanceOrigin" in info, flavor == "acceptance")
            for flavor in ("preview", "acceptance"):
                # Inherited publisher inputs cannot make a test bundle contact a feed.
                with patch.dict(
                    os.environ,
                    {"COGNIS_PACKAGE_FLAVOR": flavor, "COGNIS_UPDATE_FEED": "not-a-feed"},
                    clear=True,
                ):
                    MODULE.write_info(path, {"identity": MODULE.identity(), "assets": {}})
                info = plistlib.loads(path.read_bytes())
                self.assertNotIn("SUFeedURL", info)
                self.assertNotIn("SUPublicEDKey", info)

    def test_conflicting_flavors_rejected_and_flavor_change_invalidates_prebuilt_manifest(self):
        with (
            patch.dict(
                os.environ,
                {"COGNIS_PACKAGE_FLAVOR": "preview", "COGNIS_ACCEPTANCE_BUILD": "1"},
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            MODULE.package_flavor()
        with (
            patch.dict(os.environ, {"COGNIS_PACKAGE_FLAVOR": "unknown"}, clear=True),
            self.assertRaises(ValueError),
        ):
            MODULE.package_flavor()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Info.plist"
            with patch.dict(os.environ, {"COGNIS_PACKAGE_FLAVOR": "acceptance"}, clear=True):
                recorded = {"identity": MODULE.identity(), "assets": {}}
            with (
                patch.dict(os.environ, {"COGNIS_PACKAGE_FLAVOR": "preview"}, clear=True),
                self.assertRaises(ValueError),
            ):
                MODULE.write_info(path, recorded)

    def test_acceptance_bundle_uses_only_isolated_m2_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Info.plist"
            with patch.dict(os.environ, {"COGNIS_ACCEPTANCE_BUILD": "1"}):
                MODULE.write_info(path, {"identity": MODULE.identity(), "assets": {}})
            info = plistlib.loads(path.read_bytes())
            self.assertEqual(info["CognisAcceptanceOrigin"], "http://127.0.0.1:18763")
            self.assertNotEqual(info["CFBundleIdentifier"], "cz.fpy.cognis.companion")

    def test_no_publisher_is_honest_and_partial_configuration_fails(self):
        self.assertEqual(MODULE.publisher({}), {})
        with self.assertRaises(ValueError):
            MODULE.publisher({"COGNIS_UPDATE_FEED": "http://localhost/feed"})
        with self.assertRaises(ValueError):
            MODULE.publisher({"COGNIS_UPDATE_PUBLIC_KEY": "not-a-key"})

    def test_identity_is_stable_and_release_rejects_dirty_or_missing_inputs(self):
        self.assertEqual(MODULE.identity(), MODULE.identity())
        with (
            patch.dict(os.environ, {"COGNIS_RELEASE_BUILD": "1"}, clear=True),
            self.assertRaises(ValueError),
        ):
            MODULE.identity()

    def test_prebuilt_requires_identical_source_and_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stamp = root / "stamp.json"
            with patch.object(MODULE, "verify", return_value={"index.html": "hash"}):
                MODULE.stamp_web(root, stamp)
                MODULE.check_web(root, stamp)
                with (
                    patch.object(MODULE, "identity", return_value={"source": "changed"}),
                    self.assertRaises(ValueError),
                ):
                    MODULE.check_web(root, stamp)
                with (
                    patch.object(MODULE, "verify", return_value={"index.html": "changed"}),
                    self.assertRaises(ValueError),
                ):
                    MODULE.check_web(root, stamp)
