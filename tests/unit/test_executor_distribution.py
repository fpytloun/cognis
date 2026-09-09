"""Focused tests for executor assets and the Homebrew formula generator."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import signal
import sys
import tarfile
from pathlib import Path

import pytest
from packaging.version import Version

from cognis.executor import __main__ as executor_main


def _load_script(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_HOME_BREW_DIR = Path(__file__).parents[2] / "packaging" / "homebrew"
build_executor_asset = _load_script(
    "cognis_test_build_executor_asset", _HOME_BREW_DIR / "build_executor_asset.py"
)
generate_formula = _load_script(
    "cognis_test_generate_formula", _HOME_BREW_DIR / "generate_formula.py"
)


def _payload(tmp_path: Path) -> Path:
    payload = tmp_path / "payload"
    site_packages = payload / "lib" / "python3.12" / "site-packages"
    (site_packages / "cognis").mkdir(parents=True)
    (payload / "lib" / "python3.12" / "site-packages" / "cognis" / "__init__.py").write_text(
        "VERSION = '0.13.0'\n", encoding="utf-8"
    )
    for name in ("cognis_common", "cognis_executor"):
        metadata = site_packages / f"{name}-0.13.0.dist-info"
        metadata.mkdir()
        distribution = name.replace("_", "-")
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.3\nName: {distribution}\nVersion: 0.13.0\n"
            "\nName: misleading description content\nVersion: 999\n",
            encoding="utf-8",
        )
    return payload


def test_manifest_and_archive_are_deterministic(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "uv.lock").write_text("lock\n", encoding="utf-8")
    manifest = build_executor_asset.create_manifest(
        root, payload, target="aarch64-apple-darwin", revision="ae9a38e9", version="0.13.0"
    )
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    build_executor_asset.write_deterministic_archive(payload, manifest, first)
    build_executor_asset.write_deterministic_archive(payload, manifest, second)
    assert first.read_bytes() == second.read_bytes()
    assert manifest["schema"] == "cognis.executor.asset.v1"
    assert (
        manifest["build_constraints_sha256"]
        == hashlib.sha256(build_executor_asset.BUILD_CONSTRAINTS.read_bytes()).hexdigest()
    )
    digest = hashlib.sha256()
    for path in sorted(path for path in payload.rglob("*") if path.is_file()):
        content = path.read_bytes()
        digest.update(
            path.relative_to(payload).as_posix().encode()
            + b"\0"
            + str(len(content)).encode()
            + b"\0"
            + content
        )
    assert manifest["payload_sha256"] == digest.hexdigest()
    assert {item["name"] for item in manifest["packages"]} == {
        "cognis-common",
        "cognis-executor",
    }
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames() == [
            "manifest.json",
            "payload/lib/python3.12/site-packages/cognis/__init__.py",
            "payload/lib/python3.12/site-packages/cognis_common-0.13.0.dist-info/METADATA",
            "payload/lib/python3.12/site-packages/cognis_executor-0.13.0.dist-info/METADATA",
        ]


def test_archive_has_no_source_path_or_secret_metadata(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    source_path = payload / "source.pth"
    source_path.write_text(str(tmp_path), encoding="utf-8")
    scripts = payload / "bin"
    scripts.mkdir()
    (scripts / "chardetect").write_text(f"#!{tmp_path}/python\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock\n", encoding="utf-8")
    (payload / "lib" / "python3.12" / "site-packages" / "direct_url.json").write_text(
        str(tmp_path), encoding="utf-8"
    )
    dist_info = (
        payload / "lib" / "python3.12" / "site-packages" / "cognis_executor-0.13.0.dist-info"
    )
    (dist_info / "uv_cache.json").write_text('{"timestamp": 1}\n', encoding="utf-8")
    (dist_info / "RECORD").write_text(
        "cognis_executor-0.13.0.dist-info/uv_cache.json,,\n"
        "cognis_executor-0.13.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    build_executor_asset._remove_nonportable_metadata(payload)
    manifest = build_executor_asset.create_manifest(
        tmp_path, payload, target="x86_64-apple-darwin", revision="ae9a38e9", version="0.13.0"
    )
    archive = tmp_path / "asset.tar.gz"
    build_executor_asset.write_deterministic_archive(payload, manifest, archive)
    assert not source_path.exists()
    assert not scripts.exists()
    assert not (dist_info / "uv_cache.json").exists()
    assert (dist_info / "RECORD").read_text(encoding="utf-8") == (
        "cognis_executor-0.13.0.dist-info/RECORD,,\n"
    )
    assert str(tmp_path).encode() not in archive.read_bytes()


def test_package_versions_are_read_from_both_pyprojects(tmp_path: Path) -> None:
    for package in ("common", "executor"):
        package_dir = tmp_path / "packages" / package
        package_dir.mkdir(parents=True)
        (package_dir / "pyproject.toml").write_text(
            "[project]\nversion = '2.4.6'\n", encoding="utf-8"
        )
    assert build_executor_asset.verify_package_versions(tmp_path) == "2.4.6"
    (tmp_path / "packages" / "executor" / "pyproject.toml").write_text(
        "[project]\nversion = '2.4.7'\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="must match"):
        build_executor_asset.verify_package_versions(tmp_path)


def test_formula_requires_real_immutable_urls_and_hashes() -> None:
    assert Version("1.2.3").major == 1
    values = {
        "version": "0.13.0",
        "arm64_url": "https://github.com/fpytloun/cognis/releases/download/v0.13.0/cognis-executor-0.13.0-macos-arm64.tar.gz",
        "arm64_sha256": "a" * 63 + "b",
        "x86_64_url": "https://github.com/fpytloun/cognis/releases/download/v0.13.0/cognis-executor-0.13.0-macos-x86_64.tar.gz",
        "x86_64_sha256": "b" * 63 + "c",
    }
    formula = generate_formula.generate_formula(**values)
    assert 'depends_on "python@3.12"' in formula
    assert 'depends_on "uv"' in formula
    assert 'depends_on "node"' in formula
    assert formula.index('depends_on "uv"') < formula.index("  preserve_rpath")
    assert formula.index("  preserve_rpath") < formula.index("  def install")
    assert 'run opt_bin/"cognis-executor"' in formula
    assert "if build.head?" in formula
    assert "packages/executor[full]" in formula
    assert 'libexec.install Dir["payload/*"]' in formula
    assert "PYTHONNOUSERSITE=1" in formula
    assert 'export PYTHONPATH="#{libexec}/lib/python3.12/site-packages"' in formula
    assert "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" in formula
    assert "if Hardware::CPU.arm?" in formula
    assert "on_arm do" not in formula
    assert 'log_path var/"log/cognis-executor.log"' in formula
    assert 'error_log_path var/"log/cognis-executor.error.log"' in formula
    assert "cognis-executor logs --follow" in formula
    assert "token" not in formula.lower()
    head_formula = generate_formula.generate_formula(
        **values, head_url="ssh://git.fpy.cz:2222/filip/cognis.git"
    )
    assert 'head "ssh://git.fpy.cz:2222/filip/cognis.git", branch: "main"' in head_formula
    with pytest.raises(ValueError, match="non-placeholder|immutable"):
        generate_formula.generate_formula(
            **{**values, "arm64_url": "https://downloads.example.invalid/asset.tar.gz"}
        )
    with pytest.raises(ValueError, match="SHA-256"):
        generate_formula.generate_formula(**{**values, "arm64_sha256": "0" * 64})


def test_local_file_formula_requires_development_flag(tmp_path: Path) -> None:
    (tmp_path / "arm64.tar.gz").write_bytes(b"arm64")
    (tmp_path / "x86_64.tar.gz").write_bytes(b"x86_64")
    values = {
        "version": "0.13.0",
        "arm64_url": f"file://{tmp_path}/arm64.tar.gz",
        "arm64_sha256": "a" * 63 + "b",
        "x86_64_url": f"file://{tmp_path}/x86_64.tar.gz",
        "x86_64_sha256": "b" * 63 + "c",
    }
    with pytest.raises(ValueError, match="--development"):
        generate_formula.generate_formula(**values)
    assert "file://" in generate_formula.generate_formula(**values, development=True)


def test_local_wheels_are_built_and_installed_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        build_executor_asset.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command),
    )
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    build_executor_asset.build_local_wheels(tmp_path, wheel_dir)
    build_executor_asset.install_local_packages(
        tmp_path,
        wheel_dir,
        tmp_path / "payload",
        "aarch64-apple-darwin",
        "0.13.0",
        "/python3.12",
    )
    assert [command[4] for command in commands[:2]] == ["cognis-common", "cognis-executor"]
    assert all("--build-constraints" in command for command in commands[:2])
    assert all("--require-hashes" in command for command in commands[:2])
    install = commands[2]
    assert "--no-index" in install
    assert "--find-links" in install
    assert "--no-deps" in install
    assert install[install.index("--python") + 1] == "/python3.12"
    assert "cognis-common==0.13.0" in install
    assert "cognis-executor==0.13.0" in install


def test_locked_dependencies_allow_only_the_pinned_pure_source_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        build_executor_asset.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command),
    )
    build_executor_asset.install_locked_dependencies(
        tmp_path,
        tmp_path / "requirements.txt",
        tmp_path / "payload",
        "aarch64-apple-darwin",
        "/python3.12",
    )
    command = commands[0]
    assert command[command.index("--only-binary") + 1] == ":all:"
    assert command[command.index("--no-binary") + 1] == "jstyleson"
    assert command[command.index("--python") + 1] == "/python3.12"
    assert "--build-constraints" in command
    assert "--require-hashes" in command


def test_manifest_rejects_wrong_python_prefix(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    metadata = payload / "lib" / "python3.14" / "site-packages"
    for name in ("cognis_common", "cognis_executor"):
        dist_info = metadata / f"{name}-0.13.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.3\nName: {name.replace('_', '-')}\nVersion: 0.13.0\n",
            encoding="utf-8",
        )
    (tmp_path / "uv.lock").write_text("lock\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Python 3.12 metadata"):
        build_executor_asset.create_manifest(
            tmp_path,
            payload,
            target="aarch64-apple-darwin",
            revision="c9548361",
            version="0.13.0",
        )


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM is not available")
def test_sigterm_requests_runner_stop() -> None:
    class FakeRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        def stop(self) -> None:
            self.stopped.set()

        async def run(self) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()

    async def exercise() -> None:
        runner = FakeRunner()
        task = asyncio.create_task(executor_main._run_foreground(runner))
        await runner.started.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=2)
        assert runner.stopped.is_set()

    asyncio.run(exercise())
