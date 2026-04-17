from __future__ import annotations

from pathlib import Path


def test_memory_stale_removed_from_runtime_code() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_files = list((root / "cognis").rglob("*.py")) + list((root / "tests").rglob("*.py"))
    offenders: list[str] = []
    for path in runtime_files:
        if path.name == "test_no_memory_stale.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "memory_stale" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
