from __future__ import annotations

from cognis.channels.signal_formatting import format_for_signal, to_signal_text_styles


def test_format_for_signal_renders_headers_and_styles() -> None:
    chunks = format_for_signal(
        "# Heading\n\nThis has **bold**, *italic*, `code`, ~~strike~~, ||spoiler||.",
        10000,
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.plain_text == "Heading\n\nThis has bold, italic, code, strike, spoiler."
    assert (
        chunk.markdown_text
        == "**Heading**\n\nThis has **bold**, *italic*, `code`, ~strike~, ||spoiler||."
    )
    assert to_signal_text_styles(chunk.plain_text, chunk.styles) == [
        "0:7:BOLD",
        "18:4:BOLD",
        "24:6:ITALIC",
        "32:4:MONOSPACE",
        "38:6:STRIKETHROUGH",
        "46:7:SPOILER",
    ]


def test_format_for_signal_renders_code_blocks_and_links() -> None:
    chunks = format_for_signal(
        "See [docs](https://example.com).\n\n```python\nprint('hi')\n```",
        10000,
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.plain_text == "See docs (https://example.com).\n\nprint('hi')"
    assert chunk.markdown_text == "See docs (https://example.com).\n\n```\nprint('hi')\n```"
    assert to_signal_text_styles(chunk.plain_text, chunk.styles) == ["33:11:MONOSPACE"]


def test_format_for_signal_chunks_without_breaking_styles() -> None:
    chunks = format_for_signal(
        "# Heading\n\nParagraph with **bold text** that should chunk cleanly.",
        32,
    )

    assert len(chunks) == 3
    assert chunks[0].markdown_text == "**Heading**"
    assert chunks[1].markdown_text.startswith("Paragraph with **bold text**")
    assert chunks[2].markdown_text.endswith("chunk cleanly.")


def test_to_signal_text_styles_uses_utf16_offsets() -> None:
    chunks = format_for_signal("Hi 😀 **world**", 10000)

    assert len(chunks) == 1
    assert to_signal_text_styles(chunks[0].plain_text, chunks[0].styles) == ["6:5:BOLD"]
