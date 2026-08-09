"""Conservatively move oversized user-message content into text artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass

LARGE_CODE_BLOCK_CHARS = 12 * 1024
MAX_INLINE_USER_MESSAGE_CHARS = 64 * 1024

_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\r\n]*)\r?\n?$")
_LANGUAGE_EXTENSIONS = {
    "bash": "sh",
    "c++": "cpp",
    "c#": "cs",
    "css": "css",
    "go": "go",
    "html": "html",
    "java": "java",
    "javascript": "js",
    "json": "json",
    "jsx": "jsx",
    "kotlin": "kt",
    "markdown": "md",
    "md": "md",
    "php": "php",
    "python": "py",
    "py": "py",
    "rust": "rs",
    "sh": "sh",
    "shell": "sh",
    "sql": "sql",
    "text": "txt",
    "toml": "toml",
    "tsx": "tsx",
    "typescript": "ts",
    "ts": "ts",
    "yaml": "yaml",
    "yml": "yml",
}


@dataclass(frozen=True)
class TextArtifact:
    """Text content that must be persisted as an attachment."""

    filename: str
    content: str


@dataclass(frozen=True)
class UserMessageNormalization:
    """Canonical inline content plus oversized text to persist."""

    content: str
    artifacts: tuple[TextArtifact, ...]


def normalize_user_message_content(content: str) -> UserMessageNormalization:
    """Extract oversized fenced code and cap oversized inline user text."""

    extracted_content, code_artifacts = _extract_large_code_blocks(content)
    if len(extracted_content.encode()) <= MAX_INLINE_USER_MESSAGE_CHARS:
        return UserMessageNormalization(extracted_content, tuple(code_artifacts))

    full_message = TextArtifact(filename="user-message.md", content=content)
    placeholder = (
        "\n\n[The full message was moved to the attached `user-message.md` "
        "because it exceeds the inline context limit.]\n"
    )
    prefix_limit = MAX_INLINE_USER_MESSAGE_CHARS - len(placeholder.encode()) - 8
    prefix = _markdown_safe_prefix(extracted_content, prefix_limit)
    return UserMessageNormalization(
        content=f"{prefix}{placeholder}",
        artifacts=tuple([*code_artifacts, full_message]),
    )


def _extract_large_code_blocks(content: str) -> tuple[str, list[TextArtifact]]:
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    artifacts: list[TextArtifact] = []
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN_RE.match(lines[index])
        if opening is None or not _is_valid_opening_fence(opening):
            output.append(lines[index])
            index += 1
            continue

        indent, fence, info = opening.groups()
        closing_index = _find_closing_fence(lines, index + 1, indent, fence)
        if closing_index is None:
            output.append(lines[index])
            index += 1
            continue

        block_lines = lines[index : closing_index + 1]
        code = "".join(lines[index + 1 : closing_index])
        if len(code.encode()) < LARGE_CODE_BLOCK_CHARS:
            output.extend(block_lines)
            index = closing_index + 1
            continue

        language = info.strip().split(maxsplit=1)[0].lower() if info.strip() else ""
        filename = _code_block_filename(len(artifacts) + 1, language)
        artifacts.append(TextArtifact(filename=filename, content=code))
        output.append(f"{indent}[Large code block moved to attached `{filename}`.]\n")
        index = closing_index + 1
    return "".join(output), artifacts


def _find_closing_fence(
    lines: list[str],
    start: int,
    _indent: str,
    opening_fence: str,
) -> int | None:
    marker = opening_fence[0]
    minimum_length = len(opening_fence)
    for index in range(start, len(lines)):
        if _is_closing_fence(lines[index], marker, minimum_length):
            return index
    return None


def _code_block_filename(index: int, language: str) -> str:
    extension = _LANGUAGE_EXTENSIONS.get(language, "txt")
    return f"code-block-{index}.{extension}"


def _markdown_safe_prefix(content: str, limit: int) -> str:
    """Return a newline-bounded prefix, closing an open fenced code block when needed."""

    if len(content.encode()) <= limit:
        return content
    prefix_limit = limit
    while prefix_limit > 0:
        prefix = _newline_bounded_utf8_prefix(content, prefix_limit)
        open_fence = _open_fence_in(prefix)
        if open_fence is None:
            return prefix
        suffix = f"\n{open_fence}\n"
        if len(prefix.encode()) + len(suffix.encode()) <= limit:
            return f"{prefix}{suffix}"
        prefix_limit -= len(suffix.encode())
    return ""


def _utf8_prefix(content: str, byte_limit: int) -> str:
    total = 0
    for index, character in enumerate(content):
        size = len(character.encode())
        if total + size > byte_limit:
            return content[:index]
        total += size
    return content


def _newline_bounded_utf8_prefix(content: str, byte_limit: int) -> str:
    prefix = _utf8_prefix(content, byte_limit)
    line_break = max(prefix.rfind("\n"), prefix.rfind("\r"))
    return prefix[: line_break + 1] if line_break > 0 else prefix


def _open_fence_in(content: str) -> str | None:
    open_fence: str | None = None
    for line in content.splitlines(keepends=True):
        if open_fence is not None:
            if _is_closing_fence(line, open_fence[0], len(open_fence)):
                open_fence = None
            continue
        match = _FENCE_OPEN_RE.match(line)
        if match is not None and _is_valid_opening_fence(match):
            open_fence = match.group(2)
    return open_fence


def _is_valid_opening_fence(match: re.Match[str]) -> bool:
    fence = match.group(2)
    info = match.group(3)
    return fence[0] != "`" or "`" not in info


def _is_closing_fence(line: str, marker: str, minimum_length: int) -> bool:
    return (
        re.match(
            rf"^ {{0,3}}{re.escape(marker)}{{{minimum_length},}}[ \t]*\r?\n?$",
            line,
        )
        is not None
    )
