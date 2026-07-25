"""Natural-boundary message splitting for channel delivery."""

from __future__ import annotations

from cognis.channels.rich_markdown import split_markdown


def split_message(text: str, max_length: int) -> list[str]:
    """Split canonical Markdown at structurally safe boundaries."""

    return split_markdown(text, max_length=max_length)
