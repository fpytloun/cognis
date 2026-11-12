"""Read-only unsigned package/resource inspection; never installs or signs."""

import argparse
import json
import plistlib
import subprocess
import sys
from pathlib import Path

from package_info import FLAVORS
from verify_web import verify


def verify_package(app: Path, *, mach_o: bool = False) -> None:
    contents = app / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    recorded = json.loads((contents / "Resources/BuildIdentity.json").read_text())
    flavor = info.get("CognisPackageFlavor")
    if flavor not in FLAVORS:
        raise ValueError("Missing or unknown package flavor")
    name, bundle_id = FLAVORS[flavor]
    if info.get("CFBundleIdentifier") != bundle_id or info.get("CFBundleDisplayName") != name:
        raise ValueError("Package flavor/identity mismatch")
    if recorded["identity"].get("flavor") != flavor:
        raise ValueError("Recorded package flavor mismatch")
    expected_urls = (
        []
        if flavor == "acceptance"
        else [
            {
                "CFBundleURLName": bundle_id,
                "CFBundleURLSchemes": ["cognis" if flavor == "standard" else "cognis-preview"],
                "CFBundleTypeRole": "Viewer",
            }
        ]
    )
    if info.get("CFBundleURLTypes", []) != expected_urls:
        raise ValueError("Package URL scheme does not match its flavor")
    if flavor != "acceptance" and "CognisAcceptanceOrigin" in info:
        raise ValueError("Only acceptance may contain a fixed origin")
    if flavor != "standard" and ("SUFeedURL" in info or "SUPublicEDKey" in info):
        raise ValueError("Preview/acceptance must not contain publisher configuration")
    if recorded["identity"] != info["CognisBuildIdentity"]:
        raise ValueError("Native/web identity mismatch")
    if recorded["assets"] != verify(contents / "Resources/Web"):
        raise ValueError("Packaged web assets differ from build identity")
    framework = contents / "Frameworks/Sparkle.framework"
    if (
        not (framework / "Sparkle").is_symlink()
        or not (framework / "Versions/Current").is_symlink()
    ):
        raise ValueError("Sparkle framework symlinks were lost")
    for name in (
        "Sparkle",
        "Autoupdate",
        "Updater.app/Contents/MacOS/Updater",
        "XPCServices/Downloader.xpc/Contents/MacOS/Downloader",
        "XPCServices/Installer.xpc/Contents/MacOS/Installer",
    ):
        path = framework / "Versions/B" / name
        if not path.is_file() or not path.stat().st_mode & 0o111:
            raise ValueError(f"Missing/non-executable Sparkle component: {name}")
    if not (contents / "Resources/Licenses/Sparkle.txt").read_text().strip():
        raise ValueError("Sparkle license missing")
    if flavor == "acceptance" and info.get("CognisAcceptanceOrigin") != "http://127.0.0.1:18763":
        raise ValueError("Acceptance package must target isolated M2")
    if mach_o:
        if sys.platform != "darwin":
            raise ValueError("Mach-O inspection requires macOS")
        binary = contents / "MacOS/Cognis"
        linked = subprocess.check_output(["/usr/bin/otool", "-L", str(binary)], text=True)
        paths = subprocess.check_output(["/usr/bin/otool", "-l", str(binary)], text=True)
        if "@rpath/Sparkle.framework/Versions/B/Sparkle" not in linked:
            raise ValueError("Sparkle framework linkage missing")
        if "@executable_path/../Frameworks" not in paths:
            raise ValueError("Relative framework runpath missing")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--mach-o", action="store_true")
    args = parser.parse_args()
    verify_package(args.app, mach_o=args.mach_o)
    print("PASS: matching native/web assets and complete Sparkle helper/license layout (unsigned)")
