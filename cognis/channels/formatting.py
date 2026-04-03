"""Message formatting and splitting for channel delivery.

Adapts LLM-generated content (potentially long markdown) to platform
constraints: message length limits, markdown support, and splitting.
"""

from __future__ import annotations

import re

from cognis.models.channel import ChannelCapabilities


def format_for_channel(
    content: str,
    capabilities: ChannelCapabilities,
) -> list[str]:
    """Format and split content for a specific channel.

    Returns a list of message chunks, each within the channel's
    ``max_message_length``.  If the channel doesn't support markdown,
    markdown syntax is stripped.

    Args:
        content: Raw content (potentially markdown).
        capabilities: Target channel capabilities.

    Returns:
        List of message strings ready to send.
    """
    if not content:
        return []

    # Strip markdown if the channel doesn't support it
    if not capabilities.supports_markdown:
        content = strip_markdown(content)

    # Split into chunks
    max_len = capabilities.max_message_length
    if len(content) <= max_len:
        return [content]

    return split_message(content, max_len)


def strip_markdown(text: str) -> str:
    """Strip common markdown formatting to plain text.

    Preserves content and structure (newlines, lists) but removes
    formatting syntax.
    """
    # Remove code blocks (preserve content)
    text = re.sub(r"```[\w]*\n(.*?)```", r"\1", text, flags=re.DOTALL)

    # Remove inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Remove bold/italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"___(.+?)___", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)

    # Remove headers (keep text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Remove link syntax (keep text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Remove images (keep alt text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)

    # Remove horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Remove blockquote markers
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)

    # Clean up excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def split_message(text: str, max_length: int) -> list[str]:
    """Split a message into chunks respecting natural boundaries.

    Splitting priority:
    1. Paragraph boundaries (double newline)
    2. Line boundaries (single newline)
    3. Sentence boundaries (. ! ?)
    4. Word boundaries (space)
    5. Hard split at max_length (last resort)
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try to find a good split point
        split_at = _find_split_point(remaining, max_length)
        chunk = remaining[:split_at].rstrip()
        remaining = remaining[split_at:].lstrip()

        if chunk:
            chunks.append(chunk)

    return chunks


def _find_split_point(text: str, max_length: int) -> int:
    """Find the best split point within max_length characters."""
    # Try paragraph boundary
    idx = text.rfind("\n\n", 0, max_length)
    if idx > max_length // 4:
        return idx + 2

    # Try line boundary
    idx = text.rfind("\n", 0, max_length)
    if idx > max_length // 4:
        return idx + 1

    # Try sentence boundary
    for pattern in (". ", "! ", "? "):
        idx = text.rfind(pattern, 0, max_length)
        if idx > max_length // 4:
            return idx + len(pattern)

    # Try word boundary
    idx = text.rfind(" ", 0, max_length)
    if idx > max_length // 4:
        return idx + 1

    # Hard split
    return max_length
