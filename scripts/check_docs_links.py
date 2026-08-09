"""Check relative Markdown links in repository documentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def main() -> int:
    failures: list[str] = []
    for document in sorted((ROOT / "docs").rglob("*.md")):
        for raw_target in LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            path = unquote(target.split("#", 1)[0])
            if path and not (document.parent / path).resolve().exists():
                failures.append(f"{document.relative_to(ROOT)}: missing {target}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Documentation links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
