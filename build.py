from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

REPO_ROOT = Path(__file__).resolve().parent
UI_DIR = REPO_ROOT / "ui"
UI_BUILD_DIR = UI_DIR / "build"
PACKAGE_UI_DIR = REPO_ROOT / "cognis" / "ui_dist"


class CustomBuildHook(BuildHookInterface):
    """Build and stage bundled SvelteKit assets for wheel builds."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version, build_data
        self._maybe_build_ui()
        self._stage_built_assets()

    def _maybe_build_ui(self) -> None:
        if os.environ.get("COGNIS_SKIP_UI_BUILD") == "1":
            self._warn("Skipping UI build because COGNIS_SKIP_UI_BUILD=1.")
            return

        npm = shutil.which("npm")
        node = shutil.which("node")
        if npm is None or node is None:
            self._warn("Node.js/npm not found; continuing without rebuilding bundled UI assets.")
            return

        subprocess.run([npm, "ci"], cwd=UI_DIR, check=True)
        subprocess.run([npm, "run", "build"], cwd=UI_DIR, check=True)

    def _stage_built_assets(self) -> None:
        if PACKAGE_UI_DIR.exists():
            shutil.rmtree(PACKAGE_UI_DIR)

        if not UI_BUILD_DIR.exists():
            self._warn(
                "No built UI assets found; packaged wheel will not include bundled UI files."
            )
            return

        shutil.copytree(UI_BUILD_DIR, PACKAGE_UI_DIR)

    def _warn(self, message: str) -> None:
        sys.stderr.write(f"[cognis build] {message}\n")
