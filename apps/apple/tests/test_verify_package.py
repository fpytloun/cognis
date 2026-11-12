import importlib.util
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SPEC = importlib.util.spec_from_file_location(
    "verify_package", Path(__file__).resolve().parents[1] / "verify_package.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PRODUCER_SPEC = importlib.util.spec_from_file_location(
    "package_info", Path(__file__).resolve().parents[1] / "package_info.py"
)
assert PRODUCER_SPEC and PRODUCER_SPEC.loader
PRODUCER = importlib.util.module_from_spec(PRODUCER_SPEC)
PRODUCER_SPEC.loader.exec_module(PRODUCER)


class PackageLayoutTests(unittest.TestCase):
    def test_complete_layout_and_missing_helper_or_asset_mismatch(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(MODULE, "verify", return_value={"index.html": "digest"}),
        ):
            app = Path(directory) / "Cognis.app"
            contents = app / "Contents"
            root = contents / "Frameworks/Sparkle.framework"
            version = root / "Versions/B"
            version.mkdir(parents=True)
            (root / "Versions/Current").symlink_to("B")
            (root / "Sparkle").symlink_to("Versions/Current/Sparkle")
            for name in (
                "Sparkle",
                "Autoupdate",
                "Updater.app/Contents/MacOS/Updater",
                "XPCServices/Downloader.xpc/Contents/MacOS/Downloader",
                "XPCServices/Installer.xpc/Contents/MacOS/Installer",
            ):
                path = version / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            license_path = contents / "Resources/Licenses/Sparkle.txt"
            license_path.parent.mkdir(parents=True)
            license_path.write_text("Fixture license")
            # Exercise the actual producer-to-inspector layout, not a parallel fixture convention.
            recorded = {"identity": PRODUCER.identity(), "assets": {"index.html": "digest"}}
            PRODUCER.write_info(contents / "Info.plist", recorded)
            self.assertTrue((contents / "Resources/BuildIdentity.json").is_file())
            MODULE.verify_package(app)
            info_path = contents / "Info.plist"
            info = plistlib.loads(info_path.read_bytes())
            tampered = {**info, "CFBundleURLTypes": [{"CFBundleURLSchemes": ["cognisbundle"]}]}
            info_path.write_bytes(plistlib.dumps(tampered))
            with self.assertRaisesRegex(ValueError, "URL scheme"):
                MODULE.verify_package(app)
            info_path.write_bytes(plistlib.dumps(info))
            with (
                patch.object(MODULE, "verify", return_value={"index.html": "changed"}),
                self.assertRaises(ValueError),
            ):
                MODULE.verify_package(app)
            (version / "Autoupdate").unlink()
            with self.assertRaises(ValueError):
                MODULE.verify_package(app)
