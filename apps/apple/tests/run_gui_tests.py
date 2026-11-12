"""Run SwiftPM XCTest cases in an isolated foreground AppKit host on an unlocked Mac."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
from pathlib import Path


def main() -> int:
    """Build a task-local host and fail closed on missing, skipped, or failing tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--filter", required=True, help="Regular expression matching XCTest case names"
    )
    parser.add_argument("--bundle", default="CompanionWebKitTests")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    package = root / "apps/apple"
    output = root / ".local/native-gui-tests"
    app = output / "Cognis GUI Test Host.app"
    resources = app / "Contents/Resources"
    executable = app / "Contents/MacOS/GUIHost"
    resources.mkdir(parents=True, exist_ok=True)
    executable.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "swift",
            "build",
            "--package-path",
            str(package),
            "--build-tests",
            "--force-resolved-versions",
        ],
        check=True,
    )
    binaries = Path(
        subprocess.check_output(
            ["swift", "build", "--package-path", str(package), "--show-bin-path"], text=True
        ).strip()
    )
    platform = Path(
        subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-platform-path"], text=True
        ).strip()
    )
    frameworks = platform / "Developer/Library/Frameworks"
    subprocess.run(
        [
            "swiftc",
            str(Path(__file__).with_name("GUIHost.swift")),
            "-F",
            str(frameworks),
            "-framework",
            "XCTest",
            "-Xlinker",
            "-rpath",
            "-Xlinker",
            str(frameworks),
            "-Xlinker",
            "-rpath",
            "-Xlinker",
            str(binaries),
            "-Xlinker",
            "-rpath",
            "-Xlinker",
            str(platform / "Developer/usr/lib"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    with (app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleExecutable": "GUIHost",
                "CFBundleIdentifier": "cz.fpy.cognis.tests.gui-host",
                "CFBundleName": "Cognis GUI Test Host",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1",
            },
            stream,
        )
    result_path = output / "result.json"
    result_path.unlink(missing_ok=True)
    environment = {
        key: os.environ[key]
        for key in (
            "COGNIS_DOWNLOAD_FIXTURE",
            "COGNIS_DOWNLOAD_ORIGIN",
            "COGNIS_HTML_COMPONENT_FIXTURE",
            "COGNIS_HTML_TEST_REPO",
            "COGNIS_ARTIFACT_PREVIEW_FIXTURE",
            "COGNIS_TEST_WEB_ROOT",
        )
        if key in os.environ
    }
    (resources / "configuration.json").write_text(
        json.dumps(
            {
                "bundle": str(binaries / f"{args.bundle}.xctest"),
                "pattern": args.filter,
                "result": str(result_path),
                "environment": environment,
            }
        ),
        encoding="utf-8",
    )
    for name in ("stdout.log", "stderr.log"):
        (output / name).write_text("", encoding="utf-8")
    subprocess.run(
        [
            "open",
            "-n",
            "-W",
            "--stdout",
            str(output / "stdout.log"),
            "--stderr",
            str(output / "stderr.log"),
            str(app),
        ],
        check=True,
        timeout=300,
    )
    if not result_path.is_file():
        raise RuntimeError(f"GUI host produced no result; inspect {output}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
