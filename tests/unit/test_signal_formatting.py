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


def test_format_for_signal_preserves_intraword_underscores() -> None:
    chunks = format_for_signal("Use `YOUTUBE_API_KEY` or snake_case_value.", 10000)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.plain_text == "Use YOUTUBE_API_KEY or snake_case_value."
    assert chunk.markdown_text == "Use `YOUTUBE_API_KEY` or snake_case_value."
    assert to_signal_text_styles(chunk.plain_text, chunk.styles) == ["4:15:MONOSPACE"]


def test_format_for_signal_preserves_math_asterisks() -> None:
    chunks = format_for_signal("Keep 2*3*4 literal.", 10000)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.plain_text == "Keep 2*3*4 literal."
    assert chunk.markdown_text == "Keep 2\\*3\\*4 literal."
    assert to_signal_text_styles(chunk.plain_text, chunk.styles) == []


def test_format_for_signal_treats_inline_code_as_literal() -> None:
    chunks = format_for_signal("Use `**kwargs` and `a_b` literally.", 10000)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.plain_text == "Use **kwargs and a_b literally."
    assert chunk.markdown_text == "Use `**kwargs` and `a_b` literally."
    assert to_signal_text_styles(chunk.plain_text, chunk.styles) == [
        "4:8:MONOSPACE",
        "17:3:MONOSPACE",
    ]


def test_format_for_signal_preserves_links_with_parentheses() -> None:
    chunks = format_for_signal("See [wiki](https://example.com/Foo_(bar)).", 10000)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.plain_text == "See wiki (https://example.com/Foo_(bar))."
    assert chunk.markdown_text == "See wiki (https://example.com/Foo_(bar))."
