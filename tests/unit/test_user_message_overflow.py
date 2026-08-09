from __future__ import annotations

from cognis.core.user_message_overflow import (
    LARGE_CODE_BLOCK_CHARS,
    MAX_INLINE_USER_MESSAGE_CHARS,
    normalize_user_message_content,
)


def test_normalize_user_message_extracts_large_fenced_code_block() -> None:
    content = f"Inspect this:\n```python\n{'x' * LARGE_CODE_BLOCK_CHARS}\n```\n"

    normalized = normalize_user_message_content(content)

    assert (
        normalized.content
        == "Inspect this:\n[Large code block moved to attached `code-block-1.py`.]\n"
    )
    assert normalized.artifacts[0].filename == "code-block-1.py"
    assert normalized.artifacts[0].content == f"{'x' * LARGE_CODE_BLOCK_CHARS}\n"


def test_normalize_user_message_keeps_small_and_unclosed_fences_inline() -> None:
    small = "```python\nprint('ok')\n```\n"
    unclosed = f"```python\n{'x' * LARGE_CODE_BLOCK_CHARS}\n"

    assert normalize_user_message_content(small).content == small
    assert normalize_user_message_content(unclosed).content == unclosed


def test_normalize_user_message_accepts_a_differently_indented_closing_fence() -> None:
    content = f" ```python\n{'x' * LARGE_CODE_BLOCK_CHARS}\n```\n"

    normalized = normalize_user_message_content(content)

    assert normalized.artifacts[0].filename == "code-block-1.py"


def test_normalize_user_message_uses_utf8_bytes_and_keeps_markdown_prefix_safe() -> None:
    content = f"```\n{'🙂' * (MAX_INLINE_USER_MESSAGE_CHARS // 4)}\n"

    normalized = normalize_user_message_content(content)

    assert len(normalized.content.encode()) <= MAX_INLINE_USER_MESSAGE_CHARS
    assert normalized.content.count("```") % 2 == 0
    assert normalized.artifacts[-1].filename == "user-message.md"


def test_normalize_user_message_handles_long_fences_without_exceeding_inline_limit() -> None:
    fence = "`" * 9_000
    content = f"{fence}\n{'x' * MAX_INLINE_USER_MESSAGE_CHARS}\n"

    normalized = normalize_user_message_content(content)

    assert len(normalized.content.encode()) <= MAX_INLINE_USER_MESSAGE_CHARS
    assert normalized.artifacts[-1].filename == "user-message.md"


def test_normalize_user_message_rejects_invalid_backtick_fence_openers() -> None:
    content = f"```python`invalid\n{'x' * LARGE_CODE_BLOCK_CHARS}\n```\n"

    normalized = normalize_user_message_content(content)

    assert normalized.content == content
    assert normalized.artifacts == ()


def test_normalize_user_message_attaches_full_original_when_inline_content_is_too_large() -> None:
    content = f"# Notes\n{'a' * MAX_INLINE_USER_MESSAGE_CHARS}"

    normalized = normalize_user_message_content(content)

    assert len(normalized.content) <= MAX_INLINE_USER_MESSAGE_CHARS
    assert "full message was moved" in normalized.content
    assert normalized.artifacts == (
        type(normalized.artifacts[0])(filename="user-message.md", content=content),
    )
