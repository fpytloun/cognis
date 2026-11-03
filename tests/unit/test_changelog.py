"""Exercise the actual Towncrier boundary, archive, and release exports."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("release_changelog", ROOT / "scripts/changelog.py")
assert spec and spec.loader
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    """Minimal isolated repository; never consume the real release fragments."""
    (tmp_path / "changelog.d").mkdir()
    for path in ("pyproject.toml", "changelog.d/template.md.jinja"):
        shutil.copyfile(ROOT / path, tmp_path / path)
    config_path = tmp_path / "pyproject.toml"
    config_text = config_path.read_text(encoding="utf-8")
    next_version = tomllib.loads(config_text)["tool"]["cognis"]["changelog"]["next_version"]
    config_path.write_text(
        config_text.replace(f'next_version = "{next_version}"', 'next_version = "0.18.0"'),
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n<!-- towncrier release notes start -->\n\n"
        "## [0.17.1] - 2026-09-28\n\n### Fixed\n\n- Stable replay.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_real_towncrier_preview_is_deterministic_and_does_not_consume(release_repo: Path):
    fragment = release_repo / "changelog.d/release-browser.added.md"
    fragment.write_text("Read release notes with diacritics: café.\n", encoding="utf-8")
    first = changelog.build_index(release_repo)
    assert first == changelog.build_index(release_repo)
    assert fragment.exists()
    assert first["schema_version"] == 1
    assert first["releases"][0] == {
        "version": "0.18.0",
        "date": "Unreleased",
        "markdown": "### Added\n\n- Read release notes with diacritics: café.",
    }
    assert first["releases"][1]["version"] == "0.17.1"


def test_finalization_keeps_archive_and_preview_equivalent(release_repo: Path):
    fragment = release_repo / "changelog.d/release-browser.added.md"
    fragment.write_text("Read release notes.\n", encoding="utf-8")
    before = changelog.build_index(release_repo)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--version",
            "0.18.0",
            "--date",
            "2026-10-17",
            "--yes",
        ],
        cwd=release_repo,
        check=True,
        capture_output=True,
    )
    assert not fragment.exists()
    after = changelog.build_index(release_repo)
    assert after["releases"][0]["date"] == "2026-10-17"
    assert after["releases"][0]["markdown"] == before["releases"][0]["markdown"]
    assert after["releases"][1:] == before["releases"][1:]


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("bad-category.fixd.md", "Fix something."),
        ("empty.fixed.md", ""),
        ("heading.added.md", "## [99.0.0] - Unreleased"),
        ("bullet.added.md", "- Do not add a second bullet."),
    ],
)
def test_invalid_fragments_fail_closed(release_repo: Path, name: str, content: str):
    (release_repo / "changelog.d" / name).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        changelog.build_index(release_repo)


def test_exports_exact_release_and_exclusive_range():
    releases = [
        {"version": "0.18.0", "date": "Unreleased", "markdown": "New"},
        {"version": "0.17.1", "date": "2026-09-28", "markdown": "Replay"},
        {"version": "0.10.0", "date": "2026-08-01", "markdown": "Earlier"},
        {"version": "0.9.0", "date": "2026-07-01", "markdown": "Old"},
    ]
    assert changelog.export_notes(releases, "0.18.0") == "## [0.18.0] - Unreleased\n\nNew\n"
    notes = changelog.export_notes(releases, "0.17.1", "0.9.0")
    assert "Replay" in notes and "Earlier" in notes
    assert "New" not in notes and "Old" not in notes
    for upper, lower in (("0.1.0", None), ("0.17.1", "0.18.0"), ("0.17.1", "bad")):
        with pytest.raises(ValueError):
            changelog.export_notes(releases, upper, lower)


def test_cli_detects_stale_index_without_rewriting(release_repo: Path, monkeypatch, capsys):
    monkeypatch.setattr(changelog, "ROOT", release_repo)
    monkeypatch.setattr(sys, "argv", ["changelog.py"])
    assert changelog.main() == 0
    target = release_repo / changelog.INDEX
    assert json.loads(target.read_text())["releases"][0]["version"] == "0.17.1"
    monkeypatch.setattr(sys, "argv", ["changelog.py", "--check"])
    assert changelog.main() == 0
    target.write_text("{}\n", encoding="utf-8")
    assert changelog.main() == 1
    assert "stale" in capsys.readouterr().err
    assert target.read_text() == "{}\n"


def test_cannot_preview_a_finalized_version(release_repo: Path):
    (release_repo / "changelog.d/change.fixed.md").write_text("A fix.", encoding="utf-8")
    path = release_repo / "pyproject.toml"
    path.write_text(path.read_text().replace('next_version = "0.18.0"', 'next_version = "0.17.1"'))
    with pytest.raises(ValueError, match="newer"):
        changelog.build_index(release_repo)


def test_archive_rejects_duplicates_and_non_descending_versions():
    for text in (
        "## [0.17.1] - Unreleased\nA\n## [0.17.1] - 2026-09-28\nB",
        "## [0.9.0] - 2026-07-01\nA\n## [0.10.0] - 2026-08-01\nB",
    ):
        with pytest.raises(ValueError, match="unique"):
            changelog.parse_releases(text)
