"""Build provenance unit tests; generated HTML here is never served as an app."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tests.e2e.usage_checkpoint import build as build_module
from tests.e2e.usage_checkpoint.build import MARKER, _record, source_snapshot, verify_build


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed UI source tree, independent of the developer's working copy."""
    root = tmp_path / "repo"
    (root / "ui/src").mkdir(parents=True)
    (root / "ui/package-lock.json").write_text("{}")
    (root / "ui/src/app.html").write_text("<html></html>")
    for command in (
        ["init", "-q"],
        ["add", "ui"],
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "ui"],
    ):
        subprocess.run(["git", *command], cwd=root, check=True)
    return root


def _unit_build(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<main>Build manifest unit only; no application</main>")
    return build


def test_build_manifest_roundtrips_actual_committed_source_snapshot(
    tmp_path: Path, repo: Path
) -> None:
    build = _unit_build(tmp_path)
    before = source_snapshot(repo)
    manifest = _record(repo, build, before)
    assert verify_build(repo, build) == manifest
    assert manifest["source"]["revision"] == before["revision"]
    assert manifest["source"]["tree"] == before["tree"]
    assert (
        manifest["outputs"]["index.html"]
        == hashlib.sha256((build / "index.html").read_bytes()).hexdigest()
    )


def test_build_manifest_rejects_mismatched_source_inputs(tmp_path: Path, repo: Path) -> None:
    build = _unit_build(tmp_path)
    manifest = _record(repo, build, source_snapshot(repo))
    manifest["source"]["source_hashes"]["ui/package-lock.json"] = hashlib.sha256(
        b"changed lockfile"
    ).hexdigest()
    (build / MARKER).write_text(json.dumps(manifest))
    with pytest.raises(AssertionError, match="does not match"):
        verify_build(repo, build)


def test_build_manifest_rejects_modified_generated_bytes(tmp_path: Path, repo: Path) -> None:
    build = _unit_build(tmp_path)
    _record(repo, build, source_snapshot(repo))
    (build / "index.html").write_text("<main>Modified after build</main>")
    with pytest.raises(AssertionError, match="output changed"):
        verify_build(repo, build)


def test_existing_build_without_manifest_is_not_retroactively_stamped(
    tmp_path: Path, repo: Path
) -> None:
    build = _unit_build(tmp_path)
    with pytest.raises(FileNotFoundError):
        verify_build(repo, build)


@pytest.mark.parametrize("existing_stamp", [False, True])
def test_apple_mode_rejected_before_npm_without_touching_old_build(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, existing_stamp: bool
) -> None:
    build = tmp_path / "ui/build"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<main>Previous canonical build; unit only</main>")
    if existing_stamp:
        _record(repo, build, source_snapshot(repo))

    def snapshot() -> dict[str, tuple[bytes, int, int, int]]:
        return {
            path.name: (
                path.read_bytes(),
                path.stat().st_ino,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
            )
            for path in build.iterdir()
            if path.is_file()
        }

    before = snapshot()
    calls: list[str] = []

    def forbid_npm(*_args: object, **_kwargs: object) -> None:
        calls.append("subprocess.run")
        pytest.fail("Rejected Apple mode must not invoke npm or another subprocess")

    monkeypatch.setattr(build_module, "ROOT", tmp_path)
    monkeypatch.setattr(build_module.subprocess, "run", forbid_npm)
    monkeypatch.setenv("COGNIS_APPLE_BUNDLE", "1")
    with pytest.raises(AssertionError, match="COGNIS_APPLE_BUNDLE"):
        build_module.main()
    assert calls == []
    assert snapshot() == before
    assert (build / MARKER).exists() is existing_stamp
    assert build_module.os.environ["COGNIS_APPLE_BUNDLE"] == "1"
