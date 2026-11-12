"""Validate line anchors against one complete, exact unified-diff hunk."""

from __future__ import annotations

import re

from cognis.core.feedback.primitives import FeedbackError, digest
from cognis.models.feedback import DiffLocator

_HEADER = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@.*")


def validate_diff_lines(text: str, locator: DiffLocator) -> None:
    """Prove the side, line range, hunk digest and exact unprefixed quote together."""
    invalid = FeedbackError(
        422, "invalid_anchor", "Selected diff lines do not match the exact hunk"
    )
    if (
        locator.side is None
        or locator.start_line is None
        or locator.end_line is None
        or locator.quote is None
        or len(locator.quote.encode("utf-8")) > 2048
    ):
        raise invalid
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    headers = [
        (index, match) for index, line in enumerate(lines) if (match := _HEADER.fullmatch(line))
    ]
    matches = 0
    for ordinal, (start, header) in enumerate(headers):
        end = headers[ordinal + 1][0] if ordinal + 1 < len(headers) else len(lines)
        values = [value if value is not None else "1" for value in header.groups()]
        if any(len(value) > 16 or int(value) > 2**53 - 1 for value in values):
            raise invalid
        old, old_remaining, new, new_remaining = map(int, values)
        if (old_remaining and old < 1) or (new_remaining and new < 1):
            raise invalid
        selected: list[tuple[int, str]] = []
        for raw in lines[start + 1 : end]:
            if raw == "\\ No newline at end of file":
                continue
            old_line = new_line = None
            if raw.startswith("+") and new_remaining:
                new_line = new
                new += 1
                new_remaining -= 1
            elif raw.startswith("-") and old_remaining:
                old_line = old
                old += 1
                old_remaining -= 1
            elif raw.startswith(" ") and old_remaining and new_remaining:
                old_line, new_line = old, new
                old += 1
                new += 1
                old_remaining -= 1
                new_remaining -= 1
            else:
                raise invalid
            number = old_line if locator.side == "old" else new_line
            if number is not None and locator.start_line <= number <= locator.end_line:
                selected.append((number, raw[1:]))
        if old_remaining or new_remaining:
            raise invalid
        if digest("\n".join(lines[start:end]).encode("utf-8")) == locator.hunk_digest:
            matches += (
                len(selected) == locator.end_line - locator.start_line + 1
                and all(
                    number == locator.start_line + index
                    for index, (number, _) in enumerate(selected)
                )
                and "\n".join(value for _, value in selected) == locator.quote
            )
    if matches != 1:
        raise invalid
