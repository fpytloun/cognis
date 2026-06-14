from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PACKAGE_DIR = REPO_ROOT / "cognis"
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the cognis-executor distribution")
    parser.add_argument(
        "--out-dir",
        default="dist-executor",
        help="Output directory for built distributions (default: dist-executor)",
    )
    args = parser.parse_args()

    metadata = load_root_metadata()
    out_dir = (REPO_ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cognis-executor-build-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        build_root = temp_dir / "cognis-executor"
        build_root.mkdir()

        shutil.copytree(SOURCE_PACKAGE_DIR, build_root / "cognis")
        (build_root / "README.md").write_text(executor_readme(), encoding="utf-8")
        (build_root / "pyproject.toml").write_text(
            render_executor_pyproject(metadata),
            encoding="utf-8",
        )

        subprocess.run(
            [sys.executable, "-m", "build"],
            cwd=build_root,
            check=True,
        )

        for artifact in (build_root / "dist").iterdir():
            shutil.copy2(artifact, out_dir / artifact.name)


def load_root_metadata() -> dict[str, object]:
    with ROOT_PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    return pyproject["project"]


def render_executor_pyproject(project: dict[str, object]) -> str:
    version = project["version"]
    requires_python = project["requires-python"]
    description = "Standalone remote executor for Cognis"
    license_text = project["license"]["text"]
    dependencies = project["dependencies"]
    authors = project["authors"]
    classifiers = project["classifiers"]

    dependency_lines = "\n".join(f'    "{dependency}",' for dependency in dependencies)
    author_lines = "\n".join(
        "    { " + ", ".join(f'{key} = "{value}"' for key, value in author.items()) + " },"
        for author in authors
    )
    classifier_lines = "\n".join(f'    "{classifier}",' for classifier in classifiers)

    return (
        textwrap.dedent(
            f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [project]
        name = "cognis-executor"
        version = "{version}"
        description = "{description}"
        readme = "README.md"
        license = {{ text = "{license_text}" }}
        requires-python = "{requires_python}"
        authors = [
        {author_lines}
        ]
        classifiers = [
        {classifier_lines}
        ]
        dependencies = [
        {dependency_lines}
        ]

        [project.scripts]
        cognis-executor = "cognis.executor.__main__:main"

        [tool.hatch.build.targets.wheel]
        packages = ["cognis"]
        """
        ).strip()
        + "\n"
    )


def executor_readme() -> str:
    return (
        textwrap.dedent(
            """
        # cognis-executor

        Standalone remote executor for Cognis.

        This package provides the `cognis-executor` command used to connect a
        remote executor process to a Cognis controller over WebSocket.
        """
        ).strip()
        + "\n"
    )


if __name__ == "__main__":
    main()
