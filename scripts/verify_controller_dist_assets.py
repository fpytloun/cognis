"""Verify controller distribution and image-boundary invariants."""

from __future__ import annotations

import argparse
import importlib.metadata
import tarfile
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

REQUIRED_ASSETS = {
    "cognis/rendering/deliverables.py",
    "cognis/rendering/assets/noto-emoji/NotoColorEmoji.ttf",
    "cognis/rendering/assets/noto-emoji/OFL.txt",
    "cognis/rendering/assets/noto-emoji/README.md",
}
FORBIDDEN_REQUIREMENTS = (
    "cognis-executor",
    "lsp",
    "patchright",
    "playwright",
    "playwright-stealth",
)
FORBIDDEN_MODULE_PREFIXES = (
    "cognis/executor/runner.py",
    "cognis/executor/handlers.py",
    "cognis/executor/lsp_runtime.py",
    "playwright/",
    "playwright_stealth/",
    "patchright/",
)
FORBIDDEN_RUNTIME_PATHS = (
    "src",
    "packages",
    "docs",
    "scripts",
    "tests",
    "ui",
    "build.py",
    "README.md",
    "pyproject.toml",
    "uv.lock",
)


def _wheel_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def _sdist_members(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name.split("/", 1)[1] for member in archive.getmembers() if "/" in member.name
        }


def _wheel_metadata(path: Path) -> tuple[str, list[str]]:
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser(policy=policy.compat32).parsebytes(archive.read(metadata_name))
        entry_points_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        entry_points = (
            archive.read(entry_points_name).decode("utf-8").splitlines()
            if entry_points_name is not None
            else []
        )
    if any(line.startswith("cognis-executor") for line in entry_points):
        raise RuntimeError(f"{path.name} contains the cognis-executor CLI entry point")
    return metadata["Name"] or "", metadata.get_all("Requires-Dist", [])


def verify_distribution(path: Path) -> None:
    members = _wheel_members(path) if path.suffix == ".whl" else _sdist_members(path)
    missing = REQUIRED_ASSETS - members
    if missing:
        formatted = ", ".join(sorted(missing))
        raise RuntimeError(f"{path.name} is missing required assets: {formatted}")
    if path.suffix != ".whl":
        return

    name, requirements = _wheel_metadata(path)
    if name != "cognis-controller":
        raise RuntimeError(f"{path.name} has unexpected distribution name: {name!r}")
    forbidden_requirements = [
        requirement
        for requirement in requirements
        if any(forbidden in requirement.lower() for forbidden in FORBIDDEN_REQUIREMENTS)
    ]
    if forbidden_requirements:
        raise RuntimeError(f"{path.name} has forbidden requirements: {forbidden_requirements}")

    ui_members = [member for member in members if member.startswith("cognis/ui_dist/")]
    if "cognis/ui_dist/index.html" not in members or not ui_members:
        raise RuntimeError(f"{path.name} does not contain one complete cognis/ui_dist tree")
    duplicate_ui_trees = [
        member
        for member in members
        if member.startswith(("ui/", "cognis/ui_dist/standalone/ui_dist/"))
    ]
    if duplicate_ui_trees:
        raise RuntimeError(f"{path.name} contains duplicate UI trees: {duplicate_ui_trees}")
    forbidden_modules = [
        member for member in members if member.startswith(FORBIDDEN_MODULE_PREFIXES)
    ]
    if forbidden_modules:
        raise RuntimeError(f"{path.name} contains forbidden runtime modules: {forbidden_modules}")


def verify_runtime(root: Path) -> None:
    site_packages = next(root.glob("opt/venv/lib/python*/site-packages"), None)
    if site_packages is None:
        raise RuntimeError(f"runtime venv site-packages not found below {root}")
    distributions = {
        distribution.metadata["Name"].lower()
        for distribution in importlib.metadata.distributions(path=[str(site_packages)])
        if distribution.metadata["Name"]
    }
    forbidden_distributions = {
        name
        for name in distributions
        if any(forbidden in name for forbidden in FORBIDDEN_REQUIREMENTS)
    }
    if forbidden_distributions:
        raise RuntimeError(
            f"runtime has forbidden distributions: {sorted(forbidden_distributions)}"
        )

    module_paths = {
        path.relative_to(site_packages).as_posix()
        for path in site_packages.rglob("*")
        if path.is_file()
    }
    forbidden_modules = [
        path for path in module_paths if path.startswith(FORBIDDEN_MODULE_PREFIXES)
    ]
    if forbidden_modules:
        raise RuntimeError(f"runtime has forbidden modules: {forbidden_modules}")
    ui_trees = [path for path in site_packages.rglob("ui_dist") if path.is_dir()]
    if len(ui_trees) != 1:
        raise RuntimeError(f"runtime must contain exactly one ui_dist tree, found {ui_trees}")

    executables = {path.name for path in (root / "opt/venv/bin").iterdir()}
    cognis_executables = executables & {"cognis-controller", "cognis-executor"}
    if cognis_executables != {"cognis-controller"}:
        raise RuntimeError(f"unexpected Cognis executables: {sorted(cognis_executables)}")
    if not (root / "usr/bin/ffmpeg").exists():
        raise RuntimeError("runtime does not contain ffmpeg")
    forbidden_paths = [path for path in FORBIDDEN_RUNTIME_PATHS if (root / path).exists()]
    if forbidden_paths:
        raise RuntimeError(f"runtime contains source/build paths: {forbidden_paths}")


def verify_dockerfile(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    required_fragments = (
        "from=common-wheel,source=/dist,target=/tmp/common-wheels",
        "from=controller-wheel,source=/dist,target=/tmp/controller-wheels",
        'CMD ["cognis-controller", "serve"]',
        "urllib.request.urlopen",
        "ffmpeg",
    )
    missing = [fragment for fragment in required_fragments if fragment not in content]
    if missing:
        raise RuntimeError(f"{path} is missing required image settings: {missing}")
    forbidden_fragments = ("curl", "uv sync", "COPY packages/executor", "HEALTHCHECK CMD curl")
    present = [fragment for fragment in forbidden_fragments if fragment in content]
    if present:
        raise RuntimeError(f"{path} contains forbidden image settings: {present}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--dockerfile", type=Path)
    args = parser.parse_args()

    wheels = sorted(args.dist_dir.glob("cognis_controller-*.whl"))
    sdists = sorted(args.dist_dir.glob("cognis_controller-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            f"expected one Cognis wheel and one sdist in {args.dist_dir}, "
            f"found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    for distribution in (*wheels, *sdists):
        verify_distribution(distribution)
        print(f"verified {distribution.name}")
    if args.runtime_root is not None:
        verify_runtime(args.runtime_root)
        print(f"verified runtime {args.runtime_root}")
    if args.dockerfile is not None:
        verify_dockerfile(args.dockerfile)
        print(f"verified Dockerfile {args.dockerfile}")


if __name__ == "__main__":
    main()
