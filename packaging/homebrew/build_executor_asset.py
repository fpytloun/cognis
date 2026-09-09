"""Build a relocatable, architecture-specific Cognis executor payload.

The payload contains wheels resolved by ``uv.lock`` and the current local
``cognis-common`` and ``cognis-executor`` source packages.  It is deliberately
independent of the checkout path so it can be copied into a Homebrew formula.
"""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any

PYTHON_VERSION = "3.12"
ARCH_TARGETS = {
    "arm64": "aarch64-apple-darwin",
    "x86_64": "x86_64-apple-darwin",
}
# jstyleson publishes only a source distribution. It is pure Python, pinned and
# hash-checked by uv.lock, so building it while creating the release asset does
# not make the installed service depend on a compiler or package index.
SOURCE_BUILD_ALLOWLIST = ("jstyleson",)
MANIFEST_SCHEMA = "cognis.executor.asset.v1"
BUILD_CONSTRAINTS = Path(__file__).with_name("build-constraints.txt")


@dataclass(frozen=True)
class AssetResult:
    archive: Path
    manifest: dict[str, Any]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _package_version(path: Path) -> str:
    with (path / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream).get("project", {})
    version = project.get("version")
    if not isinstance(version, str):
        raise TypeError(f"Missing package version in {path / 'pyproject.toml'}")
    return version


def verify_package_versions(root: Path) -> str:
    versions = {
        "cognis-common": _package_version(root / "packages" / "common"),
        "cognis-executor": _package_version(root / "packages" / "executor"),
    }
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        raise RuntimeError(f"Package versions must match: {versions}")
    return unique_versions.pop()


def export_requirements(root: Path, destination: Path) -> None:
    """Export only frozen third-party requirements for the executor full extra."""
    command = [
        "uv",
        "--quiet",
        "export",
        "--package",
        "cognis-executor",
        "--extra",
        "full",
        "--locked",
        "--format",
        "requirements.txt",
        "--no-emit-project",
        "--no-emit-workspace",
        "--no-emit-local",
        "--no-annotate",
        "--no-header",
        "--output-file",
        str(destination),
    ]
    subprocess.run(command, cwd=root, check=True)


def find_python(root: Path) -> str:
    """Return an actual Python 3.12 interpreter for stable prefix layout."""
    return subprocess.check_output(
        ["uv", "python", "find", PYTHON_VERSION],
        cwd=root,
        text=True,
    ).strip()


def install_locked_dependencies(
    root: Path,
    requirements: Path,
    payload: Path,
    target: str,
    python: str,
) -> None:
    """Install locked target wheels and the allowlisted pure-Python source package."""
    source_build_args = [
        argument for package in SOURCE_BUILD_ALLOWLIST for argument in ("--no-binary", package)
    ]
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--prefix",
            str(payload),
            "--python",
            python,
            "--python-platform",
            target,
            "--only-binary",
            ":all:",
            *source_build_args,
            "--require-hashes",
            "--build-constraints",
            str(BUILD_CONSTRAINTS),
            "--requirement",
            str(requirements),
        ],
        cwd=root,
        check=True,
    )


def build_local_wheels(root: Path, destination: Path) -> None:
    """Build both workspace packages as wheels, not editable source installs."""
    for package in ("cognis-common", "cognis-executor"):
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--package",
                package,
                "--out-dir",
                str(destination),
                "--build-constraints",
                str(BUILD_CONSTRAINTS),
                "--require-hashes",
            ],
            cwd=root,
            check=True,
        )


def install_local_packages(
    root: Path,
    wheel_dir: Path,
    payload: Path,
    target: str,
    version: str,
    python: str,
) -> None:
    """Install local wheels by distribution name and retain their dist-info."""
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--prefix",
            str(payload),
            "--python",
            python,
            "--python-platform",
            target,
            "--no-index",
            "--find-links",
            str(wheel_dir),
            "--no-deps",
            f"cognis-common=={version}",
            f"cognis-executor=={version}",
        ],
        cwd=root,
        check=True,
    )


def _remove_nonportable_metadata(payload: Path) -> None:
    # Console-script shebangs contain the build interpreter's absolute path.
    # The formula provides the only supported entry point and external tools
    # come from Homebrew dependencies, so no payload scripts are required.
    shutil.rmtree(payload / "bin", ignore_errors=True)
    for path in payload.rglob("*"):
        if path.is_dir() and (path.name == "__pycache__" or path.name.endswith(".egg-info")):
            shutil.rmtree(path)
        elif path.is_file() and (
            path.suffix in {".pyc", ".pyo"}
            or path.name.endswith(".pth")
            or path.name == "direct_url.json"
            or path.name == "uv_cache.json"
            or path.name.startswith("__editable__")
        ):
            path.unlink()
    _rewrite_distribution_records(payload)


def _rewrite_distribution_records(payload: Path) -> None:
    """Remove deleted installer metadata and normalize installed wheel records."""
    site_packages = payload / "lib" / f"python{PYTHON_VERSION}" / "site-packages"
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        rows: list[tuple[str, str, str]] = []
        with record.open(newline="", encoding="utf-8") as stream:
            paths = [row[0] for row in csv.reader(stream) if row]
        for relative in sorted(paths):
            installed = site_packages / relative
            if installed == record:
                rows.append((relative, "", ""))
                continue
            if not installed.is_file():
                continue
            content = installed.read_bytes()
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            )
            rows.append((relative, f"sha256={digest}", str(len(content))))
        with record.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream, lineterminator="\n").writerows(rows)


def _payload_digest(payload: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    file_count = 0
    unpacked_size = 0
    files = sorted(path for path in payload.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(payload).as_posix().encode()
        content = path.read_bytes()
        digest.update(relative + b"\0" + str(len(content)).encode() + b"\0" + content)
        file_count += 1
        unpacked_size += len(content)
    return digest.hexdigest(), file_count, unpacked_size


def _package_inventory(payload: Path) -> list[dict[str, str]]:
    site_packages = payload / "lib" / f"python{PYTHON_VERSION}" / "site-packages"
    inventory: list[dict[str, str]] = []
    for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
        headers = Parser().parsestr(
            metadata.read_text(encoding="utf-8"),
            headersonly=True,
        )
        name = headers.get("Name")
        version = headers.get("Version")
        if name and version:
            inventory.append({"name": name, "version": version})
    return sorted(inventory, key=lambda item: (item["name"].lower(), item["version"]))


def create_manifest(
    root: Path,
    payload: Path,
    *,
    target: str,
    revision: str,
    version: str,
) -> dict[str, Any]:
    digest, file_count, unpacked_size = _payload_digest(payload)
    packages = _package_inventory(payload)
    package_names = {package["name"].lower() for package in packages}
    required_packages = {"cognis-common", "cognis-executor"}
    if not required_packages <= package_names:
        raise RuntimeError(
            "Payload does not contain Python 3.12 metadata for cognis-common and cognis-executor"
        )
    return {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "target": {"architecture": target.split("-", 1)[0], "platform": target},
        "python": {"version": PYTHON_VERSION, "abi": "cp312"},
        "git_revision": revision,
        "lock_sha256": _sha256_file(root / "uv.lock"),
        "build_constraints_sha256": _sha256_file(BUILD_CONSTRAINTS),
        "packages": packages,
        "file_count": file_count,
        "unpacked_size": unpacked_size,
        "payload_sha256": digest,
    }


def _tar_info(path: Path, name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = path.stat().st_size
    info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def write_deterministic_archive(payload: Path, manifest: dict[str, Any], destination: Path) -> None:
    """Write ``manifest.json`` and payload files in stable order and metadata."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    with (
        destination.open("wb") as output,
        gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mode = 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for path in sorted(path for path in payload.rglob("*") if path.is_file()):
            relative = path.relative_to(payload).as_posix()
            with path.open("rb") as stream:
                archive.addfile(_tar_info(path, f"payload/{relative}"), stream)


def build_asset(
    root: Path,
    *,
    architecture: str,
    output: Path,
    revision: str | None = None,
    workdir: Path | None = None,
) -> AssetResult:
    """Build one architecture asset and return its manifest."""
    version = verify_package_versions(root)
    try:
        target = ARCH_TARGETS[architecture]
    except KeyError as exc:
        raise ValueError(f"Unsupported architecture: {architecture}") from exc
    revision = (
        revision
        or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    )
    with tempfile.TemporaryDirectory(dir=workdir) as temporary:
        staging = Path(temporary)
        requirements = staging / "requirements.txt"
        wheel_dir = staging / "wheels"
        payload = staging / "payload"
        wheel_dir.mkdir()
        python = find_python(root)
        export_requirements(root, requirements)
        install_locked_dependencies(root, requirements, payload, target, python)
        build_local_wheels(root, wheel_dir)
        install_local_packages(root, wheel_dir, payload, target, version, python)
        _remove_nonportable_metadata(payload)
        manifest = create_manifest(root, payload, target=target, revision=revision, version=version)
        write_deterministic_archive(payload, manifest, output)
    return AssetResult(output, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=sorted(ARCH_TARGETS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=_repository_root())
    args = parser.parse_args()
    result = build_asset(args.root.resolve(), architecture=args.architecture, output=args.output)
    print(json.dumps({"archive": str(result.archive), "manifest": result.manifest}, sort_keys=True))


if __name__ == "__main__":
    main()
