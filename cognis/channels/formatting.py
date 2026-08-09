"""Natural-boundary message splitting for channel delivery."""

from __future__ import annotations

from cognis.channels.rich_markdown import split_markdown


def split_message(text: str, max_length: int | None) -> list[str]:
    """Split canonical Markdown at structurally safe boundaries."""

    if max_length is None:
        return [text] if text else []
    return split_markdown(text, max_length=max_length)
