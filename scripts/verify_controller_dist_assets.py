"""Verify required runtime assets are present in controller distributions."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

REQUIRED_ASSETS = {
    "cognis/rendering/deliverables.py",
    "cognis/rendering/assets/noto-emoji/NotoColorEmoji.ttf",
    "cognis/rendering/assets/noto-emoji/OFL.txt",
    "cognis/rendering/assets/noto-emoji/README.md",
}


def _wheel_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def _sdist_members(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name.split("/", 1)[1] for member in archive.getmembers() if "/" in member.name
        }


def verify_distribution(path: Path) -> None:
    members = _wheel_members(path) if path.suffix == ".whl" else _sdist_members(path)
    missing = REQUIRED_ASSETS - members
    if missing:
        formatted = ", ".join(sorted(missing))
        raise RuntimeError(f"{path.name} is missing required assets: {formatted}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
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


if __name__ == "__main__":
    main()
