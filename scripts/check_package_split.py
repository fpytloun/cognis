"""Validate ownership and dependency edges of all Cognis wheels."""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path
from zipfile import ZipFile

COMMON_ALLOWED_PREFIXES = (
    "cognis/__init__.py",
    "cognis/json_stream.py",
    "cognis/logging.py",
    "cognis/mcp_runtime.py",
    "cognis/ownership.py",
    "cognis/providers/retry.py",
    "cognis/providers/circuit_breaker.py",
    "cognis/core/anchored_output.py",
    "cognis/core/deliverable_links.py",
    "cognis/core/local_models.py",
    "cognis/core/project_context.py",
    "cognis/core/tool_arguments.py",
    "cognis/channels/",
    "cognis/executor/unary_dedup.py",
    "cognis/executor/project_context_shared.py",
    "cognis/executor/runtime_capabilities.py",
    "cognis/models/",
    "cognis/providers/llm/anthropic/",
    "cognis/providers/llm/errors.py",
    "cognis/providers/llm/terminal.py",
    "cognis/rendering/rich_visuals.py",
    "cognis/tools/argument_normalization.py",
    "cognis/tools/argument_aliases.py",
    "cognis/tools/executor/definitions.py",
    "cognis/tools/executor/lsp/types.py",
    "cognis/tools/executor/lsp/runtime.py",
    "cognis/tools/executor/web/definitions.py",
    "cognis/tools/executor/web/__init__.py",
    "cognis/tools/introspection.py",
    "cognis/tools/native_validation.py",
    "cognis/tools/registry.py",
)
COMMON_FORBIDDEN_IMPORTS = (
    b"\nimport subprocess",
    b"\nfrom subprocess",
    b"\nimport multiprocessing",
    b"\nfrom multiprocessing",
    b"create_subprocess",
)
COMMON_RUNTIME_IMPORT_ALLOWED_PREFIXES = ("cognis/channels/adapters/signal_cli_",)
FORBIDDEN_EXECUTOR_PREFIXES = (
    "cognis/api/",
    "cognis/store/",
    "cognis/tools/builtin/",
    "cognis/tools/controller_native_validation.py",
    "cognis/core/agent_",
    "cognis/core/decision.py",
    "cognis/core/session",
    "cognis/core/workflow",
    "cognis/providers/memory/",
    "cognis/providers/guardrails/",
)
ALLOWED_EXECUTOR_LLM_PATHS = frozenset(
    {
        "cognis/providers/llm/anthropic_subscription.py",
        "cognis/providers/llm/errors.py",
        "cognis/providers/llm/ollama.py",
        "cognis/providers/llm/performance.py",
        "cognis/providers/llm/responses_bridge.py",
        "cognis/providers/llm/retry.py",
        "cognis/providers/llm/terminal.py",
    }
)
ALLOWED_EXECUTOR_LLM_PREFIXES = ("cognis/providers/llm/anthropic/",)


def wheel_paths(path: Path) -> set[str]:
    """Return every path recorded by a wheel."""
    with ZipFile(path) as archive:
        record = next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
        return {
            row[0] for row in csv.reader(io.StringIO(archive.read(record).decode("utf-8"))) if row
        }


def metadata(path: Path) -> str:
    """Return the wheel metadata payload."""
    with ZipFile(path) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


def entry_points(path: Path) -> str:
    """Return the console entry-point declarations."""
    with ZipFile(path) as archive:
        name = next(
            item for item in archive.namelist() if item.endswith(".dist-info/entry_points.txt")
        )
        return archive.read(name).decode("utf-8")


def _requires(metadata_text: str) -> list[str]:
    return [
        re.split(
            r"[<=>!~\[]",
            line.removeprefix("Requires-Dist: ").split(";", 1)[0].strip().lower(),
            maxsplit=1,
        )[0]
        for line in metadata_text.splitlines()
        if line.startswith("Requires-Dist:")
    ]


def assert_split(common: Path, controller: Path, executor: Path) -> None:
    paths = {
        "common": wheel_paths(common),
        "controller": wheel_paths(controller),
        "executor": wheel_paths(executor),
    }
    for left, left_paths in paths.items():
        for right, right_paths in paths.items():
            if left >= right:
                continue
            overlap = sorted(left_paths & right_paths)
            if overlap:
                raise AssertionError(f"{left}/{right} wheel RECORD paths overlap: {overlap}")

    common_code = set(paths["common"]) - {item for item in paths["common"] if ".dist-info/" in item}
    forbidden_common = sorted(
        path
        for path in common_code
        if not any(path == prefix or path.startswith(prefix) for prefix in COMMON_ALLOWED_PREFIXES)
    )
    if forbidden_common:
        raise AssertionError(f"common wheel contains non-allowlisted paths: {forbidden_common}")
    with ZipFile(common) as archive:
        forbidden_content = sorted(
            path
            for path in common_code
            if path.endswith(".py")
            and not any(
                path.startswith(prefix) for prefix in COMMON_RUNTIME_IMPORT_ALLOWED_PREFIXES
            )
            and any(token in archive.read(path) for token in COMMON_FORBIDDEN_IMPORTS)
        )
    if forbidden_content:
        raise AssertionError(f"common wheel contains runtime imports: {forbidden_content}")

    controller_paths = paths["controller"]
    executor_paths = paths["executor"]
    if overlap:
        raise AssertionError(f"wheel RECORD paths overlap: {overlap}")
    forbidden = sorted(
        path
        for path in executor_paths
        if any(path.startswith(prefix) for prefix in FORBIDDEN_EXECUTOR_PREFIXES)
        or (
            path.startswith("cognis/providers/llm/")
            and path not in ALLOWED_EXECUTOR_LLM_PATHS
            and not any(path.startswith(prefix) for prefix in ALLOWED_EXECUTOR_LLM_PREFIXES)
        )
    )
    if forbidden:
        raise AssertionError(f"controller paths leaked into executor wheel: {forbidden}")

    required_controller = {"cognis/main.py", "cognis/api/app.py"}
    required_executor = {"cognis/executor/runner.py"}
    missing_controller = required_controller - controller_paths
    missing_executor = required_executor - executor_paths
    if missing_controller or missing_executor:
        raise AssertionError(
            f"missing ownership paths: controller={sorted(missing_controller)}, "
            f"executor={sorted(missing_executor)}"
        )

    if "cognis/executor/runner.py" in controller_paths:
        raise AssertionError("controller wheel owns executor runner")
    if "cognis/main.py" in executor_paths:
        raise AssertionError("executor wheel owns controller entrypoint")

    common_metadata = metadata(common)
    controller_metadata = metadata(controller)
    executor_metadata = metadata(executor)
    if "Version: 0.14.0" not in common_metadata:
        raise AssertionError("common metadata does not report version 0.14.0")
    if "Version: 0.14.0" not in controller_metadata:
        raise AssertionError("controller metadata does not report version 0.14.0")
    if "Version: 0.14.0" not in executor_metadata:
        raise AssertionError("executor metadata does not report version 0.14.0")
    if _requires(controller_metadata) != [
        item for item in _requires(controller_metadata) if item != "cognis-executor"
    ]:
        raise AssertionError("controller metadata depends on executor")
    if any(
        item in {"playwright", "playwright-stealth", "patchright"}
        for item in _requires(controller_metadata)
    ):
        raise AssertionError("controller metadata contains executor-only browser dependencies")
    if _requires(controller_metadata).count("cognis-common") != 1:
        raise AssertionError("controller must depend on cognis-common exactly once")
    if _requires(executor_metadata).count("cognis-common") != 1:
        raise AssertionError("executor must depend on cognis-common exactly once")
    controller_entry_points = entry_points(controller)
    executor_entry_points = entry_points(executor)
    if "cognis-controller =" not in controller_entry_points:
        raise AssertionError("controller CLI is missing")
    if "cognis-executor =" in controller_entry_points:
        raise AssertionError("executor CLI leaked into controller metadata")
    if "cognis-executor =" not in executor_entry_points:
        raise AssertionError("executor CLI is missing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args()
    common = next(args.dist_dir.glob("cognis_common-*.whl"), None)
    controller = next(args.dist_dir.glob("cognis_controller-*.whl"), None)
    executor = next(args.dist_dir.glob("cognis_executor-*.whl"), None)
    if common is None or controller is None or executor is None:
        parser.error("dist_dir must contain common, controller, and executor wheels")
    assert_split(common, controller, executor)
    print(f"validated {common.name}, {controller.name}, and {executor.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
