"""Focused checks for the controller/executor workspace split."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import zipfile
from functools import partial
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def built_wheels(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    output = tmp_path_factory.mktemp("workspace-wheels")
    environment = {**os.environ, "COGNIS_SKIP_UI_BUILD": "1"}
    subprocess.run(
        [
            "uv",
            "build",
            "--all-packages",
            "--wheel",
            "--out-dir",
            str(output),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
    )
    common = next(output.glob("cognis_common-*.whl"))
    controller = next(output.glob("cognis_controller-*.whl"))
    executor = next(output.glob("cognis_executor-*.whl"))
    return common, controller, executor


def test_workspace_wheels_have_exclusive_ownership(
    built_wheels: tuple[Path, Path, Path],
) -> None:
    subprocess.run(
        [sys.executable, "scripts/check_package_split.py", str(built_wheels[0].parent)],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )


def test_executor_wheel_imports_without_controller_tree(
    built_wheels: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    executor_root = tmp_path / "executor"
    common_root = tmp_path / "common"
    with zipfile.ZipFile(built_wheels[2]) as archive:
        archive.extractall(executor_root)
    with zipfile.ZipFile(built_wheels[0]) as archive:
        archive.extractall(common_root)
    purelib = Path(sysconfig.get_paths()["purelib"])
    subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                f"import sys; sys.path[:0] = {[str(executor_root), str(common_root), str(purelib)]!r}; "
                "import cognis.executor.runner"
            ),
        ],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        check=True,
    )


def _executor_metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


def _common_metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


def test_executor_base_metadata_is_minimal_and_extras_are_explicit(
    built_wheels: tuple[Path, Path, Path],
) -> None:
    common_metadata = _common_metadata(built_wheels[0])
    metadata = _executor_metadata(built_wheels[2])
    base = [line for line in metadata.splitlines() if line.startswith("Requires-Dist:")]
    assert "Requires-Dist: litellm" not in base
    assert not any(line.startswith("Requires-Dist: mcp") for line in base)
    assert "Requires-Dist: playwright" not in base
    assert "Requires-Dist: httpx>=0.27.0" in base
    assert "Requires-Dist: httpx>=0.27.0" in common_metadata
    assert "Requires-Dist: mcp<2,>=1.28" in common_metadata
    assert "Requires-Dist: google-auth[requests]>=2.0.0" in common_metadata
    assert "Requires-Dist: prometheus-client>=0.20.0" in common_metadata
    assert "Requires-Dist: websockets>=14.0" in common_metadata
    assert "Provides-Extra: mcp" in metadata
    for extra, package in {
        "browser": "playwright",
        "web": "ddgs",
        "documents": "pypdf",
        "inference": "litellm",
    }.items():
        assert f"extra == '{extra}'" in metadata
        assert f"{package}" in metadata
    assert "Provides-Extra: channels" in metadata
    assert "extra == 'full'" in metadata


def test_missing_optional_components_do_not_import_or_register() -> None:
    from cognis.executor import component_registry
    from cognis.tools.executor import definitions

    original = component_registry.component_available
    try:
        component_registry.component_available = lambda name: name in {"lsp", "officecli"}
        definitions.load_component = component_registry.load_component
        names = {tool.name for tool in definitions.executor_tool_definitions()}
        handlers = definitions.executor_tool_handlers()
    finally:
        component_registry.component_available = original
    assert {"read", "write", "glob", "grep", "bash"} <= names
    assert not names & {"browser_open", "document_generate"}
    assert "browser_open" not in handlers
    assert "document_generate" not in handlers


def test_editable_workspace_exposes_both_trees_and_one_version(tmp_path: Path) -> None:
    environment = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / ".venv"),
    }
    subprocess.run(
        ["uv", "sync", "--all-packages", "--all-extras"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-c",
            (
                "import cognis; import cognis.main; import cognis.executor.runner; "
                "assert cognis.__version__ == '0.14.1'; "
                "assert len(cognis.__path__) == 3"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("command", ["cognis-controller", "cognis-executor"])
def test_workspace_console_scripts_start(command: str) -> None:
    subprocess.run(
        ["uv", "run", command, "--help"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(
    os.environ.get("COGNIS_RUN_ISOLATED_PACKAGE_TESTS") != "1",
    reason="set COGNIS_RUN_ISOLATED_PACKAGE_TESTS=1 for real pip environment acceptance",
)
@pytest.mark.parametrize(
    ("installed", "assertion"),
    [
        (
            ("common",),
            (
                "import importlib.util, cognis; assert cognis.__version__ == '0.14.1'; "
                "import cognis.core.tool_arguments; import cognis.tools.introspection; "
                "import cognis.mcp_runtime; "
                "from cognis.channels.factory import create_adapter; "
                "assert create_adapter('matrix').channel_type == 'matrix'; "
                "assert create_adapter('google_chat').channel_type == 'google_chat'; "
                "import cognis.tools.executor.web.definitions; "
                "assert importlib.util.find_spec('cognis.main') is None; "
                "assert importlib.util.find_spec('cognis.executor.runner') is None"
            ),
        ),
        (
            ("common", "controller"),
            (
                "import importlib.util; "
                "from importlib.metadata import PackageNotFoundError, distribution; "
                "import cognis.main; "
                "from cognis.channels.factory import create_adapter; "
                "assert create_adapter('matrix').channel_type == 'matrix'; "
                "assert create_adapter('google_chat').channel_type == 'google_chat'; "
                "assert importlib.util.find_spec('cognis.executor.runner') is None; "
                "\ntry:\n distribution('cognis-executor')\n"
                "except PackageNotFoundError:\n pass\n"
                "else:\n raise AssertionError('executor distribution unexpectedly installed')"
            ),
        ),
        (
            ("common", "controller", "executor"),
            "import cognis.main; import cognis.executor.runner",
        ),
    ],
)
def test_isolated_install_combinations(
    built_wheels: tuple[Path, Path, Path],
    tmp_path: Path,
    installed: tuple[str, ...],
    assertion: str,
) -> None:
    wheels = dict(zip(("common", "controller", "executor"), built_wheels, strict=True))
    environment = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    venv = tmp_path / "-".join(installed)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], env=environment, check=True)
    python = venv / "bin" / "python"
    pip = [str(python), "-m", "pip"]
    subprocess.run(
        [*pip, "install", *(str(wheels[name]) for name in installed)],
        env=environment,
        check=True,
    )
    subprocess.run([*pip, "check"], env=environment, check=True)
    subprocess.run(
        [python, "-c", assertion],
        cwd=tmp_path,
        env=environment,
        check=True,
    )
    if "controller" in installed:
        subprocess.run(
            [venv / "bin" / "cognis-controller", "--help"],
            cwd=tmp_path,
            env=environment,
            check=True,
        )
    if "executor" in installed:
        subprocess.run(
            [venv / "bin" / "cognis-executor", "--help"],
            cwd=tmp_path,
            env=environment,
            check=True,
        )


@pytest.mark.skipif(
    os.environ.get("COGNIS_RUN_ISOLATED_PACKAGE_TESTS") != "1",
    reason="set COGNIS_RUN_ISOLATED_PACKAGE_TESTS=1 for real pip environment acceptance",
)
def test_isolated_minimal_full_and_uninstall_acceptance(
    built_wheels: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    common, controller, executor = built_wheels
    environment = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    run = partial(subprocess.run, env=environment)
    venv = tmp_path / "minimal-venv"
    run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin" / "python"
    pip = [str(python), "-m", "pip"]
    run([*pip, "install", str(common), str(executor)], check=True)
    run([*pip, "check"], check=True)
    run(
        [
            str(python),
            "-c",
            (
                "import sys; from pathlib import Path; import cognis; "
                "import cognis.executor.runner; assert cognis.__version__ == '0.14.1'; "
                "assert Path(cognis.__file__).resolve().is_relative_to("
                "Path(sys.prefix).resolve())"
            ),
        ],
        cwd=tmp_path,
        check=True,
    )
    run([str(venv / "bin" / "cognis-executor"), "--help"], check=True)

    full_venv = tmp_path / "full-venv"
    run([sys.executable, "-m", "venv", str(full_venv)], check=True)
    full_python = full_venv / "bin" / "python"
    full_pip = [str(full_python), "-m", "pip"]
    run([*full_pip, "install", str(common), f"{executor}[full]"], check=True)
    run([*full_pip, "check"], check=True)
    run(
        [
            full_python,
            "-c",
            (
                "import sys; from pathlib import Path; import cognis; "
                "assert Path(cognis.__file__).resolve().is_relative_to("
                "Path(sys.prefix).resolve()); "
                "import cognis.executor.channel_handler; "
                "import cognis.executor.inference; "
                "from cognis.executor.component_registry import load_component; "
                "components = ('browser', 'mcp', 'web', 'documents', 'inference', 'channels'); "
                "assert not (missing := [name for name in components if not load_component(name)]), "
                "missing; "
                "from cognis.channels.factory import create_adapter; "
                "[create_adapter(name) for name in "
                "('signal', 'whatsapp', 'telegram', 'discord', 'slack', 'matrix', "
                "'irc', 'google_chat', 'bluebubbles')]; "
                "from cognis.tools.executor.definitions import executor_tool_definitions; "
                "from cognis.tools.introspection import audit_tool_descriptors; "
                "from cognis.tools.native_validation import registered_native_validator_ids; "
                "definitions = executor_tool_definitions(); assert definitions; "
                "assert registered_native_validator_ids() == set(); "
                "assert audit_tool_descriptors(definitions) == []"
            ),
        ],
        cwd=tmp_path,
        check=True,
    )
    run([*full_pip, "install", str(controller)], check=True)
    run([*full_pip, "check"], check=True)
    run(
        [
            full_python,
            "-c",
            (
                "from cognis.tools.native_validation import registered_native_validator_ids; "
                "assert 'schedule.definition' in registered_native_validator_ids()"
            ),
        ],
        cwd=tmp_path,
        check=True,
    )
    run([*full_pip, "uninstall", "-y", "cognis-controller"], check=True)
    run([*full_pip, "check"], check=True)
    run(
        [full_python, "-c", "import cognis.executor.runner"],
        cwd=tmp_path,
        check=True,
    )
    run([*full_pip, "install", str(controller)], check=True)
    run([*full_pip, "uninstall", "-y", "cognis-executor"], check=True)
    run([*full_pip, "check"], check=True)
    full_purelib = run(
        [
            full_python,
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert (Path(full_purelib) / "cognis" / "main.py").is_file()
    run(
        [
            full_python,
            "-c",
            (
                "from importlib.metadata import PackageNotFoundError, distribution; "
                "import cognis.main; "
                "\ntry:\n distribution('cognis-executor')\n"
                "except PackageNotFoundError:\n pass\n"
                "else:\n raise AssertionError('executor distribution still installed')"
            ),
        ],
        cwd=tmp_path,
        check=True,
    )
    run([*full_pip, "install", str(common), f"{executor}[full]"], check=True)
    run([*full_pip, "check"], check=True)
    run(
        [full_python, "-c", "import cognis.main"],
        cwd=tmp_path,
        check=True,
    )
