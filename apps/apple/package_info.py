"""Record and verify source identity before native packaging (never sign)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from verify_web import verify

ROOT = Path(__file__).resolve().parents[2]
FLAVORS = {
    "standard": ("Cognis", "cz.fpy.cognis.companion"),
    "preview": ("Cognis Preview", "cz.fpy.cognis.companion.preview"),
    "acceptance": ("Cognis Acceptance", "cz.fpy.cognis.companion.acceptance"),
}


def package_flavor() -> str:
    """Legacy acceptance switch remains restricted; contradictory inputs fail closed."""
    explicit = os.environ.get("COGNIS_PACKAGE_FLAVOR")
    legacy = os.environ.get("COGNIS_ACCEPTANCE_BUILD") == "1"
    flavor = explicit or ("acceptance" if legacy else "standard")
    if flavor not in FLAVORS or (legacy and flavor != "acceptance"):
        raise ValueError("Unknown or conflicting package flavor")
    return flavor


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def identity() -> dict[str, object]:
    """Fingerprint source and locked inputs, including uncommitted development edits."""
    paths = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    digest = hashlib.sha256()
    for name in sorted(set(paths)):
        if not name:
            continue
        path = ROOT / name
        digest.update(name.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    version = os.environ.get(
        "COGNIS_APP_VERSION", json.loads((ROOT / "ui/package.json").read_text())["version"]
    )
    build = os.environ.get("COGNIS_APP_BUILD", git("rev-list", "--count", "HEAD"))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[1-9]\d{0,8}", build):
        raise ValueError(
            "App version must be x.y.z and build a positive integer (at most 9 digits)"
        )
    dirty = bool(git("status", "--porcelain"))
    if os.environ.get("COGNIS_RELEASE_BUILD") == "1" and (
        dirty or not all(os.environ.get(key) for key in ("COGNIS_APP_VERSION", "COGNIS_APP_BUILD"))
    ):
        raise ValueError("Release packaging requires clean source and explicit app version/build")
    distribution = os.environ.get("COGNIS_DISTRIBUTION", "direct")
    if distribution not in {"direct", "homebrew"}:
        raise ValueError("Unknown distribution mode")
    return {
        "schema": 1,
        "source": git("rev-parse", "HEAD"),
        "dirty": dirty,
        "sourceDigest": digest.hexdigest(),
        "version": version,
        "build": build,
        "webVersion": json.loads((ROOT / "ui/package.json").read_text())["version"],
        "bridge": 1,
        "distribution": distribution,
        "flavor": package_flavor(),
    }


def publisher(env: dict[str, str]) -> dict[str, object]:
    """Only publisher build inputs can enable updates. No fallback URL or key."""
    feed, key = env.get("COGNIS_UPDATE_FEED", ""), env.get("COGNIS_UPDATE_PUBLIC_KEY", "")
    if not feed and not key:
        return {}
    url = urlsplit(feed)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Publisher feed must be an HTTPS URL without credentials or fragment")
    try:
        valid_key = len(base64.b64decode(key, validate=True)) == 32
    except ValueError:
        valid_key = False
    if not valid_key:
        raise ValueError("Publisher Ed25519 public key must contain exactly 32 bytes")
    return {"SUFeedURL": feed, "SUPublicEDKey": key}


def stamp_web(web: Path, stamp: Path) -> None:
    stamp.write_text(
        json.dumps({"identity": identity(), "assets": verify(web)}, sort_keys=True, indent=2) + "\n"
    )


def check_web(web: Path, stamp: Path) -> dict[str, object]:
    recorded = json.loads(stamp.read_text())
    if recorded != {"identity": identity(), "assets": verify(web)}:
        raise ValueError("Stale or mismatched web export: rebuild and stamp from this exact source")
    return recorded


def write_info(destination: Path, recorded: dict[str, object]) -> None:
    value = identity()
    if recorded.get("identity") != value:
        raise ValueError("Native packaging inputs differ from the verified web manifest")
    flavor = package_flavor()
    name, bundle_id = FLAVORS[flavor]
    info: dict[str, object] = {
        "CFBundleExecutable": "Cognis",
        "CFBundleIdentifier": bundle_id,
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "Cognis.icns",
        "CFBundleShortVersionString": value["version"],
        "CFBundleVersion": value["build"],
        "LSMinimumSystemVersion": "15.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "CognisWebVersion": value["webVersion"],
        "CognisSourceCommit": value["source"],
        "CognisBuildIdentity": value,
        "CognisDistribution": value["distribution"],
        "CognisPackageFlavor": flavor,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "SUEnableAutomaticChecks": False,
        "SUAutomaticallyUpdate": False,
        "SUAllowsAutomaticUpdates": True,
        "SUEnableSystemProfiling": False,
        "SURequireSignedFeed": True,
        "SUVerifyUpdateBeforeExtraction": True,
        "SUSignedFeedFailureExpirationInterval": 0,
    }
    if flavor != "acceptance":
        info["CFBundleURLTypes"] = [
            {
                "CFBundleURLName": bundle_id,
                "CFBundleURLSchemes": ["cognis" if flavor == "standard" else "cognis-preview"],
                "CFBundleTypeRole": "Viewer",
            }
        ]
    # Human preview and automation have no publisher config, even if inherited
    # shell variables describe another distribution's feed/key.
    if flavor == "standard":
        info.update(publisher(dict(os.environ)))
    if flavor == "acceptance":
        info["CognisAcceptanceOrigin"] = "http://127.0.0.1:18763"
    destination.write_bytes(plistlib.dumps(info))
    resources = destination.parent / "Resources"
    resources.mkdir(exist_ok=True)
    (resources / "BuildIdentity.json").write_text(
        json.dumps(recorded, sort_keys=True, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stamp-web", "check-web", "plist", "app-name"])
    parser.add_argument("--web", type=Path, default=ROOT / "ui/build-apple")
    parser.add_argument("--stamp", type=Path, default=ROOT / "ui/build-apple-manifest.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "app-name":
        print(FLAVORS[package_flavor()][0] + ".app")
    elif args.action == "stamp-web":
        stamp_web(args.web, args.stamp)
    else:
        record = check_web(args.web, args.stamp)
        if args.action == "plist":
            if args.output is None:
                parser.error("--output is required")
            write_info(args.output, record)
